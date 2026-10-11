# Stop the agent and pause the game. Does not close RimWorld or discard a save.
[CmdletBinding()]
param(
    [string]$DashboardUrl = 'http://127.0.0.1:8770',
    [string]$BridgeUrl = 'http://127.0.0.1:8765',
    [ValidateRange(1, 300)][int]$WaitSeconds = 30,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$dashboardUri = [Uri]$DashboardUrl
if ($dashboardUri.Host -notin @('localhost','127.0.0.1','::1')) { throw 'DashboardUrl must be loopback.' }
try {
    Invoke-RestMethod -Uri ($BridgeUrl.TrimEnd('/') + '/rpc') -Method Post -ContentType 'application/json' -Body '{"method":"game.pause","params":{"paused":true},"timeout_ms":2000}' -TimeoutSec 5 | Out-Null
} catch { Write-Warning 'Could not pause via RimBridge; pause the game manually.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $dashboardUri.Port -ErrorAction SilentlyContinue)
if (-not $listeners.Count) { Write-Host 'Agent dashboard is already stopped. RimWorld remains open.'; return }
$agentIds = @($listeners.OwningProcess | Sort-Object -Unique)
try {
    $response = Invoke-RestMethod -Uri ($DashboardUrl.TrimEnd('/') + '/api/control') -Method Post -ContentType 'application/json' -Body '{"action":"kill"}' -TimeoutSec 10
    if (-not $response.ok) { throw 'Agent rejected stop request.' }
} catch { Write-Warning 'Graceful stop request failed.' }
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    $alive = @($agentIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if (-not $alive.Count) { Write-Host 'Agent stopped. Save the paused game in RimWorld.'; return }
    Start-Sleep -Milliseconds 250
}
if (-not $Force) { throw 'Agent is still waiting for a request. Retry with -Force to stop only the verified rimagent process; uncertain API usage will need reconciliation.' }
foreach ($agentId in $agentIds) {
    $agentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$agentId" -ErrorAction Stop
    if (-not $agentProcess) { continue }
    if ($agentProcess.CommandLine -notmatch [regex]::Escape($repoRoot) -or $agentProcess.CommandLine -notmatch 'rimagent' -or $agentProcess.CommandLine -notmatch '\bplay\b') {
        throw "Refusing to stop unrelated process $agentId."
    }
    Stop-Process -Id $agentId -ErrorAction Stop
}
Write-Host 'Agent forcibly stopped. RimWorld remains open. Check usage before restarting.'
