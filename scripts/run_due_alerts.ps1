[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$logRoot = Join-Path $env:USERPROFILE ".atlas-logs"
$logPath = Join-Path $logRoot "due-alerts.log"

foreach ($name in @(
    "ATLAS_TRUST_ANCHOR_ROOT",
    "ATLAS_TRUST_ANCHOR_NAMESPACE",
    "ATLAS_TRUST_ANCHOR_HMAC_KEY"
)) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$name is not configured in the current user's persistent environment"
    }
    Set-Item -Path "Env:$name" -Value $value
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "ATLAS requires Python 3.12 at $python"
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$output = & $python (Join-Path $workspace "atlas.py") alerts --process-due --json 2>&1
$exitCode = $LASTEXITCODE
$output | Out-File -LiteralPath $logPath -Append -Encoding utf8
$output | Write-Output
exit $exitCode
