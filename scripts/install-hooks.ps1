<#
.SYNOPSIS
  Install the ossbot pre-commit hook in this clone (DESIGN 18.5, AC 17).
.DESCRIPTION
  Downloads the pinned gitleaks Windows binary (config/versions.lock.yaml gitleaks.windows_url) into
  tools\bin\gitleaks.exe (gitignored), verifies its sha256 against gitleaks.windows_sha256 before
  anything is extracted, and sets this clone's core.hooksPath to .githooks. The hook runs gitleaks on
  the staged changes and `ossctl lint personal` through the distro. Idempotent: a verified binary and
  the right hooksPath print "unchanged". Touches only this clone (its .git/config and tools\bin).
.PARAMETER SkipDownload
  Only set core.hooksPath (the hook then falls back to a gitleaks on PATH or the distro's).
#>
[CmdletBinding()]
param([switch]$SkipDownload)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # PowerShell 5.1's download progress bar slows Invoke-WebRequest a lot
. (Join-Path $PSScriptRoot 'lib\common.ps1')
$repo = Get-OssRepoRoot

function Get-LockValue([string]$Section, [string]$Key) {
    # versions.lock.yaml is plain 2-space nested scalars (scripts/lib/common.sh lock_get reads it the same way)
    $inSection = $false
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $repo 'config\versions.lock.yaml'))) {
        if ($line -match '^([A-Za-z0-9_]+):\s*$') { $inSection = ($Matches[1] -eq $Section); continue }
        if ($inSection -and $line -match ('^  ' + [regex]::Escape($Key) + ':\s*"?([^"#]+?)"?\s*$')) { return $Matches[1] }
    }
    throw "versions.lock.yaml has no $Section.$Key"
}

if (-not $SkipDownload) {
    $url = Get-LockValue 'gitleaks' 'windows_url'
    $want = (Get-LockValue 'gitleaks' 'windows_sha256').ToLowerInvariant()
    $bin = Join-Path $repo 'tools\bin'
    $exe = Join-Path $bin 'gitleaks.exe'
    $stamp = Join-Path $bin 'gitleaks.exe.zip-sha256'
    $current = (Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $stamp) -and
        ([IO.File]::ReadAllText($stamp).Trim() -eq $want)
    if ($current) {
        Write-OssStep 'gitleaks' 'unchanged' $exe
    } else {
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        $zip = Join-Path $bin 'gitleaks-download.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
        $got = (Get-OssSha256Hex -Bytes ([IO.File]::ReadAllBytes($zip))).ToLowerInvariant()
        if ($got -ne $want) {
            Remove-Item -LiteralPath $zip -Force
            Write-OssStep 'gitleaks' 'FAIL' "sha256 mismatch for $url (got $got, want $want); nothing installed"
            exit 1
        }
        $tmp = Join-Path $bin 'gitleaks-extract'
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp
        Copy-Item -LiteralPath (Join-Path $tmp 'gitleaks.exe') -Destination $exe -Force
        Remove-Item -LiteralPath $tmp -Recurse -Force
        Remove-Item -LiteralPath $zip -Force
        [IO.File]::WriteAllText($stamp, $want)
        Write-OssStep 'gitleaks' 'changed' "$exe (zip sha256 verified)"
    }
}

$hooksPath = (& git -C $repo config --local --get core.hooksPath) 2>$null
if ($hooksPath -eq '.githooks') {
    Write-OssStep 'hooks' 'unchanged' 'core.hooksPath=.githooks'
} else {
    & git -C $repo config --local core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) { Write-OssStep 'hooks' 'FAIL' 'git config core.hooksPath failed'; exit 1 }
    Write-OssStep 'hooks' 'changed' 'core.hooksPath=.githooks'
}
exit 0
