"""Links, rename with link updates, search, tasks, properties, composer, canvas, bases, templates."""

from __future__ import annotations

from datetime import datetime

import pytest

from obsidian_mcp import bases, canvas
from obsidian_mcp.errors import ObsidianError
from obsidian_mcp.templates import moment_format, render


def test_backlinks_unresolved_orphans(service):
    bl = service.backlinks(None, "Projects/Plan.md", include_unlinked=True)
    sources = {b["source"] for b in bl["backlinks"]}
    assert sources == {"Home.md"}
    assert any(m["source"] == "Ideas.md" for m in bl["unlinked_mentions"])
    unresolved = service.link_report(None, "unresolved")
    assert any(i["target"] == "Missing Note" for i in unresolved["items"])
    orphans = service.link_report(None, "orphans")["items"]
    assert "Orphan.md" in orphans and "Home.md" not in orphans
    assert "Orphan.md" in service.link_report(None, "deadends")["items"]


def test_rename_updates_wikilinks_and_markdown_links(service, vault_dir):
    preview = service.rename_file(None, "Projects/Plan.md", "Roadmap", dry_run=True)
    assert preview["dry_run"] and preview["link_updates"]
    assert (vault_dir / "Projects/Plan.md").exists()
    out = service.rename_file(None, "Projects/Plan.md", "Roadmap")
    home = (vault_dir / "Home.md").read_text(encoding="utf-8")
    assert "[[Projects/Roadmap]]" in home  # path-style link keeps its style
    assert "](Projects/Roadmap.md#Goals)" in home
    assert "[[Ideas|my ideas]]" in home  # untouched link keeps alias
    assert home.startswith("---\ntags: [hub]")  # frontmatter untouched
    service.rollback(None, out["operation_id"])
    assert (vault_dir / "Projects/Plan.md").exists()
    assert "[[Projects/Plan]]" in (vault_dir / "Home.md").read_text(encoding="utf-8")


def test_move_folder_updates_links(service, vault_dir):
    service.move_file(None, "Projects", "Archive/Projects")
    home = (vault_dir / "Home.md").read_text(encoding="utf-8")
    assert "Archive/Projects/Plan" in home
    assert (vault_dir / "Archive/Projects/Other.md").exists()


def test_search_operators_and_pagination(service):
    r = service.search_notes(None, "goal")
    assert r["results"][0]["path"] == "Projects/Plan.md"
    assert r["results"][0]["matches"]
    assert service.search_notes(None, "tag:#hub")["total"] == 1
    assert service.search_notes(None, "path:Projects -Other")["total"] == 1
    assert service.search_notes(None, "[aliases:Start]")["results"][0]["path"] == "Home.md"
    assert service.search_notes(None, "/plan|ideas/")["total"] >= 2
    assert service.search_notes(None, "नमस्ते")["total"] == 1
    page = service.search_notes(None, "", limit=2)
    assert page["next_offset"] == 2 and len(page["results"]) == 2
    assert service.search_notes(None, "", tags=["ટૅગ"])["total"] == 1


def test_tags_properties_and_metadata(service):
    tags = service.tags(None)["tags"]
    assert "#hub" in tags and "#ટૅગ/ઉપ" in tags
    tree = service.tags(None, tree=True)["tags"]
    assert tree["ટૅગ"]["children"]["ઉપ"]["count"] == 1
    meta = service.note_metadata(None, "Ideas")
    assert meta["block_ids"] == {"task1": 2}
    assert meta["tasks"][1]["status"] == "x"
    assert "tags" in service.properties(None)["properties"]


def test_set_properties_keeps_body(service, vault_dir):
    service.set_properties(None, "Home.md", {"status": "draft", "rating": 5}, remove=["aliases"])
    text = (vault_dir / "Home.md").read_text(encoding="utf-8")
    assert "status: draft" in text and "aliases" not in text
    assert text.split("---\n", 2)[2].startswith("# Home\nSee [[Projects/Plan]]")
    service.set_properties(None, "Orphan.md", {"new": True})
    assert (vault_dir / "Orphan.md").read_text(encoding="utf-8") == "---\nnew: true\n---\nnobody links here\n"


def test_patch_ops(service, vault_dir):
    service.patch_note(None, "Projects/Plan.md", [
        {"op": "insert", "heading": "Goals", "content": "- added goal"},
        {"op": "replace_section", "heading": "Notes", "content": "New notes"},
        {"op": "replace", "find": "# Plan", "replace": "# The Plan"},
    ])
    text = (vault_dir / "Projects/Plan.md").read_text(encoding="utf-8")
    assert text == "# The Plan\n## Goals\nGoal text\n- added goal\n## Notes\nNew notes\n"
    with pytest.raises(ObsidianError):
        service.patch_note(None, "Projects/Plan.md", [{"op": "insert", "heading": "Nope", "content": "x"}])
    service.patch_note(None, "Ideas.md", [{"op": "replace_block", "block_id": "task1", "content": "- [ ] new"}])
    assert "- [ ] new ^task1" in (vault_dir / "Ideas.md").read_text(encoding="utf-8")


def test_tasks(service, vault_dir):
    tasks = service.list_tasks(None)
    done = [t for t in tasks["items"] if t["status"] == "x"][0]
    assert done["tasks_plugin"] == {"due": "2026-01-02", "priority": "high"}
    service.set_task_status(None, "Ideas.md", 2, "x", expected_text="write docs")
    assert "- [x] write docs" in (vault_dir / "Ideas.md").read_text(encoding="utf-8")
    with pytest.raises(ObsidianError):
        service.set_task_status(None, "Ideas.md", 1, "x")


def test_merge_and_extract(service, vault_dir):
    preview = service.merge_notes(None, "Projects/Other.md", "Projects/Plan.md")
    assert preview["dry_run"]
    service.merge_notes(None, "Projects/Other.md", "Projects/Plan.md", dry_run=False)
    assert "Link to [[Missing Note]]" in (vault_dir / "Projects/Plan.md").read_text(encoding="utf-8")
    assert not (vault_dir / "Projects/Other.md").exists()
    service.extract_note(None, "Projects/Plan.md", "Goals note", heading="Goals", dry_run=False)
    assert (vault_dir / "Goals note.md").read_text(encoding="utf-8").startswith("## Goals\nGoal text")
    assert "[[Goals note]]" in (vault_dir / "Projects/Plan.md").read_text(encoding="utf-8")


def test_link_notes_and_repair(service, vault_dir):
    service.link_notes(None, "Orphan.md", "Ideas.md", text="See")
    assert (vault_dir / "Orphan.md").read_text(encoding="utf-8").endswith("See [[Ideas]]")
    sugg = service.repair_links(None)
    assert sugg["unresolved"] == 1
    service.repair_links(None, {"Missing Note": "Orphan.md"}, dry_run=False)
    assert "[[Orphan]]" in (vault_dir / "Projects/Other.md").read_text(encoding="utf-8")


def test_duplicates_and_related(service):
    service.write_note(None, "Copy.md", (service.read_note(None, "Projects/Plan.md")["content"]))
    dups = service.find_duplicates(None)
    assert any(set(group) == {"Copy.md", "Projects/Plan.md"} for group in dups["exact_content"])
    related = service.related_notes(None, "Projects/Plan.md")["related"]
    assert related[0]["path"] == "Copy.md"


def test_daily_and_unique_and_templates(service, vault_dir):
    (vault_dir / "Templates").mkdir()
    (vault_dir / "Templates/Day.md").write_bytes(b"# {{title}}\nCreated {{date:dddd, MMMM Do YYYY}} {{custom}}\n")
    daily = service.daily_note(None, "2026-03-05")
    assert daily["path"] == "Daily/2026-03-05.md" and daily["created"]
    again = service.daily_note(None, "2026-03-05")
    assert again["created"] is False
    out = service.render_template(None, "Templates/Day.md", title="T", date="2026-03-05")
    assert out["content"].startswith("# T\nCreated Thursday, March 5th 2026")
    assert out["unresolved_variables"] == ["{{custom}}"]
    uniq = service.unique_note(None, "hello")
    assert (vault_dir / uniq["path"]).read_text(encoding="utf-8") == "hello"


def test_moment_tokens():
    dt = datetime(2026, 1, 4, 15, 7, 9)
    assert moment_format(dt, "YYYY-MM-DD HH:mm:ss") == "2026-01-04 15:07:09"
    assert moment_format(dt, "[Week] WW gggg, h:mm A") == "Week 01 2026, 3:07 PM"
    assert render("{{time}}", now=dt)[0] == "15:07"


def test_canvas_validate_and_edit(service, vault_dir):
    out = service.write_canvas(None, "Board", ops=[
        {"op": "add_node", "node": {"id": "a", "type": "text", "text": "Hello **world**"}},
        {"op": "add_node", "node": {"id": "b", "type": "file", "file": "Ideas.md", "color": "4"}},
        {"op": "add_edge", "edge": {"id": "e1", "fromNode": "a", "toNode": "b", "label": "relates"}},
    ])
    assert out["summary"]["nodes"] == 2
    data = service.read_canvas(None, "Board.canvas")
    assert data["problems"] == []
    assert data["canvas"]["nodes"][1]["x"] > 0
    with pytest.raises(ObsidianError):
        service.write_canvas(None, "Board.canvas", ops=[{"op": "add_edge", "edge": {"fromNode": "a",
                                                                                     "toNode": "zzz"}}])
    removed = service.write_canvas(None, "Board.canvas", ops=[{"op": "remove_node", "id": "a"}])
    assert removed["ops"][0]["removed_edges"] == ["e1"]
    assert canvas.validate({"nodes": [{"id": "x", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1,
                                       "text": "t", "color": "9"}]})


def test_bases_structure(service, vault_dir):
    service.write_base(None, "Books", ops=[
        {"op": "set_filters", "filters": {"and": ['file.hasTag("book")']}},
        {"op": "set_formula", "name": "ppu", "expression": "(price / age).toFixed(2)"},
        {"op": "add_view", "view": {"type": "table", "name": "All", "order": ["file.name", "formula.ppu"]}},
    ])
    data = service.read_base(None, "Books.base")
    assert data["definition"]["views"][0]["name"] == "All"
    assert data["definition"]["validation"]["errors"] == []
    with pytest.raises(ObsidianError):
        service.write_base(None, "Books.base", ops=[{"op": "set_filters", "filters": {"xor": []}}])
    assert bases.validate(bases.load("views:\n  - name: x\n"))["errors"]


def test_rename_bare_wikilink_keeps_alias_and_short_form(service, vault_dir):
    service.rename_file(None, "Ideas.md", "Concepts")
    assert "[[Concepts|my ideas]]" in (vault_dir / "Home.md").read_text(encoding="utf-8")
