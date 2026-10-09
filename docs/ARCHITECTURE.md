# Architecture

```
 ChatGPT / Codex / Claude / any MCP client
        │  stdio  or  Streamable HTTP (bearer token)
        ▼
 ┌───────────────────────── obsidian_mcp.server ─────────────────────────┐
 │ MCPServer: 94 tools · resources (obsidian://vault/{id}/{+path}) ·     │
 │ prompts · subscriptions (legacy subscribe + 2026 subscriptions/listen)│
 │ Extensions: MCP Apps (ui://obsidian/app.html) · OpenAI settings ·     │
 │ mentions · entrypoints · extended forms (MRTR)                        │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌──────────────────────── obsidian_mcp.service ─────────────────────────┐
 │ ObsidianService: one API, adapter selection, permission policy,       │
 │ confirmation tokens, previews/diffs, batch + rollback                 │
 └──────┬──────────────┬──────────────────┬──────────────────┬──────────┘
        ▼              ▼                  ▼                  ▼
  Filesystem       Obsidian CLI       Bridge plugin       Obsidian Headless
  vault.py         adapters/cli.py    adapters/bridge.py  adapters/headless.py
  index.py         (catalog-checked)  (127.0.0.1+token)   (`ob`, no secrets)
  markdown.py
  search.py edits.py canvas.py bases.py templates.py community.py
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Config at `$OBSIDIAN_MCP_HOME/config.json` (0600, atomic): vaults, permissions, adapter paths |
| `registry.py` | Vault discovery (Obsidian's vault list, read-only) and explicit registration |
| `vault.py` | Path confinement, atomic writes, ETags, newline/BOM preservation, backups, `.trash`, op log, rollback |
| `markdown.py` | Frontmatter (ruamel round-trip), headings, block IDs, links, embeds, tags, tasks, footnotes, callouts; masking of code, math, and comments |
| `index.py` | Incremental metadata index, link resolution, backlinks, graph, tags, properties |
| `search.py` | Local query language, snippets, paging, TF-IDF related notes, duplicates |
| `edits.py` | Pure text transforms: patch ops, link rewriting, Note composer merge/extract |
| `canvas.py` / `bases.py` | JSON Canvas 1.0 validation and edits; Bases structural validation and edits (no evaluation) |
| `templates.py` | Moment.js formatting, template variables, daily/unique note settings |
| `community.py` | Installed plugin discovery and the adapter registry (Dataview, Tasks, Templater, Periodic Notes) |
| `policy.py` | Risk class for every documented CLI command; permission checks; one-time confirmation tokens |
| `forms.py` | OpenAI extended form schemas, validated with OpenAI's form protocol package |
| `server.py` | MCP surface, OpenAI extensions, resource read with ETag/representation, subscriptions |
| `app/` (TypeScript) | MCP App UI bundled to `src/obsidian_mcp/app/index.html` |
| `bridge-plugin/` (TypeScript) | Obsidian plugin exposing public Plugin API features on localhost |

## Key decisions

1. **The filesystem is the baseline.** Every read and edit works without the app, so behavior is deterministic and testable. App-only features are added on top through official interfaces.
2. **Only documented interfaces.** CLI calls are validated against a catalog generated from the official help page (`scripts/gen_cli_catalog.py`). The bridge uses only `obsidian.d.ts` public API. Commands are run through the CLI because `app.commands` is private.
3. **No guessed semantics.** Bases filters and formulas are never evaluated locally; results come from `base:query`. Unique-note defaults and config-file layouts that are not documented are labelled as assumptions.
4. **Two-layer safety.** Permission flags are user-owned: they are not exposed through MCP settings tools, so a model cannot enable them. On top of that, irreversible or public actions need a one-time `confirm_token` tied to the exact arguments.
5. **Note content is data.** The server never interprets note text as instructions. Server instructions and skills tell models the same.
6. **Previews everywhere.** Most edits accept `dry_run` and return unified diffs. Composite operations (move with link updates, merge, extract, batch) log one operation that can be rolled back.

## Data flow: link-preserving rename (filesystem)

1. Force-refresh the index and compute the old→new path mapping (folders map every child).
2. Plan link rewrites for every note whose links resolve to a moved path, keeping wikilink vs. Markdown style, relative vs. absolute paths, subpaths, and aliases.
3. `dry_run` returns the per-file diffs. Otherwise, under the vault lock: move, write the rewritten notes atomically, and log a single `move_with_links` operation.
4. Invalidate the index and publish resource-updated notifications.

## Security model

- Paths are normalized and resolved; anything outside the vault root (including through symlinks) is rejected. Hidden folders are not content.
- HTTP transport requires a bearer token (a random one is generated if unset). `--no-auth` is allowed only on localhost. For multiple users, OAuth mode validates tokens from your authorization server by introspection. Per-user vault allow-lists are enforced in `ObsidianService.vault()`, the single choke point for every tool and resource read.
- The bridge binds 127.0.0.1, requires a 256-bit token, and rejects browser `Origin` headers and non-localhost `Host` headers (DNS rebinding).
- Headless never receives credentials. `ob login` is interactive in the user's terminal.
- Host file paths from OpenAI file entrypoints grant vault features only when they fall inside a connected vault, and never inside hidden folders.
