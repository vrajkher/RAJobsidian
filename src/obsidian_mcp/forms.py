"""OpenAI extended forms for picking vault resources.

Schemas use the documented OpenAI form extensions: resource selection (``x-openai-input``)
with option thumbnails and previews, titled ``oneOf`` options with descriptions and
``x-openai-thumbnail``, and ``x-openai-suggestions``. Every schema is validated with
OpenAI's own ``openai_mcp_form_protocol`` before it is sent.

Transport depends on the client:
* MCP 2026-07-28 (required for OpenAI-registered servers): MRTR. The extended schema rides
  in a standard ``elicitation/create`` input request; the server-sealed ``request_state``
  carries the query to the retry.
* Legacy direct connections advertising ``openai/elicitation``: ``openai/elicitation/create``.
* Plain MCP clients with elicitation: a standard form with an enum (no extensions).
* Clients without elicitation: candidates are returned for the model to ask in text.
"""

from __future__ import annotations

import base64
import posixpath
from typing import Any

from openai_mcp_form_protocol import FormField, FormSchema, validate_form_selections

from .service import ObsidianService
from .vault import file_kind, mime_of

MAX_THUMB_BYTES = 96_000


def resource_option(svc: ObsidianService, vault_id: str, path: str, uri: str, preview_tool: str) -> dict[str, Any]:
    option: dict[str, Any] = {"uri": uri, "name": posixpath.basename(path), "title": path, "mimeType": mime_of(path)}
    meta: dict[str, Any] = {"openai/preview": {"target": {"type": "mcp_app_tool", "name": preview_tool,
                                                         "arguments": {"path": path}}}}
    option["_meta"] = meta
    return option


def thumbnail(svc: ObsidianService, vault_id: str, path: str) -> dict[str, str] | None:
    if file_kind(path) != "image":
        return None
    try:
        data = svc.vault(vault_id).read_bytes(path, max_bytes=MAX_THUMB_BYTES)
    except Exception:
        return None
    return {"src": f"data:{mime_of(path)};base64,{base64.b64encode(data).decode()}", "mimeType": mime_of(path)}


def pick_schema(svc: ObsidianService, vault_id: str, items: list[str], uri_for: Any, *, multiple: bool,
                images: bool, tag_suggestions: list[str]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if images:
        # Titled oneOf options with thumbnails: every option gets an image (spec SHOULD).
        options = []
        for p in items:
            opt: dict[str, Any] = {"const": uri_for(vault_id, p), "title": posixpath.basename(p),
                                   "description": p}
            thumb = thumbnail(svc, vault_id, p)
            if thumb:
                opt["x-openai-thumbnail"] = thumb
            options.append(opt)
        if options and not all("x-openai-thumbnail" in o for o in options):
            for o in options:
                o.pop("x-openai-thumbnail", None)
        props["selection"] = {"type": "string", "title": "Image", "oneOf": options}
    else:
        resources = [resource_option(svc, vault_id, p, uri_for(vault_id, p), "open_vault_browser") for p in items]
        field: dict[str, Any] = {"title": "Notes", "x-openai-input": {"type": "resource", "options": resources}}
        if multiple:
            field.update(type="array", items={"type": "string", "format": "uri"})
            field["x-openai-input"]["selection"] = "explicit"
        else:
            field.update(type="string", format="uri")
        props["selection"] = field
    props["action"] = {"type": "string", "title": "Then", "default": "read", "oneOf": [
        {"const": "read", "title": "Read them", "description": "Return the selected notes' text to the chat."},
        {"const": "links", "title": "Just reference them", "description": "Return links only, no content."},
        {"const": "tag", "title": "Add a tag", "description": "Add the tag below to each selected note."},
    ]}
    props["tag"] = {"type": "string", "title": "Tag (for 'Add a tag')", "pattern": "^#?[^\\s#]*$",
                    "x-openai-suggestions": [{"const": t, "title": t} for t in tag_suggestions[:12]]}
    schema = {"type": "object", "properties": props, "required": ["selection"]}
    FormSchema[FormField].model_validate(schema)  # fail fast on anything the host would reject
    return schema


def plain_schema(uris: list[str], titles: list[str]) -> dict[str, Any]:
    """Standard MCP elicitation schema (no OpenAI extensions)."""
    return {"type": "object", "required": ["selection"], "properties": {
        "selection": {"type": "string", "title": "Note", "oneOf": [{"const": u, "title": t} for u, t in zip(uris, titles, strict=True)]},
        "action": {"type": "string", "title": "Then", "enum": ["read", "links"], "default": "read"},
    }}


def validate_answer(schema: dict[str, Any], content: dict[str, Any]) -> None:
    validate_form_selections(FormSchema[FormField].model_validate(schema), content)
