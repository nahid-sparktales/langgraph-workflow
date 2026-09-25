// Dev-only: stands in for Locus so plugin/ui can be reviewed in a browser.
(async () => {
  const now = Date.now() / 1000;
  for (const link of document.querySelectorAll('link[rel="stylesheet"]')) link.href = link.href.split("?")[0] + "?t=" + Date.now();
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
  const agents = [
    { id: "a-nova", name: "Nova", role: "planner", provider: "Claude plan", model: "claude-sonnet-4.5", access: "workspace_write", available: true },
    { id: "a-kai", name: "Kai", role: "implementer", provider: "Custom endpoint", model: "vLLM · qwen3-coder-30b", access: "workspace_write", available: true },
    { id: "a-mira", name: "Mira", role: "reviewer", provider: "ChatGPT plan", model: "gpt-5", access: "workspace_write", available: true },
    { id: "a-juno", name: "Juno", role: "researcher", provider: "Kimi Code", model: "kimi-k2", access: "read_only", available: true },
    { id: "a-ollie", name: "Ollie", role: "generalist", provider: "Local", model: "qwen3:8b", access: "read_only", available: true },
  ];
  const definitions = {
    "plan-build-review": { id: "plan-build-review", title: "Plan, build, review", description: "Nova plans, Kai builds on vLLM, Mira reviews.", agent: "", agent_name: "",
      nodes: [{ id: "start", type: "start", title: "Start", x: 40, y: 180 },
        { id: "plan", type: "task", title: "Plan the change", access: "read", instruction: "Write a short numbered plan.", agent: "a-nova", agent_name: "Nova", choices: [], max_visits: 1, x: 180, y: 160 },
        { id: "ok", type: "approval", title: "Approve the plan", prompt: "", max_visits: 1, x: 440, y: 160 },
        { id: "build", type: "task", title: "Make the change", access: "write", instruction: "Carry out the plan.", agent: "a-kai", agent_name: "Kai", choices: [], max_visits: 3, x: 680, y: 160 },
        { id: "check", type: "check", title: "Run the checks", max_visits: 3, x: 940, y: 160 },
        { id: "review", type: "task", title: "Review the change", access: "read", instruction: "Review against the goal.", agent: "a-mira", agent_name: "Mira", choices: ["approve", "changes"], max_visits: 2, x: 1180, y: 160 },
        { id: "end", type: "end", title: "End", x: 1440, y: 180 }],
      edges: [{ from: "start", on: "next", to: "plan" }, { from: "plan", on: "done", to: "ok" }, { from: "ok", on: "approved", to: "build" },
        { from: "build", on: "done", to: "check" }, { from: "check", on: "passed", to: "review" }, { from: "check", on: "failed", to: "build" },
        { from: "review", on: "approve", to: "end" }, { from: "review", on: "changes", to: "build" }] },
  };
  const customSteps = (current) => definitions["plan-build-review"].nodes.filter((n) => n.type !== "start").map((n, i, all) => ({
    id: n.id, label: n.title, type: n.type, state: i < all.findIndex((x) => x.id === current) ? "done" : n.id === current ? "current" : "upcoming" }));
  runs["lgw-eeeeeeeeeeee-0000000005"] = {
    attempt_id: "lgw-eeeeeeeeeeee-0000000005", workflow: "custom", title: "Plan, build, review", project: "locus",
    goal: "Add keyboard shortcuts to the command palette", status: "waiting_for_job", blocker: "job_in_progress", needs_you: false,
    updated_at: now - 120, waiting_jobs: 1, phase: "build", steps: customSteps("build"),
    graph: definitions["plan-build-review"], decision: null, plan: [], investigations: [], repair_rounds: 0, result: {},
    outputs: [{ step: "plan", title: "Plan the change", summary: "1. Map shortcuts in PaletteModel\n2. Show hints next to commands\n3. Add tests" }],
    handoffs: [{ operation_id: "lgw-eeeeeeeeeeee-0000000005/build-1", agent: "a-kai", agent_name: "Kai", title: "Make the change" }],
    jobs: [{ operation_id: "lgw-eeeeeeeeeeee-0000000005/build-1", kind: "task", access: "write", title: "Make the change", agent: "a-kai", agent_name: "Kai", instruction: "Carry out the plan." }],
    checks: [{ id: "c1", kind: "file_contains", requirement: "Palette.swift contains “shortcut”", state: "not_run", detail: "" }],
    activity: [{ type: "workflow.started", payload: {} }, { type: "decision.resolved", payload: { choice: "approve" } }],
  };
  const handed = new Set();
  const schema = await fetch("../../plugin/settings.schema.json").then((r) => r.json()).catch(() => ({ properties: {} }));
  const defaults = Object.fromEntries(Object.entries(schema.properties || {}).map(([k, v]) => [k, v.default]));
  let values = { ...defaults };
  let revision = "rev-1";
  const body = await fetch("../../plugin/ui/index.html").then((r) => r.text());
  document.body.innerHTML = new DOMParser().parseFromString(body, "text/html").body.innerHTML.replace(/<script[^>]*><\/script>/g, "");
  const reply = (requestID, ok, result, error) => setTimeout(() => window.locusPanel.receive({ version: 1, type: "response", requestID, ok, result, error }), 60);
  window.webkit = { messageHandlers: { locusPanel: { postMessage(message) {
    if (message.type === "ready") return setTimeout(() => window.locusPanel.receive({ version: 1, type: "hello", project: "locus", panel: "workflows", capabilities: ["plugin.settings", "plugin.tools", "chat.compose", "agents.read", "agents.dispatch"] }), 30);
    if (message.type === "composeChat") return console.log("composeChat", message.text);
    if (message.type === "openAgentChat") return console.log("openAgentChat", message.runID, message.agentID);
    if (message.type === "listAgents") return reply(message.requestID, true, { agents });
    if (message.type === "confirmRun") { console.log("confirmRun", message); return reply(message.requestID, true, { confirmed: true }); }
    if (message.type === "dispatchJob") { console.log("dispatchJob", message.agentID, message.text); return reply(message.requestID, true, { sessionID: "s-" + message.operationID }); }
    if (message.type === "getSettings") return reply(message.requestID, true, { schema, values: { ...values }, revision, error: null });
    if (message.type === "saveSettings") { values = { ...defaults, ...message.values }; revision = "rev-" + Date.now(); return reply(message.requestID, true, { schema, values: { ...values }, revision, error: null }); }
    if (message.type === "callTool") {
      const { tool, arguments: args } = message;
      let out;
      if (tool === "workflow_overview") out = { runs: Object.values(runs).map(({ attempt_id, workflow, title, goal, status, blocker, needs_you, project, updated_at, waiting_jobs, handoffs }) => ({ attempt_id, workflow, title, goal, status, blocker, needs_you, project, updated_at, waiting_jobs, handoffs: handoffs || [] })), settings: values };
      else if (tool === "workflow_definitions") out = { definitions: Object.values(definitions).map((d) => ({ id: d.id, title: d.title, description: d.description,
        steps: d.nodes.filter((n) => !["start", "end"].includes(n.type)).length, agents: [...new Set(d.nodes.map((n) => n.agent_name).filter(Boolean))], updated_at: now })) };
      else if (tool === "workflow_definition") out = JSON.parse(JSON.stringify(definitions[args.definition_id]));
      else if (tool === "workflow_save_definition") {
        const d = args.definition;
        const problems = [];
        if (d.nodes.filter((n) => n.type === "start").length !== 1) problems.push("a workflow needs exactly one start step");
        if (!d.nodes.some((n) => n.type === "end")) problems.push("a workflow needs at least one end step");
        for (const n of d.nodes) if (n.type === "task" && !n.instruction.trim()) problems.push(`Step “${n.title}”: instruction is required`);
        if (problems.length) out = { problems: problems.slice(0, 1) };
        else { definitions[d.id] = d; out = { saved: d }; }
      } else if (tool === "workflow_delete_definition") { out = { deleted: Boolean(definitions[args.definition_id]) }; delete definitions[args.definition_id]; }
      else if (tool === "workflow_launch") {
        const id = "lgw-ffffffffffff-" + String(Date.now()).slice(-10);
        const d = definitions[args.definition_id];
        const first = d.nodes.find((n) => n.id === d.edges.find((e) => e.from === "start").to);
        runs[id] = { attempt_id: id, workflow: "custom", title: d.title, project: "locus", goal: args.goal, status: "waiting_for_job", blocker: "", needs_you: false,
          updated_at: Date.now() / 1000, waiting_jobs: 1, phase: first.id, graph: d, decision: null, plan: [], investigations: [], repair_rounds: 0, result: {}, outputs: [],
          steps: d.nodes.filter((n) => n.type !== "start").map((n) => ({ id: n.id, label: n.title, type: n.type, state: n.id === first.id ? "current" : "upcoming" })),
          handoffs: first.agent ? [{ operation_id: id + "/" + first.id + "-1", agent: first.agent, agent_name: first.agent_name, title: first.title }] : [],
          jobs: [{ operation_id: id + "/" + first.id + "-1", kind: "task", access: first.access, title: first.title, agent: first.agent, agent_name: first.agent_name, instruction: first.instruction }],
          checks: (args.checks || []).map((c) => ({ ...c, state: "not_run", detail: "" })), activity: [{ type: "workflow.started", payload: {} }] };
        out = runs[id];
      } else if (tool === "workflow_dispatch" && args.delivered) {
        handed.add(args.operation_id);
        out = { delivered: true };
      } else if (tool === "workflow_dispatch") {
        const run = runs[args.attempt_id];
        const job = run.jobs.find((j) => j.operation_id === args.operation_id);
        out = { agent: job.agent, agent_name: job.agent_name, title: job.title, access: job.access, handed_before: handed.has(job.operation_id),
          workspace: "/Users/you/Projects/locus", text: `LangGraph Workflows hands you step "${job.title}"…` };
      }
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
  for (const name of ["app.js", "editor.js"]) {
    const script = document.createElement("script");
    script.src = "../../plugin/ui/" + name + "?t=" + Date.now();  // always the latest while editing
    script.async = false;
    document.body.append(script);
  }
})();
