<#
.SYNOPSIS
  Enter the ossbot credentials (DESIGN 18.3, ADR-0010). Nothing is written on Windows.
.DESCRIPTION
  Prompts with Read-Host -AsSecureString, converts to plain text only in memory, and streams
  scripts/setup-secrets.sh plus a NUL-separated record over stdin to
  wsl.exe -d ossbot -u root --exec bash -s -- --stdin. The Linux side validates the formats and
  writes /etc/ossbot/secrets/*.env (ext4, root:<svc> 0640), keeping the seeded internal randoms.
  An empty answer keeps the existing value. The Telegram user id is not a secret: it goes into the
  gitignored config/profile.yaml (telegram.user_id); apply it with `ossctl config reload`.
  -SelfTest runs the Linux-side validation against a temp root only.
#>
[CmdletBinding()]
param([switch]$SelfTest)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$shText = Get-OssScriptText @('scripts/setup-secrets.sh')
if ($SelfTest) {
    $code = Invoke-OssStream -Text $shText -Arguments @('--selftest')
    exit $code
}

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -AsSecureString -Prompt "$Prompt (empty = keep)"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$values = [ordered]@{
    TELEGRAM_BOT_TOKEN   = Read-Secret 'Telegram bot token (BotFather)'
    GITHUB_WRITE_TOKEN   = Read-Secret 'GitHub classic PAT, scope public_repo only (ghp_...)'
    GITHUB_READ_TOKEN    = Read-Secret 'GitHub fine-grained PAT, public repositories read-only (github_pat_...)'
    KIRO_API_KEY_SANDBOX = Read-Secret 'Kiro API key "ossbot-sandbox" (ksk_...)'
    KIRO_API_KEY_HERMES  = Read-Secret 'Kiro API key "ossbot-hermes" (ksk_...)'
    # Optional hook (DESIGN 14.12.7): no reverse image search provider is wired yet; leave it empty.
    REVERSE_IMAGE_SEARCH_KEY = Read-Secret 'Optional: reverse image search provider key (none wired yet)'
}

# Telegram user id -> config/profile.yaml (pre-filled from the existing profile).
$profilePath = Join-Path (Get-OssRepoRoot) 'config\profile.yaml'
if (Test-Path -LiteralPath $profilePath) {
    $text = [IO.File]::ReadAllText($profilePath)
    $current = if ($text -match '(?m)^\s+user_id:\s*(\d+)') { $Matches[1] } else { '0' }
    $uid = Read-Host -Prompt "Telegram user id (numeric) [$current]"
    if ($uid -and $uid -ne $current) {
        if ($uid -notmatch '^\d{1,15}$') { Write-Host 'user id must be numeric'; exit 1 }
        $text = [regex]::Replace($text, '(?m)^(\s+user_id:\s*)\d+', "`${1}$uid")
        [IO.File]::WriteAllText($profilePath, $text, (New-Object System.Text.UTF8Encoding $false))
        Write-Host 'config/profile.yaml telegram.user_id updated; apply with: scripts\ossctl.ps1 config reload'
    }
} else {
    Write-Host 'config/profile.yaml missing: create it from config/profile.example.yaml first.'
}

# Script + record via stdin as raw UTF-8 bytes (no PowerShell newline translation).
$utf8 = New-Object System.Text.UTF8Encoding $false
$ms = New-Object System.IO.MemoryStream
$bytes = $utf8.GetBytes($shText.TrimEnd("`n") + "`n")
$ms.Write($bytes, 0, $bytes.Length)
foreach ($k in $values.Keys) {
    $entry = $utf8.GetBytes("$k=$($values[$k])")
    $ms.Write($entry, 0, $entry.Length)
    $ms.WriteByte(0)
}
$values.Clear()
Set-OssStdinEncoding
$psi = New-Object System.Diagnostics.ProcessStartInfo 'wsl.exe'
$psi.Arguments = "-d $($script:OssDistro) -u root --exec bash -s -- --stdin"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$p = [System.Diagnostics.Process]::Start($psi)
$buf = $ms.ToArray()
$p.StandardInput.BaseStream.Write($buf, 0, $buf.Length)
$p.StandardInput.BaseStream.Flush()
$p.StandardInput.Close()
[Array]::Clear($buf, 0, $buf.Length)
$p.WaitForExit()
if ($p.ExitCode -eq 0) { Write-Host 'secrets written; next: scripts\ossctl.ps1 doctor' }
exit $p.ExitCode
