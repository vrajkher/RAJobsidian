# Troubleshooting

Errors look like `[CODE] message. Hint: …`. The hint is usually the fix.

| Code | Meaning | Fix |
|---|---|---|
| `NO_VAULT` | No vault connected | `obsidian-mcp vault add <path> --default`, or ask to connect a vault |
| `VAULT_NOT_FOUND` | Unknown vault name/id, or the folder is missing | `obsidian-mcp vault list` |
| `NOT_FOUND` | No such file or note | Search first; note names work without `.md` |
| `INVALID_PATH` / `PATH_OUTSIDE_VAULT` | Absolute path, `..`, hidden folder, or a symlink leaving the vault | Use vault-relative paths like `Folder/Note.md` |
| `CONFLICT` | File changed since you read it (ETag mismatch) | Read it again and reapply the edit |
| `PATCH_FAILED` | Patch text, heading, or block not found, or not unique | Read the note; add context to `find`; check heading names |
| `PERMISSION_DENIED` | Permission is off | `obsidian-mcp config set <permission> true` (only if you intend to) |
| `CONFIRMATION_REQUIRED` | Token expired or arguments changed | Ask again without the token to get a fresh preview |
| `ADAPTER_UNAVAILABLE` | CLI, bridge, or Headless not found or not running | See below |
| `CLI_ERROR` | Obsidian CLI reported an error | Read the message; make sure the right vault is open |
| `TIMEOUT` | CLI or Headless took too long | Is Obsidian showing a dialog? Raise `cli_timeout_seconds` |
| `TOO_LARGE` | File bigger than `max_read_bytes` | Read a line range, or raise the limit |
| `UNSUPPORTED` | No documented interface for this | See the capability matrix for alternatives |

## Obsidian CLI not found

1. Install the Obsidian **1.12.7+ installer** (not just an in-app update).
2. Enable **Settings → General → Command line interface** and accept PATH registration.
3. Restart your terminal **and** the MCP client.
   - macOS: `ls -l /usr/local/bin/obsidian`
   - Linux: `ls -l ~/.local/bin/obsidian` (and add `~/.local/bin` to `PATH`)
   - Windows: `Obsidian.com` next to `Obsidian.exe`
4. Or set the path explicitly: `obsidian-mcp config set cli_path /path/to/obsidian`.
5. Keep Obsidian running. The first CLI call launches it if it is closed.

## Plugin bridge not reachable

- Copy `bridge-plugin/{manifest.json,main.js}` to `<vault>/.obsidian/plugins/mcp-bridge/` and enable **MCP Bridge** in Community plugins.
- Copy the token from the plugin settings: `obsidian-mcp config set bridge_token <token>`.
- Port in use? Change it in the plugin settings, then `obsidian-mcp config set bridge_url http://127.0.0.1:<port>`.

## Headless

- `ob` not found: install Node 22+, then `npm install -g obsidian-headless`.
- "not logged in": run `ob login` yourself. This server never asks for passwords.
- Sync refused with "same device": desktop Sync is enabled for that vault. The Obsidian docs say not to run both on one device.

## Links changed unexpectedly after a rename

Filesystem renames keep each link's style. Rename through the bridge (`adapter: "bridge"`) to use Obsidian's own link settings instead. Every rename is reversible with `rollback_operation`.

## Notes edited while Obsidian is open

Obsidian picks up external file changes automatically. If you were typing in the same note at that moment, the ETag check stops a stale overwrite from this server. Use the bridge's `editor_edit` for edits that should go through the open editor (with undo).
