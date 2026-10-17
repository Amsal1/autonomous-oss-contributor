<#
.SYNOPSIS
  WSL isolation test (DESIGN 22.8, acceptance criterion 2). Run from Windows in the repo root.
.DESCRIPTION
  Restarts WSL (wsl --shutdown; WSL is dedicated to ossbot), then checks: /mnt/c absent, /mnt/d
  lists only Work, /mnt/d/Work only the repo, no Windows entries on PATH, a PE file on the mount
  fails with "Exec format error", WSLInterop binfmt absent or disabled, default login user is
  ossnobody, nproc 20, ~20 GB RAM, ~8 GB swap, and Windows localhost cannot reach a distro
  127.0.0.1 listener. Exit 0 when every check passes.
.PARAMETER NoRestart
  Skip the wsl --shutdown/restart (faster re-checks; the full test restarts).
#>
[CmdletBinding()]
param([switch]$NoRestart)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')

$repoLinux = $script:OssLinuxRepo
$results = New-Object System.Collections.Generic.List[object]
function Add-Result([string]$Name, [bool]$Ok, [string]$Detail) {
    $results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail })
    $tag = if ($Ok) { 'PASS' } else { 'FAIL' }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    Write-Host ("{0} {1}: {2}" -f $tag, $Name, $Detail) -ForegroundColor $color
}

if (-not $NoRestart) {
    Write-Host 'restarting WSL (wsl --shutdown) ...'
    $state = Restart-OssWsl
    Write-Host "  systemd: $state"
}

# Filesystem view.
$r = Invoke-OssWsl -User root -Command @('test', '-e', '/mnt/c')
Add-Result 'mnt_c_absent' ($r.Code -ne 0) '/mnt/c must not exist'
$r = Invoke-OssWsl -User root -Command @('ls', '-A', '/mnt/d')
$entries = @($r.Out | Where-Object { $_ })
Add-Result 'mnt_d_only_work' (($entries -join ',') -eq 'Work') ("/mnt/d: " + ($entries -join ' '))
$r = Invoke-OssWsl -User root -Command @('ls', '-A', '/mnt/d/Work')
$entries = @($r.Out | Where-Object { $_ })
Add-Result 'mnt_d_work_only_repo' (($entries -join ',') -eq 'autonomous-oss-contributor') ("/mnt/d/Work: " + ($entries -join ' '))
$r = Invoke-OssWsl -User root -Command @('findmnt', '-rn', '-o', 'TARGET,FSTYPE,OPTIONS', '--target', $repoLinux)
Add-Result 'repo_drvfs_mount' (($r.Code -eq 0) -and (($r.Out -join ' ') -match 'uid=1000') -and (($r.Out -join ' ') -match 'gid=1500')) ($r.Out -join ' ')

# PATH and interop.
foreach ($u in @('ossnobody', 'ossadmin', 'root')) {
    $r = Invoke-OssWsl -User $u -Command @('bash', '-lc', 'printf %s $PATH')
    $path = ($r.Out -join '')
    $bad = ($path -split ':') | Where-Object { $_ -match '^/mnt/|WINDOWS|Windows|System32' }
    Add-Result "path_no_windows_$u" (-not $bad) $path
}
$isoDir = New-OssLocalDir 'isolation'
$pe = Join-Path $isoDir 'whoami.exe'
Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\whoami.exe') -Destination $pe -Force
$peLinux = "$repoLinux/.ossbot-local/isolation/whoami.exe"
$r = Invoke-OssWsl -User ossadmin -Command @('bash', '-c', '$1; echo rc=$?', '_', $peLinux)
$text = ($r.Out -join ' ')
Add-Result 'pe_exec_format_error' (($text -match 'Exec format error') -and ($text -notmatch 'rc=0')) $text
$r = Invoke-OssWsl -User root -Command @('bash', '-c', 'f=/proc/sys/fs/binfmt_misc/WSLInterop; if [ -e $f ]; then head -n1 $f; else echo absent; fi')
$text = ($r.Out -join ' ').Trim()
Add-Result 'binfmt_wslinterop' (($text -eq 'absent') -or ($text -eq 'disabled')) $text
$r = Invoke-OssWsl -User root -Command @('bash', '-c', 'command -v cmd.exe powershell.exe explorer.exe || true')
Add-Result 'no_windows_tools' (-not (($r.Out -join '').Trim())) (($r.Out -join ' ').Trim())

# Default login user (plain wsl.exe -d ossbot).
$out = ConvertFrom-OssWslText (& wsl.exe -d $script:OssDistro --exec id -un 2>&1)
Add-Result 'default_user_ossnobody' ((($out -join '').Trim()) -eq 'ossnobody') (($out -join '').Trim())

# Resources (.wslconfig).
$problems = @(Test-OssWslResources)
$n = (Invoke-OssWsl -User root -Command @('nproc')).Out -join ''
$mem = (Invoke-OssWsl -User root -Command @('awk', '/^(MemTotal|SwapTotal):/ {print $1, int($2/1024/1024*10)/10}', '/proc/meminfo')).Out -join ''
Add-Result 'resources' ($problems.Count -eq 0) ("nproc $n; $mem " + ($problems -join '; '))

# Localhost forwarding: a distro listener on 127.0.0.1:18642 must not be reachable from Windows.
$port = 18642
$listener = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $script:OssDistro, '-u', 'ossadmin', '--exec', 'timeout', '25', 'python3', '-m', 'http.server', '--bind', '127.0.0.1', "$port") -WindowStyle Hidden -PassThru
$inside = $false
for ($i = 0; $i -lt 20 -and -not $inside; $i++) {
    Start-Sleep -Milliseconds 500
    $r = Invoke-OssWsl -User ossadmin -Command @('python3', '-c', "import socket; socket.create_connection(('127.0.0.1', $port), 1).close()")
    $inside = ($r.Code -eq 0)
}
$reached = $false
foreach ($hostName in @('127.0.0.1', 'localhost')) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($hostName, $port)
        if ($task.Wait(3000) -and $client.Connected) { $reached = $true }
    } catch { } finally { $client.Close() }
}
try { if (-not $listener.HasExited) { $listener.Kill() } } catch { }
Add-Result 'localhost_forwarding_blocked' ($inside -and -not $reached) ("listener up inside: $inside; reachable from Windows: $reached")

$failed = @($results | Where-Object { -not $_.Ok })
if ($failed.Count) {
    Write-Host ("test-isolation: FAIL ({0} of {1} checks failed)" -f $failed.Count, $results.Count) -ForegroundColor Red
    exit 1
}
Write-Host ("test-isolation: PASS ({0} checks)" -f $results.Count) -ForegroundColor Green
exit 0
