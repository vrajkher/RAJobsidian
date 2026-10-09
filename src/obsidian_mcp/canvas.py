"""JSON Canvas 1.0 (https://jsoncanvas.org/spec/1.0/): validate, create, and edit .canvas files.

Unknown keys are preserved on round-trip so data written by newer Obsidian versions or
plugins is not lost.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from .errors import Code, ObsidianError

NODE_TYPES = {"text", "file", "link", "group"}
SIDES = {"top", "right", "bottom", "left"}
ENDS = {"none", "arrow"}
BG_STYLES = {"cover", "ratio", "repeat"}
COLOR_RE = re.compile(r"^(?:[1-6]|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})$")
REQUIRED = {"text": ["text"], "file": ["file"], "link": ["url"], "group": []}


def new_id() -> str:
    return secrets.token_hex(8)


def load(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"nodes": [], "edges": []}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ObsidianError(Code.PARSE_ERROR, f"Canvas is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ObsidianError(Code.PARSE_ERROR, "Canvas top level must be an object.")
    data.setdefault("nodes", [])
    data.setdefault("edges", [])
    return data


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent="\t", ensure_ascii=False)


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    problems: list[str] = []
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return ["'nodes' and 'edges' must be arrays."]
    ids: set[str] = set()
    for i, node in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(node, dict):
            problems.append(f"{where} is not an object.")
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            problems.append(f"{where}.id must be a non-empty string.")
        elif nid in ids:
            problems.append(f"{where}.id {nid!r} is duplicated.")
        else:
            ids.add(nid)
        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            problems.append(f"{where}.type {ntype!r} must be one of {sorted(NODE_TYPES)}.")
            continue
        for key in ("x", "y", "width", "height"):
            if not isinstance(node.get(key), int) or isinstance(node.get(key), bool):
                problems.append(f"{where}.{key} must be an integer.")
        for key in REQUIRED[ntype]:
            if not isinstance(node.get(key), str):
                problems.append(f"{where}.{key} is required for {ntype} nodes.")
        if "color" in node and not (isinstance(node["color"], str) and COLOR_RE.match(node["color"])):
            problems.append(f"{where}.color must be '1'-'6' or a hex color.")
        if ntype == "file" and "subpath" in node and not str(node["subpath"]).startswith("#"):
            problems.append(f"{where}.subpath must start with '#'.")
        if ntype == "group" and "backgroundStyle" in node and node["backgroundStyle"] not in BG_STYLES:
            problems.append(f"{where}.backgroundStyle must be one of {sorted(BG_STYLES)}.")
    eids: set[str] = set()
    for i, edge in enumerate(edges):
        where = f"edges[{i}]"
        if not isinstance(edge, dict):
            problems.append(f"{where} is not an object.")
            continue
        eid = edge.get("id")
        if not isinstance(eid, str) or not eid:
            problems.append(f"{where}.id must be a non-empty string.")
        elif eid in eids:
            problems.append(f"{where}.id {eid!r} is duplicated.")
        else:
            eids.add(eid)
        for key in ("fromNode", "toNode"):
            if edge.get(key) not in ids:
                problems.append(f"{where}.{key} {edge.get(key)!r} does not match a node id.")
        for key in ("fromSide", "toSide"):
            if key in edge and edge[key] not in SIDES:
                problems.append(f"{where}.{key} must be one of {sorted(SIDES)}.")
        for key in ("fromEnd", "toEnd"):
            if key in edge and edge[key] not in ENDS:
                problems.append(f"{where}.{key} must be 'none' or 'arrow'.")
        if "color" in edge and not (isinstance(edge["color"], str) and COLOR_RE.match(edge["color"])):
            problems.append(f"{where}.color must be '1'-'6' or a hex color.")
    return problems


def require_valid(data: dict[str, Any]) -> None:
    problems = validate(data)
    if problems:
        raise ObsidianError(Code.INVALID_ARGUMENT, "Canvas is invalid: " + "; ".join(problems[:10]),
                            problems=problems)


def _next_position(data: dict[str, Any]) -> tuple[int, int]:
    nodes = data.get("nodes") or []
    if not nodes:
        return 0, 0
    right = max(int(n.get("x", 0)) + int(n.get("width", 0)) for n in nodes)
    top = min(int(n.get("y", 0)) for n in nodes)
    return right + 40, top


def apply_ops(data: dict[str, Any], ops: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Edit a canvas. Returns (new_data, per-op results). Ops:

    add_node {node}            -- id/x/y/width/height default sensibly
    update_node {id, set{}}    -- set keys (null removes a key)
    remove_node {id}           -- also removes connected edges
    add_edge {edge}
    update_edge {id, set{}}
    remove_edge {id}
    """
    data = json.loads(json.dumps(data))
    results = []
    for op in ops:
        kind = op.get("op")
        if kind == "add_node":
            node = dict(op.get("node") or {})
            node.setdefault("id", new_id())
            x, y = _next_position(data)
            node.setdefault("x", x)
            node.setdefault("y", y)
            node.setdefault("width", 400 if node.get("type") != "group" else 800)
            node.setdefault("height", 400 if node.get("type") in ("file", "link", "group") else 200)
            data["nodes"].append(node)
            results.append({"op": kind, "id": node["id"]})
        elif kind in ("update_node", "update_edge"):
            coll = data["nodes" if kind == "update_node" else "edges"]
            item = next((n for n in coll if n.get("id") == op.get("id")), None)
            if item is None:
                raise ObsidianError(Code.NOT_FOUND, f"No {kind.split('_')[1]} with id {op.get('id')!r}.")
            for key, value in (op.get("set") or {}).items():
                if key == "id":
                    continue
                if value is None:
                    item.pop(key, None)
                else:
                    item[key] = value
            results.append({"op": kind, "id": op["id"]})
        elif kind == "remove_node":
            before = len(data["nodes"])
            data["nodes"] = [n for n in data["nodes"] if n.get("id") != op.get("id")]
            if len(data["nodes"]) == before:
                raise ObsidianError(Code.NOT_FOUND, f"No node with id {op.get('id')!r}.")
            removed = [e["id"] for e in data["edges"] if op["id"] in (e.get("fromNode"), e.get("toNode"))]
            data["edges"] = [e for e in data["edges"] if e.get("id") not in removed]
            results.append({"op": kind, "id": op["id"], "removed_edges": removed})
        elif kind == "add_edge":
            edge = dict(op.get("edge") or {})
            edge.setdefault("id", new_id())
            data["edges"].append(edge)
            results.append({"op": kind, "id": edge["id"]})
        elif kind == "remove_edge":
            before = len(data["edges"])
            data["edges"] = [e for e in data["edges"] if e.get("id") != op.get("id")]
            if len(data["edges"]) == before:
                raise ObsidianError(Code.NOT_FOUND, f"No edge with id {op.get('id')!r}.")
            results.append({"op": kind, "id": op["id"]})
        else:
            raise ObsidianError(Code.INVALID_ARGUMENT, f"Unknown canvas op {kind!r}.")
    require_valid(data)
    return data, results


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    nodes = data.get("nodes", [])
    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n.get("type", "?")] = by_type.get(n.get("type", "?"), 0) + 1
    return {"nodes": len(nodes), "edges": len(data.get("edges", [])), "by_type": by_type,
            "files": sorted({n["file"] for n in nodes if n.get("type") == "file" and "file" in n}),
            "links": sorted({n["url"] for n in nodes if n.get("type") == "link" and "url" in n}),
            "groups": [n.get("label") for n in nodes if n.get("type") == "group"]}
