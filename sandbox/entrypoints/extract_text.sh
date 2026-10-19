#!/usr/bin/env bash
# PDF text for the assistant (DESIGN §14.12.5, §14.19 item 9; profile `extract`, --network none, no
# secrets, no repo). The PDF the owner sent comes in as /io/input/doc.pdf; pdftotext (poppler-utils)
# writes UTF-8 text, cut at OSSBOT_TEXT_MAX bytes (2 MiB):
#   /io/output/text.txt     the text (possibly empty: a scanned PDF has no text layer)
#   /io/output/result.json  {"ok", "error_class", "bytes", "truncated"}
# OSSBOT_IO, OSSBOT_PDFTOTEXT and OSSBOT_TEXT_MAX only relocate the fixed values (tests).
set -euo pipefail

IO="${OSSBOT_IO:-/io}"
PDFTOTEXT="${OSSBOT_PDFTOTEXT:-/usr/bin/pdftotext}"
MAX="${OSSBOT_TEXT_MAX:-2097152}"
IN="$IO/input/doc.pdf"
OUT="$IO/output"

result() { # result <ok:true|false> <error_class|null> <bytes> <truncated:true|false>
  local err_json="null"
  [[ "$2" != "null" ]] && err_json="\"$2\""
  printf '{"ok":%s,"error_class":%s,"bytes":%s,"truncated":%s}\n' "$1" "$err_json" "$3" "$4" >"$OUT/result.json"
}

mkdir -p "$OUT"
if [[ ! -f "$IN" || -L "$IN" ]]; then
  result false no_input 0 false
  exit 2
fi
if [[ "$(head -c 5 "$IN")" != "%PDF-" ]]; then
  result false not_pdf 0 false
  exit 2
fi
# One byte over the cap tells "truncated" apart; poppler gets a CPU-time bound on top of the job timeout.
set +e
( ulimit -t 90; "$PDFTOTEXT" -q -enc UTF-8 -eol unix -nopgbrk "$IN" - ) | head -c "$((MAX + 1))" >"$OUT/text.full"
codes=("${PIPESTATUS[@]}")
set -e
code="${codes[0]}"
if [[ "$code" -ne 0 && "$code" -ne 141 ]]; then # 141: SIGPIPE after head stopped reading
  rm -f "$OUT/text.full"
  result false "pdftotext_exit_$code" 0 false
  exit 1
fi
size=$(stat -c %s "$OUT/text.full")
truncated=false
if [[ "$size" -gt "$MAX" ]]; then
  truncated=true
  size="$MAX"
fi
head -c "$MAX" "$OUT/text.full" >"$OUT/text.txt"
rm -f "$OUT/text.full"
result true null "$size" "$truncated"
