#!/usr/bin/env bash
# Install obsidian-mcp as a command-line tool (macOS/Linux).
#   ./scripts/install.sh            install from this checkout
#   ./scripts/install.sh --plugin "/path/to/Vault"   also install the bridge plugin into a vault
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
uv tool install --force "$here"
echo "Installed: $(command -v obsidian-mcp || echo '~/.local/bin/obsidian-mcp (add ~/.local/bin to PATH)')"

if [[ "${1:-}" == "--plugin" ]]; then
  vault="${2:?usage: install.sh --plugin /path/to/Vault}"
  dest="$vault/.obsidian/plugins/mcp-bridge"
  mkdir -p "$dest"
  cp "$here/bridge-plugin/manifest.json" "$here/bridge-plugin/main.js" "$dest/"
  echo "Bridge plugin copied to $dest. Enable 'MCP Bridge' in Obsidian → Settings → Community plugins,"
  echo "then run: obsidian-mcp config set bridge_token <token from the plugin settings>"
fi

cat <<'MSG'

Next steps:
  obsidian-mcp vault add "/path/to/Vault" --default
  obsidian-mcp doctor
  Add to your MCP client:  { "command": "obsidian-mcp", "args": ["serve"] }
MSG
