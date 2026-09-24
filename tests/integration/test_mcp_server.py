"""The plugin's MCP server over real stdio, driven the way Locus's client does:
``mcp.Client`` with elicitation (human decisions) and workspace roots."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import Client, types  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

CHECK = {"id": "result", "kind": "file_contains", "path": "result.txt", "value": "done",
         "requirement": "result.txt says done"}


def run(workspace: Path, data: Path, script, answers, env=None):
    asked = []

    async def elicitation(context, params):
        asked.append(params.message)
        choice = answers.pop(0)
        if choice is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content={"choice": choice})

    async def roots(context):
        return types.ListRootsResult(roots=[types.Root(uri=workspace.as_uri(), name="ws")])

    async def main():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "langgraph_workflow.mcp_server", str(data)],
                                       env=env)
        # protocol_mode "legacy" is what the plugin's .mcp.json asks Locus for.
        client = Client(stdio_client(params), read_timeout_seconds=60, mode="legacy",
                        elicitation_callback=elicitation, list_roots_callback=roots,
                        client_info=types.Implementation(name="Locus", version="test"))
        async with client:
            tools = {t.name: t for t in (await client.list_tools()).tools}

            async def call(name, **args):
                result = await client.call_tool(name, args)
                if result.is_error:
                    return {"error": result.content[0].text}
                return result.structured_content or json.loads(result.content[0].text)

            return tools, await script(call)

    return asyncio.run(main()), asked


def test_agent_driven_verified_change_with_elicited_approval(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()

    async def script(call):
        out = await call("workflow_start", workflow="verified_change",
                         goal="Create result.txt saying done", checks=[CHECK])
        attempt = out["attempt_id"]
        steps = [out]
        job = out["jobs"][0]
        assert job["kind"] == "inspect" and job["access"] == "read"
        out = await call("workflow_report", attempt_id=attempt, operation_id=job["operation_id"],
                         outcome="completed", result={"summary": "empty"})
        job = out["jobs"][0]
        assert job["kind"] == "plan"
        # The approval prompt happens inside this call, answered by the "user".
        out = await call("workflow_report", attempt_id=attempt, operation_id=job["operation_id"],
                         outcome="completed",
                         result={"plan": {"steps": [{"title": "Write result.txt"}]}})
        job = out["jobs"][0]
        assert job["kind"] == "write"
        forged = await call("workflow_report", attempt_id=attempt,
                            operation_id="lgw-000000000000-0000000000/x", outcome="completed")
        (workspace / "result.txt").write_text("done\n")
        out = await call("workflow_report", attempt_id=attempt, operation_id=job["operation_id"],
                         outcome="completed", result={"summary": "wrote result.txt"})
        steps.append(out)
        status = await call("workflow_status", attempt_id=attempt)
        return steps, forged, status

    (tools, (steps, forged, status)), asked = run(workspace, data, script, ["approve"])
    assert {"workflow_start", "workflow_report", "workflow_status", "workflow_cancel"} <= set(tools)
    assert "workflow_decide" not in tools  # the agent cannot answer decisions itself
    assert tools["workflow_status"].annotations.read_only_hint is True
    assert len(asked) == 1 and asked[0].startswith("Approve this plan?")
    assert "does not belong" in forged["error"]
    assert steps[-1]["status"] == "verified"
    assert status["status"] == "verified" and all(status["result"]["evidence"])


def test_declined_decision_waits_and_can_be_answered_later(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()

    async def script(call):
        out = await call("workflow_start", workflow="verified_change", goal="g",
                         checks=[CHECK], plan_steps=["Write result.txt"])
        first = out["status"]
        later = await call("workflow_status", attempt_id=out["attempt_id"])
        return first, later

    (tools, (first, later)), asked = run(workspace, data, script, [None, "deny"])
    assert first == "waiting_for_input"
    assert (later["status"], later["blocker"]) == ("denied", "plan_denied")
    assert len(asked) == 2


def test_state_survives_a_server_restart(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()

    async def start(call):
        return await call("workflow_start", workflow="research", goal="Which engine?")

    (_, first), _ = run(workspace, data, start, [])
    job = first["jobs"][0]

    async def finish(call):
        out = await call("workflow_report", attempt_id=first["attempt_id"],
                         operation_id=job["operation_id"], outcome="completed",
                         result={"claims": {"engine": "sqlite"}, "sources": ["docs"]})
        synth = out["jobs"][0]
        return await call("workflow_report", attempt_id=first["attempt_id"],
                          operation_id=synth["operation_id"], outcome="completed",
                          result={"claims": {"engine": "sqlite"}})

    (_, final), _ = run(workspace, data, finish, [])
    assert (final["status"], final["blocker"]) == ("needs_review", "no_checks_declared")


def test_window_tools_follow_saved_settings(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    # Written by Locus from the window; ill-typed values fall back to defaults.
    (data / "locus-settings.json").write_text(json.dumps(
        {"reviewer_default": True, "max_jobs": 5, "plan_approval": "off"}))

    async def script(call):
        out = await call("workflow_start", workflow="verified_change", goal="g",
                         checks=[CHECK], plan_steps=["Write result.txt"])
        overview = await call("workflow_overview")
        detail = await call("workflow_run", attempt_id=out["attempt_id"])
        decision = detail["decision"]
        stale = await call("workflow_decide", attempt_id=out["attempt_id"],
                           decision_id=decision["decision_id"], revision=decision["revision"],
                           digest="0" * 64, choice="approve")
        after = await call("workflow_decide", attempt_id=out["attempt_id"],
                           decision_id=decision["decision_id"], revision=decision["revision"],
                           digest=decision["digest"], choice="approve")
        return overview, detail, stale, after

    (tools, (overview, detail, stale, after)), asked = run(
        workspace, data, script, [None], env={"LOCUS_PANEL_TOOLS": "workflow_decide"})
    assert tools["workflow_overview"].annotations.read_only_hint is True
    assert tools["workflow_decide"].annotations.read_only_hint is False
    assert len(asked) == 1  # declined in chat, answered in the window
    settings = overview["settings"]
    assert (settings["max_jobs"], settings["reviewer_default"], settings["plan_approval"]) == (
        5, True, True)
    [run_] = overview["runs"]
    assert run_["needs_you"] and run_["status"] == "waiting_for_input"
    assert detail["reviewer"] is True and detail["plan"] == ["Write result.txt"]
    states = {step["id"]: step["state"] for step in detail["steps"]}
    assert states["approve"] == "current" and states["review"] == "upcoming"
    assert "error" in stale
    assert after["status"] == "waiting_for_job" and after["jobs"][0]["kind"] == "write"
    assert {step["id"]: step["state"] for step in after["steps"]}["build"] == "current"


def test_plan_approval_can_be_turned_off(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    (data / "locus-settings.json").write_text(json.dumps({"plan_approval": False}))

    async def script(call):
        return await call("workflow_start", workflow="verified_change", goal="g",
                          checks=[CHECK], plan_steps=["Write result.txt"])

    (tools, out), asked = run(workspace, data, script, [])
    assert "workflow_decide" not in tools
    assert asked == [] and out["jobs"][0]["kind"] == "write"
