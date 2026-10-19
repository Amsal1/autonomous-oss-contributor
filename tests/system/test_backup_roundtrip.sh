#!/usr/bin/env bash
# DESIGN AC 22 in place (PLAN item 43, §18.8): `ossctl backup` then `ossctl restore` of that archive on the
# live distro, with a throwaway age identity under /root/.cache/ossbot-test (never the owner's key):
#   backup   pauses for the copy and lifts its own pause (ossbot-resume --paused-only), writes one
#            age-encrypted archive (no plaintext), records backup_created;
#   workers  a backup during a workers down lifts its pause but starts no unit (runner, Hermes and
#            sandboxd stay stopped, the lifecycle flag stays down); ossctl up;
#   tamper   a copy with one flipped byte is refused by restore (age integrity), nothing installed;
#   restore  killed (reason restore) + dry-run ON, no open approval in the three decision services,
#            audit verify passes against the restored anchors, the anchors append-only again, the
#            Hermes memories and SOUL.md identical, the previous state moved aside (never deleted);
#   then the test resumes (offline resume) and every unit is active again.
# The DB is rolled back to the backup moment (seconds earlier). Telegram: the pause / offline resume /
# kill FYIs go to the configured owner only. The encrypted test archives are removed at the end.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317
source "$(dirname "$0")/lib.sh"
require_running

OSSCTL=/opt/ossbot/app/.venv/bin/ossctl
DB=/srv/ossbot/gatekeeper/ossbot.db
ANCHORS=/srv/ossbot/logs/audit-anchors.log
KEYDIR=/root/.cache/ossbot-test
KEY=$KEYDIR/backup-test.agekey
q() {
  python3 -I -c 'import sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10); print(c.execute(sys.argv[2]).fetchone()[0])' "$DB" "$1"
}
if ! command -v age >/dev/null || ! command -v age-keygen >/dev/null; then
  fail "age / age-keygen not installed (provision step apt-base)"
  finish
fi
OUT=$(mktemp -d /var/tmp/ossbot-backup-test-XXXXXX)
trap 'rm -rf "$OUT"' EXIT
install -d -m 0700 "$KEYDIR"
[[ -s $KEY ]] || age-keygen -o "$KEY" >/dev/null 2>&1
RECIP=$(age-keygen -y "$KEY")
hermes_sums() { (cd /home/hermes/.hermes && find memories SOUL.md -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum); }
asides() { find /srv/ossbot -maxdepth 1 -name 'restore-previous-*' | wc -l; }
open_approvals() {
  q "SELECT (SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed','confirmed'))
          + (SELECT COUNT(*) FROM proposal_approvals WHERE status IN ('pending','armed','confirmed'))
          + (SELECT COUNT(*) FROM skill_approvals WHERE status IN ('pending','armed','confirmed'))"
}
before_sums=$(hermes_sums)
PROFILE=$REPO/config/profile.yaml
profile_meta() { stat -c '%U:%G %a' "$PROFILE"; }
before_profile=$(profile_meta)
gk_healthy() { [[ $(gk GET /v1/health | cut -d' ' -f1) == 200 ]]; }

# --- backup -------------------------------------------------------------------------------------------
report=$("$OSSCTL" backup --out "$OUT" --recipient "$RECIP" 2>/dev/null)  # stdout = the JSON report
rc=$?
check "ossctl backup exits 0" test "$rc" -eq 0
archive=$(find "$OUT" -maxdepth 1 -name 'ossbot-*.tar.age' | head -n 1)
check "one archive written" test -f "$archive"
check "the archive is age-encrypted" bash -c "head -c 21 '$archive' | grep -q 'age-encryption.org/v1'"
check "no plaintext in the archive" bash -c "! grep -qa -e 'MANIFEST.json' -e 'files/srv/ossbot' '$archive'"
check "the backup paused for the copy" test "$(jq -r .paused_for_backup <<<"$report" 2>/dev/null)" = true
check "the backup lifted its own pause" test "$(jq -r .resume <<<"$report" 2>/dev/null)" = resumed
check "the backup counted the audit rows of its copy" test "$(jq -r .audit_rows <<<"$report" 2>/dev/null)" -gt 0
check "no pause flag left" wait_for 30 bash -c "! test -e $CONTROL/paused"
check "the lift is logged as paused_only in resume.log" bash -c "tail -n 1 /srv/ossbot/logs/resume.log | grep -q '\"paused_only\":true'"
name=$(basename "$archive")
recorded() { [[ $(q "SELECT COUNT(*) FROM audit_events WHERE event = 'backup_created' AND data_json LIKE '%$name%'") -ge 1 ]]; }
check "backup_created recorded in the audit log" recorded
state_running() { [[ $(gk GET /v1/control) == *'"state":"running"'* ]]; }
check "the gatekeeper is running again (reconciled)" wait_for 60 state_running

# --- a backup during a workers down lifts its pause and starts nothing (F008-R1-2) --------------------
workers_stopped() { ! systemctl is-active --quiet ossbot-runner && ! systemctl is-active --quiet ossbot-hermes \
  && ! systemctl is-active --quiet ossbot-sandboxd; }
check "ossctl down --scope workers before the second backup" "$OSSCTL" down --timeout 5 --scope workers
report2=$("$OSSCTL" backup --out "$OUT" --recipient "$RECIP" 2>/dev/null)
rc=$?
check "the backup during a workers down exits 0" test "$rc" -eq 0
check "... and lifted its own pause" test "$(jq -r .resume <<<"$report2" 2>/dev/null)" = resumed
check "runner, Hermes and sandboxd still stopped after that backup" workers_stopped
check "the lifecycle flag still says down / workers" \
  test "$(jq -r '.state + "/" + .scope' "$CONTROL/lifecycle" 2>/dev/null)" = down/workers
check "ossctl up after the workers-down backup" "$OSSCTL" up --timeout 120
check "every unit active again" wait_for 60 units_active "${PHASE1_UNITS[@]}"

# --- a corrupted archive is refused, nothing installed -------------------------------------------------
bad=$OUT/ossbot-20000101-000000.tar.age
cp "$archive" "$bad"
size=$(stat -c %s "$bad")
printf 'X' | dd of="$bad" bs=1 seek=$((size / 2)) conv=notrunc status=none
n_aside=$(asides)
"$OSSCTL" restore "$bad" --yes <"$KEY" >/dev/null 2>&1
rc=$?
check "restore refuses the corrupted archive" test "$rc" -ne 0
check "nothing moved aside for it" test "$(asides)" -eq "$n_aside"
check "it stays killed after the refusal" test -e "$CONTROL/killed"
check "the gatekeeper answers in killed mode after the refusal" wait_for 90 gk_healthy
check "resume after the refused restore" resume_all
check "running again after the refused restore" wait_for 60 state_running

# --- restore ------------------------------------------------------------------------------------------
out=$("$OSSCTL" restore "$archive" --yes <"$KEY" 2>&1)
rc=$?
check "ossctl restore exits 0" test "$rc" -eq 0
check "it says how to go on" grep -q 'Restored. Run ossctl doctor, then /resume on Telegram.' <<<"$out"
check "killed with reason restore" test "$(jq -r .reason "$CONTROL/killed" 2>/dev/null)" = restore
check "DB state killed, dry-run ON" test "$(q "SELECT state || '/' || dry_run FROM control_state")" = killed/1
check "no open approval in the three decision services" test "$(open_approvals)" -eq 0
check "restored audited" test "$(q "SELECT COUNT(*) FROM audit_events WHERE event = 'restored'")" -ge 1
check "the repo profile keeps its owner and mode (drvfs)" test "$(profile_meta)" = "$before_profile"
check "the gatekeeper can read its config again" as gatekeeper test -r "$PROFILE"
check "the gatekeeper runs in killed mode and answers" wait_for 90 gk_healthy
check "the proxy runs in killed mode" wait_for 60 units_active ossbot-proxy
audit_ok() { [[ $(gk GET /v1/audit/verify) == 200*'"ok":true'* ]]; }
check "audit verify passes against the restored anchors" wait_for 30 audit_ok
check "the anchors are append-only again" bash -c "lsattr -d $ANCHORS | awk '{print \$1}' | grep -q a"
check "Hermes memories and SOUL.md identical" test "$(hermes_sums)" = "$before_sums"
check "the previous state was moved aside" test "$(asides)" -eq $((n_aside + 1))
newest=$(find /srv/ossbot -maxdepth 1 -name 'restore-previous-*' | sort | tail -n 1)
check "... including the previous gatekeeper DB" test -f "$newest/srv/ossbot/gatekeeper/ossbot.db"
check "workers stay stopped while killed" bash -c '! systemctl is-active --quiet ossbot-hermes && ! systemctl is-active --quiet ossbot-sandboxd'
check "restored files keep their owners (gatekeeper DB)" test "$(stat -c %U:%G "$DB")" = "$(stat -c %U:%G "$newest/srv/ossbot/gatekeeper/ossbot.db")"

# --- resume -------------------------------------------------------------------------------------------
check "resume restores the units" resume_all
refuse "no kill flag after the resume" test -e "$CONTROL/killed"
check "the gatekeeper reports running again" wait_for 60 state_running
refuse "no pause flag (nothing went unhealthy meanwhile)" test -e "$CONTROL/paused"
echo "moved-aside state kept at $newest"
finish
