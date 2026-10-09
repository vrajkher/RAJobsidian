# Capability matrix

Source-backed inventory of Obsidian and OpenAI MCP Extensions features, and how this project covers them.

**Sources (pinned):**
- Obsidian Help: [`obsidianmd/obsidian-help@b95e59d`](https://github.com/obsidianmd/obsidian-help/tree/b95e59dab8b5d94ff06376f1437c8492f14da80d) (2026-10-08). Covers CLI, Headless, Sync, Publish, Bases, core plugins, Templates, and Note composer.
- Plugin API typings: [`obsidianmd/obsidian-api`](https://github.com/obsidianmd/obsidian-api) 1.14.4 (`obsidian.d.ts`)
- JSON Canvas: [spec 1.0](https://jsoncanvas.org/spec/1.0/) (2024-03-11)
- OpenAI MCP Extensions: [`openai/mcp-extensions`](https://github.com/openai/mcp-extensions) `docs/spec.md`, Python SDK `openai-mcp-extensions` 0.1.0, TS SDK `@openai/mcp-extensions` 0.1.0
- MCP Python SDK `mcp` 2.3.0 (protocol 2026-07-28 with legacy 2025-11-25 fallback); MCP Apps `@modelcontextprotocol/ext-apps` 1.7.5

**Status legend**
- ✅ **Done**: implemented and covered by automated tests in this repo.
- 🟡 **Implemented, unverified**: code exists and matches the documented contract (argument validation is tested), but it has not been run against a real Obsidian app, account, or ChatGPT host. See [VERIFICATION.md](VERIFICATION.md).
- 🟠 **Partial**: some of the feature is covered; the limitation says what is missing.
- ⛔ **Unsupported**: no documented interface exists. It is not faked.

**Adapters:** FS = filesystem (Obsidian may be closed) · CLI = official Obsidian CLI (1.12.7+ installer, app running) · Bridge = optional `bridge-plugin/` (public Plugin API) · Headless = `ob` (open beta, Node 22+).

## 1. Vaults, files, attachments

| Feature | Official interface | Adapter | Requirements | Status | Test evidence | Limitations |
|---|---|---|---|---|---|---|
| Vault discovery | Obsidian's `obsidian.json` (undocumented); CLI `vaults` | FS, CLI | – | ✅ | `list_vaults` in e2e | `obsidian.json` is read best-effort and never written |
| Register / select / disconnect vaults, explicit targeting | – | Service | – | ✅ | `conftest`, settings test | Only registered vaults are accessible |
| Connection & capability check | – | All | – | ✅ | `test_capabilities_report` | `probe=true` runs `obsidian version`, which launches Obsidian if it is closed |
| Vault metadata & statistics | CLI `vault` | FS | – | ✅ | `vault_info` | Word count is approximate (see Word count) |
| List files/folders, paging | CLI `files`, `folders` | FS | – | ✅ | `test_hidden_config_is_not_content` | Hidden folders (`.obsidian`, `.trash`, `.git`) are never listed |
| Read / create / write / append / prepend | CLI `read/create/append/prepend` | FS | – | ✅ | `test_vault_safety.py` | `prepend` inserts after frontmatter, matching the CLI |
| Precise patching | – | FS | – | ✅ | `test_patch_ops` | Ops by exact text, regex, heading section, block ID, line range |
| Copy | – | FS | – | ✅ | `test_binary_attachment_roundtrip` | |
| Move / rename with link updates | CLI `move`/`rename`; `FileManager.renameFile` | FS, Bridge, CLI | – | ✅ FS / 🟡 Bridge, CLI | `test_rename_*`, `test_move_folder_updates_links`, bridge contract | FS rewrites wikilinks, Markdown links, embeds, and frontmatter links itself, keeping each link's style. Bridge/CLI follow the user's Obsidian link settings. |
| Trash & recovery | CLI `delete` (trash default) | FS (`.trash`), Bridge (`trashFile`) | – | ✅ | `test_trash_restore_and_permanent_delete_flow` | FS uses the vault `.trash` folder; Bridge follows "Deleted files" (may be system trash, not listable here) |
| Permanent delete | CLI `delete permanent` | FS | `permanent_delete` permission + confirmation | ✅ | same | A backup copy is still kept outside the vault |
| Binary attachments, MIME types (images, PDF, audio, video) | Accepted file formats | FS | – | ✅ | `test_binary_attachment_roundtrip`, resource blob test | Max read size is `max_read_bytes × 5` |
| Attachment location | Settings → "Default location for new attachments" | FS | – | 🟠 | – | Reads `app.json` `attachmentFolderPath` (undocumented key); falls back to the vault root |
| Batch ops with previews, per-item results, atomic rollback | – | FS | – | ✅ | `test_batch_atomic_rolls_back` | |
| Conflict detection (ETags) | – | FS | – | ✅ | `test_etag_conflict_detected`, concurrency test | |
| Atomic writes, backups, rollback | – | FS | – | ✅ | `test_backup_and_rollback`, `test_rollback_refuses_after_later_edit` | Backups live in `~/.config/obsidian-mcp/state`, pruned to `backups_keep` |
| Unicode, Gujarati/Hindi, CRLF, BOM, frontmatter preservation | – | FS | – | ✅ | `test_unicode_and_crlf_preserved`, `test_set_properties_keeps_body` | |

## 2. Notes and knowledge

| Feature | Official interface | Adapter | Requirements | Status | Test evidence | Limitations |
|---|---|---|---|---|---|---|
| Properties / YAML frontmatter (typed) | Properties; CLI `property:*` | FS (ruamel round-trip), Bridge (`processFrontMatter`) | – | ✅ | `test_set_properties_keeps_body` | Types are inferred; Obsidian's `types.json` can be read with `read_app_config` |
| Tags, nested tags, aliases | Tags; CLI `tags`, `aliases` | FS | – | ✅ | `test_tags_properties_and_metadata` | Tags in code, math, and comments are ignored |
| Headings, block IDs, footnotes, callouts, code blocks, math, tables | Formatting syntax pages | FS | – | ✅ | `tests/test_markdown.py` | Parsing is for metadata; no HTML rendering |
| Wikilinks, Markdown links, embeds, subpaths, aliases | Internal links, Embed files | FS | – | ✅ | rename/backlink tests | |
| Full-text search, operators, filters, context, pagination | Search (local subset) | FS | – | ✅ | `test_search_operators_and_pagination` | Supports words, `"phrases"`, OR, `-x`, `/regex/`, `file:`, `path:`, `tag:`, `content:`, `[prop:value]` |
| Native Obsidian search | CLI `search`, `search:context` | CLI | app running | 🟡 | argv tests | Exact app semantics |
| Incremental indexing | – | FS | – | ✅ | implicit in all tests | mtime/size scan, re-parses changed notes only |
| Backlinks, outgoing, unresolved, orphans, dead ends | CLI `backlinks/links/unresolved/orphans/deadends` | FS | – | ✅ | `test_backlinks_unresolved_orphans` | Link resolution approximates Obsidian's rules (same folder, then shortest path) |
| Unlinked mentions | Backlinks pane | FS | – | ✅ | same | Name and alias text match outside links and code |
| Graph data | Graph view | FS | – | ✅ | `graph_data` | Opening the Graph view UI uses `run_command` |
| Merge, extract (Note composer) | Note composer | FS | – | ✅ | `test_merge_and_extract` | Note composer templates are not applied |
| Link insertion, link repair | – | FS | – | ✅ | `test_link_notes_and_repair` | |
| Duplicate detection | – | FS | – | ✅ | `test_duplicates_and_related` | Exact, near (shingles), and same name |
| Semantic search | – | FS + user's embedding endpoint | opt-in (`semantic_search`); local model server by default | ✅ | `tests/test_semantic.py` | Off by default. Any OpenAI-compatible `/embeddings` endpoint (Ollama, LM Studio, llama.cpp, vLLM; remote only with `embedding_allow_remote`). Excludes folders and `ai: false` notes. Incremental cache. `related_notes` (TF-IDF) still works offline. |

## 3. Core plugins

Each plugin is split into native UI control (opening or driving the app's interface) and data processing (working with the files underneath).

| Core plugin | Data processing | Native control | Status | Limitations |
|---|---|---|---|---|
| Audio recorder | `write_attachment`, `read_attachment`, `list_files kinds=[audio]` | `run_command` (record command from `list_commands`) | 🟠 | **Recording needs microphone permission in the app; no documented API.** This server only handles audio files. |
| Backlinks | `backlinks` (+unlinked mentions) | – | ✅ | |
| Bases | `read_base`, `edit_base` (validated structure) | `query_base`, `create_base_item` (CLI) | ✅ structure / 🟡 query | Filters and formulas are **never evaluated locally**; results come from Obsidian |
| Bookmarks | `read_app_config bookmarks.json` | `cli_run bookmarks/bookmark` | 🟠 | `bookmarks.json` is internal; adding bookmarks needs the CLI |
| Canvas | `read_canvas`, `edit_canvas` (JSON Canvas 1.0) | `open_in_obsidian` | ✅ | |
| Command palette | `list_commands` | `run_command` (needs confirmation) | 🟡 | App commands are private API; CLI only |
| Daily notes | `daily_note` (format, folder, template) | `cli_run daily` | ✅ | Settings are read from `daily-notes.json` and fall back to documented defaults |
| File explorer | `list_files`, move/rename/trash | – | ✅ | |
| File recovery | – | `file_history`, `restore_file_version` | 🟡 | Snapshots live inside the app |
| Footnotes view | `note_metadata.footnotes` | – | ✅ | |
| Graph view | `graph_data` | `run_command` | ✅ data | |
| Note composer | `merge_notes`, `extract_note` | – | ✅ | |
| Outgoing links | `outgoing_links` | – | ✅ | |
| Outline | `outline` | – | ✅ | |
| Page preview | `read_note` (with line range) | – | ✅ data | Hover previews are UI-only |
| Properties view | `list_properties`, `set_properties` | – | ✅ | |
| Publish | – | `publish_status`, `publish_files`, Headless `publish` | 🟡 | Needs subscription, the `publish` permission, and confirmation |
| Quick switcher | `search_notes` (`file:`) | `open_in_obsidian` | ✅ data | |
| Random note | `random_note` | `cli_run random` | ✅ | |
| Search | `search_notes` | `native_search` | ✅ / 🟡 | |
| Slash commands | `list_commands` | – | 🟠 | Editor-only UI; commands are listed through the CLI |
| Slides | `slides_outline` (`---` separators) | `run_command` | ✅ data | |
| Sync | – | `sync_status`, `file_history source=sync`, `cli_run sync` | 🟡 | Needs subscription; pause/resume needs `sync_control` |
| Tags view | `list_tags tree=true` | – | ✅ | |
| Templates | `list_templates`, `render_template`, `create_from_template` | `cli_run template:insert` | ✅ | `{{title}}`, `{{date}}`, `{{time}}`, `{{date:FORMAT}}` (Moment tokens) |
| Unique note creator | `unique_note` | `cli_run unique` | ✅ | The default format `YYYYMMDDHHmm` and config file name are **assumed** (not documented) |
| Web viewer | – | `cli_run web url=...` | 🟡 | |
| Word count | `word_count` (approximate) | `cli_run wordcount` (exact) | ✅ / 🟡 | |
| Workspaces | `read_app_config workspaces.json` | `workspace` (save/load/delete/tree/tabs) | 🟡 | |
| Format converter (documented, deprecated-format migration) | – | `run_command` | 🟠 | Vault-wide; back up first |

Optional official plugins:

| Plugin | Coverage | Status | Limitations |
|---|---|---|---|
| Importer | `run_command` for its commands; `write_note` / `write_attachment` for plain Markdown imports | 🟠 | Importer is interactive with no documented automation API |
| Maps (Bases Map view) | `edit_base` accepts `type: map` views; results via `query_base` | 🟠 | Map rendering is UI-only |

## 4. Editor, workspace, customization, developer tools

| Feature | Interface | Adapter | Requirements | Status | Evidence | Limitations |
|---|---|---|---|---|---|---|
| Active note, selection, cursors | `Workspace`, `Editor` (public) | Bridge | bridge plugin | 🟡 | bridge contract test | |
| Text edits in the editor (undoable) | `Editor.replaceRange/Selection` | Bridge | bridge | 🟡 | contract (incl. 409 on stale) | |
| Tabs/panes/layout | `iterateAllLeaves`, `getLayout`; CLI `tabs`, `workspace` | Bridge, CLI | – | 🟡 | contract | No public leaf IDs; leaves are indexed |
| Navigation (open note at line) | `WorkspaceLeaf.openFile`; CLI `open` | Bridge, CLI | – | 🟡 | contract | |
| Saved layouts | CLI `workspace:*` | CLI | Workspaces plugin | 🟡 | argv tests | |
| Workspace & vault events | `Workspace.on`, `Vault.on`, `MetadataCache.on` | Bridge (`app_events` long-poll) | bridge | 🟡 | contract | Ring buffer of 500 events; `missedEvents` flag |
| Commands incl. plugin commands | CLI `commands`, `command` | CLI | `ui_control` + confirm | 🟡 | argv tests | |
| Hotkeys | CLI `hotkeys`, `hotkey`; `hotkeys.json` | CLI, FS | – | 🟡 / ✅ read | | |
| Themes, CSS snippets | CLI `themes/theme:*/snippet*`; `.obsidian/snippets` | CLI, FS | `settings_changes` for writes | ✅ list/write / 🟡 enable | | |
| Settings | `app.json`, `appearance.json` (read-only) | FS | – | 🟠 | | Config files are internal; only the CLI changes documented settings |
| Plugin inventory | CLI `plugins`; `manifest.json` | FS, CLI | – | ✅ | | |
| Plugin install/enable/disable/reload, restricted mode | CLI `plugin:*` | CLI | `plugin_management` + confirm | 🟡 | policy tests | |
| Dev diagnostics: errors, console, screenshot, DOM/CSS, mobile emulation | CLI `dev:*` | CLI | `developer_tools` | 🟡 | policy tests | |
| Arbitrary JS (`eval`, `dev:cdp`) | CLI | CLI | `eval_code` + confirm (off by default) | 🟡 | `test_confirmation_for_eval` | |

## 5. Sync, Publish, Headless, community plugins

| Feature | Interface | Adapter | Requirements | Status | Limitations |
|---|---|---|---|---|---|
| Sync status/history/read/restore/deleted | CLI `sync:*` | CLI | Sync subscription | 🟡 | |
| Headless sync (one-shot), status, config, setup, unlink | `ob sync*` | Headless | `headless` (+`sync_control`); `ob login` done by the user | 🟡 (guards ✅) | Refuses when desktop Sync is on for the vault (documented conflict). No continuous mode. Passwords are never accepted. |
| Publish list/status/add/remove | CLI `publish:*` | CLI | Publish subscription, `publish` + confirm | 🟡 | |
| Headless publish (with dry-run preview), config, site options | `ob publish*` | Headless | as above | 🟡 (guards ✅) | Headless has no single-file unpublish; use CLI `publish:remove` |
| Community adapter registry | manifests + documented plugin syntax | FS | – | ✅ | Unknown plugins are listed with `operations: {}` |
| Dataview | inline fields (local); DQL via documented `api.queryMarkdown` | FS, CLI eval | eval permission for DQL | ✅ fields / 🟡 DQL | |
| Tasks | documented emoji metadata | FS | – | ✅ | Read-only metadata; edits are generic task edits |
| Templater | settings (templates folder); commands | FS, CLI | – | 🟠 | Templater rendering is interactive; this server never evaluates it |
| Periodic Notes | path computation from settings | FS | – | ✅ | |

## 6. OpenAI MCP Extensions

| Extension (spec section) | Implementation | Status | Evidence | Limitations |
|---|---|---|---|---|
| Global sidebar entrypoint (+ quick action) | `open_vault_browser` (`{}` accepted), quick action → `open_daily_note_app` | ✅ server / 🟡 host | e2e meta test, browser e2e | |
| Thread entrypoint | `open_notes_tray` | ✅ / 🟡 | same | |
| File-extension entrypoints | `open_obsidian_file` for `.md`, `.canvas`, `.base` | ✅ / 🟡 | `test_file_entrypoint_*` | Desktop only (per spec). The host path maps to vault features **only** inside a connected vault. |
| App deep links | `/note?path=`, `/search?q=`, `/vault?id=`, `/tasks` via `hostContext["openai/deepLink"]` | ✅ | browser e2e | Not supported on Android (per spec) |
| Structured settings | `settings.read`/`settings.update` with layout and a tool action | ✅ | `test_settings_capability_and_roundtrip` | Risky permissions are deliberately not exposed |
| Plugin onboarding skill | `plugin/skills/setup/SKILL.md` referenced from `plugin.json` | ✅ (file) / 🟡 host | – | |
| Inline & fullscreen display modes | resource `_meta["openai/ui"]` | ✅ | resource test | `pip` not supported by ChatGPT |
| Composer mentions | `search_mentions` tool (`mentions/search`, visibility `app`) | ✅ | mentions test | Desktop only (per spec) |
| Model context (text, resource links, structured, background, titles) | App "Add to chat" / tray | ✅ | browser e2e | Embedded resources and images are supported by the host but not used |
| Context-change notifications | `hostContext["openai/modelContext"]` → tray selection sync | 🟡 | – | |
| UI messages (target active/new) | "Summarize" / "New chat" | ✅ | browser e2e | iOS/Android: active+send only (per spec) |
| Local file opening | "Open file" → `openai/files/open` | 🟡 | – | Desktop only |
| Resource reading, representation selection | `obsidian://vault/{id}/{+path}`; honors `_meta["openai/resource"].representation` | ✅ | resource test | |
| Resource subscriptions & updates | `resources/subscribe` (legacy) + `subscriptions/listen` (2026) + file poller | ✅ | `test_resource_update_notifications_delivered`, `test_listen_stream_receives_resource_updates` | External edits are detected for subscribed or `watch_note`d URIs (2 s poll); the server's own edits are always published |
| Host-resource writes with writable/ETag | App uses `openai/resources/write` with `ifMatch`; handles conflict/too-large | 🟡 | type-checked | Needs a ChatGPT desktop host |
| Extended forms (descriptions, thumbnails, suggestions, resource selection, previews) | `choose_notes` | ✅ | `test_choose_notes_*` | MRTR path tested; the legacy `openai/elicitation/create` path is untested (no client in the SDK) |
| Capability advertisement | `extensions`/legacy `experimental` `openai/settings`; Apps extension | ✅ | settings test | |
| Runtime host detection & fallbacks | App feature-detects every extension; server falls back for forms | ✅ | browser e2e, form fallback test | |

## 7. MCP protocol & safety

| Feature | Status | Evidence |
|---|---|---|
| Tools with schemas, annotations, structured output, stable error codes | ✅ | e2e |
| Resources (static + `{+path}` template), prompts | ✅ | e2e |
| stdio and Streamable HTTP (bearer token, localhost-only `--no-auth`) | ✅ | manual smoke run (see VERIFICATION.md) |
| MRTR (2026-07-28) and legacy elicitation | ✅ / 🟠 | form tests |
| Vault-root confinement, symlink/traversal protection, hidden-folder refusal | ✅ | `test_vault_safety.py`, resource traversal test |
| Concurrency (per-vault locks, ETags), safe retries (idempotent reads) | ✅ | concurrency test |
| Secret redaction (`bridge_token` never echoed; config 0600) | ✅ | `public_dict` |
| Prompt-injection stance (note content is data; permissions not model-changeable) | ✅ design | instructions, settings exclusions |
| Cancellation / timeouts / progress | ✅ | `tests/test_progress.py`: `batch_edit` and `headless` send `notifications/progress`; cancelling stops the work (batch rolls back atomically; the `ob` process is killed). CLI/Headless timeouts. |
| Per-user isolation over HTTP | 🟠 | One process per user (`OBSIDIAN_MCP_HOME`); no multi-tenant OAuth |
