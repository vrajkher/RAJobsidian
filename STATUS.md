# Implementation status (checkpoint)

Last session: 2026-10-09. Work stopped partway through Phase 2 at the user's request.

## Done
- **Research** (sources pinned):
  - Obsidian help `obsidianmd/obsidian-help@b95e59d`: CLI, Headless, Sync, Publish, Bases, core plugins, Note composer, Templates
  - Plugin API `obsidianmd/obsidian-api` 1.14.4: `app.commands` is **private**, so commands go through the CLI, not the bridge
  - JSON Canvas spec 1.0
  - `openai/mcp-extensions` spec + Python SDK 0.1.0 (needs MCP Python SDK 2.x: `MCPServer`, `mcp.server.apps`)
- `scripts/gen_cli_catalog.py` → `src/obsidian_mcp/data/cli_catalog.json` (115 documented CLI commands with params and flags)
- `errors.py`: stable error codes (subclasses ToolError so messages reach clients)
- `config.py`: config store (0600, atomic) with permissions; risky permissions are off by default
- `policy.py`: risk class for every CLI command, plus one-time confirmation tokens
- `markdown.py`: frontmatter (ruamel round-trip), headings, block IDs, wikilinks/MD links/embeds, tags (incl. Gujarati/Hindi), tasks, footnotes, callouts, code/math masking, sections. Smoke-tested by hand.
- `vault.py`: path confinement, atomic writes, ETag conflicts, newline/BOM preservation, backups outside the vault, `.trash`, restore, op log + rollback. **Untested.**
- `index.py`: incremental index, link resolution, backlinks, unresolved links, orphans, dead ends, unlinked mentions, tags, properties, graph. **Untested.**
- `search.py`: local query syntax, filters, snippets, pagination, TF-IDF related notes, duplicates. **Untested.**
- `templates.py`: Moment.js formatting, template variables, daily/unique/composer settings. **Untested.**

## Not started (next steps, in order)
1. `edits.py`: patch ops (replace / insert under heading / replace section / replace block / line range), link rewriting on move/rename, Note composer merge/extract
2. `tests/` with disposable vaults: path traversal, symlinks, Unicode, CRLF, ETag conflict, rollback, link rewrite
3. `registry.py` (vault discovery + registration), `service.py`, `server.py` (MCPServer tools/resources/prompts, stdio + Streamable HTTP), `__main__.py`
4. Phase 3: `adapters/cli.py` (catalog-validated, no shell), TypeScript bridge plugin (`bridge-plugin/`)
5. Phase 4: `canvas.py`, `bases.py` (structure only; queries via CLI `base:query`), tasks, workspace
6. Phase 5: OpenAI extensions (settings, mentions, global/thread/file entrypoints, resource writes with ETag), TS MCP App UI
7. Phase 6–7: Headless `ob` wrapper, community adapter registry, docs/CAPABILITY_MATRIX.md, install scripts

## Known issues
- `policy.CLI_READ_GATES` is unused; remove it.
- The unique-note config file name `zk-prefixer.json` and its default format are assumed, not documented.
- Nothing has been verified against a real Obsidian install yet.
