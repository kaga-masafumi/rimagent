# Start human-directed mode against a colony already loaded by the human.
[CmdletBinding()]
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Command uv -ErrorAction Stop | Out-Null
Push-Location (Join-Path $repoRoot 'agent')
try {
    $assistArgs = @('run','--frozen','rimagent','assist')
    if ($NoBrowser) { $assistArgs += '--no-browser' }
    $logPath = Join-Path $repoRoot ('runs/assist-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Write-Host 'Load a colony in RimWorld first. Enable Run in background.'
    Write-Host "Assist log: $logPath"
    & uv @assistArgs 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) { throw "Assist exited with code $LASTEXITCODE" }
} finally { Pop-Location }
