# Obsidian MCP

An MCP server for Obsidian vaults. It searches, reads, and safely edits notes, canvases, and bases from ChatGPT, Codex, Claude, or any MCP client. It works with Obsidian closed, and uses the official Obsidian CLI, an optional local plugin, and Obsidian Headless when they are available.

- **94 tools, plus resources and prompts.** Notes, attachments, links, tasks, templates, daily notes, Canvas, Bases, Sync, Publish, workspace, editor, commands, and plugins.
- **Safe by default.** Edits are previewed as diffs, checked against ETags, written atomically, backed up, and reversible. Deleting, publishing, plugin management, and code execution stay off until you enable them.
- **Native ChatGPT experience** through [OpenAI MCP Extensions](https://github.com/openai/mcp-extensions): a sidebar vault browser, a thread notes tray, `.md`/`.canvas`/`.base` file viewers, deep links, structured settings, composer @-mentions, model context, and extended pick-a-note forms.
- **Honest coverage.** See the [capability matrix](docs/CAPABILITY_MATRIX.md), which marks every feature as done, unverified, partial, or unsupported, with test evidence.

## Quick start

```sh
# 1. Install (Python 3.11+; uv recommended)
./scripts/install.sh              # or: uv tool install git+https://github.com/vrajkher/RAJobsidian

# 2. Connect your vault (the folder that contains .obsidian)
obsidian-mcp vault add "~/Documents/My Vault" --default

# 3. Check what works on this machine
obsidian-mcp doctor
```

Then add the server to your client. See [configuration examples](docs/USER_GUIDE.md#connect-a-client):

```json
{ "mcpServers": { "obsidian": { "command": "obsidian-mcp", "args": ["serve"] } } }
```

## How you use it

**Connect vault → Check capabilities → Search or select a note → Choose an action → Preview → Apply → Verify.**

Ask things like:
- "Find my notes about the Q3 roadmap and summarize open tasks."
- "Rename *Plan* to *Roadmap 2026* and fix every link." (A preview is shown first.)
- "Add a `status: done` property to these three notes."
- "Make a canvas linking my project notes."

## Integration paths

| Path | When | Needs |
|---|---|---|
| Filesystem | Always; Obsidian can be closed | A connected vault folder |
| [Obsidian CLI](https://help.obsidian.md/cli) | Native search, Bases queries, file history, Sync/Publish, commands, themes, dev tools | Obsidian 1.12.7+ installer, CLI enabled, app running |
| [Bridge plugin](bridge-plugin/README.md) | Live editor, selection, tabs, events, link-aware rename via Obsidian | Install `bridge-plugin/` in the vault |
| [Obsidian Headless](https://help.obsidian.md/headless) | Sync/Publish without the desktop app | Node 22+, `npm i -g obsidian-headless`, `ob login` (you run it) |

## Documentation

- [User guide](docs/USER_GUIDE.md): install, connect clients, permissions, everyday tasks
- [Capability matrix](docs/CAPABILITY_MATRIX.md) and [CLI coverage](docs/CLI_COVERAGE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Verification](docs/VERIFICATION.md): what was tested here, and what you need to check locally
- [Status & next steps](STATUS.md)

## Development

```sh
uv sync && uv run pytest              # Python server + protocol tests
npm --prefix bridge-plugin ci && npm --prefix bridge-plugin run build
npm --prefix app ci && npm --prefix app run build && node app/test/e2e.mjs   # browser e2e
```

MIT License.
