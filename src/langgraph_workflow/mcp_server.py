"""MCP server that lets an agent runtime drive the workflows (the Locus plugin).

    python -m langgraph_workflow.mcp_server DATA_DIR

The agent drives each attempt: ``workflow_start`` → do the job it returns with
the runtime's own tools → ``workflow_report`` → … until the result. Human
decisions (plan approval, conflict resolution) are asked with MCP
elicitation, so they reach the user through the client's own UI; no tool lets
the agent answer them. Requires the ``mcp`` extra.
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import mcp_types
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.message import ServerMessageMetadata
from mcp_types import ToolAnnotations
from pydantic import create_model

from .agent_host import AgentHost, ReportRejected
from .contracts import TERMINAL_STATUSES, AttemptStatus, ContractError, digest
from .definitions import validate_definition
from .executor import DecisionRejected, WorkflowExecutor

RESULT_SHAPES = {
    "inspect": {"summary": "what you found that matters for the goal"},
    "plan": {"plan": {"steps": [{"title": "one deliverable-sized step"}]}},
    "write": {"summary": "what you changed"},
    "review": {"verdict": "approve | changes_requested", "findings": ["issue to fix"]},
    "investigate": {"claims": {"short_key": "value"}, "sources": ["where each claim came from"]},
    "synthesize": {"summary": "answer", "claims": {"short_key": "value"},
                   "sources": ["..."], "conflicts_reported": ["conflicting claim keys"]},
    "task": {"summary": "what you did or found, for the next step and the user",
             "choice": "only when inputs.choices is not empty: exactly one of them"},
}
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
STATEFUL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                           openWorldHint=False)
_ATTEMPT = re.compile(r"^lgw-([0-9a-f]{12})-[0-9a-f]{10}$")
_DEFINITION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# User settings, edited in the Locus window and stored by Locus in the plugin's
# data folder. Mirrors plugin/settings.schema.json (a test keeps them equal).
SETTINGS = {
    "plan_approval": True,
    "conflict_approval": True,
    "reviewer_default": False,
    "max_repair_rounds": 2,
    "max_review_rounds": 2,
    "max_jobs": 12,
    "max_investigations": 4,
    "max_parallel_reads": 2,
    "keep_finished_days": 30,
}
_LIMITS = ("max_repair_rounds", "max_review_rounds", "max_jobs", "max_investigations",
           "max_parallel_reads")

# Plain-language steps for the window's timeline, and which graph phases or
# pending job kinds belong to each.
STEPS = {
    "verified_change": [
        ("understand", "Understand", {"admission", "validate", "inspect"}),
        ("plan", "Plan", {"plan"}),
        ("approve", "Approve", {"approve"}),
        ("build", "Build", {"implement", "repair", "write"}),
        ("check", "Check", {"verify", "finalize"}),
        ("review", "Review", {"review"}),
        ("done", "Done", {"finish"}),
    ],
    "research": [
        ("scope", "Scope", {"admission", "validate", "scope"}),
        ("investigate", "Investigate", {"investigate"}),
        ("compare", "Compare", {"collect", "resolve"}),
        ("synthesize", "Synthesize", {"synthesize"}),
        ("check", "Check", {"verify"}),
        ("done", "Done", {"finish"}),
    ],
}


def load_settings(data: Path) -> dict[str, Any]:
    """Saved settings over defaults; unknown or ill-typed values are ignored."""
    try:
        saved = json.loads((data / "locus-settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    settings = dict(SETTINGS)
    if isinstance(saved, dict):
        for key, default in SETTINGS.items():
            value = saved.get(key)
            if type(value) is type(default):
                settings[key] = value
    return settings


def _policy(settings: dict[str, Any]) -> dict[str, Any]:
    return {"plan_approval": settings["plan_approval"],
            "conflict_approval": settings["conflict_approval"],
            "limits": {key: settings[key] for key in _LIMITS}}


def timeline(status: AttemptStatus, reviewer: bool, pending_kinds: list[str]) -> list[dict]:
    """Ordered steps with state done | current | upcoming | stopped | skipped."""
    steps = STEPS.get(status.workflow, [])
    if status.pending_decision:
        marker = ("approve" if status.pending_decision.get("kind") == "plan_approval"
                  else "resolve")
    elif status.status == "waiting_for_job" and pending_kinds:
        marker = pending_kinds[0]
    else:
        marker = status.phase or "admission"
    current = next((i for i, (_, _, phases) in enumerate(steps) if marker in phases), 0)
    finished = status.status in TERMINAL_STATUSES
    out = []
    for index, (key, label, _) in enumerate(steps):
        if key == "review" and not reviewer:
            state = "skipped"
        elif finished and status.status == "verified":
            state = "done"
        elif finished:
            state = "done" if index < current else "stopped" if index == current else "upcoming"
        else:
            state = "done" if index < current else "current" if index == current else "upcoming"
        out.append({"id": key, "label": label, "state": state})
    return out


class Definitions:
    """Workflow graphs drawn in the Locus window: one JSON file each, shared
    by every project. A run pins its own copy, so editing never changes a
    run that already started."""

    def __init__(self, data: Path) -> None:
        self.root = data / "definitions"

    def _path(self, definition_id: str) -> Path:
        if not isinstance(definition_id, str) or not _DEFINITION_ID.match(definition_id):
            raise ToolError("definition_id must come from workflow_definitions")
        return self.root / f"{definition_id}.json"

    def get(self, definition_id: str) -> dict:
        path = self._path(definition_id)
        try:
            return json.loads(path.read_text())["definition"]
        except (OSError, ValueError, KeyError) as error:
            raise ToolError(f"no workflow named {definition_id}") from error

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("*.json")) if self.root.is_dir() else []:
            try:
                saved = json.loads(path.read_text())
                d = saved["definition"]
            except (OSError, ValueError, KeyError):
                continue
            agents = sorted(({n.get("agent_name") or "" for n in d["nodes"]
                              if n["type"] == "task"} | {d.get("agent_name") or ""}) - {""})
            out.append({"id": d["id"], "title": d["title"], "description": d["description"],
                        "steps": sum(1 for n in d["nodes"] if n["type"] not in ("start", "end")),
                        "agents": agents, "updated_at": saved.get("updated_at", 0)})
        return sorted(out, key=lambda d: -d["updated_at"])

    def save(self, definition: dict) -> dict:
        definition = validate_definition(definition)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(definition["id"])
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"definition": definition, "updated_at": time.time()}, handle)
        tmp.replace(path)
        return definition

    def delete(self, definition_id: str) -> bool:
        path = self._path(definition_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed


class Workspaces:
    """One host and executor per workspace, created on first use."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.registry = data / "workspaces.json"
        self._open: dict[str, tuple[AgentHost, WorkflowExecutor]] = {}

    def settings(self) -> dict[str, Any]:
        return load_settings(self.data)

    def _paths(self) -> dict[str, str]:
        return json.loads(self.registry.read_text()) if self.registry.exists() else {}

    def key_for(self, workspace: Path) -> str:
        key = digest(str(workspace))[:12]
        paths = self._paths()
        if paths.get(key) != str(workspace):
            paths[key] = str(workspace)
            self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.registry.with_suffix(".tmp")
            tmp.write_text(json.dumps(paths))
            tmp.replace(self.registry)
        return key

    def get(self, key: str) -> tuple[AgentHost, WorkflowExecutor]:
        if key not in self._open:
            workspace = self._paths().get(key)
            if workspace is None:
                raise ToolError("unknown attempt_id")
            root = self.data / "workspaces" / key
            host = AgentHost(root, workspace, policy=lambda: _policy(self.settings()))
            executor = WorkflowExecutor(host, root / "checkpoints.sqlite3")
            days = self.settings()["keep_finished_days"]
            if days > 0 and (root / "checkpoints.sqlite3").exists():
                # Housekeeping: finished attempts' checkpoint history only.
                executor.store.prune(older_than_seconds=days * 86400, terminal=TERMINAL_STATUSES)
            self._open[key] = (host, executor)
        return self._open[key]

    def keys(self) -> list[str]:
        return list(self._paths())

    def name(self, key: str) -> str:
        return Path(self._paths().get(key, "")).name

    def for_attempt(self, attempt_id: str) -> tuple[AgentHost, WorkflowExecutor]:
        match = _ATTEMPT.match(attempt_id)
        if not match:
            raise ToolError("attempt_id must come from workflow_start")
        return self.get(match.group(1))


async def _workspace(ctx: Context) -> Path:
    # Locus shares its open workspace as the only root when the server sets
    # share_workspace_root. Sent on this request's back-channel (MCP 2.x).
    try:
        roots = (await ctx.session.send_request(
            mcp_types.ListRootsRequest(), mcp_types.ListRootsResult,
            metadata=ServerMessageMetadata(related_request_id=ctx.request_id))).roots
    except Exception:  # noqa: BLE001 - client may not share roots
        roots = []
    for root in roots:
        uri = urlparse(str(root.uri))
        if uri.scheme == "file":
            return Path(unquote(uri.path)).resolve()
    fallback = os.environ.get("LGW_WORKSPACE", "")
    if fallback and not fallback.startswith("${") and Path(fallback).is_dir():
        return Path(fallback).resolve()
    raise ToolError("no workspace: open a workspace in Locus, then try again")


class NoPrompt(Exception):
    """The client cannot show decision prompts (no elicitation back-channel)."""


async def _ask(ctx: Context, pending: dict) -> dict | None:
    """Ask the human through the client's UI; None if not answered."""
    options = [o for o in pending["options"] if o != "edit"]
    schema = create_model("Decision", choice=(Literal[tuple(options)], ...))
    title = {"plan_approval": "Approve this plan?", "approval": "Your approval is needed",
             "conflict_resolution": "The investigations disagree. How should they be resolved?"}
    message = f"{title.get(pending['kind'], 'Decision needed')}\n\n{pending['summary']}"
    try:
        answer = await ctx.elicit(message, schema)
    except Exception as error:  # noqa: BLE001 - client without elicitation support
        raise NoPrompt from error
    if answer.action != "accept" or answer.data is None:
        return None
    return {"decision_id": pending["decision_id"], "run_id": pending["run_id"],
            "attempt_id": pending["attempt_id"], "revision": pending["revision"],
            "digest": pending["digest"], "choice": answer.data.choice, "actor": "user"}


def _job(spec: dict) -> dict[str, Any]:
    return {"operation_id": spec["operation_id"], "kind": spec["kind"], "access": spec["access"],
            "instruction": spec["instruction"], "inputs": spec["inputs"],
            "result_shape": RESULT_SHAPES[spec["kind"]]}


def _handoff_text(attempt_id: str, spec: dict, claim: str) -> str:
    """The message Locus sends to the assigned agent's chat for one job."""
    inputs = spec["inputs"]
    lines = [
        f"LangGraph Workflows hands you step \"{inputs.get('title', '')}\" of a workflow run.",
        f"Goal of the run: {inputs.get('goal', '')}",
        "",
        spec["instruction"],
        "",
        "This step may only read files; do not change any file." if spec["access"] == "read"
        else "This step may change files in the project.",
    ]
    if inputs.get("context"):
        lines += ["", "Results of earlier steps:"]
        lines += [f"- {c.get('title', '')}: {c.get('summary', '')}" for c in inputs["context"]]
    if inputs.get("choices"):
        lines += ["", "Finish by choosing exactly one of: " + ", ".join(inputs["choices"])]
    lines += ["", "When done, call the workflow_report tool with:",
              json.dumps({"attempt_id": attempt_id, "operation_id": spec["operation_id"],
                          "claim": claim, "outcome": "completed | failed | refused",
                          "result": RESULT_SHAPES["task"]}, indent=2),
              "Report only what actually happened; file changes are checked independently."]
    return "\n".join(lines)


def _custom_steps(definition: dict, values: dict, status: AttemptStatus) -> list[dict]:
    """Drawn steps in breadth-first order from start, with their state."""
    by_id = {n["id"]: n for n in definition["nodes"]}
    start = next(n["id"] for n in definition["nodes"] if n["type"] == "start")
    order, queue = [], [start]
    while queue:
        current = queue.pop(0)
        if current in order:
            continue
        order.append(current)
        queue += [e["to"] for e in definition["edges"] if e["from"] == current]
    visits, current = values.get("visits") or {}, values.get("node")
    finished = status.status in TERMINAL_STATUSES
    out = []
    for node_id in order:
        node = by_id[node_id]
        if node["type"] == "start":
            continue
        if node_id == current and not finished:
            state = "current"
        elif node_id == current:
            state = "done" if status.status == "verified" else "stopped"
        elif visits.get(node_id):
            state = "done"
        else:
            state = "upcoming"
        out.append({"id": node_id, "label": node["title"], "state": state,
                    "type": node["type"], "visits": visits.get(node_id, 0)})
    return out


def _view(host: AgentHost, status: AttemptStatus) -> dict[str, Any]:
    out: dict[str, Any] = {"attempt_id": status.attempt_id, "workflow": status.workflow,
                           "status": status.status, "phase": status.phase}
    if status.blocker:
        out["blocker"] = status.blocker
    if status.status == "waiting_for_job":
        pending = host.pending(status.attempt_id)
        out["jobs"] = [_job(spec) for spec in pending if not spec.get("assignee")]
        handoffs = [{"operation_id": spec["operation_id"],
                     "step": spec["inputs"].get("title", ""),
                     "agent": spec["inputs"].get("agent_name") or "another agent"}
                    for spec in pending if spec.get("assignee")]
        out["next_step"] = (
            "Do each job with your normal tools. Jobs with access 'read' must not change any "
            "file. Then call workflow_report with the operation_id, outcome 'completed', "
            "'failed' or 'refused' (when permission was denied), and a result in result_shape. "
            "Report only what actually happened.")
        if handoffs:
            out["handoffs"] = handoffs
            if not out["jobs"]:
                out["next_step"] = (
                    "The next step belongs to " + ", ".join(h["agent"] for h in handoffs) +
                    ". Do not do it yourself: it is handed to that agent's own chat from the "
                    "LangGraph Workflows window. Tell the user, then stop.")
    elif status.status == "waiting_for_input":
        out["next_step"] = ("A decision from the user is pending. Ask them to answer the "
                            "prompt, then call workflow_status.")
    elif status.status in TERMINAL_STATUSES:
        out["result"] = status.result
        out["next_step"] = {
            "verified": "Done: every declared check passed on the current files.",
            "needs_review": "Stopped: some requirement is not machine-verified; tell the user "
                            "what remains (see blocker).",
        }.get(status.status, "Stopped; tell the user the status and blocker.")
    else:
        out["next_step"] = "Call workflow_status to continue."
    return out


def build_server(data: Path) -> MCPServer:
    spaces = Workspaces(data)
    definitions = Definitions(data)
    panel_tools = set(os.environ.get("LOCUS_PANEL_TOOLS", "").split(","))
    server = MCPServer(
        name="langgraph-workflow",
        instructions="Durable, verified workflows. Start one with workflow_start, perform "
                     "each returned job yourself, and report it with workflow_report.")

    async def advance(ctx: Context, host: AgentHost, executor: WorkflowExecutor,
                      status: AttemptStatus) -> dict:
        while status.pending_decision:
            try:
                response = await _ask(ctx, status.pending_decision)
            except NoPrompt:
                view = _view(host, status)
                view["next_step"] = ("This client cannot show decision prompts, so the workflow "
                                     "cannot continue. In Locus the plugin needs protocol_mode "
                                     "'legacy' (set in its .mcp.json).")
                return view
            if response is None:
                break
            try:
                status = await asyncio.to_thread(executor.decide, response)
            except DecisionRejected:
                # Already answered elsewhere (for example in the Locus window).
                status = await asyncio.to_thread(executor.status, status.attempt_id)
        return _view(host, status)

    @server.tool(annotations=STATEFUL)
    async def workflow_start(
        workflow: Literal["verified_change", "research", "custom"], goal: str, ctx: Context,
        checks: list[dict] | None = None, plan_steps: list[str] | None = None,
        investigations: list[str] | None = None, reviewer: bool | None = None,
        definition_id: str | None = None,
    ) -> dict:
        """Start a workflow in the current workspace.

        verified_change: plan (or use plan_steps) → user approves → implement →
        verify the declared checks → bounded repair → optional review.
        research: read-only investigations (one per question) → synthesis.
        checks: acceptance checks, e.g. {"id": "readme", "kind": "file_contains",
        "path": "README.md", "value": "Install", "requirement": "..."}; kinds
        file_exists, file_contains, json_value (pointer + value) are verified by
        this plugin; command and human_review end in needs_review.
        reviewer: add a review step; defaults to the user's setting.
        custom: a workflow the user drew; pass its definition_id from
        workflow_definitions. Steps assigned to other agents are handed to
        them from the LangGraph Workflows window.
        """
        host, executor, request = await begin(ctx, workflow, goal, checks, plan_steps,
                                              investigations, reviewer, definition_id)
        status = await asyncio.to_thread(executor.start, request)
        return await advance(ctx, host, executor, status)

    async def begin(ctx, workflow, goal, checks, plan_steps, investigations, reviewer,
                    definition_id):
        if reviewer is None:
            reviewer = spaces.settings()["reviewer_default"]
        key = spaces.key_for(await _workspace(ctx))
        host, executor = spaces.get(key)
        attempt = f"lgw-{key}-{uuid.uuid4().hex[:10]}"
        request: dict[str, Any] = {
            "workflow": workflow, "run_id": attempt, "task_id": attempt, "attempt_id": attempt,
            "workspace_id": f"ws-{key}", "goal": goal, "checks": list(checks or []),
            "investigations": list(investigations or []), "reviewer": reviewer}
        if plan_steps:
            request["plan"] = {"steps": [{"title": s} for s in plan_steps]}
        if workflow == "custom":
            if not definition_id:
                raise ToolError("custom workflows need a definition_id from workflow_definitions")
            request["definition"] = definitions.get(definition_id)
            request["reviewer"] = False
        elif definition_id:
            raise ToolError("definition_id is only for workflow='custom'")
        return host, executor, request

    @server.tool(annotations=STATEFUL)
    async def workflow_report(
        attempt_id: str, operation_id: str, outcome: Literal["completed", "failed", "refused"],
        ctx: Context, result: dict | None = None, note: str = "", claim: str = "",
    ) -> dict:
        """Report the outcome of one job you performed, then get the next step.
        claim: required for a step that was handed to you; copy it from the hand-off."""
        host, executor = spaces.for_attempt(attempt_id)
        if not operation_id.startswith(attempt_id + "/"):
            raise ToolError("operation_id does not belong to this attempt")
        try:
            host.report(operation_id, outcome, result, note, claim)
        except ReportRejected as error:
            raise ToolError(str(error)) from error
        status = await asyncio.to_thread(executor.resume, attempt_id)
        return await advance(ctx, host, executor, status)

    @server.tool(annotations=READ_ONLY)
    async def workflow_status(attempt_id: str, ctx: Context) -> dict:
        """Current status, pending jobs, and result. Re-asks a pending decision."""
        host, executor = spaces.for_attempt(attempt_id)
        status = await asyncio.to_thread(executor.status, attempt_id)
        if status.status not in TERMINAL_STATUSES and not status.pending_decision and (
                status.status != "waiting_for_job" or not host.pending(attempt_id)):
            status = await asyncio.to_thread(executor.resume, attempt_id)
        return await advance(ctx, host, executor, status)

    @server.tool(annotations=STATEFUL)
    async def workflow_cancel(attempt_id: str) -> dict:
        """Cancel an attempt. Stop working on any job it gave you."""
        host, executor = spaces.for_attempt(attempt_id)
        try:
            status = await asyncio.to_thread(executor.cancel, attempt_id)
        except DecisionRejected as error:
            raise ToolError(error.code) from error
        return _view(host, status)

    def summary(key: str, host: AgentHost, status: AttemptStatus) -> dict[str, Any]:
        pending = (host.pending(status.attempt_id) if status.status == "waiting_for_job"
                   else [])
        return {"attempt_id": status.attempt_id, "workflow": status.workflow,
                "title": status.title,
                "goal": status.goal[:300], "status": status.status, "blocker": status.blocker,
                "needs_you": bool(status.pending_decision), "project": spaces.name(key),
                "updated_at": status.updated_at,
                "waiting_jobs": len(pending),
                "handoffs": [{"operation_id": j["operation_id"], "agent": j["assignee"],
                              "agent_name": j["inputs"].get("agent_name", ""),
                              "title": j["inputs"].get("title", "")}
                             for j in pending if j.get("assignee")]}

    @server.tool(annotations=READ_ONLY)
    async def workflow_overview() -> dict:
        """All recent workflow runs across projects, newest first, plus the
        settings in effect for new runs."""
        runs = []
        for key in spaces.keys():
            host, executor = spaces.get(key)
            for row in await asyncio.to_thread(executor.store.attempts, 50):
                try:
                    status = await asyncio.to_thread(executor.status, row["attempt_id"])
                except Exception:  # noqa: BLE001 - one unreadable run must not hide the rest
                    continue
                runs.append(summary(key, host, status))
        runs.sort(key=lambda run: (not run["needs_you"], -run["updated_at"]))
        return {"runs": runs[:50], "settings": spaces.settings()}

    @server.tool(annotations=READ_ONLY)
    async def workflow_run(attempt_id: str) -> dict:
        """Everything about one run: steps, plan, pending decision and jobs,
        checks with evidence, and recent activity."""
        host, executor = spaces.for_attempt(attempt_id)
        match = _ATTEMPT.match(attempt_id)
        status = await asyncio.to_thread(executor.status, attempt_id)
        values, _ = await asyncio.to_thread(executor._snapshot, attempt_id)
        request = values.get("request", {})
        pending = host.pending(attempt_id)
        results = {r["check_id"]: r for r in (status.verification or {}).get("results", [])}
        definition = request.get("definition")
        steps = (_custom_steps(definition, values, status) if definition else
                 timeline(status, bool(request.get("reviewer")), [j["kind"] for j in pending]))
        return {
            **summary(match.group(1), host, status),
            "goal": status.goal, "phase": status.phase, "detail": status.detail,
            "reviewer": bool(request.get("reviewer")),
            "steps": steps,
            "graph": definition and {"nodes": definition["nodes"], "edges": definition["edges"],
                                     "agent": definition["agent"],
                                     "agent_name": definition["agent_name"]},
            "outputs": [values.get("outputs", {})[i] for i in values.get("order", [])
                        if i in values.get("outputs", {})][-10:],
            "plan": [step.get("title", "") for step in (values.get("plan") or {}).get("steps", [])],
            "decision": status.pending_decision,
            "jobs": [{"operation_id": j["operation_id"], "kind": j["kind"], "access": j["access"],
                      "title": j["inputs"].get("title", ""), "agent": j.get("assignee", ""),
                      "agent_name": j["inputs"].get("agent_name", ""),
                      "instruction": j["instruction"][:500]} for j in pending],
            "checks": [{"id": c["id"], "kind": c.get("kind", ""),
                        "requirement": c.get("requirement", ""),
                        "state": results.get(c["id"], {}).get("state", "not_run"),
                        "detail": results.get(c["id"], {}).get("detail", "")}
                       for c in request.get("checks", [])],
            "investigations": list(request.get("investigations", [])),
            "repair_rounds": status.repair_rounds, "result": status.result,
            "activity": [{"type": e["type"], "operation_id": e["operation_id"],
                          "payload": e["payload"]}
                         for e in host.events_after(attempt_id, 0, 500)[-20:]
                         if not e.get("diagnostic")],
        }

    @server.tool(annotations=READ_ONLY)
    async def workflow_definitions() -> dict:
        """Workflows the user drew in the LangGraph Workflows window. Start one
        with workflow_start(workflow="custom", definition_id=...)."""
        return {"definitions": definitions.list()}

    @server.tool(annotations=READ_ONLY)
    async def workflow_definition(definition_id: str) -> dict:
        """One drawn workflow: its steps, connections and assigned agents."""
        return definitions.get(definition_id)

    # Tools below are registered only when Locus confirms it hides them from
    # agents, so they are called only by the user's LangGraph Workflows window.
    if "workflow_save_definition" in panel_tools:
        @server.tool(annotations=STATEFUL)
        async def workflow_save_definition(definition: dict) -> dict:
            """Save a workflow drawn in the window; returns problems instead
            of saving when the graph cannot run."""
            try:
                return {"saved": definitions.save(definition)}
            except ContractError as error:
                return {"problems": [str(error)]}

    if "workflow_delete_definition" in panel_tools:
        @server.tool(annotations=STATEFUL)
        async def workflow_delete_definition(definition_id: str) -> dict:
            """Delete a drawn workflow. Runs that already started keep their copy."""
            return {"deleted": definitions.delete(definition_id)}

    if "workflow_launch" in panel_tools:
        @server.tool(annotations=STATEFUL)
        async def workflow_launch(
            workflow: Literal["verified_change", "research", "custom"], goal: str, ctx: Context,
            checks: list[dict] | None = None, plan_steps: list[str] | None = None,
            investigations: list[str] | None = None, reviewer: bool | None = None,
            definition_id: str | None = None,
        ) -> dict:
            """Start a run from the window. Decisions wait in the window
            instead of prompting in a chat."""
            host, executor, request = await begin(ctx, workflow, goal, checks, plan_steps,
                                                  investigations, reviewer, definition_id)
            await asyncio.to_thread(executor.start, request)
            return await workflow_run(request["attempt_id"])

    if "workflow_dispatch" in panel_tools:
        @server.tool(annotations=STATEFUL)
        async def workflow_dispatch(attempt_id: str, operation_id: str) -> dict:
            """The message that hands one assigned step to its agent's chat."""
            host, _ = spaces.for_attempt(attempt_id)
            if not operation_id.startswith(attempt_id + "/"):
                raise ToolError("operation_id does not belong to this attempt")
            try:
                handed = host.dispatch(operation_id)
            except ReportRejected as error:
                raise ToolError(str(error)) from error
            spec = handed["spec"]
            return {"agent": spec["assignee"], "agent_name": spec["inputs"].get("agent_name", ""),
                    "title": spec["inputs"].get("title", ""), "access": spec["access"],
                    "handed_before": not handed["first"],
                    "text": _handoff_text(attempt_id, spec, handed["claim"])}

    if "workflow_decide" in panel_tools:
        # Answering a decision stays a human action in the Locus window.
        @server.tool(annotations=STATEFUL)
        async def workflow_decide(attempt_id: str, decision_id: str, revision: int,
                                  digest: str, choice: str) -> dict:
            """Answer a pending decision from the Locus window."""
            host, executor = spaces.for_attempt(attempt_id)
            status = await asyncio.to_thread(executor.status, attempt_id)
            try:
                await asyncio.to_thread(executor.decide, {
                    "decision_id": decision_id, "run_id": status.run_id, "attempt_id": attempt_id,
                    "revision": revision, "digest": digest, "choice": choice, "actor": "user"})
            except DecisionRejected as error:
                raise ToolError(error.code) from error
            return await workflow_run(attempt_id)

    return server


def main() -> None:
    data = os.environ.get("LGW_DATA") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not data or data.startswith("${"):
        raise SystemExit("usage: python -m langgraph_workflow.mcp_server DATA_DIR")
    build_server(Path(data)).run("stdio")


if __name__ == "__main__":
    main()
