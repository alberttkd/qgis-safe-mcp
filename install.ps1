<#
.SYNOPSIS
  Instala el plugin QGIS Safe MCP en el perfil de QGIS y registra el servidor MCP en Claude Code.
.PARAMETER Profile
  Perfil de QGIS (por defecto "default").
.PARAMETER QgisMajor
  3 o 4. Por defecto se usa QGIS4 si existe su carpeta de perfiles.
.PARAMETER SkipClaude
  No registrar el servidor en Claude Code.
#>
param(
    [string]$Profile = "default",
    [ValidateSet("", "3", "4")][string]$QgisMajor = "",
    [switch]$SkipClaude
)
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

if (-not $QgisMajor) {
    $QgisMajor = if (Test-Path "$env:APPDATA\QGIS\QGIS4\profiles") { "4" } else { "3" }
}
$plugins = "$env:APPDATA\QGIS\QGIS$QgisMajor\profiles\$Profile\python\plugins"
New-Item -ItemType Directory -Force $plugins | Out-Null

$target = Join-Path $plugins "qgis_safe_mcp"
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
Copy-Item (Join-Path $repo "qgis_safe_mcp") $target -Recurse -Exclude "__pycache__"
Write-Host "Plugin copiado en $target"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "No se encontró 'uv'. Instálalo desde https://docs.astral.sh/uv/ y vuelve a ejecutar."
}
uv --directory "$repo\server" sync --quiet
Write-Host "Dependencias del servidor MCP instaladas"

$server = Join-Path $repo "server"
if (-not $SkipClaude -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    # Reinstalación: quita el registro anterior si existe (si no existe, claude escribe en stderr)
    $ErrorActionPreference = "Continue"
    claude mcp remove qgis-safe --scope user *> $null
    $global:LASTEXITCODE = 0
    $ErrorActionPreference = "Stop"
    # Se usa el lanzador nativo (claude.cmd): si se llama a claude.ps1, PowerShell se come el "--"
    $cli = (Get-Command claude.cmd -ErrorAction SilentlyContinue).Source
    if (-not $cli) { $cli = (Get-Command claude).Source }
    & $cli mcp add qgis-safe --scope user -- uv --directory "$server" run qgis_safe_mcp_server.py
    if ($LASTEXITCODE -ne 0) { throw "No se pudo registrar el servidor en Claude Code" }
    Write-Host "Servidor 'qgis-safe' registrado en Claude Code (ámbito usuario)"
} else {
    Write-Host "Para otros clientes MCP usa: uv --directory `"$server`" run qgis_safe_mcp_server.py"
}

Write-Host ""
Write-Host "Siguiente paso: reinicia QGIS, activa 'QGIS Safe MCP' en Complementos > Administrar e instalar,"
Write-Host "abre el panel desde la barra de herramientas y pulsa 'Iniciar'."
