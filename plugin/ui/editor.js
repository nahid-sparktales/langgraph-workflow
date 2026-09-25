"use strict";
// Workflow editor: draw a graph of steps, give each step a Locus agent, save.
// Uses h(), $(), bridge, state, can() and toast() from app.js. Everything the
// user types is inserted with textContent or form values, never as HTML.

const GRID = 20;
const LINE_COLORS = 6;
const TYPES = {
  start: { label: "Start", ports: ["next"], blurb: "Where every run begins." },
  task: { label: "Agent step", ports: ["done", "failed"], blurb: "An agent does something: reads, or edits files." },
  approval: { label: "Approval", ports: ["approved", "declined"], blurb: "The run waits until you approve." },
  check: { label: "Checks", ports: ["passed", "failed", "needs_review"], blurb: "Verifies the run's “Done when” checks on your files." },
  split: { label: "Split", ports: ["next"], blurb: "Runs read-only steps side by side." },
  join: { label: "Join", ports: ["next"], blurb: "Waits for every branch of its split." },
  end: { label: "End", ports: [], blurb: "The run finishes here." },
};
const PORT_LABEL = {
  next: "next", done: "done", failed: "if it fails", approved: "approved", declined: "if declined",
  passed: "passed", needs_review: "needs review",
};
const OPTIONAL_PORT_RESULT = {
  "task:failed": "Ends the run as failed", "approval:declined": "Ends the run as declined",
  "check:failed": "Ends the run for your review", "check:needs_review": "Ends the run for your review",
};

const editor = {
  list: [], drafts: {}, current: null, selected: null, problems: [], loaded: false, saving: false,
};

// ---- templates ---------------------------------------------------------------
function templates() {
  const node = (id, type, x, y, extra) => Object.assign({ id, type, title: TYPES[type].label, x, y }, extra);
  const edge = (from, on, to) => ({ from, on, to });
  return [
    { key: "blank", title: "Blank", text: "Start and End only.", make: () => ({
      title: "New workflow", nodes: [node("start", "start", 40, 140), node("end", "end", 420, 140)],
      edges: [edge("start", "next", "end")] }) },
    { key: "change", title: "Plan, approve, build, check", text: "A plan you approve, then edits until your checks pass.", make: () => ({
      title: "Plan, approve, build, check",
      nodes: [node("start", "start", 40, 160),
        node("plan", "task", 180, 140, { title: "Plan the change", access: "read", instruction: "Read the project and write a short numbered plan for the goal. Don't change files." }),
        node("approve", "approval", 440, 140, { title: "Approve the plan", prompt: "" }),
        node("build", "task", 680, 140, { title: "Make the change", access: "write", instruction: "Carry out the approved plan. Keep changes small and focused.", max_visits: 3 }),
        node("check", "check", 940, 140, { title: "Run the checks", max_visits: 3 }),
        node("end", "end", 1200, 160)],
      edges: [edge("start", "next", "plan"), edge("plan", "done", "approve"), edge("approve", "approved", "build"),
        edge("build", "done", "check"), edge("check", "passed", "end"), edge("check", "failed", "build")] }) },
    { key: "research", title: "Research in parallel", text: "Three read-only investigations side by side, then one answer.", make: () => ({
      title: "Research in parallel",
      nodes: [node("start", "start", 40, 220), node("split", "split", 180, 220),
        node("a", "task", 400, 60, { title: "Investigate the first angle", access: "read", instruction: "Investigate the first angle of the goal. Cite where each finding comes from." }),
        node("b", "task", 400, 220, { title: "Investigate the second angle", access: "read", instruction: "Investigate the second angle of the goal. Cite sources." }),
        node("c", "task", 400, 380, { title: "Investigate the third angle", access: "read", instruction: "Investigate the third angle of the goal. Cite sources." }),
        node("join", "join", 660, 220),
        node("answer", "task", 860, 200, { title: "Write the answer", access: "read", instruction: "Combine the findings into one answer. Say where they disagree." }),
        node("end", "end", 1120, 220)],
      edges: [edge("start", "next", "split"), edge("split", "next", "a"), edge("split", "next", "b"), edge("split", "next", "c"),
        edge("a", "done", "join"), edge("b", "done", "join"), edge("c", "done", "join"), edge("join", "next", "answer"),
        edge("answer", "done", "end")] }) },
    { key: "review", title: "Build with a reviewer", text: "One agent builds, another reviews and can send it back.", make: () => ({
      title: "Build with a reviewer",
      nodes: [node("start", "start", 40, 160),
        node("build", "task", 180, 140, { title: "Make the change", access: "write", instruction: "Make the change the goal asks for. If the review sent it back, address its findings.", max_visits: 3 }),
        node("check", "check", 440, 140, { title: "Run the checks", max_visits: 3 }),
        node("review", "task", 700, 140, { title: "Review the change", access: "read", instruction: "Review the change against the goal. Choose approve, or changes with what to fix.", choices: ["approve", "changes"], max_visits: 3 }),
        node("end", "end", 980, 160)],
      edges: [edge("start", "next", "build"), edge("build", "done", "check"), edge("check", "passed", "review"),
        edge("check", "failed", "build"), edge("review", "approve", "end"), edge("review", "changes", "build")] }) },
  ];
}

// ---- helpers -------------------------------------------------------------------
const slug = (text) => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "workflow";
const portsOf = (node) => node.type === "task" && node.choices && node.choices.length ? [...node.choices, "failed"] : TYPES[node.type].ports;
const byId = (id) => editor.current.nodes.find((node) => node.id === id);
const agentsList = () => state.agents || [];
const agentOf = (id) => agentsList().find((agent) => agent.id === id);

function lineIndex(agentID) {
  const ids = agentsList().map((agent) => agent.id);
  const index = ids.indexOf(agentID);
  return index < 0 ? -1 : index % LINE_COLORS;
}

function effectiveAgent(node) {
  const id = node.agent || editor.current.agent || "";
  const name = node.agent ? node.agent_name : editor.current.agent_name;
  return { id, name: (agentOf(id) || {}).name || name || "" };
}

function agentLabel(agent) {
  const bits = [agent.provider, agent.model].filter(Boolean).join(" · ");
  return bits ? `${agent.name} (${bits})` : agent.name;
}

function freshID(base) {
  let id = slug(base).slice(0, 36) || "step", n = 2;
  while (editor.current.nodes.some((node) => node.id === id)) id = `${slug(base).slice(0, 34)}-${n++}`;
  return id;
}

function markDirty() {
  editor.current.dirty = true;
  editor.drafts[editor.current.id] = editor.current;
  renderToolbar();
}

// ---- data ----------------------------------------------------------------------
async function loadDefinitions(selectID) {
  if (!bridge.available || !can("plugin.tools")) return;
  try {
    editor.list = (await bridge.tool("workflow_definitions", {})).definitions || [];
    editor.loaded = true;
    const next = selectID || (editor.current && editor.current.id) || (editor.list[0] && editor.list[0].id);
    if (next) await openDefinition(next);
    else renderEditor();
    if (typeof syncCustomChoices === "function") syncCustomChoices();
  } catch (error) {
    editor.problems = [error.message];
    renderEditor();
  }
}

async function openDefinition(id) {
  editor.selected = null;
  editor.problems = [];
  editor.picking = false;
  if (editor.drafts[id]) {
    editor.current = editor.drafts[id];
  } else {
    try {
      const saved = await bridge.tool("workflow_definition", { definition_id: id });
      editor.current = Object.assign(saved, { dirty: false, isNew: false });
    } catch (error) {
      toast(error.message, true);
      return;
    }
  }
  renderEditor();
}

function newFromTemplate(template) {
  const made = template.make();
  for (const node of made.nodes) if (node.type !== "start") node.x += 40;  // room for Start's outcome
  let id = slug(made.title), n = 2;
  const taken = (candidate) => editor.list.some((d) => d.id === candidate) || editor.drafts[candidate];
  while (taken(id)) id = `${slug(made.title).slice(0, 30)}-${n++}`;
  editor.current = Object.assign({ id, description: "", agent: "", agent_name: "", dirty: true, isNew: true }, made);
  editor.drafts[id] = editor.current;
  editor.selected = null;
  editor.problems = [];
  editor.picking = false;
  renderEditor();
}

function serializable(d = editor.current) {
  return {
    id: d.id, title: d.title, description: d.description || "", agent: d.agent || "", agent_name: d.agent_name || "",
    nodes: d.nodes.map((node) => {
      const out = { id: node.id, type: node.type, title: node.title, x: Math.round(node.x), y: Math.round(node.y) };
      if (node.type === "task") Object.assign(out, { instruction: node.instruction || "", access: node.access || "read",
        agent: node.agent || "", agent_name: node.agent_name || "", choices: node.choices || [] });
      if (node.type === "approval") out.prompt = node.prompt || "";
      if (["task", "approval", "check"].includes(node.type)) out.max_visits = node.max_visits || 1;
      return out;
    }),
    edges: d.edges.map((edge) => ({ from: edge.from, on: edge.on, to: edge.to })),
  };
}

async function saveCurrent() {
  if (!editor.current || editor.saving) return;
  editor.saving = true;
  renderToolbar();
  const draft = editor.current;
  const sent = JSON.stringify(serializable(draft));
  try {
    const result = await bridge.tool("workflow_save_definition", { definition: JSON.parse(sent) });
    if (result.problems) {
      editor.problems = result.problems;
      toast("Not saved yet: fix the problem shown above the map.", true);
    } else {
      editor.problems = [];
      draft.isNew = false;
      if (JSON.stringify(serializable(draft)) === sent) {  // not edited while saving
        delete editor.drafts[draft.id];
        if (editor.current === draft) editor.current = Object.assign(result.saved, { dirty: false, isNew: false });
      }
      toast("Workflow saved");
      await loadDefinitions(editor.current ? editor.current.id : draft.id);
    }
  } catch (error) {
    editor.problems = [error.message];
  }
  editor.saving = false;
  renderEditor();
}

async function deleteCurrent() {
  const d = editor.current;
  if (!d) return;
  if (!d.isNew) {
    try { await bridge.tool("workflow_delete_definition", { definition_id: d.id }); } catch (error) { toast(error.message, true); return; }
  }
  delete editor.drafts[d.id];
  editor.current = null;
  editor.confirmDelete = false;
  toast("Workflow deleted");
  await loadDefinitions();
  if (!editor.current) renderEditor();
}

// ---- rendering: toolbar -----------------------------------------------------------
function renderEditor() {
  renderToolbar();
  renderCanvas();
  renderInspector();
}

function renderToolbar() {
  const bar = $("wf-toolbar");
  if (!bar) return;
  const typing = document.activeElement && document.activeElement.classList.contains("wf-title")
    ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const d = editor.current;
  const picker = h("select", { id: "wf-picker", "aria-label": "Workflow", onchange: (event) => {
    if (event.target.value === "__new") { editor.picking = true; renderEditor(); } else openDefinition(event.target.value);
  } },
  editor.list.map((item) => h("option", { value: item.id, selected: d && item.id === d.id }, item.title)),
  Object.values(editor.drafts).filter((draft) => draft.isNew).map((draft) =>
    h("option", { value: draft.id, selected: d && draft.id === d.id }, draft.title + " (unsaved)")),
  h("option", { value: "__new", selected: !d || editor.picking }, "New workflow…"));
  const nodes = [picker];
  if (d && !editor.picking) {
    const title = h("input", { type: "text", value: d.title, maxlength: 120, "aria-label": "Workflow name", class: "wf-title" });
    title.addEventListener("input", () => { d.title = title.value; markDirty(); });
    nodes.push(title, agentSelect(d.agent, "Default agent for steps", (id, name) => {
      d.agent = id; d.agent_name = name; markDirty(); renderCanvas(); renderInspector();
    }, "Any agent (whoever runs it)"));
    nodes.push(h("span", { class: "wf-spacer" }));
    if (d.dirty) nodes.push(h("span", { class: "unsaved" }, "Unsaved"));
    nodes.push(h("button", { type: "button", class: "primary", disabled: editor.saving || !can("plugin.tools"), onclick: saveCurrent },
      editor.saving ? "Saving…" : "Save"));
    nodes.push(h("button", { type: "button", class: "secondary", disabled: d.dirty || d.isNew, title: d.dirty ? "Save first" : "",
      onclick: () => { if (typeof startWith === "function") startWith(d.id); } }, "Start a run…"));
    if (editor.confirmDelete) {
      nodes.push(h("span", { class: "confirm" }, "Delete this workflow?",
        h("button", { type: "button", class: "danger", onclick: deleteCurrent }, "Delete"),
        h("button", { type: "button", class: "quiet", onclick: () => { editor.confirmDelete = false; renderToolbar(); } }, "Keep")));
    } else {
      nodes.push(h("button", { type: "button", class: "danger", "aria-label": "Delete workflow",
        onclick: () => { editor.confirmDelete = true; renderToolbar(); } }, "Delete"));
    }
  }
  bar.replaceChildren(...nodes);
  const title = bar.querySelector(".wf-title");
  if (typing && title) { title.focus(); title.setSelectionRange(...typing); }
}

function agentSelect(value, label, onPick, noneLabel) {
  const agents = agentsList();
  const select = h("select", { "aria-label": label, class: "agent-select" },
    h("option", { value: "" }, noneLabel),
    agents.map((agent) => h("option", { value: agent.id, selected: agent.id === value, disabled: agent.available === false }, agentLabel(agent))),
    value && !agentOf(value) ? h("option", { value, selected: true }, "Agent not found in Locus") : null);
  select.addEventListener("change", () => {
    const agent = agentOf(select.value);
    onPick(select.value, agent ? agent.name : "");
  });
  return select;
}

// ---- rendering: canvas --------------------------------------------------------------
function renderCanvas() {
  const board = $("wf-board");
  const banner = $("wf-problems");
  if (!board) return;
  banner.hidden = !editor.problems.length;
  banner.replaceChildren(...editor.problems.map((text) => h("p", {}, text)));
  const picking = editor.picking || !editor.current;
  $("wf-palette").parentElement.classList.toggle("picking", picking);
  if (picking) {
    board.replaceChildren(templatePicker());
    board.style.width = board.style.height = "";
    $("wf-legend").replaceChildren();
    $("wf-palette").hidden = true;
    return;
  }
  $("wf-palette").hidden = false;
  const d = editor.current;
  const width = Math.max(1100, ...d.nodes.map((node) => node.x + 360));
  const height = Math.max(560, ...d.nodes.map((node) => node.y + 240));
  board.style.width = width + "px";
  board.style.height = height + "px";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "wf-edges");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  board.replaceChildren(svg, ...d.nodes.map(nodeCard));
  $("wf-legend").replaceChildren(...legend());
  drawEdges();
}

function templatePicker() {
  return h("div", { class: "wf-templates" },
    h("h2", {}, "Start from"),
    h("p", { class: "hint" }, "Pick a shape to begin with. You can change every step afterwards."),
    h("div", { class: "wf-template-grid" }, templates().map((template) =>
      h("button", { type: "button", class: "wf-template", onclick: () => newFromTemplate(template) },
        h("strong", {}, template.title), h("span", {}, template.text)))),
    editor.list.length ? h("button", { type: "button", class: "quiet", onclick: () => {
      editor.picking = false; openDefinition(editor.list[0].id);
    } }, "Back to my workflows") : null);
}

function nodeCard(node) {
  const selected = editor.selected && editor.selected.kind === "node" && editor.selected.id === node.id;
  const agent = node.type === "task" ? effectiveAgent(node) : null;
  const line = agent ? lineIndex(agent.id) : -1;
  const card = h("div", {
    class: `wf-node t-${node.type}${selected ? " selected" : ""}${line >= 0 ? " line-" + line : ""}`,
    style: `left:${node.x}px;top:${node.y}px`, tabindex: 0, role: "button", "data-id": node.id,
    "aria-label": `${TYPES[node.type].label}: ${node.title}`,
  },
  h("div", { class: "wf-head" }, h("span", { class: "wf-glyph", "aria-hidden": "true" }), h("span", { class: "wf-name" }, node.title)),
  node.type === "task" ? h("div", { class: "wf-sub" },
    h("span", { class: "chip agent" }, agent.name || "Any agent"),
    h("span", { class: "chip " + (node.access === "write" ? "edits" : "reads") }, node.access === "write" ? "Edits files" : "Reads")) : null,
  node.max_visits > 1 ? h("span", { class: "wf-loops", title: "Can run this many times in a loop" }, `×${node.max_visits}`) : null,
  portsOf(node).length ? h("div", { class: "wf-ports" }, portsOf(node).map((port) =>
    h("span", { class: "wf-port " + port, "data-port": port, title: `Drag to connect “${PORT_LABEL[port] || port}”` },
      PORT_LABEL[port] || port, h("i", { "aria-hidden": "true" })))) : null);
  card.addEventListener("pointerdown", (event) => startDrag(event, node, card));
  card.addEventListener("keydown", (event) => nodeKey(event, node));
  return card;
}

function legend() {
  const used = new Map();
  for (const node of editor.current.nodes.filter((n) => n.type === "task")) {
    const agent = effectiveAgent(node);
    if (!used.has(agent.id)) used.set(agent.id, agent);
  }
  return [...used.values()].map((agent) => {
    const full = agentOf(agent.id);
    return h("span", { class: "legend-item line-" + lineIndex(agent.id) },
      h("i", { "aria-hidden": "true" }), agent.name || "Any agent",
      full && (full.provider || full.model) ? h("small", {}, [full.provider, full.model].filter(Boolean).join(" · ")) : null);
  });
}

function anchor(board, element, side) {
  const b = board.getBoundingClientRect(), r = element.getBoundingClientRect();
  return { x: (side === "right" ? r.right : r.left) - b.left, y: r.top + r.height / 2 - b.top };
}

function curve(a, b) {
  if (b.x < a.x + 30) {  // a loop back: swing under the cards instead of through them
    const drop = Math.max(a.y, b.y) + 110;
    return `M${a.x},${a.y} C${a.x + 90},${a.y} ${a.x + 90},${drop} ${(a.x + b.x) / 2},${drop} ` +
      `S${b.x - 90},${b.y} ${b.x},${b.y}`;
  }
  const bend = Math.max(50, Math.abs(b.x - a.x) / 2);
  return `M${a.x},${a.y} C${a.x + bend},${a.y} ${b.x - bend},${b.y} ${b.x},${b.y}`;
}

function drawEdges(extra) {
  const board = $("wf-board");
  const svg = board && board.querySelector(".wf-edges");
  if (!svg || !editor.current) return;
  const ns = "http://www.w3.org/2000/svg";
  const paths = [];
  for (const edge of editor.current.edges) {
    const from = board.querySelector(`.wf-node[data-id="${CSS.escape(edge.from)}"] .wf-port[data-port="${CSS.escape(edge.on)}"] i`);
    const to = board.querySelector(`.wf-node[data-id="${CSS.escape(edge.to)}"]`);
    if (!from || !to) continue;
    const a = anchor(board, from, "right"), b = anchor(board, to.querySelector(".wf-head"), "left");
    const selected = editor.selected && editor.selected.kind === "edge" && editor.selected.key === edgeKey(edge);
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", curve(a, b));
    path.setAttribute("class", `wf-edge on-${edge.on}${selected ? " selected" : ""}`);
    const hit = document.createElementNS(ns, "path");
    hit.setAttribute("d", curve(a, b));
    hit.setAttribute("class", "wf-hit");
    hit.addEventListener("click", () => { editor.selected = { kind: "edge", key: edgeKey(edge) }; drawEdges(); renderInspector(); });
    paths.push(hit, path);
  }
  if (extra) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", curve(extra.a, extra.b));
    path.setAttribute("class", "wf-edge pending");
    paths.push(path);
  }
  svg.replaceChildren(...paths);
}

const edgeKey = (edge) => `${edge.from}|${edge.on}|${edge.to}`;

// ---- interaction ---------------------------------------------------------------------
function startDrag(event, node, card) {
  if (event.button !== 0) return;
  const port = event.target.closest(".wf-port");
  const board = $("wf-board");
  if (port) return startConnect(event, node, port.dataset.port, board);
  event.preventDefault();
  card.focus();
  const origin = { x: event.clientX, y: event.clientY, nx: node.x, ny: node.y };
  let moved = false;
  card.setPointerCapture(event.pointerId);
  const move = (e) => {
    const dx = e.clientX - origin.x, dy = e.clientY - origin.y;
    if (!moved && Math.hypot(dx, dy) < 4) return;
    moved = true;
    node.x = Math.max(0, Math.round((origin.nx + dx) / GRID) * GRID);
    node.y = Math.max(0, Math.round((origin.ny + dy) / GRID) * GRID);
    card.style.left = node.x + "px";
    card.style.top = node.y + "px";
    drawEdges();
  };
  const up = () => {
    card.removeEventListener("pointermove", move);
    card.removeEventListener("pointerup", up);
    if (moved) { markDirty(); renderCanvas(); } else selectNode(node.id);
  };
  card.addEventListener("pointermove", move);
  card.addEventListener("pointerup", up);
}

function startConnect(event, node, port, board) {
  event.preventDefault();
  event.stopPropagation();
  const dot = event.target.closest(".wf-port").querySelector("i");
  const a = anchor(board, dot, "right");
  const b0 = board.getBoundingClientRect();
  const move = (e) => drawEdges({ a, b: { x: e.clientX - b0.left, y: e.clientY - b0.top } });
  const up = (e) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    const target = document.elementFromPoint(e.clientX, e.clientY);
    const card = target && target.closest(".wf-node");
    // A click on an outcome (no drag) selects the step instead of looping it to itself.
    if (Math.hypot(e.clientX - event.clientX, e.clientY - event.clientY) < 8) selectNode(node.id);
    else if (card) connect(node.id, port, card.dataset.id);
    else drawEdges();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function connect(from, port, to) {
  const source = byId(from), target = to ? byId(to) : null;
  if (!source || (to && (!target || target.type === "start"))) { toast("Nothing can connect into Start.", true); drawEdges(); return; }
  const d = editor.current;
  if (source.type === "split") {
    if (to && !d.edges.some((e) => e.from === from && e.to === to)) d.edges.push({ from, on: port, to });
  } else {
    d.edges = d.edges.filter((e) => !(e.from === from && e.on === port));
    if (to) d.edges.push({ from, on: port, to });
  }
  markDirty();
  renderCanvas();
  renderInspector();
}

function selectNode(id) {
  editor.selected = { kind: "node", id };
  renderCanvas();
  renderInspector();
  const card = document.querySelector(`.wf-node[data-id="${CSS.escape(id)}"]`);
  if (card) card.focus();
}

function nodeKey(event, node) {
  const step = { ArrowLeft: [-GRID, 0], ArrowRight: [GRID, 0], ArrowUp: [0, -GRID], ArrowDown: [0, GRID] }[event.key];
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(node.id); }
  else if (step) {
    event.preventDefault();
    node.x = Math.max(0, node.x + step[0]);
    node.y = Math.max(0, node.y + step[1]);
    markDirty();
    selectNode(node.id);
  } else if ((event.key === "Delete" || event.key === "Backspace") && node.type !== "start") {
    event.preventDefault();
    removeNode(node.id);
  }
}

function addNode(type) {
  const d = editor.current;
  if (!d) return;
  const canvas = $("wf-canvas");
  const x = Math.round((canvas.scrollLeft + canvas.clientWidth / 2 - 90) / GRID) * GRID;
  let y = Math.round((canvas.scrollTop + 60) / GRID) * GRID;
  while (d.nodes.some((node) => Math.abs(node.x - x) < 210 && Math.abs(node.y - y) < 130)) y += 140;
  const node = { id: freshID(type === "task" ? "step" : type), type, title: TYPES[type].label, x, y };
  if (type === "task") Object.assign(node, { title: "New step", access: "read", instruction: "", agent: "", agent_name: "", choices: [], max_visits: 1 });
  if (type === "approval") Object.assign(node, { title: "Your approval", prompt: "", max_visits: 1 });
  if (type === "check") Object.assign(node, { title: "Run the checks", max_visits: 1 });
  d.nodes.push(node);
  markDirty();
  selectNode(node.id);
}

function removeNode(id) {
  const d = editor.current;
  d.nodes = d.nodes.filter((node) => node.id !== id);
  d.edges = d.edges.filter((edge) => edge.from !== id && edge.to !== id);
  editor.selected = null;
  markDirty();
  renderCanvas();
  renderInspector();
}

// ---- rendering: inspector ---------------------------------------------------------------
function field(label, control, hint) {
  return h("label", { class: "wf-field" }, h("span", { class: "wf-label" }, label), control, hint ? h("span", { class: "hint" }, hint) : null);
}

function renderInspector() {
  const panel = $("wf-inspector");
  if (!panel) return;
  const d = editor.current;
  if (!d || editor.picking) { panel.replaceChildren(); panel.hidden = true; return; }
  panel.hidden = false;
  const selection = editor.selected;
  if (selection && selection.kind === "edge") return panel.replaceChildren(...edgeInspector(selection.key));
  const node = selection && selection.kind === "node" ? byId(selection.id) : null;
  if (!node) return panel.replaceChildren(...workflowInspector(d));
  panel.replaceChildren(...nodeInspector(node));
}

function workflowInspector(d) {
  const description = h("textarea", { rows: 3, maxlength: 2000, "aria-label": "Description" });
  description.value = d.description || "";
  description.addEventListener("input", () => { d.description = description.value; markDirty(); });
  const agents = agentsList();
  return [
    h("h3", {}, "Workflow"),
    field("What it's for", description, "Shown in the list when you start a run."),
    h("h4", {}, "Agents"),
    can("agents.read")
      ? agents.length
        ? h("p", { class: "hint" }, "Choose an agent for each step, or a default in the bar above. Each agent brings its own model: a Claude or ChatGPT plan, Kimi, a custom endpoint such as vLLM, or a local model.")
        : h("p", { class: "hint" }, "No saved agents yet. Create them in Locus (Agent World or Settings → Agents), then reopen this window.")
      : h("p", { class: "hint" }, "This Locus can't list agents for plugins, so every step is done by whichever agent runs the workflow."),
    h("h4", {}, "Add a step"),
    h("p", { class: "hint" }, "Use the palette on the left, then drag from an outcome to the next step. Select a step to edit it."),
  ];
}

function nodeInspector(node) {
  const out = [h("p", { class: "wf-kind" }, TYPES[node.type].label), h("p", { class: "hint" }, TYPES[node.type].blurb)];
  const title = h("input", { type: "text", value: node.title, maxlength: 120 });
  title.addEventListener("input", () => { node.title = title.value; markDirty(); renderCanvas(); });
  out.push(field("Name", title));
  if (node.type === "task") {
    out.push(field("Agent", agentSelect(node.agent, "Agent for this step", (id, name) => {
      node.agent = id; node.agent_name = name; markDirty(); renderCanvas(); renderInspector();
    }, editor.current.agent ? `Workflow default (${effectiveAgent({ agent: "" }).name || "set above"})` : "Any agent (whoever runs it)")));
    const chosen = agentOf(node.agent || editor.current.agent);
    if (chosen && node.access === "write" && chosen.access === "read_only") {
      out.push(h("p", { class: "warn" }, `${chosen.name} is limited to reading in Locus, so it can't do a step that edits files.`));
    }
    const instruction = h("textarea", { rows: 6, maxlength: 4000 });
    instruction.value = node.instruction || "";
    instruction.addEventListener("input", () => { node.instruction = instruction.value; markDirty(); });
    out.push(field("What to do", instruction, "The agent also gets the run's goal and the results of earlier steps."));
    const access = h("div", { class: "seg", role: "radiogroup", "aria-label": "Access" },
      [["read", "Reads only"], ["write", "Edits files"]].map(([value, label]) =>
        h("button", { type: "button", role: "radio", "aria-checked": String(node.access === value), onclick: () => {
          node.access = value; markDirty(); renderCanvas(); renderInspector();
        } }, label)));
    out.push(h("div", { class: "wf-field" }, h("span", { class: "wf-label" }, "Access"), access,
      h("span", { class: "hint" }, node.access === "write" ? "Its edits are recorded and your checks decide if they worked." : "If it changes any file, the run stops.")));
    const choices = h("input", { type: "text", value: (node.choices || []).join(", "), placeholder: "e.g. approve, changes", spellcheck: "false" });
    choices.addEventListener("change", () => {
      const next = [...new Set(choices.value.split(",")
        .map((c) => c.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40))
        .filter((c) => c && c !== "failed"))].slice(0, 6);
      const gone = (node.choices && node.choices.length ? node.choices : ["done"]).filter((p) => !next.includes(p));
      editor.current.edges = editor.current.edges.filter((e) => !(e.from === node.id && gone.includes(e.on)));
      node.choices = next;
      markDirty(); renderCanvas(); renderInspector();
    });
    out.push(field("Outcomes the agent picks (optional)", choices, "Leave empty for a single “done”. With outcomes, the agent must pick one and the run follows it."));
  }
  if (node.type === "approval") {
    const prompt = h("textarea", { rows: 3, maxlength: 2000, placeholder: "What should you look at before approving?" });
    prompt.value = node.prompt || "";
    prompt.addEventListener("input", () => { node.prompt = prompt.value; markDirty(); });
    out.push(field("Message", prompt, "Shown with the previous step's result."));
  }
  if (["task", "approval", "check"].includes(node.type)) out.push(visitsControl(node));
  out.push(...outcomeControls(node));
  if (node.type !== "start") {
    out.push(h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: () => removeNode(node.id) }, "Remove step")));
  }
  return out;
}

function visitsControl(node) {
  const value = h("output", {}, String(node.max_visits || 1));
  const set = (next) => { node.max_visits = Math.min(5, Math.max(1, next)); value.textContent = String(node.max_visits); markDirty(); renderCanvas(); minus.disabled = node.max_visits <= 1; plus.disabled = node.max_visits >= 5; };
  const minus = h("button", { type: "button", "aria-label": "Fewer runs", disabled: (node.max_visits || 1) <= 1, onclick: () => set((node.max_visits || 1) - 1) }, "−");
  const plus = h("button", { type: "button", "aria-label": "More runs", disabled: (node.max_visits || 1) >= 5, onclick: () => set((node.max_visits || 1) + 1) }, "+");
  return h("div", { class: "wf-field" }, h("span", { class: "wf-label" }, "Times it may run"),
    h("span", { class: "stepper" }, minus, value, plus),
    h("span", { class: "hint" }, "Raise this for steps a loop comes back to. The run stops for your review when it's used up."));
}

function outcomeControls(node) {
  const d = editor.current;
  const ports = portsOf(node);
  if (!ports.length) return [];
  const out = [h("h4", {}, node.type === "split" ? "Branches" : "Then")];
  if (node.type === "split") {
    const candidates = d.nodes.filter((n) => n.type === "task" && n.access === "read");
    out.push(h("p", { class: "hint" }, "Pick the read-only steps to run side by side. Each must lead to the same Join."));
    for (const candidate of candidates) {
      const checked = d.edges.some((e) => e.from === node.id && e.to === candidate.id);
      const box = h("input", { type: "checkbox", checked });
      box.addEventListener("change", () => {
        d.edges = d.edges.filter((e) => !(e.from === node.id && e.to === candidate.id));
        if (box.checked) d.edges.push({ from: node.id, on: "next", to: candidate.id });
        markDirty(); renderCanvas();
      });
      out.push(h("label", { class: "check-line" }, box, candidate.title));
    }
    return out;
  }
  for (const port of ports) {
    const current = d.edges.find((e) => e.from === node.id && e.on === port);
    const select = h("select", { "aria-label": `Where “${PORT_LABEL[port] || port}” goes` },
      h("option", { value: "" }, OPTIONAL_PORT_RESULT[`${node.type}:${port}`] || "Not connected yet"),
      d.nodes.filter((n) => n.type !== "start").map((n) => h("option", { value: n.id, selected: current && current.to === n.id }, n.title)));
    select.addEventListener("change", () => connect(node.id, port, select.value));
    out.push(field(PORT_LABEL[port] || port, select));
  }
  return out;
}

function edgeInspector(key) {
  const [from, on, to] = key.split("|");
  const source = byId(from), target = byId(to);
  return [
    h("p", { class: "wf-kind" }, "Connection"),
    h("p", {}, `${source ? source.title : from} → ${target ? target.title : to}`),
    h("p", { class: "hint" }, `When “${PORT_LABEL[on] || on}”.`),
    h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: () => {
      editor.current.edges = editor.current.edges.filter((e) => edgeKey(e) !== key);
      editor.selected = null; markDirty(); renderCanvas(); renderInspector();
    } }, "Remove connection")),
  ];
}

// ---- setup -------------------------------------------------------------------------------
function initEditor() {
  const palette = $("wf-palette");
  if (!palette) return;
  palette.replaceChildren(...["task", "approval", "check", "split", "join", "end"].map((type) =>
    h("button", { type: "button", class: "wf-add t-" + type, onclick: () => addNode(type), title: TYPES[type].blurb },
      h("span", { class: "wf-glyph", "aria-hidden": "true" }), TYPES[type].label)));
  $("wf-board").addEventListener("pointerdown", (event) => {
    if (event.target === $("wf-board") || event.target.classList.contains("wf-edges")) {
      editor.selected = null; renderCanvas(); renderInspector();
    }
  });
  window.addEventListener("resize", () => drawEdges());
}

initEditor();
