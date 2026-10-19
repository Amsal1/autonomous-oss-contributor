#!/usr/bin/env bash
# Prepare the first upstream contribution (DESIGN §15.2, §9.13): the fork's V3 adapter as a git bundle
# on top of tobenwarrior/hermes-kiro-provider@UPSTREAM_COMMIT, plus PR title and body files, for
#   ossctl contribution import --repo tobenwarrior/hermes-kiro-provider --from-bundle ... --base ...
# Reads upstream only (a clone); writes nothing to GitHub. The import still goes through both gates.
# Usage (any user with git, from the repo root): bash scripts/dev/kiro-provider-bundle.sh [outdir]
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
vendor="$repo_root/vendor/hermes-kiro-provider"
out=${1:-"$repo_root/.ossbot-local/upstream-bundle"}
base=$(tr -d '[:space:]' < "$vendor/UPSTREAM_COMMIT")
url=$(tr -d '[:space:]' < "$vendor/UPSTREAM_URL")
[[ $base =~ ^[0-9a-f]{40}$ ]] || { echo "UPSTREAM_COMMIT is not a sha" >&2; exit 1; }
[[ $url == https://github.com/* ]] || { echo "UPSTREAM_URL is not a GitHub https URL" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -c core.hooksPath=/dev/null clone --quiet --filter=blob:none "$url.git" "$work/up"
git -C "$work/up" checkout --quiet -b ossbot-change "$base"

# the fork's files, without ossbot's bookkeeping and bytecode
(cd "$vendor" && find . -type f ! -name UPSTREAM_COMMIT ! -name UPSTREAM_URL ! -path '*/__pycache__/*' -print0) |
  while IFS= read -r -d '' f; do
    mkdir -p "$work/up/$(dirname "$f")"
    cp "$vendor/$f" "$work/up/$f"
  done

# The author identity comes from the caller's git config (git var GIT_AUTHOR_IDENT), never from this
# file (no personal values in tracked files outside docs/); without one, no bundle is built.
ident=$(git -C "$repo_root" var GIT_AUTHOR_IDENT 2>/dev/null || true)
name=${ident%% <*}
email=${ident#*<}
email=${email%%>*}
if [[ -z $ident || -z $name || $name == "$ident" || ! $email =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]]; then
  echo "no git author identity (git var GIT_AUTHOR_IDENT); set user.name and user.email first" >&2
  exit 1
fi
title_body="$repo_root/docs/upstream/hermes-kiro-provider-v3.md"
git -C "$work/up" add -A
git -C "$work/up" -c user.name="$name" -c user.email="$email" commit --quiet \
  -m "$(head -n 1 "$title_body")" -m "Signed-off-by: $name <$email>"

mkdir -p "$out"
git -C "$work/up" bundle create "$out/hermes-kiro-provider-v3.bundle" refs/heads/ossbot-change "^$base" 2>/dev/null
head -n 1 "$title_body" > "$out/title.txt"
tail -n +2 "$title_body" > "$out/body.md"
git -C "$work/up" diff --stat "$base" HEAD > "$out/diffstat.txt"
echo "bundle: $out/hermes-kiro-provider-v3.bundle ($(git -C "$work/up" rev-parse HEAD))"
echo "import (operator, after review):"
echo "  ossctl contribution import --repo tobenwarrior/hermes-kiro-provider --base $base \\"
echo "    --from-bundle $out/hermes-kiro-provider-v3.bundle --title-file $out/title.txt --body-file $out/body.md"
