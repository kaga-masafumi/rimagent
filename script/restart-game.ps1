# Relaunch through Steam so Workshop Harmony loads. Unsaved progress is lost.
[CmdletBinding()]
param([int]$StopTimeoutSeconds = 10, [int]$LaunchTimeoutSeconds = 30)
$ErrorActionPreference = 'Stop'

$gameProcesses = @(Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue)
if ($gameProcesses.Count -gt 0) {
    Write-Host "Stopping RimWorld pid(s): $($gameProcesses.Id -join ', ')"
    $gameProcesses | Stop-Process -ErrorAction Stop
    $stopDeadline = [DateTime]::UtcNow.AddSeconds($StopTimeoutSeconds)
    while ((Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $stopDeadline) {
        Start-Sleep -Milliseconds 250
    }
    $remaining = @(Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) {
        $remaining | Stop-Process -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 500
        if (Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue) {
            throw 'RimWorld could not be stopped. No replacement instance was launched.'
        }
    }
}
Start-Process 'steam://rungameid/294100' -WindowStyle Hidden -ErrorAction Stop
$launchDeadline = [DateTime]::UtcNow.AddSeconds($LaunchTimeoutSeconds)
while ([DateTime]::UtcNow -lt $launchDeadline) {
    if (Get-Process -Name 'RimWorldWin64' -ErrorAction SilentlyContinue) {
        Write-Host 'RimWorld relaunched via Steam; wait for RimBridge to finish loading.'
        return
    }
    Start-Sleep -Milliseconds 500
}
throw 'Steam did not launch RimWorld within the timeout. Check Steam and the game installation.'
