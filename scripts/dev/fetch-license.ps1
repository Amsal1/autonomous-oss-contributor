<#
.SYNOPSIS
  Download the canonical Apache License 2.0 text to LICENSE (research R7) and print its sha256.
.DESCRIPTION
  Fetches https://www.apache.org/licenses/LICENSE-2.0.txt, normalises line endings to LF (the repo's
  eol rule), writes it byte for byte to LICENSE, then appends the third-party pointer. Prints the
  sha256 of the downloaded text (normalised) and of the final LICENSE file; record both in
  config/versions.lock.yaml (apache_license_2_0).
.EXAMPLE
  powershell -NoProfile -File scripts\dev\fetch-license.ps1
#>
[CmdletBinding()]
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$url = 'https://www.apache.org/licenses/LICENSE-2.0.txt'
$pointer = "`n------------------------------------------------------------------------------`n" +
    "Third-party components: see THIRD_PARTY_NOTICES.md.`n" +
    "Attribution notices that redistributions must keep (section 4(d)): see NOTICE.`n"

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') } finally { $sha.Dispose() }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$resp = Invoke-WebRequest -UseBasicParsing -Uri $url
$raw = $resp.RawContentStream.ToArray()
Write-Output "downloaded bytes: $($raw.Length)"
Write-Output "sha256 as served: $(Get-Sha256Hex $raw)"
$utf8 = New-Object System.Text.UTF8Encoding $false
$text = $utf8.GetString($raw) -replace "`r`n", "`n"
$apache = $utf8.GetBytes($text)
Write-Output "sha256 LF-normalised: $(Get-Sha256Hex $apache)"
$final = $utf8.GetBytes($text + $pointer)
$target = Join-Path $repo 'LICENSE'
if ($CheckOnly) {
    $have = [IO.File]::ReadAllBytes($target)
    $prefix = New-Object byte[] ([Math]::Min($apache.Length, $have.Length))
    [Array]::Copy($have, $prefix, $prefix.Length)
    Write-Output "LICENSE starts with the apache.org text: $((Get-Sha256Hex $prefix) -eq (Get-Sha256Hex $apache))"
    Write-Output "LICENSE sha256: $(Get-Sha256Hex $have)"
    exit 0
}
[IO.File]::WriteAllBytes($target, $final)
Write-Output "LICENSE sha256: $(Get-Sha256Hex $final)"
