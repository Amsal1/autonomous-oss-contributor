# shellcheck shell=bash
# Step 'users': every user, group, uid and directory of DESIGN §7.3, plus the ossadmin
# login environment and the sudoers drop-in of PLAN Decision 3. Requires common.sh.

OSSBOT_SUDOERS=/etc/sudoers.d/ossbot-ossadmin

step_users() {
  # Groups. osscfg has a fixed gid: the drvfs fstab line uses gid=1500 (§7.2).
  ensure_group osscfg 1500
  local g
  for g in ossapi ossart osscontrol osssbx ossdock; do ensure_group "$g"; done

  # Humans: ossadmin (uid 1000, owns the drvfs mount) and ossnobody (1001, default WSL login).
  ensure_user ossadmin 1000 /home/ossadmin yes /bin/bash
  ensure_user ossnobody 1001 /home/ossnobody yes /bin/bash
  # Service users (system uids, user-private group = the per-file secrets group).
  ensure_user gatekeeper system /srv/ossbot/gatekeeper no /usr/sbin/nologin
  ensure_user ossrunner system /srv/ossbot/runner no /usr/sbin/nologin
  ensure_user ossbox system /srv/ossbot/sandboxd no /usr/sbin/nologin
  ensure_user hermes system /home/hermes yes /usr/sbin/nologin

  ensure_user_groups ossadmin sudo osscfg ossapi
  ensure_user_groups ossnobody
  ensure_user_groups gatekeeper osscfg ossart osscontrol
  ensure_user_groups ossrunner ossapi ossart osscontrol osssbx
  ensure_user_groups ossbox ossapi ossart osscontrol ossdock
  ensure_user_groups hermes ossapi
  # The docker group, if present, has no members (§7.3).
  if getent group docker >/dev/null && [[ -n $(getent group docker | cut -d: -f4) ]]; then
    gpasswd -M '' docker >/dev/null
    mark_changed "emptied group docker"
  fi

  # Directories (ext4).
  ensure_dir /srv/ossbot root root 0755
  ensure_dir /srv/ossbot/gatekeeper gatekeeper gatekeeper 0700
  ensure_dir /srv/ossbot/runner ossrunner ossrunner 0700
  ensure_dir /srv/ossbot/sandboxd ossbox ossbox 0700
  # dockerd resets its data root to 0710 on every start; root:root 0710 is still root-only.
  ensure_dir /srv/ossbot/docker root root 0710
  ensure_dir /srv/ossbot/logs root ossart 0750
  ensure_dir /srv/ossbot/artifacts ossbox ossart 0750
  ensure_dir /srv/ossbot/config-snapshot gatekeeper gatekeeper 0755
  ensure_dir /etc/ossbot root root 0755
  ensure_dir /etc/ossbot/secrets root root 0751
  ensure_dir /var/lib/ossbot root root 0755
  # setgid: flags written by the gatekeeper inherit group osscontrol (runner/sandboxd read them).
  ensure_dir /var/lib/ossbot/control gatekeeper osscontrol 2750
  ensure_dir /var/lib/ossbot/probes root gatekeeper 0750
  ensure_dir /var/lib/ossbot/staging root root 0700
  ensure_dir /opt/ossbot root root 0755
  ensure_dir /opt/ossbot/bin root root 0755
  ensure_dir /home/hermes hermes hermes 0750
  ensure_dir /home/ossadmin ossadmin ossadmin 0750
  ensure_dir /home/ossadmin/.venvs ossadmin ossadmin 0755

  # ossadmin login environment (dev venv on ext4, PLAN Decision 2).
  # shellcheck disable=SC2016  # $PATH/$HOME expand at login, not here
  ensure_managed_block /home/ossadmin/.profile ossadmin ossadmin 0644 <<'EOF'
export PATH="/opt/ossbot/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/ossbot"
EOF

  # root runs git read-only on the repo (gitleaks over the history, doctor); the repo is owned by
  # ossadmin on drvfs, so mark exactly this path safe for root (no repo config is touched).
  ensure_managed_block /root/.gitconfig root root 0644 <<EOF
[safe]
directory = ${OSSBOT_REPO:-/mnt/d/Work/autonomous-oss-contributor}
EOF

  # sudo: NOPASSWD only for the root helpers and ossctl; ossadmin has no password (Decision 3).
  local tmp
  tmp=$(mktemp)
  cat >"$tmp" <<'EOF'
# Managed by ossbot scripts/provision.sh (step users). PLAN Decision 3.
ossadmin ALL=(root) NOPASSWD: /opt/ossbot/bin/*, /opt/ossbot/app/.venv/bin/ossctl
EOF
  visudo -cqf "$tmp" || die "generated sudoers drop-in does not validate"
  ensure_file "$OSSBOT_SUDOERS" root root 0440 <"$tmp"
  rm -f "$tmp"
  visudo -cqf "$OSSBOT_SUDOERS" || die "$OSSBOT_SUDOERS does not validate"
}
