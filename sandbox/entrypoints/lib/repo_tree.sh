#!/usr/bin/env bash
# Repository tree for the scout (DESIGN §9.5a rule 6, ADR-0011 addendum "scout repo read").
# usage: repo_tree.sh <repo> <out> [max_bytes=24000]
# Called by fetch.sh in the credential-free fetch job. Built only from `git ls-files` of the
# checked-out tree (nothing from the repo runs; core.quotePath stays on, so control characters and
# non-ASCII bytes in names stay C-quoted on one line). Depth 2 with per-directory file counts,
# sorted with LC_ALL=C, cut on whole lines to <= max_bytes. The runner inlines the file into the
# scout task as untrusted data, so the scout sees the layout without a large tool result.
set -euo pipefail

REPO="${1:?repo}"
OUT="${2:?out}"
MAX="${3:-24000}"
[[ "$MAX" =~ ^[1-9][0-9]{2,6}$ ]] || { echo "repo_tree: bad max_bytes" >&2; exit 2; }

export LC_ALL=C GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
G=(git -C "$REPO" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c "safe.directory=*" -c protocol.file.allow=never)

LIST="$OUT.ls"
PART="$OUT.part"
trap 'rm -f -- "$LIST" "$PART"' EXIT
"${G[@]}" ls-files >"$LIST"
FILES="$(wc -l <"$LIST" | tr -d ' ')"

# One entry per top-level file, top-level directory, second-level file and second-level directory.
awk '
{
  p = $0
  if (substr(p, 1, 1) == "\"") p = substr(p, 2, length(p) - 2)  # C-quoted name: keep the escapes
  n = split(p, a, "/")
  if (n == 1) { print p; next }
  top[a[1]]++
  if (n == 2) { print a[1] "/" a[2]; next }
  sub2[a[1] "/" a[2]]++
}
function files(k) { return "(" k " file" (k == 1 ? "" : "s") ")" }
END {
  for (d in top) print d "/ " files(top[d])
  for (d in sub2) print d "/ " files(sub2[d])
}' "$LIST" | sort | awk -v max="$MAX" -v total="$FILES" '
{ line[NR] = $0 }
END {
  head = "# git ls-files of the default branch, depth 2, " total " files (generated; paths are repository data)"
  used = length(head) + 1
  all = used
  for (i = 1; i <= NR; i++) all += length(line[i]) + 1
  print head
  if (all <= max) { for (i = 1; i <= NR; i++) print line[i]; exit }
  reserve = 100  # the truncation line below
  for (i = 1; i <= NR; i++) {
    if (used + length(line[i]) + 1 + reserve > max) break
    print line[i]
    used += length(line[i]) + 1
  }
  print "# truncated: " (NR - i + 1) " more entries; list a subdirectory with list_directory (depth 1)"
}' >"$PART"
mv -f -- "$PART" "$OUT"
