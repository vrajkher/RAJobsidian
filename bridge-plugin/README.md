# MCP Bridge (Obsidian plugin)

A small, desktop-only plugin that lets `obsidian-mcp` use features that need the running app:
the active editor (selection, cursor, undoable edits), open tabs and layout, app and vault events,
and link-aware rename/trash through Obsidian's own `FileManager`.

- Uses only the public Plugin API (`obsidian` 1.14.4 typings). No command execution and no `eval`.
- Listens on `127.0.0.1:27125` and needs a random 256-bit token. Requests with a browser `Origin`
  or a non-localhost `Host` header are rejected.

## Install

```sh
./scripts/install.sh --plugin "/path/to/Vault"     # or copy manifest.json + main.js by hand
```

Enable **MCP Bridge** in Settings → Community plugins, open its settings, click **Copy**, then:

```sh
obsidian-mcp config set bridge_token <token>
```

## Build

```sh
npm ci && npm run build   # type-checks against the official API, bundles main.js
```

## API (JSON over HTTP, `Authorization: Bearer <token>`)

| Route | Purpose |
|---|---|
| `GET /status` | Plugin version, vault name, active file, features |
| `GET /active` | Active file, mode, selection(s), cursor |
| `POST /editor` | `replace_selection` / `replace_range` / `insert_at_cursor` / `set_selection` (409 if `expected` differs) |
| `GET /workspace` | Leaves (type, file, active, pinned) and `getLayout()` |
| `POST /workspace/open` | Open a file (new tab/split/window, optional line) |
| `POST /file/rename` | `FileManager.renameFile` (updates links per user settings) |
| `POST /file/trash` | `FileManager.trashFile` (respects the "Deleted files" setting) |
| `POST /frontmatter` | `FileManager.processFrontMatter` |
| `GET /metadata?path=` | `MetadataCache.getFileCache` + resolved/unresolved links |
| `POST /link` | `generateMarkdownLink` using the user's link format |
| `GET /events?since=&timeout=` | Long-poll of workspace/vault/metadata events (500-event ring buffer) |
