"""Bases (.base) files: parse, structurally validate, and edit the documented YAML schema.

Query evaluation is deliberately NOT reimplemented here. Filters and formulas use
Obsidian's expression language and plugin-provided functions; guessing their results
would be wrong. Results come from the official CLI (``base:query``) when Obsidian runs.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .errors import Code, ObsidianError
from .markdown import to_plain

TOP_KEYS = {"filters", "formulas", "properties", "summaries", "views"}
BUILTIN_VIEW_TYPES = {"table", "cards", "list", "map", "kanban"}
DEFAULT_SUMMARIES = {"Average", "Min", "Max", "Sum", "Range", "Median", "Stddev", "Earliest", "Latest",
                     "Checked", "Unchecked", "Empty", "Filled", "Unique"}


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.allow_unicode = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(text: str) -> Any:
    if not text.strip():
        return CommentedMap()
    try:
        data = _yaml().load(text)
    except YAMLError as exc:
        raise ObsidianError(Code.PARSE_ERROR, f"Base file is not valid YAML: {exc}") from exc
    if data is None:
        return CommentedMap()
    if not isinstance(data, dict):
        raise ObsidianError(Code.PARSE_ERROR, "Base file must be a YAML mapping.")
    return data


def dump(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def _check_filter(node: Any, where: str, problems: list[str]) -> None:
    if node is None or isinstance(node, str):
        return
    if isinstance(node, dict):
        keys = set(node)
        if len(keys) != 1 or not keys <= {"and", "or", "not"}:
            problems.append(f"{where} must contain exactly one of and/or/not.")
            return
        key = next(iter(keys))
        items = node[key]
        if not isinstance(items, list):
            problems.append(f"{where}.{key} must be a list.")
            return
        for i, item in enumerate(items):
            _check_filter(item, f"{where}.{key}[{i}]", problems)
        return
    problems.append(f"{where} must be a string statement or an and/or/not object.")


def validate(data: Any) -> dict[str, list[str]]:
    """Structural validation only. Returns {'errors': [...], 'warnings': [...]}."""
    errors: list[str] = []
    warnings: list[str] = []
    for key in data:
        if key not in TOP_KEYS:
            warnings.append(f"Unknown top-level key {key!r} (kept as-is).")
    _check_filter(data.get("filters"), "filters", errors)
    formulas = data.get("formulas") or {}
    if not isinstance(formulas, dict):
        errors.append("formulas must be a mapping of name -> expression string.")
    else:
        for name, expr in formulas.items():
            if not isinstance(expr, str):
                errors.append(f"formulas.{name} must be a string expression.")
    if "properties" in data and not isinstance(data["properties"], dict):
        errors.append("properties must be a mapping.")
    views = data.get("views") or []
    if not isinstance(views, list):
        errors.append("views must be a list.")
        views = []
    names = set()
    for i, view in enumerate(views):
        where = f"views[{i}]"
        if not isinstance(view, dict):
            errors.append(f"{where} must be a mapping.")
            continue
        if not view.get("type"):
            errors.append(f"{where}.type is required.")
        elif view["type"] not in BUILTIN_VIEW_TYPES:
            warnings.append(f"{where}.type {view['type']!r} is not built in; a plugin must provide it.")
        if view.get("name") in names:
            warnings.append(f"{where}.name {view.get('name')!r} is duplicated.")
        names.add(view.get("name"))
        _check_filter(view.get("filters"), f"{where}.filters", errors)
        if "order" in view and not isinstance(view["order"], list):
            errors.append(f"{where}.order must be a list of property names.")
        gb = view.get("groupBy")
        if gb is not None:
            if not isinstance(gb, dict) or "property" not in gb:
                errors.append(f"{where}.groupBy needs a 'property'.")
            elif gb.get("direction") not in (None, "ASC", "DESC"):
                errors.append(f"{where}.groupBy.direction must be ASC or DESC.")
        if "limit" in view and not isinstance(view["limit"], int):
            errors.append(f"{where}.limit must be an integer.")
        custom = set((data.get("summaries") or {}).keys())
        for prop, summary in (view.get("summaries") or {}).items():
            if summary not in DEFAULT_SUMMARIES | custom:
                warnings.append(f"{where}.summaries.{prop}: {summary!r} is not a default or custom summary.")
    return {"errors": errors, "warnings": warnings}


def describe(data: Any) -> dict[str, Any]:
    return {"filters": to_plain(data.get("filters")), "formulas": to_plain(data.get("formulas") or {}),
            "properties": to_plain(data.get("properties") or {}),
            "summaries": to_plain(data.get("summaries") or {}),
            "views": [{"name": v.get("name"), "type": v.get("type"), "filters": to_plain(v.get("filters")),
                       "order": to_plain(v.get("order")), "groupBy": to_plain(v.get("groupBy")),
                       "limit": v.get("limit")} for v in (data.get("views") or []) if isinstance(v, dict)],
            "validation": validate(data)}


def apply_ops(data: Any, ops: list[dict[str, Any]]) -> Any:
    """Edit a base definition. Ops:

    set_filters {filters}                 -- global filters (null removes)
    set_formula {name, expression}        -- expression null removes
    set_property {name, config}           -- e.g. {"displayName": "Status"}
    set_summary {name, formula}
    add_view {view}                       -- requires type and name
    update_view {name, set{}}             -- null removes a key
    remove_view {name}
    """
    for op in ops:
        kind = op.get("op")
        if kind == "set_filters":
            if op.get("filters") is None:
                data.pop("filters", None)
            else:
                data["filters"] = op["filters"]
        elif kind in ("set_formula", "set_property", "set_summary"):
            section = {"set_formula": "formulas", "set_property": "properties", "set_summary": "summaries"}[kind]
            value = op.get({"set_formula": "expression", "set_property": "config", "set_summary": "formula"}[kind])
            bucket = data.setdefault(section, CommentedMap())
            if value is None:
                bucket.pop(op["name"], None)
            else:
                bucket[op["name"]] = value
        elif kind == "add_view":
            view = op.get("view") or {}
            if not view.get("type") or not view.get("name"):
                raise ObsidianError(Code.INVALID_ARGUMENT, "add_view needs view.type and view.name.")
            data.setdefault("views", []).append(view)
        elif kind in ("update_view", "remove_view"):
            views = data.get("views") or []
            idx = next((i for i, v in enumerate(views) if isinstance(v, dict) and v.get("name") == op.get("name")),
                       None)
            if idx is None:
                raise ObsidianError(Code.NOT_FOUND, f"No view named {op.get('name')!r}.",
                                    views=[v.get("name") for v in views if isinstance(v, dict)])
            if kind == "remove_view":
                views.pop(idx)
            else:
                for key, value in (op.get("set") or {}).items():
                    if value is None:
                        views[idx].pop(key, None)
                    else:
                        views[idx][key] = value
        else:
            raise ObsidianError(Code.INVALID_ARGUMENT, f"Unknown base op {kind!r}.")
    result = validate(data)
    if result["errors"]:
        raise ObsidianError(Code.INVALID_ARGUMENT, "Base would be invalid: " + "; ".join(result["errors"]),
                            **result)
    return data
