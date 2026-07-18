[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$logRoot = Join-Path $env:USERPROFILE ".atlas-logs"
$logPath = Join-Path $logRoot "due-alerts.log"

function Write-AtlasRuntimeLog {
    param([string[]]$Lines)

    try {
        New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
        $timestamp = [DateTime]::UtcNow.ToString("o")
        @("[$timestamp]", $Lines) | Out-File -LiteralPath $logPath -Append -Encoding utf8
        return $true
    }
    catch {
        return $false
    }
}

try {
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
        throw "ATLAS requires Python 3.12 at the configured local runtime path"
    }

    $output = & $python (Join-Path $workspace "atlas.py") alerts --process-due --json 2>&1
    $exitCode = $LASTEXITCODE
}
catch {
    $output = @(
        (@{
            status = "error"
            action = "process_due_alerts"
            reason = "runtime_wrapper_failed"
            detail = $_.Exception.GetType().Name
        } | ConvertTo-Json -Compress)
    )
    $exitCode = 2
}

$lines = @($output | ForEach-Object { $_.ToString() })
if (-not (Write-AtlasRuntimeLog -Lines $lines)) {
    (@{
        status = "error"
        action = "process_due_alerts"
        reason = "runtime_log_write_failed"
    } | ConvertTo-Json -Compress) | Write-Output
    exit 2
}
$lines | Write-Output
exit $exitCode
