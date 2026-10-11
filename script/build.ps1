# Build bridge first, then Steward. Optionally test and install into a supplied Mods directory.
[CmdletBinding()]
param(
    [string]$Dotnet,
    [string]$Configuration = 'Release',
    [switch]$Test,
    [string]$ModsPath,
    [ValidateSet('Junction', 'Copy')][string]$InstallMode = 'Junction'
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$localDotnet = Join-Path $repoRoot 'runs/dotnet/dotnet.exe'
if (-not $Dotnet) {
    if (Test-Path -LiteralPath $localDotnet) { $Dotnet = $localDotnet }
    else { $Dotnet = (Get-Command dotnet -ErrorAction Stop).Source }
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'mod/Source/RimBridge.csproj'))) {
    throw 'RimBridge source missing. Run git submodule update --init --recursive first.'
}
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
if (Test-Path -LiteralPath $Dotnet) { $env:DOTNET_ROOT = Split-Path -Parent $Dotnet }
Push-Location $repoRoot
try {
    foreach ($project in @('mod/Source/RimBridge.csproj', 'mod-steward/Source/RimBridgeSteward.csproj')) {
        & $Dotnet build $project -c $Configuration --nologo -v minimal
        if ($LASTEXITCODE -ne 0) { throw "Build failed: $project (exit $LASTEXITCODE)" }
    }
    if ($Test) {
        foreach ($project in @('mod/Tests/RimBridge.Tests.csproj', 'mod-steward/Tests/RimBridgeSteward.Tests.csproj')) {
            & $Dotnet test $project --nologo -v minimal
            if ($LASTEXITCODE -ne 0) { throw "Tests failed: $project (install .NET 10 SDK)" }
        }
    }
    if ($ModsPath) {
        $modsRoot = (Resolve-Path -LiteralPath $ModsPath -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $modsRoot -PathType Container)) { throw 'ModsPath must be an existing directory.' }
        foreach ($entry in @(@{Source='mod'; Name='RimBridge'}, @{Source='mod-steward'; Name='RimBridgeSteward'})) {
            $source = (Resolve-Path -LiteralPath (Join-Path $repoRoot $entry.Source)).Path
            $destination = Join-Path $modsRoot $entry.Name
            if (Test-Path -LiteralPath $destination) {
                $existing = Get-Item -LiteralPath $destination -Force
                if ($InstallMode -eq 'Junction' -and $existing.LinkType -eq 'Junction' -and $existing.Target -contains $source) {
                    Write-Host "Already linked: $destination"
                    continue
                }
                throw "Destination already exists: $destination. Inspect it before replacing or copying anything."
            }
            if ($InstallMode -eq 'Junction') {
                New-Item -ItemType Junction -Path $destination -Target $source | Out-Null
            } else {
                New-Item -ItemType Directory -Path $destination | Out-Null
                foreach ($part in @('About', '1.6')) {
                    Copy-Item -LiteralPath (Join-Path $source $part) -Destination $destination -Recurse -ErrorAction Stop
                }
            }
            Write-Host "Installed $($entry.Name): $destination ($InstallMode)"
        }
    }
    Write-Host 'Build complete. Enable Harmony, RimBridge, then RimBridge: Steward in RimWorld; restart via Steam.'
} finally { Pop-Location }
