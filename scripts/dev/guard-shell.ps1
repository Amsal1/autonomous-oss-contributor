# guard-shell.ps1: PreToolUse hook for shell tools (.kiro/hooks/guard-shell.json).
# Rejects inline command shapes that make Kiro stop for a manual approval
# (here-strings, inline python, base64 or encoded payloads, subexpressions).
# The agent gets the reason on stderr right away instead of the run stalling.
# Input: the hook's JSON event on stdin. Scans the raw text, so it doesn't depend
# on the field names. Exit 0 = allow, exit 2 = block.
$raw = [Console]::In.ReadToEnd()
$patterns = @(
    "@'", '@\"', '@py',
    'python -c', 'python3 -c', 'py -c', 'py.exe -c',
    'base64 -d', 'base64 --decode', 'FromBase64String', 'ToBase64String',
    '-EncodedCommand', '-enc ',
    'Invoke-Expression', 'iex ',
    '$('
)
foreach ($p in $patterns) {
    if ($raw.Contains($p)) {
        [Console]::Error.WriteLine("Blocked by guard-shell: the command contains '$p'. Inline here-strings, python -c, base64/encoded payloads and `$(...) need manual approval and stall the run. Write the script to a file in this workspace (.ossbot-local/ or scripts/dev/) and run it by path, e.g. 'wsl -d ossbot -u root -- bash /mnt/d/Work/autonomous-oss-contributor/scripts/dev/<name>.sh' or 'python scripts/dev/<name>.py'. See rule 6 in docs/plan/phase2-build-plan.md.")
        exit 2
    }
}
exit 0
