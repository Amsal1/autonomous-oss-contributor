<#
.SYNOPSIS
  Regenerate the README demo media (docs/media) from the real gatekeeper renderers.
.DESCRIPTION
  1. Tools: the pinned ffmpeg (config/versions.lock.yaml ffmpeg_windows; downloaded into the gitignored
     tools\bin\ffmpeg and sha256-verified before extraction) and the system Chrome (>= chrome_headless.min_major).
  2. Capture (in the distro, dev venv): tests/e2e/test_demo_capture.py runs the hermetic e2e dry-run flow on
     the fictional upstream acme/widgets (fake GitHub, fake Bot API, fake Hermes stream, test profile) and
     writes every Bot API call the gatekeeper made to .ossbot-local\demo\capture\transcript.json.
  3. Checks and pages: personal_check.py on the transcript, build.py render (each page passes
     assert_faithful: every text and button equals the captured payload), personal_check.py on the pages.
  4. Screenshots with headless Chrome (no remote debugging port), plus --dump-dom of every page, which
     build.py check-dom re-checks against the payloads.
  5. ffmpeg: PNG stills (light and dark), demo.gif and demo.mp4, with size, width and duration gates. A
     still, the GIF or the MP4 is replaced only when its pixels or inputs changed ("unchanged" otherwise).
  6. docs/media/demo.manifest.json and the README badges.
  Sends nothing to Telegram or GitHub. Scratch goes to .ossbot-local\demo (gitignored).
.PARAMETER Check
  Capture and render only, then compare the render digest with docs/media/demo.manifest.json (no Chrome,
  no ffmpeg): exit 1 when the committed media are stale.
.PARAMETER SkipCapture
  Reuse the last transcript in .ossbot-local\demo\capture.
.EXAMPLE
  powershell -NoProfile -File scripts\dev\make-demo.ps1
#>
[CmdletBinding()]
param([switch]$Check, [switch]$SkipCapture)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot '..\lib\common.ps1')
$repo = Get-OssRepoRoot
$linuxRepo = $script:OssLinuxRepo
$scratch = Join-Path $repo '.ossbot-local\demo'
$media = Join-Path $repo 'docs\media'
$capture = Join-Path $scratch 'capture'
$pages = Join-Path $scratch 'html'
$raw = Join-Path $scratch 'raw'
$framesDir = Join-Path $scratch 'frames'
$utf8 = New-Object System.Text.UTF8Encoding $false
$GifMaxBytes = 6000000
$GifWidth = 720
$StillW = 768
$StillH = 1608

function Get-LockValue([string]$Section, [string]$Key) {
    # versions.lock.yaml is plain 2-space nested scalars (as read by scripts/install-hooks.ps1)
    $inSection = $false
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $repo 'config\versions.lock.yaml'))) {
        if ($line -match '^([A-Za-z0-9_]+):\s*$') { $inSection = ($Matches[1] -eq $Section); continue }
        if ($inSection -and $line -match ('^  ' + [regex]::Escape($Key) + ':\s*"?([^"#]+?)"?\s*$')) {
            return $Matches[1].Replace('\\', '\')
        }
    }
    throw "versions.lock.yaml has no $Section.$Key"
}

function Stop-Demo([string]$Name, [string]$Detail) {
    Write-OssStep $Name 'FAIL' $Detail
    exit 1
}

function ConvertTo-LinuxPath([string]$WindowsPath) {
    $rel = $WindowsPath.Substring($repo.Length).TrimStart('\').Replace('\', '/')
    return "$linuxRepo/$rel"
}

function Invoke-Dev {
    # one command in the distro's dev venv (as ossadmin), argv passed intact
    param([string[]]$Argv, [string[]]$Env = @())
    $wslArgs = @('-d', $script:OssDistro, '-u', 'ossadmin', '--cd', $linuxRepo, '--exec', '/usr/bin/env',
        'UV_PROJECT_ENVIRONMENT=/home/ossadmin/.venvs/ossbot') + $Env + @('/opt/ossbot/bin/uv', 'run', '--frozen') + $Argv
    & wsl.exe @wslArgs | Out-Host
    return $LASTEXITCODE
}

function Invoke-Tool {
    # an external program with a timeout; returns @{ Code; Out } (stdout as UTF-8 text)
    param([string]$Exe, [string[]]$Argv, [int]$TimeoutS = 120)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $utf8
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutS * 1000)) {
        try { $proc.Kill() } catch { Write-Verbose 'already exited' }
        return @{ Code = -1; Out = ''; Err = "timeout after $TimeoutS s" }
    }
    return @{ Code = $proc.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
}

function Get-FrameMd5([string]$Path) {
    # decoded-pixel digest (stable across PNG encoders): ffmpeg framemd5 of the rgb24 frames
    $r = Invoke-Tool $ffmpeg @('-v', 'error', '-i', $Path, '-f', 'framemd5', '-pix_fmt', 'rgb24', '-')
    if ($r.Code -ne 0) { Stop-Demo 'framemd5' "$Path : $($r.Err)" }
    $lines = $r.Out -split "`n" | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object { ($_ -split ',')[-1].Trim() }
    return ($lines -join ' ')
}

function Get-Probe([string]$Path) {
    $r = Invoke-Tool $ffprobe @('-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
        'stream=width,height,nb_read_frames:format=duration', '-of', 'json', $Path)
    if ($r.Code -ne 0) { Stop-Demo 'ffprobe' "$Path : $($r.Err)" }
    $doc = $r.Out | ConvertFrom-Json
    $s = $doc.streams[0]
    $dur = 0.0
    if ($doc.PSObject.Properties['format'] -and $doc.format.PSObject.Properties['duration']) {
        [void][double]::TryParse([string]$doc.format.duration, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$dur)
    }
    return @{ Width = [int]$s.width; Height = [int]$s.height; Frames = [int]$s.nb_read_frames; Duration = [Math]::Round($dur, 2) }
}

function Read-Manifest {
    $path = Join-Path $media 'demo.manifest.json'
    if (Test-Path -LiteralPath $path) { return ([IO.File]::ReadAllText($path, $utf8) | ConvertFrom-Json) }
    return $null
}

# ------------------------------------------------------------------ 1. preflight and tools
$dirty = (& git -C $repo status --porcelain -- docs/media) -join ' '
if ($dirty) { Write-OssStep 'preflight' 'WARN' "docs/media has uncommitted changes: $dirty" } else { Write-OssStep 'preflight' 'PASS' 'docs/media clean' }
New-Item -ItemType Directory -Force -Path $scratch, $capture | Out-Null

$ffmpeg = $null
$ffprobe = $null
$ffmpegVersion = Get-LockValue 'ffmpeg_windows' 'version'
if (-not $Check) {
    $want = (Get-LockValue 'ffmpeg_windows' 'sha256').ToLowerInvariant()
    $url = Get-LockValue 'ffmpeg_windows' 'url'
    $bin = Join-Path $repo 'tools\bin\ffmpeg'
    $ffmpeg = Join-Path $bin 'ffmpeg.exe'
    $ffprobe = Join-Path $bin 'ffprobe.exe'
    $stamp = Join-Path $bin 'zip-sha256'
    $current = (Test-Path -LiteralPath $ffmpeg) -and (Test-Path -LiteralPath $ffprobe) -and (Test-Path -LiteralPath $stamp) -and
        ([IO.File]::ReadAllText($stamp).Trim() -eq $want)
    if ($current) {
        Write-OssStep 'ffmpeg' 'unchanged' "$ffmpegVersion (zip sha256 verified earlier)"
    } else {
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        $zip = Join-Path $bin 'ffmpeg-download.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($got -ne $want) {
            Remove-Item -LiteralPath $zip -Force
            Stop-Demo 'ffmpeg' "sha256 mismatch for $url (got $got, want $want); nothing installed"
        }
        $tmp = Join-Path $bin 'extract'
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp
        foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
            $found = Get-ChildItem -LiteralPath $tmp -Recurse -Filter $exe | Select-Object -First 1
            if (-not $found) { Stop-Demo 'ffmpeg' "$exe not in the archive" }
            Copy-Item -LiteralPath $found.FullName -Destination (Join-Path $bin $exe) -Force
        }
        Remove-Item -LiteralPath $tmp -Recurse -Force
        Remove-Item -LiteralPath $zip -Force
        [IO.File]::WriteAllText($stamp, $want)
        Write-OssStep 'ffmpeg' 'changed' "$ffmpegVersion installed in tools\bin\ffmpeg (zip sha256 verified before extraction)"
    }

    $minMajor = [int](Get-LockValue 'chrome_headless' 'min_major')
    $candidates = @((Get-LockValue 'chrome_headless' 'path'),
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'))
    $chrome = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $chrome) { Stop-Demo 'chrome' 'Google Chrome not found (install it, or set chrome_headless.path)' }
    $chromeVersion = (Get-Item -LiteralPath $chrome).VersionInfo.ProductVersion
    if ([int]($chromeVersion.Split('.')[0]) -lt $minMajor) { Stop-Demo 'chrome' "Chrome $chromeVersion is older than $minMajor (new headless mode)" }
    Write-OssStep 'chrome' 'PASS' "Chrome $chromeVersion"
}

# ------------------------------------------------------------------ 2. capture
$transcript = Join-Path $capture 'transcript.json'
if (-not $SkipCapture) {
    if (Test-Path -LiteralPath $transcript) { Remove-Item -LiteralPath $transcript -Force }
    $code = Invoke-Dev -Env @("OSSBOT_DEMO_OUT=$(ConvertTo-LinuxPath $capture)") -Argv @('pytest', '-q', '-p', 'no:xdist',
        'tests/e2e/test_demo_capture.py')
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $transcript)) { Stop-Demo 'capture' "tests/e2e/test_demo_capture.py exit $code" }
    Write-OssStep 'capture' 'PASS' 'transcript.json from the hermetic e2e flow'
}
$transcriptLinux = ConvertTo-LinuxPath $transcript

# ------------------------------------------------------------------ 3. checks and pages
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/personal_check.py', $transcriptLinux)) -ne 0) {
    Stop-Demo 'personal-check' 'the captured payloads carry a personal value, secret or host path; nothing rendered'
}
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/build.py', 'render', '--transcript', $transcriptLinux, '--out', (ConvertTo-LinuxPath $pages))) -ne 0) {
    Stop-Demo 'render' 'build.py render failed (a page differs from the captured payloads?)'
}
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/personal_check.py', (ConvertTo-LinuxPath $pages))) -ne 0) {
    Stop-Demo 'personal-check' 'a rendered page carries a personal value, secret or host path'
}
$frames = [IO.File]::ReadAllText((Join-Path $pages 'frames.json'), $utf8) | ConvertFrom-Json
$manifest = Read-Manifest

if ($Check) {
    if ($null -ne $manifest -and $manifest.render_digest -eq $frames.render_digest) {
        Write-OssStep 'check' 'PASS' "docs/media matches the renderers (render digest $($frames.render_digest.Substring(0, 12)))"
        exit 0
    }
    Write-OssStep 'check' 'FAIL' 'stale: regenerate with scripts/dev/make-demo.ps1'
    exit 1
}

# ------------------------------------------------------------------ 4. screenshots and DOM dumps
foreach ($d in $raw, $framesDir) {
    if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$profileDir = Join-Path $scratch 'chrome-profile'
$common = @('--headless', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-networking', '--disable-sync', '--disable-component-update',
    '--mute-audio', '--force-color-profile=srgb', '--font-render-hinting=none', "--user-data-dir=$profileDir")
foreach ($p in $frames.pages) {
    $page = Join-Path $pages $p.page
    $url = 'file:///' + $page.Replace('\', '/')
    $png = Join-Path $raw ($p.page -replace '\.html$', '.png')
    $shot = Invoke-Tool $chrome ($common + @('--force-device-scale-factor=2', "--window-size=$($p.width),$($p.height + 200)",
        "--screenshot=$png", $url))
    if ($shot.Code -ne 0 -or -not (Test-Path -LiteralPath $png)) { Stop-Demo 'screenshot' "$($p.page): exit $($shot.Code) $($shot.Err)" }
    $dom = Invoke-Tool $chrome ($common + @("--window-size=$($p.width),$($p.height)", '--dump-dom', $url))
    if ($dom.Code -ne 0 -or -not $dom.Out) { Stop-Demo 'dump-dom' "$($p.page): exit $($dom.Code)" }
    [IO.File]::WriteAllText((Join-Path $pages ($p.page -replace '\.html$', '.dom.html')), $dom.Out, $utf8)
}
Write-OssStep 'screenshot' 'PASS' "$(@($frames.pages).Count) pages"
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/build.py', 'check-dom', '--transcript', $transcriptLinux, '--pages', (ConvertTo-LinuxPath $pages))) -ne 0) {
    Stop-Demo 'check-dom' 'what Chrome rendered differs from the captured payloads'
}

# ------------------------------------------------------------------ 5. assemble
$bitexact = @('-fflags', '+bitexact', '-flags:v', '+bitexact', '-map_metadata', '-1')
$outputs = [ordered]@{}
$stillDir = Join-Path $media 'stills'
New-Item -ItemType Directory -Force -Path $stillDir | Out-Null
$wanted = @{}
foreach ($p in ($frames.pages | Where-Object { $_.kind -eq 'still' })) {
    $src = Join-Path $raw ($p.page -replace '\.html$', '.png')
    $tmp = Join-Path $framesDir $p.still
    $r = Invoke-Tool $ffmpeg (@('-v', 'error', '-y', '-i', $src, '-vf', "crop=${StillW}:${StillH}:0:0", '-frames:v', '1') + $bitexact +
        @('-compression_level', '9', '-pred', 'mixed', $tmp))
    if ($r.Code -ne 0) { Stop-Demo 'still' "$($p.still): $($r.Err)" }
    $target = Join-Path $stillDir $p.still
    $md5 = Get-FrameMd5 $tmp
    if ((Test-Path -LiteralPath $target) -and ((Get-FrameMd5 $target) -eq $md5)) {
        Write-OssStep "still $($p.still)" 'unchanged'
    } else {
        Copy-Item -LiteralPath $tmp -Destination $target -Force
        Write-OssStep "still $($p.still)" 'changed'
    }
    $probe = Get-Probe $target
    if ($probe.Width -ne $StillW -or $probe.Height -ne $StillH) { Stop-Demo 'still' "$($p.still) is $($probe.Width)x$($probe.Height)" }
    $outputs["stills/$($p.still)"] = [ordered]@{ width = $probe.Width; height = $probe.Height; framemd5 = $md5 }
    $wanted[$p.still] = $true
}
Get-ChildItem -LiteralPath $stillDir -Filter '*.png' | Where-Object { -not $wanted.ContainsKey($_.Name) } | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force
    Write-OssStep "still $($_.Name)" 'changed' 'removed (no longer a scene)'
}

# GIF and MP4 frames: the gif pages cropped to their size, each repeated for its hold as a numbered
# sequence at $Fps (exact timing; the concat demuxer's per-file durations drift)
$Fps = 10
$seqDir = Join-Path $framesDir 'seq'
New-Item -ItemType Directory -Force -Path $seqDir | Out-Null
$gifKey = New-Object System.Text.StringBuilder
[void]$gifKey.Append($frames.render_digest)
$n = 0
foreach ($p in ($frames.pages | Where-Object { $_.kind -eq 'gif' })) {
    $src = Join-Path $raw ($p.page -replace '\.html$', '.png')
    $name = $p.page -replace '\.html$', '.png'
    $dst = Join-Path $framesDir $name
    $r = Invoke-Tool $ffmpeg (@('-v', 'error', '-y', '-i', $src, '-vf', "crop=$($p.width * 2):$($p.height * 2):0:0", '-frames:v', '1') + $bitexact + @($dst))
    if ($r.Code -ne 0) { Stop-Demo 'gif-frame' "$name : $($r.Err)" }
    $count = [int][Math]::Round([double]$p.hold_s * $Fps)
    for ($i = 0; $i -lt $count; $i++) {
        Copy-Item -LiteralPath $dst -Destination (Join-Path $seqDir ('{0:D5}.png' -f $n))
        $n++
    }
    [void]$gifKey.Append("|$name|$count|" + (Get-FrameMd5 $dst))
}
$seqInput = @('-framerate', "$Fps", '-start_number', '0', '-i', (Join-Path $seqDir '%05d.png'))
$keyBytes = $utf8.GetBytes($gifKey.ToString() + "|ffmpeg $ffmpegVersion")
$gifInputs = (Get-OssSha256Hex -Bytes $keyBytes)
$gif = Join-Path $media 'demo.gif'
$mp4 = Join-Path $media 'demo.mp4'
$fresh = ($null -ne $manifest) -and ($manifest.gif_inputs -eq $gifInputs) -and (Test-Path -LiteralPath $gif) -and (Test-Path -LiteralPath $mp4)
if ($fresh) {
    Write-OssStep 'gif' 'unchanged' 'same frames and holds'
    Write-OssStep 'mp4' 'unchanged' 'same frames and holds'
} else {
    $tmpGif = Join-Path $framesDir 'demo.gif'
    foreach ($colors in 192, 128) {
        $vf = "scale=${GifWidth}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=${colors}:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        $r = Invoke-Tool $ffmpeg (@('-v', 'error', '-y') + $seqInput + @('-vf', $vf, '-loop', '0') + $bitexact + @($tmpGif)) -TimeoutS 600
        if ($r.Code -ne 0) { Stop-Demo 'gif' $r.Err }
        if ((Get-Item -LiteralPath $tmpGif).Length -le $GifMaxBytes) { break }
        Write-OssStep 'gif' 'WARN' "over $GifMaxBytes bytes with $colors colours; retrying with fewer"
    }
    Copy-Item -LiteralPath $tmpGif -Destination $gif -Force
    $tmpMp4 = Join-Path $framesDir 'demo.mp4'
    $r = Invoke-Tool $ffmpeg (@('-v', 'error', '-y') + $seqInput + @('-vf',
        "fps=30,scale=${GifWidth}:-2:flags=lanczos,format=yuv420p", '-c:v', 'libx264', '-preset', 'slow', '-crf', '20',
        '-tune', 'stillimage', '-threads', '1', '-movflags', '+faststart') + $bitexact + @($tmpMp4)) -TimeoutS 600
    if ($r.Code -ne 0) { Stop-Demo 'mp4' $r.Err }
    Copy-Item -LiteralPath $tmpMp4 -Destination $mp4 -Force
    Write-OssStep 'gif' 'changed'
    Write-OssStep 'mp4' 'changed'
}

# ------------------------------------------------------------------ gates
foreach ($item in @(@{ Name = 'demo.gif'; Path = $gif }, @{ Name = 'demo.mp4'; Path = $mp4 })) {
    $size = (Get-Item -LiteralPath $item.Path).Length
    $probe = Get-Probe $item.Path
    if ($size -gt $GifMaxBytes) { Stop-Demo $item.Name "$size bytes > $GifMaxBytes" }
    if ($probe.Width -ne $GifWidth) { Stop-Demo $item.Name "width $($probe.Width) != $GifWidth" }
    if ($probe.Duration -lt 12 -or $probe.Duration -gt 20) { Stop-Demo $item.Name "duration $($probe.Duration) s outside 12-20 s" }
    $outputs[$item.Name] = [ordered]@{ width = $probe.Width; height = $probe.Height; frames = $probe.Frames; duration_s = $probe.Duration;
        framemd5_sha256 = (Get-OssSha256Hex -Bytes ($utf8.GetBytes((Get-FrameMd5 $item.Path)))) }  # sha256 of the per-frame md5 list
    Write-OssStep "gate $($item.Name)" 'PASS' "$size bytes, $($probe.Width)x$($probe.Height), $($probe.Duration) s, $($probe.Frames) frames"
}

# ------------------------------------------------------------------ 6. badges and manifest
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/build.py', 'badges', '--out', (ConvertTo-LinuxPath (Join-Path $media 'badges')))) -ne 0) {
    Stop-Demo 'badges' 'build.py badges failed'
}
$measure = [ordered]@{ chrome_version = $chromeVersion; ffmpeg_version = $ffmpegVersion; gif_inputs = $gifInputs; outputs = $outputs }
$measureFile = Join-Path $scratch 'measure.json'
[IO.File]::WriteAllText($measureFile, ($measure | ConvertTo-Json -Depth 6), $utf8)
if ((Invoke-Dev -Argv @('python', 'scripts/dev/demo/build.py', 'manifest', '--transcript', $transcriptLinux, '--pages',
        (ConvertTo-LinuxPath $pages), '--measure', (ConvertTo-LinuxPath $measureFile), '--media', (ConvertTo-LinuxPath $media),
        '--out', (ConvertTo-LinuxPath (Join-Path $media 'demo.manifest.json')))) -ne 0) {
    Stop-Demo 'manifest' 'build.py manifest failed'
}
$gifInfo = $outputs['demo.gif']
Write-OssStep 'make-demo' 'PASS' ("demo.gif {0} bytes, {1} px, {2} s, {3} frames; {4} stills" -f (Get-Item -LiteralPath $gif).Length,
    $gifInfo.width, $gifInfo.duration_s, $gifInfo.frames, $wanted.Count)
exit 0
