#!/usr/bin/env bash
# The assistant's code jobs (DESIGN §15.6; owner decision F007-R1-2; profile `code`: --network none, no
# secrets, no repo, read-only rootfs, exec /tmp). Hermes' code comes in as /io/input/main.py (python, the
# image's /opt/ossbot/code-py venv with Pillow, numpy, pandas) or /io/input/main.sh (bash); the owner's
# attachments are next to it in /io/input (read-only, EXIF already stripped). The code runs in the
# scratch dir /tmp/work (also HOME) under `timeout`, and may leave result files in $OSSBOT_OUTPUT_DIR
# (/tmp/work/out). Written to /io/output:
#   stdout.txt, stderr.txt   the first 32 KiB of each stream
#   out-<name>               at most 5 result files, 10 MiB in total, allowlisted types only
#   result.json              {"ok", "error_class", "exit_code", "timed_out", "stdout_bytes", "stderr_bytes",
#                             "files": [{"name", "bytes"}], "dropped": [names]}
# The gatekeeper enforces the same caps again when it reads the artifacts (the code shares this uid).
# OSSBOT_IO, OSSBOT_CODE_PYTHON and OSSBOT_WORK only relocate the fixed values (tests).
set -uo pipefail

IO="${OSSBOT_IO:-/io}"
PY="${OSSBOT_CODE_PYTHON:-/opt/ossbot/code-py/bin/python}"
WORK="${OSSBOT_WORK:-/tmp/work}"
LANGUAGE="${OSSBOT_ARG_LANGUAGE:-}"
TIMEOUT_S="${OSSBOT_ARG_TIMEOUT_S:-120}"
OUT="$IO/output"
STREAM_MAX=32768
FILES_MAX=5
TOTAL_MAX=10485760
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
EXT_RE='\.(png|jpg|jpeg|webp|gif|svg|csv|tsv|txt|md|json|html|pdf)$'

mkdir -p "$OUT"
fail() { # fail <error_class> <exit>
  jq -n --arg e "$1" '{ok: false, error_class: $e, exit_code: null, timed_out: false, stdout_bytes: 0,
    stderr_bytes: 0, files: [], dropped: []}' >"$OUT/result.json"
  exit "$2"
}

case "$LANGUAGE" in
  python) src="$IO/input/main.py"; cmd=("$PY" -I "$src") ;;
  bash) src="$IO/input/main.sh"; cmd=(/bin/bash --noprofile --norc "$src") ;;
  *) fail bad_language 2 ;;
esac
[[ -f "$src" && ! -L "$src" ]] || fail no_source 2
[[ "$TIMEOUT_S" =~ ^[0-9]{1,3}$ && "$TIMEOUT_S" -ge 1 && "$TIMEOUT_S" -le 120 ]] || fail bad_timeout 2

rm -rf "$WORK"
mkdir -p "$WORK/out" || fail no_scratch 1
cd "$WORK" || fail no_scratch 1
# -k: SIGKILL 5 s after the SIGTERM at the deadline; a bounded environment, nothing inherited but PATH.
timeout -k 5 "$TIMEOUT_S" env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WORK" LANG=C.UTF-8 \
  OSSBOT_INPUT_DIR="$IO/input" OSSBOT_OUTPUT_DIR="$WORK/out" MPLCONFIGDIR="$WORK/.mpl" \
  "${cmd[@]}" </dev/null >"$WORK/.stdout" 2>"$WORK/.stderr"
code=$?
timed_out=false
if [[ $code -eq 124 || $code -eq 137 ]]; then
  timed_out=true
fi
stdout_bytes=$(stat -c %s "$WORK/.stdout")
stderr_bytes=$(stat -c %s "$WORK/.stderr")
head -c "$STREAM_MAX" "$WORK/.stdout" >"$OUT/stdout.txt"
head -c "$STREAM_MAX" "$WORK/.stderr" >"$OUT/stderr.txt"

files='[]'
dropped='[]'
count=0
total=0
while IFS= read -r -d '' f; do
  name=$(basename "$f")
  size=$(stat -c %s "$f")
  if [[ -L "$f" || ! -f "$f" || ! "$name" =~ $NAME_RE || ! "${name,,}" =~ $EXT_RE ]] ||
    ((count >= FILES_MAX || total + size > TOTAL_MAX)); then
    dropped=$(jq -c --arg n "${name:0:64}" '. + [$n]' <<<"$dropped")
    continue
  fi
  cp --no-dereference -- "$f" "$OUT/out-$name"
  count=$((count + 1))
  total=$((total + size))
  files=$(jq -c --arg n "$name" --argjson b "$size" '. + [{name: $n, bytes: $b}]' <<<"$files")
done < <(find "$WORK/out" -mindepth 1 -maxdepth 1 -print0 | sort -z)

jq -n --argjson code "$code" --argjson t "$timed_out" --argjson so "$stdout_bytes" --argjson se "$stderr_bytes" \
  --argjson files "$files" --argjson dropped "$dropped" \
  '{ok: true, error_class: null, exit_code: $code, timed_out: $t, stdout_bytes: $so, stderr_bytes: $se,
    files: $files, dropped: $dropped}' >"$OUT/result.json"
exit 0
