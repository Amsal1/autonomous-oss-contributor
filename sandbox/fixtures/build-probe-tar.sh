#!/usr/bin/env bash
# Build /opt/ossbot/fixtures/probe-repo.tar (DESIGN §10.1): copy the fixture tree, create the
# hostile symlinks (git checkouts on Windows do not keep them), commit everything with a fixed
# identity and date, and tar the result with the symlinks preserved.
# Usage: build-probe-tar.sh <fixture-dir> <out.tar>
set -euo pipefail

SRC="${1:?fixture dir}"
OUT="${2:?output tar}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/repo"
cp -a "$SRC/." "$TMP/repo/"
cd "$TMP/repo"
ln -sfn /proc/self/environ docs/env
ln -sfn /proc/1/environ pid1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null HOME="$TMP" \
  GIT_AUTHOR_NAME=ossbot-fixture GIT_AUTHOR_EMAIL=fixture@ossbot.invalid \
  GIT_COMMITTER_NAME=ossbot-fixture GIT_COMMITTER_EMAIL=fixture@ossbot.invalid \
  GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z
git init -q -b main .
git -c core.symlinks=true add -A
git -c core.hooksPath=/dev/null commit -q -m "probe fixture"
test "$(git ls-files -s docs/env | cut -d' ' -f1)" = 120000
tar --sort=name --mtime=@0 --owner=10001 --group=10001 --numeric-owner -cf "$OUT" .
