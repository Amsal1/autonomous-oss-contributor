<#
.SYNOPSIS
  One-screen ossbot state (CR-001 5, DESIGN 13.5): distro, control (kill), lifecycle, dry-run, egress
  mode, Hermes tier, running work, pending approvals, outbox, Telegram last poll and reconnects, last
  shutdown type, the Windows tasks (autostart, shutdown blocker, shutdown hook) and the kill marker.
.DESCRIPTION
  Never starts the distro: when it is stopped, the in-distro fields say so and only the Windows-side
  facts are filled in (kill marker, tasks, last lifecycle log line). Otherwise runs `ossctl status` as
  root and prints its JSON as one screen. -Json prints the raw ossctl JSON instead.
.PARAMETER StatusJsonFile
  Tests only: render this file as the `ossctl status` JSON instead of asking the distro.
#>
[CmdletBinding()]
param([switch]$Json, [string]$StatusJsonFile = '')
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$repo = Get-OssRepoRoot
$data = $null
$raw = @()
if ($StatusJsonFile) {
    $running = $true
    $data = [IO.File]::ReadAllText($StatusJsonFile) | ConvertFrom-Json
} else {
    $running = Test-OssDistroRunning
}
if ($running -and -not $StatusJsonFile) {
    $r = Invoke-OssWsl -User root -Command @('/opt/ossbot/app/.venv/bin/ossctl', 'status')
    $raw = $r.Out
    if ($Json) { $raw | ForEach-Object { Write-Host $_ }; exit $r.Code }
    $text = ($raw -join "`n")
    $start = $text.IndexOf('{')
    if ($start -ge 0) { try { $data = $text.Substring($start) | ConvertFrom-Json } catch { $data = $null } }
}

function Field([object]$Obj, [string]$Name) {
    if ($null -eq $Obj) { return $null }
    $p = $Obj.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}
function Show([string]$Label, [string]$Value) { Write-Host ('{0,-18} {1}' -f $Label, $Value) }
$na = if ($running) { 'unknown (ossctl status gave no data)' } else { '- (distro stopped)' }
function Or([object]$Value) { if ($null -eq $Value -or "$Value" -eq '') { return $na }; return "$Value" }

Write-Host ('ossbot status at {0}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
Show 'distro' $(if ($running) { "$($script:OssDistro) running" } else { "$($script:OssDistro) stopped (scripts\up.ps1 starts it)" })
if ($running -and $null -eq $data) { $raw | Select-Object -First 5 | ForEach-Object { Write-Host "  $_" } }
$gkDown = $null -ne (Field $data 'killed') -or $null -ne (Field $data 'lifecycle_flag') -or ($raw -match 'gatekeeper unreachable').Count -gt 0

# control: running / paused / killed (level, reason)
$state = Field $data 'state'
if ($null -eq $state -and $gkDown) {
    $k = Field $data 'killed'; $pz = Field $data 'paused'
    $state = if ($k) { 'killed' } elseif ($pz) { 'paused' } else { 'running' }
}
$ctl = Or $state
if ($state -eq 'killed') {
    $level = Field $data 'kill_level'; if (-not $level) { $level = Field (Field $data 'killed') 'level' }
    $reason = Field $data 'reason'; if (-not $reason) { $reason = Field (Field $data 'killed') 'reason' }
    $ctl = "killed (level $level, $reason)"
} elseif ($state -eq 'paused') {
    $reason = Field $data 'reason'; if (-not $reason) { $reason = Field (Field $data 'paused') 'reason' }
    $ctl = "paused ($reason)"
}
if ($gkDown) { $ctl += ' [gatekeeper unreachable: flags only]' }
Show 'control' $ctl

$lc = Field $data 'lifecycle'
$scope = $null
if ($null -eq $lc) { $flag = Field $data 'lifecycle_flag'; if ($flag -is [string]) { $lc = $flag } elseif ($flag) { $lc = Field $flag 'state'; $scope = Field $flag 'scope' } }
else { $scope = Field $data 'scope' }
$since = Field $data 'lifecycle_since'
$lcText = Or $lc
if ($lc -and $lc -ne 'up') { $lcText = "$lc (scope $scope$(if ($since) { ", since $since" }))" }
Show 'lifecycle' $lcText
$dry = Field $data 'dry_run'
Show 'dry-run' $(if ($null -eq $dry) { $na } elseif ($dry) { 'ON' } else { 'OFF' })
Show 'egress mode' (Or (Field $data 'egress_mode'))
Show 'hermes tier' (Or (Field $data 'hermes_tier'))
$stages = Field $data 'running_stages'; $jobs = Field $data 'sandbox_running_jobs'; $exec = Field $data 'executing_actions'
Show 'running work' $(if ($null -eq $stages) { $na } else { "$stages stage(s), $(Or $jobs) sandbox job(s), $exec submission(s) executing" })
Show 'approvals' $(if ($null -eq (Field $data 'awaiting_approval')) { $na } else { "$(Field $data 'awaiting_approval') awaiting" })
Show 'outbox' $(if ($null -eq (Field $data 'outbox_pending')) { $na } else { "$(Field $data 'outbox_pending') pending" })
$tg = Field $data 'telegram'
Show 'telegram poll' $(if ($null -eq $tg) { $na } else {
    $ok = Field $tg 'last_ok_at'; if (-not $ok) { $ok = 'none yet since the gatekeeper started' }
    "last ok $ok, $(Field $tg 'reconnects') reconnect(s), $(Field $tg 'consecutive_failures') consecutive failure(s)" })
$ls = Field $data 'last_shutdown'
Show 'last shutdown' (Format-OssLastShutdown -Record $ls -Missing $na)

# Windows side: tasks, blocker, kill marker, last lifecycle line
$t = Get-OssHostTaskStates
Show 'autostart task' $t.Autostart
Show 'shutdown blocker' $t.Blocker
Show 'shutdown hook' $t.Hook
$marker = Join-Path $repo '.ossbot-local\KILL'
Show 'kill marker' $(if (Test-Path -LiteralPath $marker) { 'PRESENT (killed, level full; scripts\resume.ps1)' } else { 'absent' })
$log = Join-Path $repo '.ossbot-local\lifecycle.log'
Show 'last lifecycle' $(if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 1 } else { '(no lifecycle.log yet)' })
if ($running -and $null -eq $data) { exit 1 }
exit 0
