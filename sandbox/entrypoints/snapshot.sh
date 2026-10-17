#!/usr/bin/env bash
# Snapshot helper (DESIGN §10.2 step 2, H1, N3; profile `snapshot`, --network none, no secrets).
# Task volume read-only at /work, ossbot-ro-<run_id> rw at /snap. Copies tracked and
# untracked-not-ignored regular files (<= 5 MB each, never .git) to the volume ROOT (research
# mounts it at /work/repo), replaces symlinks by "<path>.symlink.txt" text stubs, and self-checks
# that /snap holds only regular files and directories within a 2 GiB cap. No repo code runs.
# OSSBOT_WORK / OSSBOT_SNAP only relocate the fixed paths for tests.
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
SNAP="${OSSBOT_SNAP:-/snap}"
REPO="$WORK/repo"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME="${OSSBOT_SNAPSHOT_HOME:-/tmp/h}"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
mkdir -p "$HOME"
G=(git -C "$REPO" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c "safe.directory=*" -c protocol.file.allow=never)

cd "$REPO"
"${G[@]}" ls-files -z --cached --others --exclude-standard \
  | rsync -a --from0 --files-from=- --no-links --no-specials --no-devices \
          --max-size=5m --exclude=/.git ./ "$SNAP/"
# Symlinks become text stubs so the planner still sees them.
"${G[@]}" ls-files -s -z | while IFS= read -r -d '' ent; do
  mode=${ent%% *}
  path=${ent#*$'\t'}
  [[ "$mode" == 120000 ]] || continue
  case "$path" in
    /* | ../* | */../* | *.. ) continue ;;
  esac
  mkdir -p -- "$SNAP/$(dirname -- "$path")"
  printf 'SYMLINK -> %s\n' "$(readlink -- "$REPO/$path" || echo '?')" >"$SNAP/$path.symlink.txt"
done
# Self-check (sandboxd re-checks from a separate read-only container afterwards).
test -z "$(find "$SNAP" -mindepth 1 ! -type f ! -type d -print -quit)"
du -s --block-size=1M "$SNAP" | awk '$1 > 2048 {exit 1}'
