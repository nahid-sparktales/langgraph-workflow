"""Headless Locus integration harness (one scenario step per process).

    python harness.py HOME SCENARIO

Runs in the disposable Locus venv. ``OLLAMA_CODE_HOME`` is pointed at
``HOME/profile`` before Locus is imported, so nothing touches a real profile.
The only fake is the provider: scripted clients stand in for the model on the
reader route and on the writer's AgentCore. Everything else — RunStore,
TeamOrchestrator.run_read_job, the model-call scheduler, the task usage
ledger, AgentCore.run_turn, PermissionManager + decider, tool receipts, and
TaskVerifier — is real Locus code. Prints one JSON report.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(sys.argv[1]).resolve()
SCENARIO = sys.argv[2]
os.environ["OLLAMA_CODE_HOME"] = str(HOME / "profile")  # before any Locus import
sys.path.insert(0, str(Path(__file__).parent))

from adapter_reference import LocusHost  # noqa: E402
from ollama_code import orchestration  # noqa: E402
from ollama_code.core import AgentCore  # noqa: E402
from ollama_code.ollama import ChatResponse, ToolCall  # noqa: E402
from ollama_code.orchestration import AgentProfile, OrchestrationBudget  # noqa: E402
from ollama_code.runstore import RunStore  # noqa: E402
from ollama_code.task_journal import TaskJournal  # noqa: E402
from ollama_code.task_usage_ledger import UsageLedger  # noqa: E402

from langgraph_workflow import DecisionRejected, WorkflowExecutor, WorkflowRequest  # noqa: E402
from langgraph_workflow.testing import run_host_contract  # noqa: E402


class ScriptedReader:
    """Deterministic provider for read jobs, keyed by the job kind marker."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.on_review = None  # optional side effect to simulate an outside change

    def chat_stream(self, model, messages, tools=None, on_token=None, should_stop=None,
                    on_thinking=None, think=False, options=None):
        prompt = messages[-1]["content"]
        kind = prompt[1:prompt.index("]")]
        self.calls.append(kind)
        if kind == "review" and self.on_review:
            self.on_review()
        queue = self.responses[kind]
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if kind == "investigate":
            payload = payload[prompt.split("Inputs (JSON): ")[1].split('"index": ')[1][0]]
        text = json.dumps(payload)
        if on_token:
            on_token(text)
        return ChatResponse(content_parts=[text], done=True, done_reason="stop",
                            prompt_eval_count=120, eval_count=40)


class ScriptedWriter:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def chat_stream(self, model, messages, tools=None, on_token=None, should_stop=None,
                    on_thinking=None, think=False, options=None):
        self.calls += 1
        return self.responses.pop(0)

    def context_length(self, name):
        return 262_144

    def loaded_context_length(self, name):
        return 0

    def resident_state(self, name):
        return {"context_length": 0, "size": 0, "size_vram": 0}

    def list_models(self):
        return [{"name": "fixture"}]


READER_SCRIPT = {
    "inspect": [{"summary": "result.txt is absent"}],
    "plan": [{"plan": {"steps": [{"title": "Create result.txt containing done"}]}}],
    "review": [{"verdict": "approve", "findings": []}],
    "investigate": [{"0": {"claims": {"engine": "sqlite"}, "sources": ["doc://storage#1"]},
                     "1": {"claims": {"wal": True}, "sources": ["doc://storage#2"]}}],
    "synthesize": [{"summary": "Use SQLite in WAL mode", "claims": {"engine": "sqlite",
                    "wal": True}, "sources": ["doc://storage#1", "doc://storage#2"]}],
}
CHECK = {"id": "result", "kind": "file_contains", "path": "result.txt", "value": "done",
         "requirement": "result.txt contains done"}


def build(decision: str = "once", *, plan_approval: bool = True):
    workspace = HOME / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runs = RunStore()  # default path under the temporary OLLAMA_CODE_HOME
    core = AgentCore(cwd=str(workspace), model="fixture",
                     config={"provider": "ollama", "auto_compact": False, "max_iterations": 6})
    core._emit_info = lambda: None
    core.client = ScriptedWriter([
        ChatResponse(tool_calls=[ToolCall("write_file", {"path": "result.txt",
                                                          "content": "done\n"})], done=True),
        ChatResponse(content_parts=["Created result.txt."], done=True, done_reason="stop"),
    ])
    reader_client = ScriptedReader(json.loads(json.dumps(READER_SCRIPT)))
    orchestration._client = lambda profile: reader_client  # provider fixture only
    reader = AgentProfile.parse({
        "id": "reader", "name": "Reader", "model": "fixture", "role": "researcher",
        "access_ceiling": "read_only", "timeout_seconds": 60, "token_limit": 20_000,
        "metering": "self_hosted", "route": {"provider": "ollama", "host": "http://127.0.0.1:9"}})
    host = LocusHost(core=core, runs=runs, run_id="run-lgw", session_id="session-lgw",
                     task_id="work:lgw", workspace=str(workspace), reader=reader,
                     budget=OrchestrationBudget.parse({"max_model_calls": 12}),
                     decider=lambda *_: decision, actor="controller",
                     plan_approval=plan_approval)
    store = HOME / "profile" / "langgraph-workflow" / "checkpoints.sqlite3"
    executor = WorkflowExecutor(host, store)
    return host, executor, runs, reader_client, core


def change_request(**extra):
    return {"workflow": "verified_change", "run_id": "run-lgw", "task_id": "work:lgw",
            "attempt_id": "attempt-1", "workspace_id": "ws-lgw",
            "checkout_ref": "path:" + str((HOME / "workspace").resolve()),
            "goal": "Create result.txt containing done", "checks": [CHECK], **extra}


def report(host, executor, runs, reader, core, status, **extra):
    events = runs.events("run-lgw")
    workflow_events = [e for e in events if e["type"] == "workflow_event"]
    journal = TaskJournal.for_owner(runs, "run:run-lgw")
    return {
        "status": status.to_dict(),
        "attempts": [{"job_id": a["job_id"].split("#")[0], "attempt_id": a["attempt_id"],
                      "state": a["state"], "execution_engine": a["execution_engine"]}
                     for a in runs.attempts("run-lgw")],
        "event_types": sorted({e["type"] for e in events}),
        "workflow_events": [[e["seq"], e["workflow_event"]["type"]] for e in workflow_events],
        "workflow_event_ids_unique": len({e["event_id"] for e in workflow_events})
        == len(workflow_events),
        "reader_calls": reader.calls,
        "writer_calls": core.client.calls,
        "permission_decisions": host.permission_decisions,
        "core_events": sorted(set(host.core_events)),
        "task_completion": list(host.store.completion("work:lgw")),
        "usage_summary": UsageLedger(journal).summary() if journal else None,
        "result_file": (HOME / "workspace" / "result.txt").read_text()
        if (HOME / "workspace" / "result.txt").exists() else None,
        **extra,
    }


def plugin(source: str) -> dict:
    """Install the plugin from a marketplace with Locus's own extension manager,
    start it with Locus's MCP runtime, and drive one verified change the way
    the Locus agent would, answering the approval prompt as the user."""
    from ollama_code.extensions import ExtensionManager
    from ollama_code.mcp_runtime import MCPManager

    workspace = HOME / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manager = ExtensionManager(str(workspace), root=HOME / "extensions")
    market = manager.add_marketplace(source)
    trust = manager.inspect_catalog_plugin(market["id"], "langgraph-workflow")
    installed = manager.install_plugin(market["id"], "langgraph-workflow",
                                       expected_digest=trust["digest"])
    prompts: list[str] = []
    runtime: MCPManager | None = None
    answer = {"action": "accept"}

    def emit(event: dict) -> None:
        if event.get("type") == "mcp_input_required":  # Locus's native prompt
            prompts.append(str(event.get("message", "")))
            runtime.answer_elicitation(event["request_id"], answer["action"],
                                       {"choice": "approve"})

    window = None
    if hasattr(manager, "plugin_settings"):  # a Locus build with plugin panels
        window = {"panels": [{k: p[k] for k in ("id", "capabilities", "tools")}
                             for p in installed.get("panels") or []]}

    runtime = MCPManager(manager, emit=emit)
    server_id = "plugin:langgraph-workflow:workflows"

    def call(name: str, **arguments) -> dict:
        text = runtime.call_tool(server_id, name, arguments)
        try:
            return json.JSONDecoder().raw_decode(text)[0]
        except ValueError:
            return {"error": text[:2000]}

    try:
        runtime.refresh(wait=True)
        status = runtime.status(server_id) or {}
        tools = sorted(t["name"] for t in runtime.available_tools())
        steps = [call("workflow_start", workflow="verified_change",
                      goal="Create result.txt containing done", checks=[CHECK])]
        while steps[-1].get("status") == "waiting_for_job":
            job = steps[-1]["jobs"][0]
            result = {"inspect": {"summary": "empty workspace"},
                      "plan": {"plan": {"steps": [{"title": "Create result.txt"}]}},
                      "write": {"summary": "created result.txt"}}[job["kind"]]
            if job["kind"] == "write":  # the agent's own tool call in real use
                (workspace / "result.txt").write_text("done\n")
            steps.append(call("workflow_report", attempt_id=steps[0]["attempt_id"],
                              operation_id=job["operation_id"], outcome="completed",
                              result=result))
        if window is not None:
            # What the window does: the user declined the chat prompt, then
            # approves from the window through the panel-only tool.
            answer["action"] = "decline"
            settings = manager.plugin_settings(installed["id"])
            saved = manager.set_plugin_settings(installed["id"], {"reviewer_default": True},
                                                expected_revision=settings["revision"])
            window |= {"default_values": settings["values"], "saved_values": saved["values"]}
            waiting = call("workflow_start", workflow="verified_change", goal="Second change",
                           checks=[CHECK], plan_steps=["Touch result.txt"])
            detail = call("workflow_run", attempt_id=waiting["attempt_id"])
            decision = detail["decision"]
            decided = call("workflow_decide", attempt_id=waiting["attempt_id"],
                           decision_id=decision["decision_id"], revision=decision["revision"],
                           digest=decision["digest"], choice="approve")
            window |= {"overview_settings": call("workflow_overview")["settings"],
                       "run_reviewer": detail["reviewer"],
                       "decided": (decided.get("status"),
                                   [j["kind"] for j in decided.get("jobs", [])])}
    finally:
        runtime.close()
    return {
        "marketplace": {k: market.get(k) for k in ("id", "kind", "source", "error")},
        "trust": {"digest": trust["digest"],
                  "mcp_servers": [{k: s.get(k) for k in ("id", "command", "args",
                                                         "protocol_mode")}
                                  for s in trust.get("plugin", trust).get("mcp_servers", [])],
                  "skills": [k.get("id") for k in trust.get("plugin", trust).get("skills", [])],
                  "unsupported": trust.get("plugin", trust).get("unsupported")},
        "installed": {k: installed.get(k) for k in ("id", "version", "digest")},
        "server_state": status.get("state"), "server_error": status.get("summary"),
        "tools": tools, "prompts": prompts,
        "statuses": [(s.get("status"), s.get("phase"), [j["kind"] for j in s.get("jobs", [])])
                     for s in steps],
        "final": steps[-1], "result_file": (workspace / "result.txt").read_text()
        if (workspace / "result.txt").exists() else None,
        "window": window,
    }


def main() -> dict:
    if SCENARIO == "change-start":
        host, ex, runs, reader, core = build()
        status = ex.start(change_request())
        return report(host, ex, runs, reader, core, status)
    if SCENARIO == "change-decide":  # a new process: fresh Locus services, same profile
        host, ex, runs, reader, core = build()
        waiting = ex.status("attempt-1")
        pending = waiting.pending_decision
        response = {"decision_id": pending["decision_id"], "run_id": "run-lgw",
                    "attempt_id": "attempt-1", "revision": pending["revision"],
                    "digest": pending["digest"], "choice": "approve", "actor": "controller"}
        try:
            ex.decide({**response, "actor": "someone-else"})
            forged = "accepted"
        except DecisionRejected as error:
            forged = error.code
        status = ex.decide(response)
        before = len(runs.events("run-lgw"))
        # Duplicate delivery of an already-published event is a no-op.
        from langgraph_workflow.events import make_event
        host.publish(make_event("workflow.outcome", status.to_dict() | {
            "workflow": "verified_change"}, key=status.status))
        cursor = runs.events("run-lgw")[-3]["seq"]
        return report(host, ex, runs, reader, core, status, forged_actor=forged,
                      events_after_duplicate_publish=len(runs.events("run-lgw")) - before,
                      replay_from_cursor=[e["seq"] for e in runs.events("run-lgw", cursor)])
    if SCENARIO == "change-deny-writes":
        host, ex, runs, reader, core = build("deny", plan_approval=False)
        status = ex.start(change_request(plan={"steps": [{"title": "Create result.txt"}]}))
        return report(host, ex, runs, reader, core, status)
    if SCENARIO == "research":
        host, ex, runs, reader, core = build(plan_approval=False)
        (HOME / "workspace" / "notes.md").write_text("storage notes\n")
        status = ex.start({"workflow": "research", "run_id": "run-lgw", "task_id": "work:lgw",
                           "attempt_id": "research-1", "workspace_id": "ws-lgw",
                           "goal": "Which storage engine?",
                           "investigations": ["Which engine?", "Which journal mode?"],
                           "checks": [{"id": "notes", "kind": "file_exists",
                                       "path": "notes.md", "requirement": "notes exist"}]})
        return report(host, ex, runs, reader, core, status)
    if SCENARIO == "stale-final":
        host, ex, runs, reader, core = build(plan_approval=False)
        reader.on_review = lambda: (HOME / "workspace" / "result.txt").write_text("tampered\n")
        status = ex.start(change_request(reviewer=True,
                                         plan={"steps": [{"title": "Create result.txt"}]}))
        return report(host, ex, runs, reader, core, status)
    if SCENARIO == "plugin":
        return plugin(sys.argv[3])
    if SCENARIO == "contract":
        host, ex, runs, reader, core = build(plan_approval=False)
        request = WorkflowRequest.from_dict(change_request(attempt_id="contract-1"))

        def count(event_id):
            return sum(1 for e in runs.events("run-lgw") if e.get("event_id") == event_id)

        return {"contract": run_host_contract(host, request, count_events=count)}
    if SCENARIO == "unpatched":
        del orchestration.TeamOrchestrator.run_read_job  # simulate Locus without patch 0001
        host, ex, runs, reader, core = build(plan_approval=False)
        needs_reads = ex.start(change_request(attempt_id="attempt-u1"))
        supplied = ex.start(change_request(attempt_id="attempt-u2",
                                           plan={"steps": [{"title": "Create result.txt"}]}))
        return report(host, ex, runs, reader, core, supplied,
                      without_supplied_plan=needs_reads.to_dict())
    raise SystemExit(f"unknown scenario {SCENARIO}")


if __name__ == "__main__":
    print(json.dumps(main(), default=str))
