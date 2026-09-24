// Dev-only: stands in for Locus so plugin/ui can be reviewed in a browser.
(async () => {
  const now = Date.now() / 1000;
  const change = ["understand", "Understand"], plan = ["plan", "Plan"], approve = ["approve", "Approve"],
    build = ["build", "Build"], check = ["check", "Check"], review = ["review", "Review"], done = ["done", "Done"];
  const steps = (list, current, finished) => list.map(([id, label], i) => ({
    id, label, state: finished === "verified" ? "done" : i < current ? "done" : i === current ? (finished ? "stopped" : "current") : (id === "review" && current < 0 ? "skipped" : "upcoming") }));
  const runs = {
    "lgw-aaaaaaaaaaaa-0000000001": {
      attempt_id: "lgw-aaaaaaaaaaaa-0000000001", workflow: "verified_change", project: "locus",
      goal: "Add a dark mode toggle to the settings page", status: "waiting_for_input", blocker: "", needs_you: true,
      updated_at: now - 40, waiting_jobs: 0, reviewer: true, phase: "plan",
      steps: steps([change, plan, approve, build, check, review, done], 2),
      plan: ["Add a theme preference to SettingsModel", "Render a toggle in the Appearance section", "Persist the choice and apply it on launch"],
      decision: { decision_id: "d1", kind: "plan_approval", revision: 1, digest: "x", options: ["approve", "deny", "edit"], summary: "" },
      jobs: [], checks: [
        { id: "c1", kind: "file_contains", requirement: "SettingsView.swift contains “Dark mode”", state: "not_run", detail: "" },
        { id: "c2", kind: "command", requirement: "Unit tests pass", state: "not_run", detail: "" }],
      investigations: [], repair_rounds: 0, result: {},
      activity: [{ type: "workflow.started", payload: {} }, { type: "job.submitted", payload: { kind: "inspect" } },
        { type: "job.outcome", payload: { kind: "inspect", status: "settled" } }, { type: "job.outcome", payload: { kind: "plan", status: "settled" } },
        { type: "decision.pending", payload: {} }],
    },
    "lgw-bbbbbbbbbbbb-0000000002": {
      attempt_id: "lgw-bbbbbbbbbbbb-0000000002", workflow: "research", project: "cache-service",
      goal: "Which storage engine fits our offline cache best?", status: "waiting_for_job", blocker: "job_in_progress",
      needs_you: false, updated_at: now - 400, waiting_jobs: 2, phase: "collect",
      steps: steps([["scope", "Scope"], ["investigate", "Investigate"], ["compare", "Compare"], ["synthesize", "Synthesize"], ["check", "Check"], done], 1),
      jobs: [{ operation_id: "o1", kind: "investigate", access: "read", instruction: "How does SQLite behave under concurrent writers?" },
        { operation_id: "o2", kind: "investigate", access: "read", instruction: "What do teams report about LMDB in production?" }],
      checks: [{ id: "deliverable", kind: "file_exists", requirement: "The answer is saved in notes/cache.md", state: "not_run", detail: "" }],
      investigations: ["How does SQLite behave under concurrent writers?", "What do teams report about LMDB in production?"],
      plan: [], repair_rounds: 0, result: {}, activity: [{ type: "workflow.started", payload: {} }, { type: "workflow.routing", payload: { mode: "parallel", investigations: 2 } }],
    },
    "lgw-cccccccccccc-0000000003": {
      attempt_id: "lgw-cccccccccccc-0000000003", workflow: "verified_change", project: "locus",
      goal: "Document the install steps in README", status: "verified", blocker: "", needs_you: false,
      updated_at: now - 7200, waiting_jobs: 0, phase: "finish",
      steps: steps([change, plan, approve, build, check, review, done], 6, "verified").map((s) => s.id === "review" ? { ...s, state: "skipped" } : s),
      jobs: [], plan: ["Add an Install section", "Link the plugin docs"],
      checks: [{ id: "c1", kind: "file_contains", requirement: "README.md contains “Install”", state: "passed", detail: "" }],
      investigations: [], repair_rounds: 1, result: {}, activity: [{ type: "verification.result", payload: { outcome: "passed", final: true } }, { type: "workflow.outcome", payload: { status: "verified" } }],
    },
    "lgw-dddddddddddd-0000000004": {
      attempt_id: "lgw-dddddddddddd-0000000004", workflow: "verified_change", project: "locus",
      goal: "Speed up the startup path", status: "needs_review", blocker: "unsupported_check", needs_you: false,
      updated_at: now - 90000, waiting_jobs: 0, phase: "finish",
      steps: steps([change, plan, approve, build, check, review, done], 4, "needs_review"),
      jobs: [], plan: ["Defer extension scan"], investigations: [], repair_rounds: 0, result: {}, activity: [],
      checks: [{ id: "c1", kind: "file_exists", requirement: "Startup.swift exists", state: "passed", detail: "" },
        { id: "c2", kind: "command", requirement: "Startup benchmark under 400 ms", state: "unsupported", detail: "'command' checks are not run by this host" }],
    },
  };
  const schema = await fetch("../../plugin/settings.schema.json").then((r) => r.json()).catch(() => ({ properties: {} }));
  const defaults = Object.fromEntries(Object.entries(schema.properties || {}).map(([k, v]) => [k, v.default]));
  let values = { ...defaults };
  let revision = "rev-1";
  const body = await fetch("../../plugin/ui/index.html").then((r) => r.text());
  document.body.innerHTML = new DOMParser().parseFromString(body, "text/html").body.innerHTML.replace(/<script[^>]*><\/script>/g, "");
  const reply = (requestID, ok, result, error) => setTimeout(() => window.locusPanel.receive({ version: 1, type: "response", requestID, ok, result, error }), 60);
  window.webkit = { messageHandlers: { locusPanel: { postMessage(message) {
    if (message.type === "ready") return setTimeout(() => window.locusPanel.receive({ version: 1, type: "hello", project: "locus", panel: "workflows", capabilities: ["plugin.settings", "plugin.tools", "chat.compose"] }), 30);
    if (message.type === "composeChat") return console.log("composeChat", message.text);
    if (message.type === "getSettings") return reply(message.requestID, true, { schema, values: { ...values }, revision, error: null });
    if (message.type === "saveSettings") { values = { ...defaults, ...message.values }; revision = "rev-" + Date.now(); return reply(message.requestID, true, { schema, values: { ...values }, revision, error: null }); }
    if (message.type === "callTool") {
      const { tool, arguments: args } = message;
      let out;
      if (tool === "workflow_overview") out = { runs: Object.values(runs).map(({ attempt_id, workflow, goal, status, blocker, needs_you, project, updated_at, waiting_jobs }) => ({ attempt_id, workflow, goal, status, blocker, needs_you, project, updated_at, waiting_jobs })), settings: values };
      else if (tool === "workflow_run") out = runs[args.attempt_id];
      else if (tool === "workflow_decide") {
        const run = runs[args.attempt_id];
        run.decision = null; run.needs_you = false;
        run.status = args.choice === "approve" ? "waiting_for_job" : "denied";
        run.jobs = args.choice === "approve" ? [{ operation_id: "w", kind: "write", access: "write", instruction: run.goal }] : [];
        run.steps = run.steps.map((s, i) => ({ ...s, state: i < 3 ? "done" : i === 3 ? "current" : s.id === "review" ? "upcoming" : "upcoming" }));
        out = run;
      } else if (tool === "workflow_cancel") { runs[args.attempt_id].status = "cancelled"; out = runs[args.attempt_id]; }
      return reply(message.requestID, true, { content: JSON.stringify(out), is_error: false });
    }
  } } } };
  const script = document.createElement("script");
  script.src = "../../plugin/ui/app.js";
  document.body.append(script);
})();
