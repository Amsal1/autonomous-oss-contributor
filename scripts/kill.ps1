<#
.SYNOPSIS
  Full kill of ossbot from Windows (DESIGN 13.1-13.3).
.DESCRIPTION
  1. Writes the marker .ossbot-local\KILL first, always (the boot gate turns it into a full kill).
  2. If the distro runs: streams scripts/bin/ossbot-kill --full over stdin as root, with a 30 s
     timeout (N8); a wedged distro is terminated with wsl --terminate (the marker keeps it killed).
  3. -Hard: also disables the 'ossbot-autostart' task and stops the whole WSL VM (wsl --shutdown).
  Undo with scripts\resume.ps1.
.PARAMETER TestHangSeconds
  Test hook (tests/system/test_kill.ps1): the in-distro kill sleeps this long first.
#>
[CmdletBinding()]
param(
    [switch]$Hard,
    [string]$Reason = 'kill.ps1',
    [int]$TimeoutSec = 30,
    [int]$TestHangSeconds = 0
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$safeReason = ($Reason -replace '[^A-Za-z0-9 _.:-]', '')
if (-not $safeReason) { $safeReason = 'kill.ps1' }
$marker = Join-Path (New-OssLocalDir) 'KILL'
$doc = [ordered]@{ reason = $safeReason; ts = (Get-Date).ToUniversalTime().ToString('o'); hard = [bool]$Hard }
[IO.File]::WriteAllText($marker, ($doc | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding $false))
Write-Host "kill marker written: $marker"

$exit = 0
if (Test-OssDistroRunning) {
    Set-OssStdinEncoding
    $psi = New-Object System.Diagnostics.ProcessStartInfo 'wsl.exe'
    $argLine = "-d $($script:OssDistro) -u root --exec bash -s -- --full --reason `"$safeReason`" --actor kill.ps1"
    if ($TestHangSeconds -gt 0) { $argLine += " --test-hang $TestHangSeconds" }
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    # Raw UTF-8 bytes: the StreamWriter of StandardInput would prepend a BOM to the script.
    $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes((Get-OssScriptText @('scripts/bin/ossbot-kill')))
    $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $p.StandardInput.BaseStream.Flush()
    $p.StandardInput.Close()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        Write-Warning "in-distro kill did not finish in $TimeoutSec s; terminating the $($script:OssDistro) distro"
        try { $p.Kill() } catch { }
        & wsl.exe --terminate $script:OssDistro | Out-Null
        $exit = 4
    } elseif ($p.ExitCode -ne 0) {
        Write-Warning "ossbot-kill exit $($p.ExitCode) (3 = partial)"
        $exit = $p.ExitCode
    }
} else {
    Write-Host "$($script:OssDistro) distro not running; marker written, the boot gate holds it killed on next start."
}
if ($Hard) {
    if (Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName 'ossbot-autostart' | Out-Null
        Write-Host 'Scheduled Task ossbot-autostart disabled'
    }
    & wsl.exe --shutdown | Out-Null
    Write-Host 'WSL stopped (wsl --shutdown)'
}
exit $exit
