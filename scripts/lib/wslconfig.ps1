# Install the tracked config/wsl/wslconfig as %USERPROFILE%\.wslconfig (DESIGN 7.7, ADR-0013).
# Dot-source after common.ps1. The only file ossbot writes outside the project folder.

function Get-OssWslConfigPath { return (Join-Path $env:USERPROFILE '.wslconfig') }

function Get-OssWslConfigBytes {
    # Render the template: no substitutions in v1; CRLF line endings, UTF-8 without BOM.
    $template = Join-Path (Get-OssRepoRoot) 'config\wsl\wslconfig'
    $text = [IO.File]::ReadAllText($template, [Text.Encoding]::UTF8) -replace "`r", ''
    $text = $text -replace "`n", "`r`n"
    return (New-Object System.Text.UTF8Encoding $false).GetBytes($text)
}

function Get-OssWslConfigState {
    # Returns @{ Want; Have; Path } with sha256 hex strings (Have = 'none' when absent).
    $path = Get-OssWslConfigPath
    $want = Get-OssSha256Hex -Bytes (Get-OssWslConfigBytes)
    $have = 'none'
    if (Test-Path -LiteralPath $path) { $have = Get-OssSha256Hex -Bytes ([IO.File]::ReadAllBytes($path)) }
    return [pscustomobject]@{ Want = $want; Have = $have; Path = $path }
}

function Install-OssWslConfig {
    # Back up a differing existing file into .ossbot-local\, record 'none' on first install, write.
    # Returns $true when the file changed (the caller restarts WSL and verifies).
    $state = Get-OssWslConfigState
    if ($state.Have -eq $state.Want) { return $false }
    $local = New-OssLocalDir
    $stateFile = Join-Path $local 'wslconfig.state'
    if ($state.Have -eq 'none') {
        if (-not (Test-Path -LiteralPath $stateFile)) {
            [IO.File]::WriteAllText($stateFile, "none`r`n")
        }
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = Join-Path $local "wslconfig.backup-$stamp"
        Copy-Item -LiteralPath $state.Path -Destination $backup
        if (-not (Test-Path -LiteralPath $stateFile)) {
            [IO.File]::WriteAllText($stateFile, "backup wslconfig.backup-$stamp`r`n")
        }
        Write-Host "existing .wslconfig backed up to $backup; differences:"
        $old = Get-Content -LiteralPath $backup
        $new = [Text.Encoding]::UTF8.GetString((Get-OssWslConfigBytes)) -split "`r`n"
        Compare-Object $old $new | ForEach-Object { Write-Host ("  {0} {1}" -f $_.SideIndicator, $_.InputObject) }
    }
    [IO.File]::WriteAllBytes($state.Path, (Get-OssWslConfigBytes))
    return $true
}

function Test-OssWslResources {
    # Verify the VM took the settings: nproc == 20, MemTotal ~ 20 GB, swap ~ 8 GB (7.7 step 4).
    # Returns a list of problems (empty = PASS).
    param([string]$Distro = $script:OssDistro)
    $problems = @()
    $n = Invoke-OssWsl -Distro $Distro -User root -Command @('nproc')
    if ((($n.Out -join '').Trim()) -ne '20') { $problems += "nproc is '$(($n.Out -join '').Trim())', expected 20" }
    $m = Invoke-OssWsl -Distro $Distro -User root -Command @('awk', '/^MemTotal:/ {print int($2/1024/1024+0.5)}', '/proc/meminfo')
    $gb = 0; [void][int]::TryParse((($m.Out -join '').Trim()), [ref]$gb)
    if ($gb -lt 18 -or $gb -gt 20) { $problems += "MemTotal is ~$gb GB, expected ~20 GB" }
    $s = Invoke-OssWsl -Distro $Distro -User root -Command @('awk', '/^SwapTotal:/ {print int($2/1024/1024+0.5)}', '/proc/meminfo')
    $sw = 0; [void][int]::TryParse((($s.Out -join '').Trim()), [ref]$sw)
    if ($sw -lt 7 -or $sw -gt 8) { $problems += "SwapTotal is ~$sw GB, expected ~8 GB" }
    if ($problems.Count) { $problems += 'hint: check `wsl --version` and the key spelling in config/wsl/wslconfig' }
    return $problems
}
