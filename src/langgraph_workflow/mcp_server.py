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
from .contracts import TERMINAL_STATUSES, AttemptStatus, digest
from .executor import DecisionRejected, WorkflowExecutor

RESULT_SHAPES = {
    "inspect": {"summary": "what you found that matters for the goal"},
    "plan": {"plan": {"steps": [{"title": "one deliverable-sized step"}]}},
    "write": {"summary": "what you changed"},
    "review": {"verdict": "approve | changes_requested", "findings": ["issue to fix"]},
    "investigate": {"claims": {"short_key": "value"}, "sources": ["where each claim came from"]},
    "synthesize": {"summary": "answer", "claims": {"short_key": "value"},
                   "sources": ["..."], "conflicts_reported": ["conflicting claim keys"]},
}
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
STATEFUL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                           openWorldHint=False)
_ATTEMPT = re.compile(r"^lgw-([0-9a-f]{12})-[0-9a-f]{10}$")


class Workspaces:
    """One host and executor per workspace, created on first use."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.registry = data / "workspaces.json"
        self._open: dict[str, tuple[AgentHost, WorkflowExecutor]] = {}

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
            host = AgentHost(root, workspace)
            self._open[key] = (host, WorkflowExecutor(host, root / "checkpoints.sqlite3"))
        return self._open[key]

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
    title = {"plan_approval": "Approve this plan?",
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


def _view(host: AgentHost, status: AttemptStatus) -> dict[str, Any]:
    out: dict[str, Any] = {"attempt_id": status.attempt_id, "workflow": status.workflow,
                           "status": status.status, "phase": status.phase}
    if status.blocker:
        out["blocker"] = status.blocker
    if status.status == "waiting_for_job":
        out["jobs"] = [{
            "operation_id": spec["operation_id"], "kind": spec["kind"], "access": spec["access"],
            "instruction": spec["instruction"], "inputs": spec["inputs"],
            "result_shape": RESULT_SHAPES[spec["kind"]],
        } for spec in host.pending(status.attempt_id)]
        out["next_step"] = (
            "Do each job with your normal tools. Jobs with access 'read' must not change any "
            "file. Then call workflow_report with the operation_id, outcome 'completed', "
            "'failed' or 'refused' (when permission was denied), and a result in result_shape. "
            "Report only what actually happened.")
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
            status = await asyncio.to_thread(executor.decide, response)
        return _view(host, status)

    @server.tool(annotations=STATEFUL)
    async def workflow_start(
        workflow: Literal["verified_change", "research"], goal: str, ctx: Context,
        checks: list[dict] | None = None, plan_steps: list[str] | None = None,
        investigations: list[str] | None = None, reviewer: bool = False,
    ) -> dict:
        """Start a workflow in the current workspace.

        verified_change: plan (or use plan_steps) → user approves → implement →
        verify the declared checks → bounded repair → optional review.
        research: read-only investigations (one per question) → synthesis.
        checks: acceptance checks, e.g. {"id": "readme", "kind": "file_contains",
        "path": "README.md", "value": "Install", "requirement": "..."}; kinds
        file_exists, file_contains, json_value (pointer + value) are verified by
        this plugin; command and human_review end in needs_review.
        """
        key = spaces.key_for(await _workspace(ctx))
        host, executor = spaces.get(key)
        attempt = f"lgw-{key}-{uuid.uuid4().hex[:10]}"
        request: dict[str, Any] = {
            "workflow": workflow, "run_id": attempt, "task_id": attempt, "attempt_id": attempt,
            "workspace_id": f"ws-{key}", "goal": goal, "checks": list(checks or []),
            "investigations": list(investigations or []), "reviewer": reviewer}
        if plan_steps:
            request["plan"] = {"steps": [{"title": s} for s in plan_steps]}
        status = await asyncio.to_thread(executor.start, request)
        return await advance(ctx, host, executor, status)

    @server.tool(annotations=STATEFUL)
    async def workflow_report(
        attempt_id: str, operation_id: str, outcome: Literal["completed", "failed", "refused"],
        ctx: Context, result: dict | None = None, note: str = "",
    ) -> dict:
        """Report the outcome of one job you performed, then get the next step."""
        host, executor = spaces.for_attempt(attempt_id)
        if not operation_id.startswith(attempt_id + "/"):
            raise ToolError("operation_id does not belong to this attempt")
        try:
            host.report(operation_id, outcome, result, note)
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

    return server


def main() -> None:
    data = os.environ.get("LGW_DATA") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not data or data.startswith("${"):
        raise SystemExit("usage: python -m langgraph_workflow.mcp_server DATA_DIR")
    build_server(Path(data)).run("stdio")


if __name__ == "__main__":
    main()
