<#
.SYNOPSIS
  Restore an ossctl backup archive (DESIGN 18.8, ADR-0016): `ossctl restore` as root.
.DESCRIPTION
  Asks for "Type RESTORE", then streams the age identity file over stdin to
  `ossctl restore <archive> --identity - --yes` (the identity is read on Windows and never written to
  the distro's disk). ossctl restore kills ossbot (reason restore), decrypts and verifies the archive
  (manifest hashes, known format), moves the current state aside to /srv/ossbot/restore-previous-<ts>/
  (never deletes), installs the archive with its owners and modes, re-applies chattr +a on the audit
  anchors, runs the migrations and the audit check, forces dry-run ON, invalidates every open
  approval and stays killed: then run scripts\ossctl.ps1 doctor and /resume on Telegram.
  The archive must lie inside the repo folder (the distro sees nothing else), e.g. backups\.
  A passphrase-protected identity needs a terminal for age's prompt, which this stdin stream does not
  give: use an unprotected copy of the identity for the restore (and delete it afterwards).
.PARAMETER Archive
  backups\ossbot-<ts>.tar.age, a bare archive name in backups\, or a full path inside the repo.
.PARAMETER Identity
  Path of the age identity file (AGE-SECRET-KEY-...) on Windows, e.g. from your password manager export.
.PARAMETER AssumeYes
  Skip the typed confirmation (tests).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Archive,
    [Parameter(Mandatory)][string]$Identity,
    [switch]$AssumeYes
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$repo = Get-OssRepoRoot
$candidates = @($Archive, (Join-Path $repo $Archive), (Join-Path $repo (Join-Path 'backups' $Archive)))
$full = $null
foreach ($c in $candidates) {
    if ([IO.Path]::IsPathRooted($c) -and (Test-Path -LiteralPath $c -PathType Leaf)) { $full = (Resolve-Path -LiteralPath $c).Path; break }
}
if (-not $full) { Write-Warning "archive not found: $Archive"; exit 2 }
if (-not $full.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning "the archive must be inside $repo (the distro sees only the repo folder)"; exit 2
}
if (-not $full.EndsWith('.tar.age', [StringComparison]::OrdinalIgnoreCase)) { Write-Warning 'not a .tar.age archive'; exit 2 }
$linux = $script:OssLinuxRepo + ($full.Substring($repo.Length) -replace '\\', '/')
if (-not (Test-Path -LiteralPath $Identity -PathType Leaf)) { Write-Warning "identity file not found: $Identity"; exit 2 }
$key = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $Identity).Path)
if ($key -notmatch 'AGE-SECRET-KEY-' -and $key -notmatch 'age-encryption.org') {
    Write-Warning 'that file does not look like an age identity (AGE-SECRET-KEY-...)'; exit 2
}
if (-not $AssumeYes) {
    Write-Host "Restore $full"
    Write-Host 'This kills ossbot, moves the current state aside (not deleted) and installs the archive.'
    $answer = Read-Host 'Type RESTORE to continue'
    if ($answer -ne 'RESTORE') { Write-Host 'not confirmed'; exit 1 }
}
if (-not (Test-OssDistroRunning)) {
    $state = Start-OssDistro
    if ($state -eq 'timeout') { Write-Warning "the $($script:OssDistro) distro did not finish booting"; exit 1 }
}
$wslArgs = @('-d', $script:OssDistro, '-u', 'root', '--exec', '/opt/ossbot/app/.venv/bin/ossctl', 'restore', $linux, '--identity', '-', '--yes')
$code = (Invoke-OssStdin -Text $key -WslArgs $wslArgs).Code
$key = $null
exit $code
