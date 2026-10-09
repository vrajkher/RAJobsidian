# Implementation status (checkpoint)

Last updated: 2026-10-09. All seven planned phases have a working implementation. See
[docs/CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md) for per-feature status and
[docs/VERIFICATION.md](docs/VERIFICATION.md) for evidence.

## Done

| Phase | Result |
|---|---|
| 1. Setup & matrix | uv project; CLI catalog generated from official docs (115 commands, `scripts/gen_cli_catalog.py`); capability matrix; CLI coverage table (`scripts/gen_cli_coverage.py`) |
| 2. Vault core | Safe filesystem layer (confinement, atomic writes, ETags, backups, trash, op log, rollback); Markdown parser; incremental index; search; edits; 94-tool MCP server; stdio + HTTP |
| 3. CLI & bridge | Catalog-validated CLI adapter with risk policy; TypeScript bridge plugin (public API only) + contract tests |
| 4. Core plugins etc. | Every current core plugin mapped (data and/or native control); Canvas (JSON Canvas 1.0); Bases (structure + native query); tasks; templates; daily/unique/periodic notes; workspace |
| 5. OpenAI extensions | Global/thread/file entrypoints, quick action, deep links, structured settings, onboarding skill, display modes, mentions, model context, messages, file opening, resource read/representation/subscriptions/writes, extended forms over MRTR; TS app with browser e2e |
| 6. Headless & community | `ob` adapter (Sync/Publish, no credentials, conflict guard); registry for Dataview, Tasks, Templater, Periodic Notes |
| 7. Verification & docs | 97 Python tests + browser e2e; README, user guide, architecture, troubleshooting, verification; install scripts; examples; CI (3 OSes) |

## Known gaps / unresolved

- 🟡 Not yet run against a real Obsidian app, Sync/Publish account, Headless login, or ChatGPT host (see VERIFICATION.md, items 1–5).
- Legacy (pre-2026) `openai/elicitation/create` path in `choose_notes` is untested: the SDK has no client for it.
- HTTP multi-user: one process per user; no OAuth.
- The unique-note config file name/default and the attachment-folder key are undocumented assumptions with fallbacks.

## Next steps (in order)

1. Run VERIFICATION.md items 1–5 locally and record results in this file; fix anything that differs.
2. ~~Add MCP progress notifications for batch edits and Headless runs.~~ Done: progress plus cancellation (batch rollback, `ob` kill).
3. ~~Optional: an embedding provider behind `semantic_search`.~~ Done: OpenAI-compatible endpoint, local by default, explicit remote opt-in, exclusions, incremental cache.
4. Optional: OAuth for remote multi-user deployments.

## How to resume

```sh
uv sync && uv run pytest -q
npm --prefix app ci && npm --prefix bridge-plugin ci
```

Regenerate the source-derived files after Obsidian docs change:

```sh
git clone --depth 1 https://github.com/obsidianmd/obsidian-help /tmp/obsidian-help
uv run python scripts/gen_cli_catalog.py /tmp/obsidian-help
uv run python scripts/gen_cli_coverage.py
```
