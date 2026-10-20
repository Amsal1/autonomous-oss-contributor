<#
.SYNOPSIS
  Record one owner live check from docs/FINAL_REPORT.md ("Owner live checks", "Restart test").
.DESCRIPTION
  Sets the named docs/TRACEABILITY.md rows to -Status (done by default), appends "<date> owner check
  <Check>: <Result>" to each row's evidence, recomputes the Totals line, and appends one row to the
  "Owner live checks (after the build)" table at the end of docs/VERIFICATION.md. Edits only those two
  files; commit them yourself. Never put a secret or personal value into -Result.
.EXAMPLE
  powershell -NoProfile -File scripts\dev\record-owner-check.ps1 -Check R1 -Rows CR1-6a,CR1-6b,DES-V-31,DES-AC-32 -Result "restart: reason shown, continued by itself after 14 s; last shutdown graceful"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Check,
    [Parameter(Mandatory)][string[]]$Rows,
    [Parameter(Mandatory)][string]$Result,
    [ValidateSet('done', 'deferred', 'missing')][string]$Status = 'done'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$utf8 = New-Object System.Text.UTF8Encoding $false
$date = Get-Date -Format 'yyyy-MM-dd'
$clean = ($Result -replace '\|', '/').Trim()
$Rows = @($Rows | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$trace = Join-Path $repo 'docs\TRACEABILITY.md'
$lines = [IO.File]::ReadAllLines($trace, $utf8)
$found = @{}
for ($i = 0; $i -lt $lines.Length; $i++) {
    if ($lines[$i] -notmatch '^\| [A-Z0-9]+-') { continue }
    $c = [regex]::Split($lines[$i], '(?<!\\)\|')
    $id = $c[1].Trim()
    if ($Rows -notcontains $id) { continue }
    $c[7] = " $Status "
    $c[8] = ' ' + $c[8].Trim() + "; $date owner check ${Check}: $clean "
    $lines[$i] = $c -join '|'
    $found[$id] = $true
}
$missing = @($Rows | Where-Object { -not $found.ContainsKey($_) })
if ($missing.Count) { throw "rows not found in TRACEABILITY.md: $($missing -join ', ')" }
$statuses = $lines | Where-Object { $_ -match '^\| [A-Z0-9]+-' } | ForEach-Object { ([regex]::Split($_, '(?<!\\)\|'))[7].Trim() }
$totals = 'Totals: ' + (($statuses | Group-Object | Sort-Object Name | ForEach-Object { "$($_.Name) $($_.Count)" }) -join ', ') + ", **$(@($statuses).Count) rows**."
for ($i = 0; $i -lt $lines.Length; $i++) { if ($lines[$i] -like 'Totals: *') { $lines[$i] = $totals; break } }
[IO.File]::WriteAllText($trace, (($lines -join "`n") + "`n"), $utf8)

$ver = Join-Path $repo 'docs\VERIFICATION.md'
$text = [IO.File]::ReadAllText($ver, $utf8)
$header = '## Owner live checks (after the build)'
if (-not $text.Contains($header)) {
    $text = $text.TrimEnd() + "`n`n$header`n`nRecorded with ``scripts/dev/record-owner-check.ps1`` (steps in docs/FINAL_REPORT.md).`n`n| Date | Check | Rows | Result |`n|---|---|---|---|`n"
}
$text = $text.TrimEnd() + "`n| $date | $Check | $($Rows -join ', ') | $clean |`n"
[IO.File]::WriteAllText($ver, $text, $utf8)
"recorded $Check for $($Rows -join ', ') ($Status); $totals"
