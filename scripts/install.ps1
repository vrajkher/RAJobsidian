# Install obsidian-mcp as a command-line tool (Windows PowerShell).
#   .\scripts\install.ps1
#   .\scripts\install.ps1 -Vault "C:\Users\me\Vault"   # also install the bridge plugin
param([string]$Vault)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Error "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
}
uv tool install --force $here
if ($Vault) {
  $dest = Join-Path $Vault ".obsidian\plugins\mcp-bridge"
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item (Join-Path $here "bridge-plugin\manifest.json"), (Join-Path $here "bridge-plugin\main.js") $dest
  Write-Host "Bridge plugin copied to $dest. Enable 'MCP Bridge' in Obsidian, then:"
  Write-Host "  obsidian-mcp config set bridge_token <token>"
}
Write-Host "`nNext: obsidian-mcp vault add `"C:\path\to\Vault`" --default; obsidian-mcp doctor"
