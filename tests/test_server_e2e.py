"""End-to-end MCP protocol tests against the real server (in-process client)."""

from __future__ import annotations

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from obsidian_mcp.server import APP_URI, build_server


@pytest.fixture()
def server(service):
    return build_server(service)


async def test_tools_listed_with_annotations_and_entrypoints(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"search_notes", "read_note", "patch_note", "move_file", "cli_run", "edit_canvas",
                "open_vault_browser", "settings.read", "settings.update"} <= set(tools)
        assert tools["read_note"].annotations.read_only_hint is True
        assert tools["delete_permanently"].annotations.destructive_hint is True
        meta = tools["open_vault_browser"].meta
        assert meta["ui"]["resourceUri"] == APP_URI
        assert meta["openai/ui"]["entrypoints"][0]["type"] == "global"
        assert tools["open_notes_tray"].meta["openai/ui"]["entrypoints"] == [{"type": "thread"}]
        file_ep = tools["open_obsidian_file"].meta["openai/ui"]["entrypoints"][0]
        assert file_ep == {"type": "file", "extensions": [".md", ".canvas", ".base"]}
        mention = [t for t in tools.values() if (t.meta or {}).get("openai/extensions")]
        assert mention and mention[0].meta["ui"]["visibility"] == ["app"]
        assert tools["open_vault_browser"].icons


async def test_settings_capability_and_roundtrip(server, service):
    async with Client(server) as client:
        caps = client.server_capabilities
        dumped = caps.model_dump(by_alias=True)
        advertised = (dumped.get("extensions") or {}) | (dumped.get("experimental") or {})
        assert advertised["openai/settings"] == {"readTool": "settings.read", "updateTool": "settings.update"}
        read = await client.call_tool("settings.read", {})
        sc = read.structured_content
        assert sc["values"]["default_vault"] == "Test"
        assert set(sc["schema"]["properties"]) == set(sc["values"])
        upd = await client.call_tool("settings.update", {"set": {"read_only": True, "page_size": 25}})
        assert upd.structured_content["values"]["read_only"] is True
        assert service.config.permissions.write is False and service.config.page_size == 25
        res = await client.call_tool("append_note", {"path": "Ideas.md", "content": "x"})
        assert res.is_error and "[PERMISSION_DENIED]" in res.content[0].text


async def test_search_read_patch_verify_flow(server, vault_dir):
    async with Client(server) as client:
        found = await client.call_tool("search_notes", {"query": "goal"})
        path = found.structured_content["results"][0]["path"]
        note = (await client.call_tool("read_note", {"path": path})).structured_content
        preview = await client.call_tool("patch_note", {"path": path, "dry_run": True, "ops": [
            {"op": "insert", "heading": "Goals", "content": "- new"}]})
        assert "+- new" in preview.structured_content["diff"]
        applied = await client.call_tool("patch_note", {"path": path, "if_match": note["etag"], "ops": [
            {"op": "insert", "heading": "Goals", "content": "- new"}]})
        assert applied.structured_content["changed"] is True
        stale = await client.call_tool("patch_note", {"path": path, "if_match": note["etag"], "ops": [
            {"op": "append", "content": "y"}]})
        assert stale.is_error and "[CONFLICT]" in stale.content[0].text
        undo = await client.call_tool("rollback_operation",
                                      {"operation_id": applied.structured_content["operation_id"]})
        assert undo.structured_content["results"][0]["status"] == "undone"


async def test_resources_with_etag_and_representation(server, service):
    vid = service.vault(None).id
    async with Client(server) as client:
        templates = (await client.list_resource_templates()).resource_templates
        assert any("{+path}" in t.uri_template for t in templates)
        res = await client.read_resource(f"obsidian://vault/{vid}/Ideas.md")
        content = res.contents[0]
        assert "write docs" in content.text
        assert content.meta["openai/resource"]["etag"]
        img = await client.read_resource(f"obsidian://vault/{vid}/pic.png")
        assert img.contents[0].mime_type == "image/png" and img.contents[0].blob
        caps = json.loads((await client.read_resource("obsidian://capabilities")).contents[0].text)
        assert caps["filesystem"]["available"]
        app = await client.read_resource(APP_URI)
        assert app.contents[0].mime_type == "text/html;profile=mcp-app"
        assert app.contents[0].meta["openai/ui"]["preferredDisplayMode"] == "fullscreen"


async def test_resource_traversal_rejected(server, service):
    vid = service.vault(None).id
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.read_resource(f"obsidian://vault/{vid}/../secret.md")


async def test_mentions_and_entrypoints_accept_empty_args(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        mention_tool = next(n for n, t in tools.items() if (t.meta or {}).get("openai/extensions"))
        out = await client.call_tool(mention_tool, {"query": "plan"})
        items = out.structured_content["items"]
        assert items[0]["type"] == "resource_link" and items[0]["uri"].startswith("obsidian://vault/")
        browser = await client.call_tool("open_vault_browser", {})
        assert browser.structured_content["view"] == "browser" and browser.structured_content["recent"]
        tray = await client.call_tool("open_notes_tray", {})
        assert tray.structured_content["view"] == "tray"


async def test_file_entrypoint_does_not_grant_access_outside_vaults(server, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")
    async with Client(server) as client:
        res = await client.call_tool("open_obsidian_file", {"file": {"name": "elsewhere.md",
                                                                     "resourceUri": "host-resource://1"}},
                                     meta={"openai/resource": {"path": str(outside)}})
        assert "vault_file" not in res.structured_content
        res = await client.call_tool("open_obsidian_file", {"file": {"name": "Ideas.md",
                                                                     "resourceUri": "host-resource://2"}})
        assert res.structured_content["file"]["kind"] == "note"


async def test_prompts(server):
    async with Client(server) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert {"daily_review", "organize_note", "fix_links"} <= names


async def test_file_entrypoint_maps_paths_inside_connected_vault(server, vault_dir, service):
    async with Client(server) as client:
        res = await client.call_tool("open_obsidian_file", {"file": {"name": "Ideas.md",
                                                                     "resourceUri": "host-resource://3"}},
                                     meta={"openai/resource": {"path": str(vault_dir / "Ideas.md")}})
        assert res.structured_content["vault_file"] == {"vault": service.vault(None).id, "path": "Ideas.md"}
        hidden = await client.call_tool("open_obsidian_file", {"file": {"name": "app.json",
                                                                        "resourceUri": "host-resource://4"}},
                                        meta={"openai/resource": {"path": str(vault_dir / ".obsidian/x.md")}})
        assert "vault_file" not in hidden.structured_content
