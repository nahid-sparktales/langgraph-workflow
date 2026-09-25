"""User-drawn workflow graphs as plain data.

A definition only chooses and connects a fixed set of step types; it never
names code, modules, expressions or tools. :func:`validate_definition`
rejects anything the interpreter in ``workflows/custom.py`` cannot run with
bounded loops, and returns the normalized definition that a run pins.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from .contracts import ContractError, canonical, small_json, text

SCHEMA = "lgw.graph/1"
MAX_NODES = 40
MAX_EDGES = 120
MAX_BYTES = 64_000
MAX_VISITS = 5
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")

# Outcome ports per step type. Ports listed in REQUIRED must be connected;
# the others end the run with the status in IMPLICIT when left unconnected.
PORTS = {
    "start": ("next",),
    "task": ("done", "failed"),
    "approval": ("approved", "declined"),
    "check": ("passed", "failed", "needs_review"),
    "split": ("next",),
    "join": ("next",),
    "end": (),
}
REQUIRED = {"start": ("next",), "task": ("done",), "approval": ("approved",),
            "check": ("passed",), "split": ("next",), "join": ("next",)}
IMPLICIT = {("task", "failed"): ("failed", "step_failed"),
            ("approval", "declined"): ("denied", "declined"),
            ("check", "failed"): ("needs_review", "checks_failed"),
            ("check", "needs_review"): ("needs_review", "checks_need_review")}


def _slug(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SLUG.match(value):
        raise ContractError(f"{label} must be 1-40 lowercase letters, digits, '-' or '_'")
    return value


def _agent(value: Any, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _AGENT.match(value):
        raise ContractError(f"{label} must be an agent id")
    return value


def _int(value: Any, label: str, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ContractError(f"{label} must be a whole number from {low} to {high}")
    return value


def ports(node: dict) -> tuple[str, ...]:
    if node["type"] == "task" and node.get("choices"):
        return (*node["choices"], "failed")
    return PORTS[node["type"]]


def _node(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("each step must be an object")
    kind = raw.get("type")
    if kind not in PORTS:
        raise ContractError(f"step type must be one of {', '.join(PORTS)}")
    node_id = _slug(raw.get("id"), "step id")
    shown = raw.get("title") if isinstance(raw.get("title"), str) and raw.get("title") else node_id
    label = f"Step “{shown[:60]}”:"
    node = {"id": node_id, "type": kind,
            "title": text((raw.get("title") or "").strip() or kind.title(), f"{label} title", 120),
            "x": _int(raw.get("x"), f"{label} x", -10_000, 10_000, 0),
            "y": _int(raw.get("y"), f"{label} y", -10_000, 10_000, 0)}
    if kind == "task":
        node["instruction"] = text(raw.get("instruction"), f"{label} instruction", 4000,
                                   required=True)
        if raw.get("access") not in ("read", "write"):
            raise ContractError(f"{label} access must be read or write")
        node["access"] = raw["access"]
        node["agent"] = _agent(raw.get("agent"), f"{label} agent")
        node["agent_name"] = text(raw.get("agent_name") or "", f"{label} agent name", 120)
        choices = raw.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 6:
            raise ContractError(f"{label} choices must be a list of at most 6")
        node["choices"] = [_slug(c, f"{label} choice") for c in choices]
        if len(set(node["choices"])) != len(node["choices"]) or "failed" in node["choices"]:
            raise ContractError(f"{label} choices must be unique and not 'failed'")
    if kind == "approval":
        node["prompt"] = text(raw.get("prompt") or "", f"{label} prompt", 2000)
    if kind in ("task", "approval", "check"):
        node["max_visits"] = _int(raw.get("max_visits"), f"{label} max visits", 1, MAX_VISITS, 1)
    unknown = set(raw) - set(node) - {"agent", "agent_name", "choices", "prompt", "max_visits",
                                      "instruction", "access"}
    if unknown:
        raise ContractError(f"{label} has unknown fields: {sorted(unknown)}")
    return node


def validate_definition(value: Any) -> dict:
    """Normalize a definition or raise :class:`ContractError` naming the problem."""
    raw = small_json(value, "workflow definition")
    if not isinstance(raw, dict):
        raise ContractError("workflow definition must be an object")
    if len(canonical(raw).encode()) > MAX_BYTES:
        raise ContractError(f"workflow definition exceeds {MAX_BYTES} bytes")
    unknown = set(raw) - {"schema", "id", "title", "description", "agent", "agent_name",
                          "nodes", "edges"}
    if unknown:
        raise ContractError(f"workflow definition has unknown fields: {sorted(unknown)}")
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise ContractError(f"workflow definition schema must be {SCHEMA}")
    nodes_raw, edges_raw = raw.get("nodes"), raw.get("edges")
    if not isinstance(nodes_raw, list) or not 2 <= len(nodes_raw) <= MAX_NODES:
        raise ContractError(f"a workflow needs 2 to {MAX_NODES} steps")
    if not isinstance(edges_raw, list) or len(edges_raw) > MAX_EDGES:
        raise ContractError(f"a workflow has at most {MAX_EDGES} connections")
    nodes = [_node(n) for n in nodes_raw]
    by_id = {n["id"]: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ContractError("step ids must be unique")
    definition = {
        "schema": SCHEMA, "id": _slug(raw.get("id"), "workflow id"),
        "title": text(raw.get("title").strip() if isinstance(raw.get("title"), str)
                      else raw.get("title"), "workflow title", 120, required=True),
        "description": text(raw.get("description") or "", "workflow description", 2000),
        "agent": _agent(raw.get("agent"), "default agent"),
        "agent_name": text(raw.get("agent_name") or "", "default agent name", 120),
        "nodes": nodes, "edges": [],
    }
    seen = set()
    for raw_edge in edges_raw:
        if not isinstance(raw_edge, dict) or set(raw_edge) - {"from", "to", "on"}:
            raise ContractError("each connection needs only from, to and on")
        source, target = by_id.get(raw_edge.get("from")), by_id.get(raw_edge.get("to"))
        if source is None or target is None:
            raise ContractError("a connection points to a missing step")
        port = raw_edge.get("on") or (ports(source) or ("",))[0]
        if port not in ports(source):
            raise ContractError(f"step {source['id']} has no outcome '{port}'")
        if target["type"] == "start":
            raise ContractError("nothing can connect into the start step")
        key = (source["id"], port, target["id"])
        if key in seen:
            continue
        if source["type"] != "split" and any(e[:2] == key[:2] for e in seen):
            raise ContractError(f"outcome '{port}' of step {source['id']} has two connections")
        seen.add(key)
        definition["edges"].append({"from": source["id"], "to": target["id"], "on": port})
    _check_shape(definition, by_id)
    return definition


def targets(definition: dict, node_id: str, port: str) -> list[str]:
    return [e["to"] for e in definition["edges"] if e["from"] == node_id and e["on"] == port]


def _check_shape(definition: dict, by_id: dict) -> None:
    starts = [n for n in definition["nodes"] if n["type"] == "start"]
    if len(starts) != 1:
        raise ContractError("a workflow needs exactly one start step")
    if not any(n["type"] == "end" for n in definition["nodes"]):
        raise ContractError("a workflow needs at least one end step")
    for node in definition["nodes"]:
        for port in REQUIRED.get(node["type"], ()) if not (
                node["type"] == "task" and node["choices"]) else node["choices"]:
            if not targets(definition, node["id"], port):
                raise ContractError(f"connect the '{port}' outcome of step {node['title']}")
    # Parallel branches: split -> read-only tasks -> one join, nothing else.
    joins_owned = {}
    for split in (n for n in definition["nodes"] if n["type"] == "split"):
        branches = targets(definition, split["id"], "next")
        if not 2 <= len(branches) <= 8:
            raise ContractError(f"split {split['title']} needs 2 to 8 branches")
        joins = set()
        for branch_id in branches:
            branch = by_id[branch_id]
            if branch["type"] != "task" or branch["access"] != "read" or branch["choices"]:
                raise ContractError(f"branches of split {split['title']} must be read-only "
                                    "agent steps without choices")
            done = targets(definition, branch_id, "done")
            if targets(definition, branch_id, "failed") or len(done) != 1 or \
                    by_id[done[0]]["type"] != "join":
                raise ContractError(f"branch {branch['title']} must lead only to a join")
            if sum(1 for e in definition["edges"] if e["to"] == branch_id) != 1:
                raise ContractError(f"branch {branch['title']} can only be reached from its split")
            joins.add(done[0])
        if len(joins) != 1:
            raise ContractError(f"all branches of split {split['title']} must meet at one join")
        join = joins.pop()
        if join in joins_owned:
            raise ContractError("each join belongs to exactly one split")
        joins_owned[join] = split["id"]
        if {e["from"] for e in definition["edges"] if e["to"] == join} != set(branches):
            raise ContractError(f"only the branches of split {split['title']} may enter its join")
    for node in definition["nodes"]:
        if node["type"] == "join" and node["id"] not in joins_owned:
            raise ContractError(f"join {node['title']} needs a split")
    # Every step is reachable from start and can reach an end.
    reach, queue = {starts[0]["id"]}, deque([starts[0]["id"]])
    while queue:
        for edge in definition["edges"]:
            if edge["from"] == queue[0] and edge["to"] not in reach:
                reach.add(edge["to"])
                queue.append(edge["to"])
        queue.popleft()
    unreachable = [by_id[n]["title"] for n in by_id if n not in reach]
    if unreachable:
        raise ContractError(f"not reachable from start: {', '.join(unreachable)}")
    ends = {n for n, node in by_id.items() if node["type"] == "end"}
    queue = deque(ends)
    while queue:
        for edge in definition["edges"]:
            if edge["to"] == queue[0] and edge["from"] not in ends:
                ends.add(edge["from"])
                queue.append(edge["from"])
        queue.popleft()
    stuck = [by_id[n]["title"] for n in by_id if n not in ends]
    if stuck:
        raise ContractError(f"no path to an end step from: {', '.join(stuck)}")
