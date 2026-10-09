"""Obsidian-flavoured Markdown parsing."""

from obsidian_mcp import markdown as md

SAMPLE = """---
title: "Test"   # comment kept
tags: [project, "nested/child"]
related: "[[Other Note]]"
---
# Heading One
Text [[Target#Sec|alias]] ![[image.png]] [md](Folder/Note%20Two.md#x) [ext](https://x.com) [self](#Sub).
#tag-one #નમસ્તે/ઉપ #हिन्दी #123 not#tag `#code [[NotLink]]`
- [ ] task one ^blk-1
- [x] done task
> [!note]+ Callout title
Footnote[^1] and inline^[note]. $x^2$ and $$ [[NoMath]] $$
```python
# not a heading [[NoCode]] #nocode
```
%% [[Hidden]] %%
[^1]: The def.
## Sub
| a | [[Tbl\\|Alias]] |
"""


def test_structure():
    n = md.parse(SAMPLE)
    assert [(h.level, h.text) for h in n.headings] == [(1, "Heading One"), (2, "Sub")]
    assert n.block_ids == {"blk-1": 8}
    assert [(t.status, t.text) for t in n.tasks] == [(" ", "task one ^blk-1"), ("x", "done task")]
    assert n.footnote_refs == ["1"] and n.footnote_defs == {"1": "The def."} and n.inline_footnotes == ["note"]
    assert [(c.type, c.fold, c.title) for c in n.callouts] == [("note", "+", "Callout title")]
    assert n.has_math and n.code_blocks[0]["language"] == "python"


def test_links_skip_code_math_comments():
    n = md.parse(SAMPLE)
    targets = [(lk.target, lk.subpath, lk.alias, lk.embed, lk.kind) for lk in n.links]
    assert ("Target", "#Sec", "alias", False, "wikilink") in targets
    assert ("image.png", None, None, True, "wikilink") in targets
    assert ("Folder/Note Two.md", "#x", "md", False, "markdown") in targets
    assert ("Tbl", None, "Alias", False, "wikilink") in targets
    assert ("Other Note", None, None, False, "frontmatter") in targets
    assert not {"NotLink", "NoMath", "NoCode", "Hidden"} & {t[0] for t in targets}
    assert all(t[0] != "https://x.com" for t in targets)


def test_tags_unicode_and_exclusions():
    tags = md.parse(SAMPLE).tags
    assert tags == ["#project", "#nested/child", "#tag-one", "#નમસ્તે/ઉપ", "#हिन्दी"]


def test_frontmatter_edit_preserves_comments_and_body():
    out = md.update_frontmatter(SAMPLE, {"status": "done"}, ["related"])
    assert '# comment kept' in out and "status: done" in out and "related" not in out
    assert out.split("---\n", 2)[2] == SAMPLE.split("---\n", 2)[2]


def test_invalid_frontmatter_reported_not_raised():
    n = md.parse("---\nkey: [unclosed\n---\nbody\n")
    assert n.frontmatter == {} and n.frontmatter_error


def test_sections():
    assert md.section_range(SAMPLE, "Heading One#Sub")[0] == 17
    assert md.section_range(SAMPLE, "missing") is None
