# shellcheck shell=bash
# Steps 'app-deploy', 'systemd', 'audit-anchors', 'secrets-seed', 'enable-start', 'doctor'
# (DESIGN §7.5, §12.7, §18.2, §18.3; PLAN Decision 5). Requires common.sh.

OSSBOT_APP=/opt/ossbot/app
OSSBOT_UNIT_DIR=/etc/systemd/system
OSSBOT_UNITS_CHANGED=0
OSSBOT_APP_CHANGED=0
# Units, boot gate first (§18.2 "boot gate enabled first"). The runner arrived with FEAT-006, Hermes
# and its probe socket with FEAT-007.
OSSBOT_ENABLE_UNITS=(
  ossbot-boot-gate.service ossbot-nointerop.service ossbot-firewall.service ossbot-firewall-docker.service
  ossbot-dockerfilter.service ossbot-gatekeeper.service ossbot-proxy.service ossbot-sandboxd.service
  ossbot-runner.service ossbot-hermes.service ossbot-hermes-probe.socket
  ossbot-anchor.socket ossbot-killswitch.path ossbot-resume.path ossbot-egress.path ossbot-watchdog.timer
  ossbot-hermes-tier.path ossbot-hermes-tier.timer ossbot-skills.path ossbot-skills-scan.timer
  ossbot-lifecycle.path ossbot-gc.timer ossbot-docker-prune.timer ossbot-proxy-rotate.timer
  ossbot-floorlog.service ossbot-pubip.timer
)
OSSBOT_BINS=(ossbot-firewall ossbot-boot-gate ossbot-kill ossbot-resume ossbot-proxy-run ossbot-egress-apply
  ossbot-hermes-tier ossbot-lifecycle ossbot-proxy-rotate)

# install_unit NAME [SUBDIR]: copy systemd/<SUBDIR>/NAME to /etc/systemd/system/<SUBDIR>/NAME.
install_unit() {
  local name=$1 sub=${2:-} before=$OSSBOT_STEP_CHANGED
  OSSBOT_STEP_CHANGED=0
  [[ -n $sub ]] && ensure_dir "$OSSBOT_UNIT_DIR/$sub" root root 0755
  ensure_file "$OSSBOT_UNIT_DIR/${sub:+$sub/}$name" root root 0644 <"$OSSBOT_REPO/systemd/${sub:+$sub/}$name"
  if ((OSSBOT_STEP_CHANGED)); then OSSBOT_UNITS_CHANGED=1; fi
  ((before)) && OSSBOT_STEP_CHANGED=1
  return 0
}

install_bins() {
  local b
  for b in "${OSSBOT_BINS[@]}"; do
    ensure_file "/opt/ossbot/bin/$b" root root 0755 <"$OSSBOT_REPO/scripts/bin/$b"
  done
}

# ---------------------------------------------------------------------------------- app deploy
step_app_deploy() {
  ensure_dir "$OSSBOT_APP" root root 0755
  ensure_dir /var/cache/ossbot-uv root root 0700
  local -a sources=()
  local p
  # LICENSE and NOTICE: pyproject's license-files, needed to build the wheel
  for p in src pyproject.toml uv.lock LICENSE NOTICE prompts vendor hermes sandbox/proxy; do
    [[ -e $OSSBOT_REPO/$p ]] && sources+=("$p")
  done
  # The wheel metadata names docs/DESIGN.md as the readme; ship it so the build is reproducible.
  sources+=(docs/DESIGN.md)
  local out content line n=0
  # --relative keeps sandbox/proxy and docs/DESIGN.md at the same relative paths; .venv is protected.
  # --checksum + --omit-dir-times: a touched file or a directory whose mtime moved (pytest creating
  # or removing an excluded __pycache__/) is not a deploy change (Phase 1 review P1-10).
  out=$(cd "$OSSBOT_REPO" && rsync -a --relative --delete --checksum --omit-dir-times --itemize-changes \
    --chown=root:root --chmod=D0755,F0644 --exclude '__pycache__/' --exclude '*.pyc' --filter 'P /.venv/' \
    "${sources[@]}" "$OSSBOT_APP/")
  if [[ -n $out ]]; then
    # Itemized lines go to the provision log so every change is traceable.
    while IFS= read -r line; do
      n=$((n + 1))
      if ((n <= 200)); then log "  rsync: $line"; fi
    done <<<"$out"
    ((n <= 200)) || log "  rsync: ... $((n - 200)) more item(s)"
    # Content changes only: deletions, new entries (+), checksum (c) or size (s) differences and
    # local changes such as a symlink target (Y = c). Time/permission/owner-only items are metadata.
    content=$(awk '/^\*deleting/ || substr($1, 1, 1) == "c" || substr($1, 3) ~ /\+/ ||
      substr($1, 3, 1) == "c" || substr($1, 4, 1) == "s"' <<<"$out")
    if [[ -n $content ]]; then
      mark_changed "rsync to $OSSBOT_APP ($(wc -l <<<"$content") content items, $n itemized)"
      OSSBOT_APP_CHANGED=1
    else
      log "  rsync: $n metadata-only item(s), no content change (no uv sync, no restarts)"
    fi
  fi
  if [[ $OSSBOT_APP_CHANGED == 1 || ! -x $OSSBOT_APP/.venv/bin/ossbot-gatekeeper ]]; then
    (cd "$OSSBOT_APP" && UV_PROJECT_ENVIRONMENT="$OSSBOT_APP/.venv" UV_CACHE_DIR=/var/cache/ossbot-uv \
      UV_PYTHON_DOWNLOADS=never /opt/ossbot/bin/uv sync --frozen --no-dev --no-editable \
      --python /usr/bin/python3.12 --reinstall-package ossbot --quiet)
    chmod -R go+rX "$OSSBOT_APP/.venv"
    mark_changed "uv sync --frozen --no-dev ($OSSBOT_APP/.venv)"
    OSSBOT_APP_CHANGED=1
  fi
  "$OSSBOT_APP/.venv/bin/python" -I -c 'import ossbot.gatekeeper.main, ossbot.dockerfilter.main, ossbot.sandboxd.main, ossbot.runner.main, ossbot.ctl.cli, ossbot.ctl.pubip, ossbot.watchdog.tick, ossbot.watchdog.floorlog' \
    || die "deployed app does not import"
}

# ------------------------------------------------------------------------------------- systemd
step_systemd() {
  install_bins
  ensure_dir /etc/tmpfiles.d root root 0755
  local before=$OSSBOT_STEP_CHANGED
  OSSBOT_STEP_CHANGED=0
  ensure_file /etc/tmpfiles.d/ossbot.conf root root 0644 <"$OSSBOT_REPO/systemd/tmpfiles.d/ossbot.conf"
  local tmp_changed=$OSSBOT_STEP_CHANGED
  ((before)) && OSSBOT_STEP_CHANGED=1
  if ((tmp_changed)) || [[ ! -d /run/ossbot ]] || [[ $(stat -c %a /run/ossbot) != 1775 ]] \
     || ! getfacl -cp /run/ossbot 2>/dev/null | grep -qx 'user:gatekeeper:rwx'; then
    systemd-tmpfiles --create /etc/tmpfiles.d/ossbot.conf
    mark_changed "systemd-tmpfiles /run/ossbot"
  fi
  local f name
  for f in "$OSSBOT_REPO"/systemd/*.service "$OSSBOT_REPO"/systemd/*.socket "$OSSBOT_REPO"/systemd/*.path \
           "$OSSBOT_REPO"/systemd/*.timer; do
    name=${f##*/}
    install_unit "$name"
  done
  install_unit ossbot.conf docker.service.d
  if ((OSSBOT_UNITS_CHANGED)); then
    systemctl daemon-reload
    mark_changed "systemctl daemon-reload"
  fi
  # Journal retention (DESIGN §18.10): time-based only; journald reads its drop-ins at start.
  ensure_dir /etc/systemd/journald.conf.d root root 0755
  before=$OSSBOT_STEP_CHANGED
  OSSBOT_STEP_CHANGED=0
  ensure_file /etc/systemd/journald.conf.d/60-ossbot.conf root root 0644 \
    <"$OSSBOT_REPO/systemd/journald.conf.d/ossbot.conf"
  local journald_changed=$OSSBOT_STEP_CHANGED
  ((before)) && OSSBOT_STEP_CHANGED=1
  if ((journald_changed)); then
    systemctl restart systemd-journald || die "restart systemd-journald failed"
    mark_changed "restarted systemd-journald (retention drop-in)"
  fi
  local u
  for u in "${OSSBOT_ENABLE_UNITS[@]}"; do
    if ! systemctl is-enabled --quiet "$u" 2>/dev/null; then
      systemctl enable "$u" >/dev/null 2>&1 || die "systemctl enable $u failed"
      mark_changed "enabled $u"
    fi
  done
}

# ------------------------------------------------------------------------------- audit anchors
step_audit_anchors() {
  local log=/srv/ossbot/logs/audit-anchors.log
  if [[ ! -e $log ]]; then
    install -o root -g root -m 0644 /dev/null "$log"
    mark_changed "created $log"
  fi
  if [[ $(stat -c '%U:%G %a' "$log") != "root:root 644" ]]; then
    chattr -a "$log"; chown root:root "$log"; chmod 0644 "$log"
    mark_changed "owner/mode of $log"
  fi
  if ! lsattr -d "$log" 2>/dev/null | awk '{print $1}' | grep -q a; then
    chattr +a "$log"
    mark_changed "chattr +a $log"
  fi
  if [[ ! -e /srv/ossbot/logs/resume.log ]]; then
    install -o root -g root -m 0644 /dev/null /srv/ossbot/logs/resume.log
    mark_changed "created resume.log"
  fi
  # Log dirs written by sandboxd (runs) and the unprivileged squid (proxy, uid 13; setgid ossart so
  # ossbox can read access.log for the workspace probe).
  ensure_dir /srv/ossbot/logs/runs ossbox ossart 0750
  if [[ ! -d /srv/ossbot/logs/proxy ]]; then
    install -d -o proxy -g ossart -m 2750 /srv/ossbot/logs/proxy
    mark_changed "dir /srv/ossbot/logs/proxy"
  elif [[ $(stat -c '%U:%G %a' /srv/ossbot/logs/proxy) != "proxy:ossart 2750" ]]; then
    chown proxy:ossart /srv/ossbot/logs/proxy; chmod 2750 /srv/ossbot/logs/proxy
    mark_changed "owner/mode of /srv/ossbot/logs/proxy"
  fi
  ensure_dir /etc/ossbot/proxy root root 0755
  # ADR-0025 public-address floor: root-only writable (pubip, the render's sentinels, the 0600 root lock of
  # C-M1); squid (uid 13) and ossbox read public.txt, code-web.txt and state.json.
  ensure_dir /etc/ossbot/proxy/floor root root 0755
  # ADR-0025 kernel floor log: written by ossbot-floorlog (root), read by sandboxd (group ossbox).
  ensure_dir /srv/ossbot/logs/floor root ossbox 0750
  local floor=/srv/ossbot/logs/floor/floor.log
  if [[ ! -e $floor ]]; then
    install -o root -g ossbox -m 0640 /dev/null "$floor"
    mark_changed "created $floor"
  elif [[ $(stat -c '%U:%G %a' "$floor") != "root:ossbox 640" ]]; then
    chown root:ossbox "$floor"; chmod 0640 "$floor"
    mark_changed "owner/mode of $floor"
  fi
}

# -------------------------------------------------------------------------------- secrets seed
_random() { python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))'; }

# _env_get FILE KEY
_env_get() { [[ -f $1 ]] && sed -n "s/^$2=//p" "$1" | tail -n 1; return 0; }

# _env_seed FILE KEY VALUE: append KEY=VALUE when KEY is absent (never rewrites a value).
_env_seed() {
  local file=$1 key=$2 value=$3
  if [[ -z $(_env_get "$file" "$key") ]]; then
    sed -i "/^$key=/d" "$file"
    printf '%s=%s\n' "$key" "$value" >>"$file"
    mark_changed "seeded $key in ${file##*/}"
  fi
}

step_secrets_seed() {
  local dir=/etc/ossbot/secrets f g
  ensure_dir "$dir" root root 0751
  for f in gatekeeper:gatekeeper runner:ossrunner sandboxd:ossbox hermes:hermes; do
    g=${f#*:}
    f="$dir/${f%%:*}.env"
    if [[ ! -e $f ]]; then
      install -o root -g "$g" -m 0640 /dev/null "$f"
      mark_changed "created ${f##*/}"
    elif [[ $(stat -c '%U:%G %a' "$f") != "root:$g 640" ]]; then
      chown "root:$g" "$f"; chmod 0640 "$f"
      mark_changed "owner/mode of ${f##*/}"
    fi
  done
  # PLAN Decision 5: only the internal randoms; user secrets come from setup-secrets.
  _env_seed "$dir/gatekeeper.env" OSSBOT_TOKEN_PEPPER "$(_random)"
  _env_seed "$dir/gatekeeper.env" HERMES_API_KEY "$(_random)"
  local key
  key=$(_env_get "$dir/gatekeeper.env" HERMES_API_KEY)
  _env_seed "$dir/hermes.env" API_SERVER_ENABLED true
  if [[ $(_env_get "$dir/hermes.env" API_SERVER_KEY) != "$key" ]]; then
    sed -i '/^API_SERVER_KEY=/d' "$dir/hermes.env"
    printf 'API_SERVER_KEY=%s\n' "$key" >>"$dir/hermes.env"
    mark_changed "API_SERVER_KEY in hermes.env = HERMES_API_KEY"
  fi
}

# -------------------------------------------------------------------------------- enable/start
_flag_level() {
  local f=/var/lib/ossbot/control/killed level
  [[ -e $f || -L $f ]] || { echo none; return; }
  level=$(head -c 4096 "$f" | jq -r 'if type == "object" then (.level // "") else "" end' 2>/dev/null || true)
  if [[ $level == soft ]]; then echo soft; else echo full; fi
}

step_enable_start() {
  local level u
  level=$(_flag_level)
  # Root helpers always run; the rest follows the kill level (ExecCondition / ConditionPathExists
  # skip them anyway, this keeps the output honest).
  local -a units=(ossbot-boot-gate.service ossbot-nointerop.service ossbot-firewall.service docker.service
    ossbot-firewall-docker.service ossbot-dockerfilter.service ossbot-anchor.socket ossbot-killswitch.path
    ossbot-resume.path ossbot-egress.path ossbot-watchdog.timer ossbot-hermes-tier.path ossbot-hermes-tier.timer
    ossbot-skills.path ossbot-skills-scan.timer ossbot-lifecycle.path ossbot-gc.timer ossbot-docker-prune.timer
    ossbot-proxy-rotate.timer ossbot-floorlog.service ossbot-pubip.timer)
  case $level in
    none) units+=(ossbot-gatekeeper.service ossbot-proxy.service ossbot-sandboxd.service ossbot-runner.service
      ossbot-hermes.service ossbot-hermes-probe.socket) ;;
    soft) units+=(ossbot-gatekeeper.service ossbot-proxy.service) ;;
  esac
  [[ $level == none ]] || warn "killed (level $level): not starting the units the kill stops"
  if ((OSSBOT_APP_CHANGED || OSSBOT_UNITS_CHANGED)); then
    # New code or units: restart the running services (dependency order; each re-checks the
    # firewall in ExecStartPre). The firewall units are one-shot appliers: step 'firewall' already
    # re-applied them on drift, and restarting them would stop every unit that Requires= them.
    for u in ossbot-floorlog.service ossbot-dockerfilter.service ossbot-gatekeeper.service ossbot-proxy.service \
      ossbot-sandboxd.service ossbot-runner.service ossbot-hermes.service; do
      if systemctl is-active --quiet "$u"; then
        systemctl restart "$u" || die "restart $u failed"
        mark_changed "restarted $u"
      fi
    done
  fi
  for u in "${units[@]}"; do
    if ! systemctl is-active --quiet "$u"; then
      systemctl reset-failed "$u" >/dev/null 2>&1 || true
      systemctl start "$u" || die "start $u failed (journalctl -u $u)"
      mark_changed "started $u"
    fi
  done
  for u in "${units[@]}"; do
    systemctl is-active --quiet "$u" || die "$u is not active after start"
  done
}

# -------------------------------------------------------------------------------------- doctor
step_doctor() {
  # Quick in-distro doctor (no live calls); failures are reported, never fatal for provisioning.
  "$OSSBOT_APP/.venv/bin/ossctl" doctor --quick || warn "ossctl doctor --quick reported problems"
}
