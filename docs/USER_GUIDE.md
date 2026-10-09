# User guide

## Install

Requirements: Python 3.11+ ([uv](https://docs.astral.sh/uv/) recommended). Optional: Obsidian 1.12.7+ installer (CLI), Node 22+ (Headless, or building the plugin/app yourself).

```sh
# macOS / Linux
./scripts/install.sh
# Windows (PowerShell)
./scripts/install.ps1
```

The scripts install the `obsidian-mcp` command with `uv tool install`. Alternatively, run it without installing: `uvx --from git+https://github.com/vrajkher/RAJobsidian obsidian-mcp serve`.

## Connect a vault

```sh
obsidian-mcp vault list                       # shows connected + discovered vaults
obsidian-mcp vault add "/path/to/Vault" --default
```

Or ask your assistant: "connect my Obsidian vault at /path/to/Vault".

## Connect a client

**Claude Desktop / Claude Code / Cursor / most MCP clients** (`examples/mcp.json`):

```json
{ "mcpServers": { "obsidian": { "command": "obsidian-mcp", "args": ["serve"] } } }
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.obsidian]
command = "obsidian-mcp"
args = ["serve"]
```

**ChatGPT plugin (Codex / ChatGPT desktop):** the `plugin/` folder is a complete plugin. It contains a manifest, an onboarding skill, a usage skill, an icon, and `.mcp.json`. Install it from a local marketplace as described in [Build plugins](https://developers.openai.com/codex/build-plugins).

**Remote (Streamable HTTP):**

```sh
export OBSIDIAN_MCP_HTTP_TOKEN="$(openssl rand -hex 24)"
obsidian-mcp serve --transport http --host 127.0.0.1 --port 8765
# Client URL: http://127.0.0.1:8765/mcp   Header: Authorization: Bearer $OBSIDIAN_MCP_HTTP_TOKEN
```

To expose it beyond localhost, put it behind HTTPS (for example a reverse proxy).

**Remote multi-user with OAuth.** Use your own OAuth 2.1 authorization server (Keycloak, Auth0, Okta, Entra ID, Authentik…). This server only *validates* tokens, using RFC 7662 introspection, and never issues them. Register it as a resource server, then:

```sh
obsidian-mcp config set oauth_issuer_url https://auth.example.com/realms/notes
obsidian-mcp config set oauth_introspection_url https://auth.example.com/realms/notes/protocol/openid-connect/token/introspect
obsidian-mcp config set oauth_resource_url https://notes.example.com/mcp      # the public URL clients use
obsidian-mcp config set oauth_client_id_env INTROSPECT_CLIENT_ID              # credentials stay in env vars
obsidian-mcp config set oauth_client_secret_env INTROSPECT_CLIENT_SECRET
obsidian-mcp config set oauth_required_scopes mcp
obsidian-mcp config set oauth_write_scope vault:write                         # tokens without it are read-only
obsidian-mcp config set user_vaults '{"alice": ["Work"], "bob": ["Family", "Recipes"], "me": ["*"]}'
obsidian-mcp config set admins me                                             # may connect vaults / change settings
obsidian-mcp serve --transport http --host 0.0.0.0 --port 8765
```

Clients discover the authorization server from `/.well-known/oauth-protected-resource/mcp` and send `Authorization: Bearer <token>`. Users are identified by the token's `sub`. Each user sees only the vaults listed for them, and a vault they may not use looks exactly like one that doesn't exist. Users not listed in `user_vaults` get no vaults. The Obsidian CLI, bridge plugin, Headless, and CSS-snippet tools control the **host computer's** Obsidian app, so in OAuth mode only `admins` can use them. Everyone else works through the filesystem tools on their own vaults.

## Permissions

Defaults: reading, editing, trash, and opening things in Obsidian are **on**. Everything risky is **off**:

| Permission | Enables | Extra confirmation |
|---|---|---|
| `write` | Any change to vault files | – |
| `trash` | Moving to `.trash` | – |
| `permanent_delete` | Deleting without trash | yes |
| `ui_control` | Opening notes/views, running app commands | commands: yes |
| `settings_changes` | Themes, CSS snippets, Headless config | – |
| `plugin_management` | Install/enable/disable plugins, restricted mode | yes |
| `publish` | Publishing/unpublishing (public!) | yes |
| `sync_control` | Pausing/resuming Sync, Headless sync | – |
| `developer_tools` | `dev:*` diagnostics (screenshots, DOM, console) | – |
| `eval_code` | Running JavaScript in Obsidian (`eval`, `dev:cdp`, Dataview DQL) | yes |
| `headless` | Using Obsidian Headless | publish: yes |

Change them yourself on the computer that runs the server:

```sh
obsidian-mcp config set publish true
obsidian-mcp config set write false      # read-only mode (also in ChatGPT settings)
obsidian-mcp config show
```

Models cannot change risky permissions. When an action needs confirmation, the assistant shows a summary and only proceeds after you agree.

## Semantic search (optional)

`search_notes` matches words. `semantic_search` finds notes by meaning, using an embedding model **you** choose. It is off until you enable it:

```sh
# Local and private (recommended): Ollama on this computer
ollama pull nomic-embed-text
obsidian-mcp config set semantic_search true

# Any other OpenAI-compatible server (LM Studio, llama.cpp, vLLM...)
obsidian-mcp config set embedding_url http://127.0.0.1:1234/v1
obsidian-mcp config set embedding_model <model-name>

# A remote service sends note text off this computer, so it needs two explicit steps:
obsidian-mcp config set embedding_url https://api.openai.com/v1
obsidian-mcp config set embedding_model text-embedding-3-small
obsidian-mcp config set embedding_api_key_env OPENAI_API_KEY   # the key stays in your environment
obsidian-mcp config set embedding_allow_remote true
```

Keep notes out of it with `obsidian-mcp config set embedding_exclude "Private,Journal"`, or add `ai: false` to a note's properties. The first search embeds the vault (with progress updates); later searches only embed changed sections. Vectors are cached in `~/.config/obsidian-mcp/state/semantic/`.

## Everyday tasks

| You want to… | Ask, or the tool used |
|---|---|
| Find notes | "find notes tagged #project about pricing" → `search_notes` |
| Read with context | "open Plan and show its backlinks" → `read_note`, `backlinks` |
| Edit one section | "add a bullet under ## Goals in Plan" → `patch_note` (insert under heading) |
| Change properties | "set status: done on these notes" → `set_properties` / `batch_edit` |
| Rename safely | "rename Plan to Roadmap" → `rename_file` (diff preview, links updated) |
| Undo | "undo that" → `rollback_operation` |
| Daily note | "add 'call Sam' to today's note" → `daily_note` + `append_note` |
| Tasks | "what's open this week?" → `list_tasks` (Tasks-plugin dates included) |
| Canvas | "make a canvas of my project notes" → `edit_canvas` |
| Bases | "create a base of books rated ≥ 4" → `edit_base`, results via `query_base` |
| Pick visually | "let me pick which notes" → `choose_notes` (native form in ChatGPT) |

## The ChatGPT app

- **Sidebar → Obsidian** opens the vault browser: search, a note viewer/editor with outline, tags and backlinks, and tasks. The quick action opens today's note.
- **Thread panel → Vault notes**: tick notes to share with the conversation as context.
- **Open a `.md`/`.canvas`/`.base` file** (desktop) to view or edit it in place. Saves are conflict-checked.
- **Deep links**: `…/app/open_vault_browser?path=%2Fnote%3Fpath%3DProjects%252FPlan.md`, `/search?q=...`, `/tasks`.
- **Settings** (plugin page): default vault, read-only mode, page size, backups, opening in Obsidian.
