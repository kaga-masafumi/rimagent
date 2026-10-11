# Config-only preflight, Steam launch if needed, bridge wait, then agent + dashboard.
[CmdletBinding(PositionalBinding = $false)]
param([ValidateRange(1, 3600)][int]$WaitSeconds = 360, [Parameter(ValueFromRemainingArguments = $true)][string[]]$PlayArgs)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Command uv -ErrorAction Stop | Out-Null
Push-Location (Join-Path $repoRoot 'agent')
try {
    $doctorOutput = & uv run --frozen rimagent doctor --require-watchdog-off --require-budget-active
    if ($LASTEXITCODE -ne 0) { throw 'Configuration preflight failed; the game was not launched.' }
    $doctor = ($doctorOutput -join "`n") | ConvertFrom-Json
    if ($doctor.bridge_source -ne 'present') { throw $doctor.bridge_source }
    $bridgeUrl = $doctor.bridge_url.TrimEnd('/')
    function Test-BridgeAlive {
        try {
            $health = Invoke-RestMethod -Uri "$bridgeUrl/health" -TimeoutSec 2 -ErrorAction Stop
            return ($health.ok -and $health.mainThreadAlive)
        } catch { return $false }
    }
    if (-not (Test-BridgeAlive)) {
        if (-not (Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue)) {
            Start-Process 'steam://rungameid/294100' -WindowStyle Hidden -ErrorAction Stop
        }
        Write-Host "Waiting for RimBridge at $bridgeUrl ..."
        $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
        while (-not (Test-BridgeAlive)) {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw 'RimBridge timeout. Check mod load order, DLLs, port 8765 and Player.log. Access-denied errors may require a loopback URL reservation.'
            }
            Start-Sleep -Seconds 2
        }
    }
    $logPath = Join-Path $repoRoot ('runs/play-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Write-Host "Agent log: $logPath ; dashboard: $($doctor.dashboard_url)"
    & uv run --frozen rimagent play -v @PlayArgs 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) { throw "Agent exited with code $LASTEXITCODE" }
} finally { Pop-Location }
