"use strict";
// LangGraph Workflows — Locus plugin window.
// Talks only to Locus through window.webkit.messageHandlers.locusPanel
// (bridge version 1). All text from runs is inserted with textContent.

const POLL_MS = 4000;
const state = {
  tab: "runs", runs: [], selected: null, detail: null, settings: null,
  settingsDraft: null, capabilities: [], project: "", confirmCancel: false,
  agents: [], confirmed: new Set(), dispatched: new Map(), handing: new Set(),
};

// ---- bridge --------------------------------------------------------------
const bridge = (() => {
  const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.locusPanel;
  const pending = new Map();
  let seq = 0;
  const post = (message) => handler && handler.postMessage(Object.assign({ version: 1 }, message));
  function request(type, fields) {
    if (!handler) return Promise.reject(new Error("Open this window from Locus."));
    const requestID = "r" + (++seq);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestID);
        reject(new Error("Locus did not answer in time. Try again."));
      }, 130000);
      pending.set(requestID, { resolve, reject, timer });
      post(Object.assign({ type, requestID }, fields));
    });
  }
  window.locusPanel = {
    receive(message) {
      if (!message || message.version !== 1) return;
      if (message.type === "hello") {
        state.project = message.project || "";
        state.capabilities = message.capabilities || [];
        onHello();
      } else if (message.type === "response") {
        const entry = pending.get(message.requestID);
        if (!entry) return;
        pending.delete(message.requestID);
        clearTimeout(entry.timer);
        message.ok ? entry.resolve(message.result) : entry.reject(new Error(message.error || "Request failed."));
      }
    },
  };
  return {
    available: Boolean(handler),
    ready: () => post({ type: "ready" }),
    settings: () => request("getSettings", {}),
    saveSettings: (values, revision) => request("saveSettings", { values, revision }),
    compose: (text) => post({ type: "composeChat", text }),
    agents: () => request("listAgents", {}),
    confirmRun: (fields) => request("confirmRun", fields),
    dispatch: (fields) => request("dispatchJob", fields),
    openChat: (runID, agentID) => post({ type: "openAgentChat", runID, agentID }),
    async tool(tool, args) {
      const result = await request("callTool", { tool, arguments: args || {} });
      const text = String((result && result.content) || "");
      if (!result || result.is_error) throw new Error(text.replace(/^Error:\s*/, "") || "The plugin reported an error.");
      const cut = text.indexOf("\n\nStructured result:");
      return JSON.parse(cut >= 0 ? text.slice(0, cut) : text);
    },
  };
})();

const can = (capability) => state.capabilities.includes(capability);

// ---- tiny DOM helper -------------------------------------------------------
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);

let toastTimer;
function toast(text, bad) {
  const node = $("toast");
  node.textContent = text;
  node.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = "toast"; }, bad ? 5000 : 2600);
}

// ---- plain language --------------------------------------------------------
const LINE = { verified_change: "Change", research: "Research", custom: "Custom" };
const lineClass = (run) => run.workflow === "research" ? "research" : run.workflow === "custom" ? "custom" : "change";
const runName = (run) => run.workflow === "custom" && run.title ? run.title : LINE[run.workflow] || run.workflow;
const TERMINAL = ["verified", "needs_review", "denied", "cancelled", "failed", "blocked"];

const BLOCKERS = {
  no_checks_declared: "No checks were declared, so the result can't be verified automatically. Look it over and accept it yourself.",
  human_review_required: "One of the checks asks for your review.",
  unsupported_check: "A command check can't be run by the plugin. Run it yourself to confirm the result.",
  check_denied: "A check points outside the project, so it wasn't run.",
  missing_evidence: "A check reported success without proof, so it doesn't count.",
  stale_evidence: "Files changed after they were checked.",
  repairs_exhausted: "The checks still fail after the allowed repair attempts.",
  no_progress: "A repair attempt didn't change any files.",
  final_checks_failed: "Files changed after the checks passed, and the final check failed.",
  review_unresolved: "The reviewer still requests changes after the allowed rounds.",
  review_rounds_exhausted: "The review rounds ran out.",
  review_invalid: "The review didn't return a clear verdict.",
  job_denied: "A step was refused, usually because a permission was denied.",
  plan_denied: "You declined the plan. Nothing was changed.",
  invalid_plan: "The plan wasn't usable.",
  read_only_violation: "A read-only step changed files, so the run was stopped.",
  conflicts_unreported: "The answer didn't mention that the investigations disagree.",
  unsupported_claim: "The answer includes a claim that no investigation backs with a source.",
  investigation_failed: "An investigation failed.",
  scope_exceeds_limit: "There were more questions than the settings allow.",
  budget_exhausted: "The run reached its step limit (see Settings).",
  transition_limit: "The run reached its safety limit on steps.",
  uncertain_action: "A step started but its result was never recorded. Check the files, then report the step in chat.",
  workspace_busy: "Another run is changing this project right now.",
  missing_capability: "This Locus can't run this workflow yet.",
  cancelled: "The run was cancelled.",
  loop_limit: "A step ran as many times as it's allowed (see “Times it may run” in the workflow).",
  declined: "You declined. Nothing after that step ran.",
  step_failed: "A step failed.",
  checks_failed: "The checks failed.",
  checks_need_review: "A check needs your review.",
  invalid_choice: "An agent didn't pick one of the step's outcomes.",
  no_checks_run: "No checks ran, so the result can't be verified automatically.",
  changed_after_checks: "Files changed after the checks passed.",
  branch_failed: "One of the parallel steps failed.",
};

function headline(run) {
  const decision = run.decision || null;
  switch (run.status) {
    case "waiting_for_input":
      if (decision && decision.kind === "conflict_resolution") return ["you", "Waiting for your choice", "The investigations disagree. Pick how to resolve it below."];
      if (decision && decision.kind === "approval") return ["you", "Waiting for your approval", "Look at it below. Nothing after this step runs until you approve."];
      return ["you", "Waiting for your approval", "Review the plan below. Nothing changes until you approve."];
    case "waiting_for_job": {
      const names = [...new Set((run.jobs || run.handoffs || []).map((job) => job.agent_name).filter(Boolean))];
      if (names.length) return ["work", `${names.join(" and ")} ${names.length > 1 ? "have" : "has"} the next step`, "It runs in that agent's own chat, on its own model, with your usual Locus permissions."];
      return ["work", "The agent has the next step", "It happens in chat, with your usual Locus permissions."];
    }
    case "running": return ["work", "Running", ""];
    case "paused": return ["work", "Paused", "Continue it from chat when you're ready."];
    case "uncertain": return ["stop", "Needs a check", BLOCKERS.uncertain_action];
    case "verified": return ["ok", "Verified", "Every check passed on the current files."];
    case "needs_review": return ["you", "Needs your review", BLOCKERS[run.blocker] || "Part of the result couldn't be verified automatically."];
    case "denied": return ["stop", "Plan declined", BLOCKERS.plan_denied];
    case "cancelled": return ["stop", "Cancelled", ""];
    default: return ["stop", "Stopped", BLOCKERS[run.blocker] || (run.blocker ? "Reason: " + run.blocker.replace(/_/g, " ") : "")];
  }
}

function ago(seconds) {
  if (!seconds) return "";
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 60) return "just now";
  if (delta < 3600) return Math.round(delta / 60) + " min ago";
  if (delta < 86400) return Math.round(delta / 3600) + " h ago";
  return Math.round(delta / 86400) + " d ago";
}

const JOB_KIND = {
  inspect: "Look around the project", plan: "Write a plan", write: "Make the change",
  review: "Review the change", investigate: "Investigate", synthesize: "Write the answer",
};

function activityText(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "workflow.started": return "Started";
    case "workflow.routing": return p.mode === "parallel" ? `Split into ${p.investigations} investigations` : "One investigation";
    case "job.submitted": return `Asked the agent: ${JOB_KIND[p.kind] || p.kind}`;
    case "job.outcome": return `${JOB_KIND[p.kind] || p.kind}: ${p.status === "settled" ? "done" : String(p.status).replace(/_/g, " ")}`;
    case "decision.pending": return "Waiting for your decision";
    case "decision.resolved": return `You chose: ${p.choice}`;
    case "verification.result": return `Checks ${p.outcome === "passed" ? "passed" : p.outcome === "failed" ? "failed" : "need review"}${p.final ? " (final)" : ""}`;
    case "workflow.paused": return "Paused";
    case "workflow.outcome": return "Finished: " + String(p.status).replace(/_/g, " ");
    default: return event.type;
  }
}

// ---- runs ------------------------------------------------------------------
async function refreshRuns() {
  if (!bridge.available || !can("plugin.tools")) return;
  try {
    const overview = await bridge.tool("workflow_overview", {});
    state.runs = overview.runs || [];
    autoHandOff();
    if (!state.settings && overview.settings) state.defaults = overview.settings;
    renderList();
    if (state.selected) await loadDetail(state.selected, true);
    else if (state.runs.length) await select(state.runs[0].attempt_id);
    else renderDetail();
  } catch (error) {
    renderList(error.message);
  }
}

function renderList(error) {
  const list = $("run-list");
  const needs = state.runs.filter((run) => run.needs_you || run.status === "waiting_for_input");
  const running = state.runs.filter((run) => !needs.includes(run) && !TERMINAL.includes(run.status));
  const finished = state.runs.filter((run) => TERMINAL.includes(run.status) && !needs.includes(run));
  const badge = $("needs-count");
  badge.hidden = needs.length === 0;
  badge.textContent = String(needs.length);
  badge.setAttribute("aria-label", needs.length + " need you");
  const groups = [["Needs you", needs], ["Running", running], ["Finished", finished]];
  const nodes = [];
  if (error) nodes.push(h("p", { class: "empty-list" }, error));
  else if (!state.runs.length) nodes.push(h("p", { class: "empty-list" }, "No workflows yet."));
  for (const [title, runs] of groups) {
    if (!runs.length) continue;
    nodes.push(h("h2", { class: "group-title" }, title));
    for (const run of runs) {
      const [tone] = headline(run);
      nodes.push(h("button", {
        type: "button", class: "run-item " + lineClass(run),
        "aria-current": run.attempt_id === state.selected ? "true" : false,
        onclick: () => select(run.attempt_id),
      },
        h("span", { class: "rail", "aria-hidden": "true" }),
        h("span", { class: "goal" }, firstLine(run.goal)),
        h("span", { class: "state" }, h("span", { class: "dot-state " + tone, title: headline(run)[1] })),
        h("span", { class: "meta" }, `${runName(run)} · ${run.project} · ${ago(run.updated_at)}`),
      ));
    }
  }
  list.replaceChildren(...nodes);
}

const firstLine = (text) => String(text || "").split("\n")[0].slice(0, 140) || "Untitled";

async function select(attemptID) {
  state.selected = attemptID;
  state.confirmCancel = false;
  renderList();
  await loadDetail(attemptID, false);
}

async function loadDetail(attemptID, quiet) {
  try {
    const detail = await bridge.tool("workflow_run", { attempt_id: attemptID });
    if (state.selected !== attemptID) return;
    state.detail = detail;
    renderDetail();
  } catch (error) {
    if (!quiet) toast(error.message, true);
  }
}

function renderDetail() {
  const root = $("run-detail");
  const run = state.detail;
  if (!run || run.attempt_id !== state.selected) {
    root.replaceChildren(h("div", { class: "detail-empty" },
      h("h2", {}, "Start a workflow"),
      h("p", {}, "Describe a change or a question. The agent does the work in chat, you approve the plan, and checks prove the result."),
      can("chat.compose") && h("button", { type: "button", class: "primary", onclick: () => showTab("new") }, "New workflow"),
    ));
    return;
  }
  const research = run.workflow === "research";
  const [tone, title, text] = headline(run);
  const children = [
    h("span", { class: "line-tag " + lineClass(run) }, run.workflow === "custom" ? runName(run) : runName(run) + " line"),
    h("h2", { class: "goal" }, firstLine(run.goal)),
    h("div", { class: "meta-line" },
      h("span", {}, run.project), h("span", {}, "Updated " + ago(run.updated_at)),
      h("span", { class: "mono", title: "Run ID" }, run.attempt_id)),
    route(run),
    h("div", { class: "status " + tone }, h("span", { class: "dot-state " + tone, "aria-hidden": "true" }),
      h("div", {}, h("strong", {}, title), text && h("p", {}, text))),
  ];
  if (run.decision) children.push(decisionCard(run));
  if (run.jobs && run.jobs.length) children.push(...jobsCard(run));
  const lower = [];
  if (run.checks && run.checks.length) lower.push(checksCard(run));
  else if (!research && run.workflow !== "custom") lower.push(h("section", { class: "card" }, h("h3", {}, "Done when"),
    h("p", {}, "No checks were declared for this run, so it will end in review.")));
  if (research && run.investigations && run.investigations.length) {
    lower.push(h("section", { class: "card" }, h("h3", {}, "Questions"),
      h("ol", {}, run.investigations.map((q) => h("li", {}, q)))));
  }
  if (run.outputs && run.outputs.length) {
    lower.push(h("section", { class: "card" }, h("h3", {}, "Results so far"),
      h("ol", { class: "results" }, run.outputs.map((out) => h("li", {},
        h("strong", {}, out.title), out.summary ? h("p", {}, out.summary.slice(0, 600)) : null)))));
  }
  if (run.plan && run.plan.length && !run.decision) {
    lower.push(h("section", { class: "card" }, h("h3", {}, "Plan"), h("ol", {}, run.plan.map((step) => h("li", {}, step)))));
  }
  lower.push(activityCard(run));
  children.push(h("div", { class: "split" }, lower));
  if (!TERMINAL.includes(run.status)) children.push(cancelRow(run));
  root.className = "detail " + lineClass(run);
  root.replaceChildren(...children);
}

function route(run) {
  const steps = run.steps || [];
  const youSteps = new Set(["approve", "compare"]);
  return h("ol", { class: "route " + lineClass(run), "aria-label": "Progress" },
    steps.map((step, index) => h("li", {
      class: [step.state, youSteps.has(step.id) || step.type === "approval" ? "you" : ""].join(" "),
      "aria-current": step.state === "current" ? "step" : false,
    },
      h("span", { class: "stop-mark", "aria-hidden": "true" }),
      h("span", { class: "stop-name" }, step.label),
      h("span", { class: "visually-hidden", style: "position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden" },
        `, step ${index + 1} of ${steps.length}, ${step.state}`),
    )));
}

function decisionCard(run) {
  const decision = run.decision;
  const canDecide = can("plugin.tools");
  if (decision.kind === "plan_approval") {
    const steps = run.plan && run.plan.length ? run.plan : String(decision.summary || "").split("; ");
    return h("section", { class: "card you", "aria-label": "Plan approval" },
      h("h3", {}, "Approve this plan?"),
      h("p", {}, "Approving lets the agent start changing files. Each change still follows your Locus permissions."),
      h("ol", {}, steps.map((step) => h("li", {}, step))),
      h("div", { class: "row" },
        h("button", { type: "button", class: "primary you-btn", disabled: !canDecide, onclick: () => decide(run, "approve", "Plan approved") }, "Approve plan"),
        h("button", { type: "button", class: "secondary", disabled: !canDecide, onclick: () => decide(run, "deny", "Plan declined") }, "Decline")));
  }
  if (decision.kind === "approval") {
    return h("section", { class: "card you", "aria-label": "Approval" },
      h("h3", {}, "Your approval is needed"),
      h("p", { class: "pre" }, decision.summary || ""),
      h("div", { class: "row" },
        h("button", { type: "button", class: "primary you-btn", disabled: !canDecide, onclick: () => decide(run, "approve", "Approved") }, "Approve"),
        h("button", { type: "button", class: "secondary", disabled: !canDecide, onclick: () => decide(run, "decline", "Declined") }, "Decline")));
  }
  const questions = run.investigations || [];
  return h("section", { class: "card you", "aria-label": "Resolve conflict" },
    h("h3", {}, "The investigations disagree"),
    h("p", {}, decision.summary || ""),
    h("div", { class: "row" }, (decision.options || []).map((option) => {
      let label = option;
      if (option === "report") label = "Report both answers";
      else if (option.startsWith("prefer:")) {
        const index = Number(option.slice(7));
        label = "Trust investigation " + (index + 1) + (questions[index] ? ": " + questions[index].slice(0, 60) : "");
      }
      return h("button", { type: "button", class: option === "report" ? "secondary" : "primary you-btn", disabled: !canDecide,
        onclick: () => decide(run, option, "Choice saved") }, label);
    })));
}

async function decide(run, choice, done) {
  const decision = run.decision;
  try {
    const detail = await bridge.tool("workflow_decide", {
      attempt_id: run.attempt_id, decision_id: decision.decision_id,
      revision: decision.revision, digest: decision.digest, choice,
    });
    state.detail = detail;
    renderDetail();
    toast(done);
    refreshRuns();
  } catch (error) {
    toast(error.message === "already_consumed" ? "This decision was already answered." : error.message, true);
    loadDetail(run.attempt_id, true);
  }
}

function jobsCard(run) {
  const assigned = run.jobs.filter((job) => job.agent);
  const open = run.jobs.filter((job) => !job.agent);
  const cards = [];
  if (assigned.length) cards.push(handOffCard(run, assigned));
  if (open.length) cards.push(openJobsCard(run, open));
  return cards;
}

function handOffCard(run, jobs) {
  const confirmed = state.confirmed.has(run.attempt_id);
  const allow = can("agents.dispatch");
  return h("section", { class: "card" },
    h("h3", {}, "Handed to agents"),
    h("p", {}, allow
      ? confirmed ? "Each step goes to its agent's own chat automatically while this window is open."
        : "Allow hand-offs for this run and each step goes to its agent's chat automatically."
      : "This Locus can't hand steps to agents from plugins. Ask each agent in its own chat."),
    jobs.map((job) => {
      const sent = state.dispatched.get(job.operation_id);
      return h("div", { class: "job" },
        h("span", { class: "agent-tag line-" + agentLine(job.agent) }, job.agent_name || "Agent"),
        h("span", { class: "kind" }, " " + (job.title || JOB_KIND[job.kind] || job.kind)),
        job.access === "read" ? h("span", { class: "subtle" }, " · read-only") : null,
        h("p", {}, job.instruction),
        h("div", { class: "row" },
          h("span", { class: "subtle" }, sent ? (sent.earlier ? "Handed over earlier" : "Handed over " + ago(sent.at / 1000)) : state.handing.has(job.operation_id) ? "Handing over…" : "Not handed over yet"),
          sent && can("agents.dispatch") ? h("button", { type: "button", class: "quiet", onclick: () => bridge.openChat(run.attempt_id, job.agent) }, "Open chat") : null,
          sent && confirmed ? h("button", { type: "button", class: "quiet", onclick: () => handOff(run.attempt_id, job, true) }, "Hand off again") : null));
    }),
    allow && !confirmed ? h("div", { class: "row" },
      h("button", { type: "button", class: "primary", onclick: () => allowHandOffs(run.attempt_id, jobs) }, "Allow hand-offs for this run")) : null);
}

function openJobsCard(run, jobs) {
  return h("section", { class: "card" },
    h("h3", {}, "Next for the agent"),
    h("p", {}, "The agent does these steps in chat. If that chat has moved on, continue from a new one."),
    jobs.map((job) => h("div", { class: "job" },
      h("span", { class: "kind" }, job.title || JOB_KIND[job.kind] || job.kind),
      job.access === "read" ? h("span", { class: "subtle" }, " · read-only") : null,
      h("p", {}, job.instruction))),
    can("chat.compose") && h("div", { class: "row" },
      h("button", { type: "button", class: "secondary", onclick: () => {
        bridge.compose(`Continue the LangGraph workflow ${run.attempt_id}: call workflow_status with attempt_id "${run.attempt_id}", then do each pending job and report it with workflow_report.`);
        toast("Drafted in a new chat");
      } }, "Continue in chat")));
}

const CHECK_STATE = {
  passed: ["ok", "✓", "Passed"], failed: ["stop", "✕", "Failed"], needs_review: ["you", "!", "Needs you"],
  denied: ["stop", "!", "Not allowed"], unsupported: ["you", "?", "Run it yourself"], stale: ["you", "!", "Out of date"],
  not_run: ["", "·", "Not checked yet"],
};

function describeCheck(check) {
  return check.kind === "command" ? "Command" : check.kind === "human_review" ? "Your review" : check.kind.replace(/_/g, " ");
}

function checksCard(run) {
  return h("section", { class: "card" },
    h("h3", {}, "Done when"),
    run.checks.map((check) => {
      const [tone, glyph, label] = CHECK_STATE[check.state] || CHECK_STATE.not_run;
      return h("div", { class: "check-row" },
        h("span", { class: "glyph " + tone, "aria-hidden": "true" }, glyph),
        h("span", { class: "what" }, check.requirement || check.id),
        h("span", { class: "label " + tone }, label),
        h("span", { class: "how" }, describeCheck(check), check.detail && check.state !== "passed" ? " — " + check.detail.slice(0, 160) : ""));
    }));
}

function activityCard(run) {
  const events = (run.activity || []).slice().reverse();
  return h("section", { class: "card" },
    h("h3", {}, "Activity"),
    events.length
      ? h("ul", { class: "activity" }, events.map((event) => h("li", {
          class: event.type.startsWith("decision") || event.type === "workflow.outcome" ? "strong" : "" }, activityText(event))))
      : h("p", {}, "Nothing yet."));
}

function cancelRow(run) {
  if (!can("plugin.tools")) return null;
  if (!state.confirmCancel) {
    return h("div", { class: "row" }, h("button", { type: "button", class: "danger",
      onclick: () => { state.confirmCancel = true; renderDetail(); } }, "Cancel run"));
  }
  return h("div", { class: "row" },
    h("span", {}, "Cancel this run? The agent is told to stop its steps."),
    h("button", { type: "button", class: "danger", onclick: async () => {
      try {
        await bridge.tool("workflow_cancel", { attempt_id: run.attempt_id });
        toast("Run cancelled");
      } catch (error) { toast(error.message, true); }
      state.confirmCancel = false;
      refreshRuns();
    } }, "Yes, cancel"),
    h("button", { type: "button", class: "secondary", onclick: () => { state.confirmCancel = false; renderDetail(); } }, "Keep running"));
}

// ---- hand-offs -----------------------------------------------------------------
function agentLine(agentID) {
  const index = state.agents.findIndex((agent) => agent.id === agentID);
  return index < 0 ? "x" : String(index % 6);
}

async function allowHandOffs(runID, jobs, title) {
  if (!can("agents.dispatch")) return false;
  let steps = jobs;
  let name = title;
  try {
    const detail = await bridge.tool("workflow_run", { attempt_id: runID });
    name = name || detail.title || detail.goal;
    const graph = detail.graph;
    if (graph) {
      steps = graph.nodes.filter((node) => node.type === "task" && (node.agent || graph.agent))
        .map((node) => ({ title: node.title, agentID: node.agent || graph.agent, access: node.access }));
    }
  } catch (_) { /* fall back to the pending jobs */ }
  steps = steps.map((step) => ({ title: step.title, agentID: step.agentID || step.agent, access: step.access }));
  try {
    const answer = await bridge.confirmRun({ runID, title: String(name || "Workflow run").slice(0, 200), steps });
    if (!answer || !answer.confirmed) { toast("Hand-offs not allowed. Steps wait until you allow them."); return false; }
    state.confirmed.add(runID);
    toast("Hand-offs allowed for this run");
    autoHandOff();
    if (state.selected === runID) loadDetail(runID, true);
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function autoHandOff() {
  if (!can("agents.dispatch")) return;
  for (const run of state.runs) {
    if (!state.confirmed.has(run.attempt_id)) continue;
    for (const job of run.handoffs || []) {
      if (!state.dispatched.has(job.operation_id) && !state.handing.has(job.operation_id)) handOff(run.attempt_id, job, false);
    }
  }
}

async function handOff(runID, job, again) {
  state.handing.add(job.operation_id);
  try {
    const message = await bridge.tool("workflow_dispatch", { attempt_id: runID, operation_id: job.operation_id });
    if (message.handed_before && !again && !state.dispatched.has(job.operation_id)) {
      // Sent before this window (or Locus) restarted: never send twice on its own.
      state.dispatched.set(job.operation_id, { at: Date.now(), earlier: true });
      return;
    }
    await bridge.dispatch({ runID, agentID: message.agent, operationID: job.operation_id,
      title: message.title, text: message.text, access: message.access });
    state.dispatched.set(job.operation_id, { at: Date.now(), earlier: false });
    toast(`Handed to ${message.agent_name || "the agent"}: ${message.title}`);
  } catch (error) {
    if (/not_confirmed/.test(error.message)) state.confirmed.delete(runID);
    else if (!/busy/.test(error.message)) toast(error.message, true);
  } finally {
    state.handing.delete(job.operation_id);
    if (state.selected === runID) loadDetail(runID, true);
  }
}

// ---- new workflow ------------------------------------------------------------
const CHECK_KINDS = [
  ["file_exists", "A file exists"],
  ["file_contains", "A file contains text"],
  ["json_value", "A JSON value equals"],
  ["human_review", "I'll check it myself"],
];

function checkRow(initial) {
  const kind = h("select", { "aria-label": "Kind of check" },
    CHECK_KINDS.map(([value, label]) => h("option", { value }, label)));
  const a = h("input", { type: "text", spellcheck: "false", autocomplete: "off" });
  const b = h("input", { type: "text", spellcheck: "false", autocomplete: "off" });
  const row = h("div", { class: "check-builder" }, kind, a, b,
    h("button", { type: "button", class: "remove", "aria-label": "Remove check", onclick: () => { row.remove(); updatePreview(); } }, "×"));
  function shape() {
    const k = kind.value;
    a.placeholder = k === "human_review" ? "What will you check?" : "path/in/project";
    a.style.fontFamily = k === "human_review" ? "" : "var(--mono)";
    a.setAttribute("aria-label", k === "human_review" ? "What you will check" : "File path");
    b.hidden = k === "file_exists" || k === "human_review";
    b.placeholder = k === "json_value" ? "/pointer = value" : "text it must contain";
    b.setAttribute("aria-label", k === "json_value" ? "JSON pointer and value" : "Text");
  }
  kind.value = (initial && initial.kind) || "file_contains";
  kind.addEventListener("change", () => { shape(); updatePreview(); });
  [a, b].forEach((input) => input.addEventListener("input", updatePreview));
  shape();
  row.read = () => {
    const k = kind.value, first = a.value.trim(), second = b.value.trim();
    if (k === "human_review") return first ? { kind: k, requirement: first } : null;
    if (!first) return null;
    if (k === "file_exists") return { kind: k, path: first, requirement: `${first} exists` };
    if (k === "file_contains") return second ? { kind: k, path: first, value: second, requirement: `${first} contains “${second}”` } : null;
    const [pointer, ...rest] = second.split("=");
    const raw = rest.join("=").trim();
    if (!pointer.trim().startsWith("/") || !raw) return null;
    let value = raw;
    try { value = JSON.parse(raw); } catch (_) { /* plain text value */ }
    return { kind: k, path: first, pointer: pointer.trim(), value, requirement: `${first} ${pointer.trim()} is ${raw}` };
  };
  return row;
}

function questionRow() {
  const input = h("input", { type: "text", placeholder: "One independent question", "aria-label": "Question" });
  const row = h("div", { class: "question-row" }, input,
    h("button", { type: "button", class: "remove", "aria-label": "Remove question", onclick: () => { row.remove(); updatePreview(); } }, "×"));
  input.addEventListener("input", updatePreview);
  row.read = () => input.value.trim();
  return row;
}

function composeRequest() {
  const workflow = document.querySelector('input[name="workflow"]:checked').value;
  const goal = $("goal").value.trim();
  const args = { workflow, goal };
  const checks = [];
  if (workflow === "custom") args.definition_id = $("definition").value;
  if (workflow !== "research") {
    [...$("checks").children].forEach((row, index) => {
      const check = row.read && row.read();
      if (check) checks.push(Object.assign({ id: "check-" + (index + 1) }, check));
    });
  }
  if (workflow === "verified_change") {
    const plan = $("plan").value.split("\n").map((line) => line.trim()).filter(Boolean);
    if (plan.length) args.plan_steps = plan;
    args.reviewer = $("reviewer").checked;
  } else if (workflow === "research") {
    const questions = [...$("questions").children].map((row) => row.read()).filter(Boolean);
    if (questions.length) args.investigations = questions;
    const deliverable = $("deliverable").value.trim();
    if (deliverable) checks.push({ id: "deliverable", kind: "file_exists", path: deliverable, requirement: `The answer is saved in ${deliverable}` });
  }
  if (checks.length) args.checks = checks;
  const drawn = workflow === "custom" && $("definition").selectedOptions[0];
  const intro = workflow === "verified_change"
    ? "Use the LangGraph Workflows plugin to run a verified change."
    : workflow === "research" ? "Use the LangGraph Workflows plugin to research this question."
      : `Use the LangGraph Workflows plugin to run my workflow “${drawn ? drawn.textContent : ""}”.`;
  return {
    goal, args, text: `${intro}\n\nGoal: ${goal || "…"}\n\nCall workflow_start with these arguments, then do each job it returns and report it with workflow_report:\n${JSON.stringify(args, null, 2)}`,
  };
}

function updatePreview() {
  $("preview").textContent = composeRequest().text;
}

function syncComposeMode() {
  const mode = document.querySelector('input[name="workflow"]:checked').value;
  const only = { verified_change: "only-change", research: "only-research", custom: "only-custom" }[mode];
  document.querySelectorAll(".only-change, .only-research, .only-custom").forEach((node) => {
    node.hidden = !node.classList.contains(only);
  });
  const custom = mode === "custom";
  $("draft").className = custom ? "secondary" : "primary";
  $("start-here").hidden = !custom || !can("plugin.tools");
  $("actions-hint").textContent = custom
    ? "Start run begins it here; steps with an agent go to that agent's chat once you allow it. Draft in chat lets an agent start it instead."
    : "Opens a new Locus chat with this request drafted. Review it, then press Send.";
  $("goal").placeholder = mode === "research"
    ? "Which storage engine fits our offline cache best?"
    : "Add a dark mode toggle to the settings page";
  if (custom) describeDefinition();
  const limit = (state.settings && state.settings.values.max_investigations) || (state.defaults && state.defaults.max_investigations) || 4;
  $("add-question").disabled = $("questions").children.length >= limit;
  updatePreview();
}

function initCompose() {
  document.querySelectorAll('input[name="workflow"]').forEach((radio) => radio.addEventListener("change", syncComposeMode));
  $("goal").addEventListener("input", () => { $("goal-error").hidden = true; updatePreview(); });
  ["plan", "deliverable"].forEach((id) => $(id).addEventListener("input", updatePreview));
  $("reviewer").addEventListener("change", updatePreview);
  $("add-check").addEventListener("click", () => { $("checks").append(checkRow()); updatePreview(); });
  $("add-question").addEventListener("click", () => { $("questions").append(questionRow()); syncComposeMode(); });
  $("checks").append(checkRow({ kind: "file_contains" }));
  $("definition").addEventListener("change", () => { describeDefinition(); updatePreview(); });
  $("start-here").addEventListener("click", startHere);
  $("compose").addEventListener("submit", (event) => {
    event.preventDefault();
    const { goal, text } = composeRequest();
    if (!goal) { $("goal-error").hidden = false; $("goal").focus(); return; }
    if (!can("chat.compose")) { toast("This Locus can't draft chat messages from plugins.", true); return; }
    bridge.compose(text);
    toast("Drafted in a new chat. Review it and press Send.");
  });
  syncComposeMode();
}

function syncCustomChoices() {
  const list = typeof editor === "object" ? editor.list : [];
  const select = $("definition");
  const keep = select.value;
  select.replaceChildren(...list.map((item) => h("option", { value: item.id, selected: item.id === keep }, item.title)));
  $("custom-card").hidden = !list.length;
  if (!list.length && document.querySelector('input[name="workflow"]:checked').value === "custom") {
    document.querySelector('input[value="verified_change"]').checked = true;
  }
  syncComposeMode();
}

function describeDefinition() {
  const item = (typeof editor === "object" ? editor.list : []).find((d) => d.id === $("definition").value);
  $("definition-agents").textContent = item
    ? `${item.steps} steps${item.agents.length ? " · agents: " + item.agents.join(", ") : " · done by whichever agent runs it"}${item.description ? " — " + item.description : ""}`
    : "";
}

function startWith(definitionID) {
  document.querySelector('input[value="custom"]').checked = true;
  syncCustomChoices();
  $("definition").value = definitionID;
  showTab("new");
  syncComposeMode();
  $("goal").focus();
}

async function startHere() {
  const { goal, args } = composeRequest();
  if (!goal) { $("goal-error").hidden = false; $("goal").focus(); return; }
  const button = $("start-here");
  button.disabled = true;
  try {
    const run = await bridge.tool("workflow_launch", args);
    toast("Run started");
    state.selected = run.attempt_id;
    state.detail = run;
    showTab("runs");
    const assigned = (run.graph ? run.graph.nodes : []).some((node) => node.type === "task" && (node.agent || run.graph.agent));
    if (assigned && can("agents.dispatch")) await allowHandOffs(run.attempt_id, run.jobs || [], run.title);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

// ---- settings ----------------------------------------------------------------
const GROUPS = [
  ["Approvals", ["plan_approval", "conflict_approval"]],
  ["Defaults", ["reviewer_default"]],
  ["Limits", ["max_repair_rounds", "max_review_rounds", "max_jobs", "max_investigations", "max_parallel_reads"]],
  ["Housekeeping", ["keep_finished_days"]],
];

async function loadSettings() {
  if (!can("plugin.settings")) {
    $("settings-fields").replaceChildren(h("p", { class: "subtle" }, "This Locus version can't edit plugin settings."));
    return;
  }
  try {
    state.settings = await bridge.settings();
    state.settingsDraft = Object.assign({}, state.settings.values);
    renderSettings();
    $("reviewer").checked = Boolean(state.settings.values.reviewer_default);
    syncComposeMode();
  } catch (error) {
    $("settings-fields").replaceChildren(h("p", { class: "error" }, error.message));
  }
}

function renderSettings() {
  const { schema, error } = state.settings;
  const properties = schema.properties || {};
  const used = new Set();
  const nodes = [];
  if (error) nodes.push(h("p", { class: "error" }, error));
  const groups = GROUPS.map(([title, keys]) => [title, keys.filter((key) => key in properties)]);
  groups.push(["Other", Object.keys(properties).filter((key) => !GROUPS.some(([, keys]) => keys.includes(key)))]);
  for (const [title, keys] of groups) {
    if (!keys.length) continue;
    nodes.push(h("div", { class: "settings-group" }, h("p", { class: "section-title" }, title),
      keys.map((key) => { used.add(key); return settingRow(key, properties[key]); })));
  }
  $("settings-fields").replaceChildren(...nodes);
  syncSettingsDirty();
}

function settingRow(key, field) {
  const id = "setting-" + key;
  const value = state.settingsDraft[key];
  let control;
  if (field.type === "boolean") {
    control = h("input", { type: "checkbox", id, role: "switch", checked: Boolean(value) });
    control.addEventListener("change", () => { state.settingsDraft[key] = control.checked; syncSettingsDirty(); });
  } else if (field.type === "integer" || field.type === "number") {
    const min = field.minimum ?? -Infinity, max = field.maximum ?? Infinity;
    const out = h("output", { id, "aria-live": "polite" }, String(value));
    const minus = h("button", { type: "button", "aria-label": "Decrease " + (field.title || key) }, "−");
    const plus = h("button", { type: "button", "aria-label": "Increase " + (field.title || key) }, "+");
    const set = (next) => {
      state.settingsDraft[key] = Math.min(max, Math.max(min, next));
      out.textContent = String(state.settingsDraft[key]);
      minus.disabled = state.settingsDraft[key] <= min;
      plus.disabled = state.settingsDraft[key] >= max;
      syncSettingsDirty();
    };
    minus.addEventListener("click", () => set(state.settingsDraft[key] - 1));
    plus.addEventListener("click", () => set(state.settingsDraft[key] + 1));
    control = h("span", { class: "stepper" }, minus, out, plus);
    minus.disabled = value <= min;
    plus.disabled = value >= max;
  } else if (field.enum) {
    control = h("select", { id }, field.enum.map((option) => h("option", { value: option, selected: option === value }, option)));
    control.addEventListener("change", () => { state.settingsDraft[key] = control.value; syncSettingsDirty(); });
  } else {
    control = h("input", { type: "text", id, value: value ?? "", maxlength: field.maxLength || 400 });
    control.addEventListener("input", () => { state.settingsDraft[key] = control.value; syncSettingsDirty(); });
  }
  const range = (field.type === "integer" && field.minimum != null) ? ` (${field.minimum}–${field.maximum})` : "";
  return h("div", { class: "setting" },
    h("label", { class: "name", for: id }, field.title || key),
    h("span", { class: "desc" }, (field.description || "") + range),
    h("span", { class: "control" }, control));
}

function syncSettingsDirty() {
  const dirty = state.settings && JSON.stringify(state.settingsDraft) !== JSON.stringify(state.settings.values);
  $("save-settings").disabled = !dirty;
}

function initSettings() {
  $("settings").addEventListener("submit", async (event) => {
    event.preventDefault();
    const properties = state.settings.schema.properties;
    // Save only what differs from the defaults, so new defaults can apply later.
    const values = {};
    for (const [key, value] of Object.entries(state.settingsDraft)) {
      if (!("default" in properties[key]) || properties[key].default !== value) values[key] = value;
    }
    try {
      state.settings = await bridge.saveSettings(values, state.settings.revision);
      state.settingsDraft = Object.assign({}, state.settings.values);
      renderSettings();
      $("reviewer").checked = Boolean(state.settings.values.reviewer_default);
      syncComposeMode();
      toast("Settings saved");
    } catch (error) {
      toast(/changed elsewhere/.test(error.message) ? "Settings changed in another window. Reloaded." : error.message, true);
      loadSettings();
    }
  });
  $("reset-settings").addEventListener("click", () => {
    if (!state.settings) return;
    const properties = state.settings.schema.properties;
    for (const key of Object.keys(properties)) {
      if ("default" in properties[key]) state.settingsDraft[key] = properties[key].default;
    }
    renderSettings();
  });
}

// ---- tabs and lifecycle --------------------------------------------------------
const TABS = ["runs", "new", "workflows", "settings"];

function showTab(name) {
  state.tab = name;
  for (const tab of TABS) {
    const button = $("tab-" + tab);
    const selected = tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $("view-" + tab).hidden = !selected;
  }
  if (name === "runs") refreshRuns();
  if (name === "workflows" && typeof loadDefinitions === "function") {
    if (!editor.loaded) loadDefinitions();
    else requestAnimationFrame(() => drawEdges());  // measured only once visible
  }
}

function initTabs() {
  TABS.forEach((tab, index) => {
    const button = $("tab-" + tab);
    button.addEventListener("click", () => showTab(tab));
    button.addEventListener("keydown", (event) => {
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      const next = TABS[(index + step + TABS.length) % TABS.length];
      showTab(next);
      $("tab-" + next).focus();
    });
  });
}

let started = false;
function onHello() {
  $("project").textContent = state.project ? "Project: " + state.project : "";
  $("tab-new").hidden = !can("chat.compose");
  $("tab-settings").hidden = !can("plugin.settings");
  $("tab-workflows").hidden = !can("plugin.tools");
  if (started) return;
  started = true;
  loadSettings();
  refreshRuns();
  loadAgents();
  if (typeof loadDefinitions === "function") loadDefinitions();
  setInterval(() => {
    if (document.visibilityState === "visible" && state.tab === "runs") refreshRuns();
  }, POLL_MS);
}

async function loadAgents() {
  if (!can("agents.read")) return;
  try {
    state.agents = (await bridge.agents()).agents || [];
    if (typeof renderEditor === "function" && editor.current) renderEditor();
  } catch (error) {
    toast("Couldn't load your agents: " + error.message, true);
  }
}

initTabs();
initCompose();
initSettings();
renderDetail();
if (bridge.available) bridge.ready();
else $("offline").hidden = false;
