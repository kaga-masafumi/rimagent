[CmdletBinding()]
param([int]$Lines = 200, [string]$LogPath)
$ErrorActionPreference = 'Stop'
if (-not $LogPath) {
    $LogPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'AppData/LocalLow/Ludeon Studios/RimWorld by Ludeon Studios/Player.log'
}
if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) { throw "Player log not found: $LogPath" }
Get-Content -LiteralPath $LogPath -Encoding UTF8 -Tail $Lines -Wait | Select-String -Pattern 'rimbridge|exception|error'
