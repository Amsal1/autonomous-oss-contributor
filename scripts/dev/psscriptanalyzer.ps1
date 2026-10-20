<#
.SYNOPSIS
  PSScriptAnalyzer over every tracked *.ps1 (CI job "psscriptanalyzer"; also runs on Windows PowerShell 5.1).
.DESCRIPTION
  Fetches the pinned PSScriptAnalyzer release nupkg from GitHub (config/versions.lock.yaml
  psscriptanalyzer.url / sha256), verifies its sha256 before extracting it into a module folder under
  -CacheDir (default .ossbot-local/psmodules, gitignored), imports it from there (no PSGallery, no
  NuGet provider, nothing installed system-wide) and fails on any finding of severity Error.
#>
[CmdletBinding()]
param([string]$CacheDir = '')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $CacheDir) { $CacheDir = Join-Path $repo '.ossbot-local/psmodules' }

function Get-LockValue([string]$Section, [string]$Key) {
    $inSection = $false
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $repo 'config/versions.lock.yaml'))) {
        if ($line -match '^([A-Za-z0-9_]+):\s*$') { $inSection = ($Matches[1] -eq $Section); continue }
        if ($inSection -and $line -match ('^  ' + [regex]::Escape($Key) + ':\s*"?([^"#]+?)"?\s*$')) { return $Matches[1] }
    }
    throw "versions.lock.yaml has no $Section.$Key"
}

$version = Get-LockValue 'psscriptanalyzer' 'version'
$url = Get-LockValue 'psscriptanalyzer' 'url'
$want = (Get-LockValue 'psscriptanalyzer' 'sha256').ToLowerInvariant()
$moduleDir = Join-Path $CacheDir "PSScriptAnalyzer/$version"
$manifest = Join-Path $moduleDir 'PSScriptAnalyzer.psd1'
if (-not (Test-Path -LiteralPath $manifest)) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $pkg = Join-Path $CacheDir "PSScriptAnalyzer.$version.nupkg.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $pkg
    $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $pkg).Hash.ToLowerInvariant()
    if ($got -ne $want) { Remove-Item -LiteralPath $pkg -Force; throw "sha256 mismatch for $url (got $got, want $want)" }
    Expand-Archive -LiteralPath $pkg -DestinationPath $moduleDir -Force
    Remove-Item -LiteralPath $pkg -Force
}
Import-Module $manifest -Force

$files = @(& git -C $repo ls-files '*.ps1' | ForEach-Object { Join-Path $repo $_ })
$findings = @()
foreach ($f in $files) { $findings += @(Invoke-ScriptAnalyzer -Path $f -Severity Error) }
foreach ($x in $findings) { Write-Output ('{0}:{1}: {2} {3}' -f $x.ScriptPath, $x.Line, $x.RuleName, $x.Message) }
Write-Output ("PSScriptAnalyzer {0}: {1} files, {2} error(s)" -f $version, $files.Count, $findings.Count)
if ($findings.Count -gt 0) { exit 1 }
exit 0
