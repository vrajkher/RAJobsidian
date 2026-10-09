# Verification

## Verified in this repository (automated)

Run everything with:

```sh
uv sync && uv run pytest -q                  # 97 tests
node app/test/e2e.mjs                        # browser e2e (Chromium via Playwright)
```

| Area | Evidence |
|---|---|
| Path isolation: `..`, absolute paths, NUL, drive letters, symlink escapes, hidden folders, resource-URI traversal | `tests/test_vault_safety.py`, `test_resource_traversal_rejected` |
| Content preservation: Unicode (Gujarati/Hindi), CRLF, BOM, frontmatter comments/order, untouched body | `test_unicode_and_crlf_preserved`, `test_set_properties_keeps_body`, `tests/test_markdown.py` |
| Binary attachments byte-exact; MIME types | `test_binary_attachment_roundtrip`, resource blob test |
| ETag conflicts, 8-thread conditional-write race (exactly one wins) | `test_etag_conflict_detected`, `test_concurrent_conditional_writes_one_wins` |
| Backups, rollback, rollback refusal after later edits, atomic batches | `test_backup_and_rollback`, `test_rollback_refuses_after_later_edit`, `test_batch_*` |
| Link-preserving rename/move (wikilinks, aliases, Markdown links, folders) + rollback | `test_rename_*`, `test_move_folder_updates_links` |
| Search operators, Unicode search, tags, properties, tasks, templates, Moment tokens, daily/unique notes | `tests/test_knowledge.py` |
| Canvas validation/editing, Bases structure | `test_canvas_validate_and_edit`, `test_bases_structure` |
| Every documented CLI command has a risk class; typed wrappers match the official CLI docs; escaping; permission gates; confirmation tokens | `tests/test_adapters.py` |
| Headless: credential refusal, unsupported commands, publish dry-run → confirmation, desktop-Sync conflict guard | `tests/test_adapters.py` |
| Bridge plugin HTTP contract (built `main.js`, mocked Obsidian runtime) incl. bad token, Origin, Host | `tests/test_bridge_contract.py` |
| MCP protocol: tools/annotations/entrypoint metadata, settings capability + round trip, resources with ETag & representation, mentions, file-entrypoint authorization, prompts | `tests/test_server_e2e.py` |
| Resource update notifications: legacy `resources/subscribe` and 2026 `subscriptions/listen`, incl. external edits | `test_resource_update_notifications_delivered`, `test_listen_stream_receives_resource_updates` |
| Extended forms over MRTR: resource picker with previews, option descriptions, suggestions, thumbnails, rejection of unoffered selections, no-forms fallback | `test_choose_*` |
| MCP App in a real browser through the official `AppBridge` host: rendering, search, viewer, backlinks, model context (incl. background context), `ui/message`, task toggle written to disk, deep link | `app/test/e2e.mjs` |
| stdio launch and Streamable HTTP (401 without token, success with bearer) | manual smoke script, 2026-10-09 (stdio read of a Gujarati note; HTTP search) |
| Wheel contains catalog and app; `obsidian-mcp` runs in a clean venv | manual, 2026-10-09 |

## Not verified here: needs your machine or accounts

These are marked 🟡 in the capability matrix. Please run them and report results:

1. **Obsidian CLI** (Obsidian 1.12.7+ installer, CLI enabled, app open):
   `obsidian-mcp doctor --probe` → `cli.reachable: true`. Then ask your assistant to run `native_search`, `query_base` on a `.base` file, `file_history` on a note, `list_commands`, and `workspace action=tabs`.
2. **Bridge plugin inside Obsidian**: install it (`scripts/install.sh --plugin <vault>`), set the token, then try `editor_state`, select text and run `editor_edit replace_selection` (Ctrl/Cmd+Z should undo it), `rename_file adapter=bridge`, and `app_events timeout_seconds=10` while switching notes.
3. **Sync / Publish** (subscriptions): `sync_status`, `publish_status`. After enabling `publish`: `publish_files dry`, which needs confirmation.
4. **Headless** (Node 22+, `npm i -g obsidian-headless`, `ob login` yourself): `headless command=sync-list-remote`, `headless command=publish flags=["dry-run"]`.
5. **ChatGPT host**: install `plugin/` as a local plugin, run onboarding, and open the sidebar app, the thread tray, a `.md` file (desktop), deep links, settings, @-mentions, `choose_notes`, and host-resource writes with conflict handling.
6. **Windows/macOS**: CI runs the Python suite on all three OSes. POSIX-only tests (fake shell binaries, symlinks) are skipped on Windows.
