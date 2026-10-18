# shellcheck shell=bash
# WSL confinement (DESIGN §7.2, ADR-0007). Streamed by scripts/bootstrap.ps1 as root, concatenated
# after lib/common.sh and lib/users.sh (never executed from the drvfs mount, N4):
#   ... | wsl.exe -d ossbot -u root --exec bash -s -- wsl apply [--form 1|2|3]
#   ... | wsl.exe -d ossbot -u root --exec bash -s -- wsl check
#   ... | wsl.exe -d ossbot -u root --exec bash -s -- wsl rollback
# 'apply' creates the users (ossnobody becomes the default login), writes /etc/wsl.conf and the
# managed '# BEGIN ossbot' fstab block (backups first: /etc/fstab.ossbot.bak,
# /etc/wsl.conf.ossbot.bak), and prepares the mountpoint on ext4. It prints 'wsl: changed' or
# 'wsl: unchanged'; the caller restarts WSL when changed and runs 'check'.

OSSBOT_MOUNT=/mnt/d/Work/autonomous-oss-contributor
OSSBOT_FSTAB_OPTS='drvfs metadata,uid=1000,gid=1500,umask=002 0 0'

# Source forms in the order to try (§7.2 [VERIFY]): \134 escapes, forward slashes, doubled backslashes.
wsl_fstab_source() {
  case $1 in
    1) printf '%s' 'D:\134Work\134autonomous-oss-contributor' ;;
    2) printf '%s' 'D:/Work/autonomous-oss-contributor' ;;
    3) printf '%s' 'D:\\Work\\autonomous-oss-contributor' ;;
    *) die "unknown fstab form $1" ;;
  esac
}

wsl_conf_content() {
  cat <<'EOF'
# Managed by ossbot scripts/lib/wsl.sh (DESIGN §7.2). Do not edit.
[boot]
systemd=true

[user]
default=ossnobody

[automount]
enabled=false
mountFsTab=true

[interop]
enabled=false
appendWindowsPath=false
EOF
}

# Run a command against the plain ext4 root (without the submounts hiding it), e.g. to create the
# mountpoint under a live drvfs mount or to remove the now-unused /mnt/c directory.
with_ext4_root() {
  local bind
  bind=$(mktemp -d)
  mount --bind / "$bind"
  local rc=0
  "$@" "$bind" || rc=$?
  umount "$bind"
  rmdir "$bind"
  return "$rc"
}

_prepare_mountpoint() {
  local root=$1 d
  if [[ ! -d $root$OSSBOT_MOUNT ]]; then
    mkdir -p "$root$OSSBOT_MOUNT"
    mark_changed "mountpoint $OSSBOT_MOUNT on ext4"
  fi
  # Automount leftovers: empty ext4 directories for drive letters other than the mount path.
  for d in "$root"/mnt/?; do
    [[ -d $d && $d != "$root/mnt/d" ]] || continue
    if rmdir "$d" 2>/dev/null; then mark_changed "removed empty ext4 dir ${d#"$root"}"; fi
  done
  for d in "$root"/mnt/d/*; do
    [[ -d $d && $d != "$root/mnt/d/Work" ]] || continue
    if rmdir "$d" 2>/dev/null; then mark_changed "removed empty ext4 dir ${d#"$root"}"; fi
  done
}

wsl_apply() {
  local form=1
  while (($#)); do
    case $1 in
      --form) form=${2:?}; shift 2 ;;
      *) die "wsl apply: unknown argument $1" ;;
    esac
  done
  step_users

  local want_conf want_line
  want_conf=$(wsl_conf_content)
  want_line="$(wsl_fstab_source "$form") $OSSBOT_MOUNT $OSSBOT_FSTAB_OPTS"
  # Back up the pre-ossbot files once (the rollback target).
  [[ -e /etc/fstab.ossbot.bak ]] || { cp -p /etc/fstab /etc/fstab.ossbot.bak; mark_changed "backup /etc/fstab.ossbot.bak"; }
  [[ -e /etc/wsl.conf.ossbot.bak ]] || {
    if [[ -e /etc/wsl.conf ]]; then cp -p /etc/wsl.conf /etc/wsl.conf.ossbot.bak; else : >/etc/wsl.conf.ossbot.bak; fi
    mark_changed "backup /etc/wsl.conf.ossbot.bak"
  }
  ensure_file /etc/wsl.conf root root 0644 <<<"$want_conf"
  ensure_managed_block /etc/fstab root root 0644 <<<"$want_line"
  with_ext4_root _prepare_mountpoint
  wsl_nointerop
}

# Verified live on WSL 3.0.3 (ADR-0007 addendum): with [interop] enabled=false the WSLInterop
# binfmt entry is still registered at boot (handler /init, which then fails on the vsock), so a PE
# file does not fail with ENOEXEC. A boot-time oneshot removes the entry. It also closes WSL's
# interop sockets (/run/WSL/<pid>_interop, mode 0777) to every non-root uid (egress threat table,
# ADR-0007 addendum): with interop disabled the host never answers them, and now no service uid
# can even connect.
wsl_nointerop() {
  ensure_file /etc/systemd/system/ossbot-nointerop.service root root 0644 <<'EOF'
# Managed by ossbot scripts/lib/wsl.sh (DESIGN §7.2): no Windows binfmt handler in this distro,
# and WSL's interop sockets under /run/WSL are root-only.
[Unit]
Description=ossbot: remove the WSLInterop binfmt_misc entry, close /run/WSL (interop disabled)
DefaultDependencies=no
After=systemd-binfmt.service proc-sys-fs-binfmt_misc.mount
Before=sysinit.target ossbot-boot-gate.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /proc/sys/fs/binfmt_misc/WSLInterop*; do [ -e "$f" ] && echo -1 >"$f"; done; [ -d /run/WSL ] && chmod 0700 /run/WSL; exit 0'

[Install]
WantedBy=sysinit.target
EOF
  if [[ ! -L /etc/systemd/system/sysinit.target.wants/ossbot-nointerop.service ]]; then
    mkdir -p /etc/systemd/system/sysinit.target.wants
    ln -sf ../ossbot-nointerop.service /etc/systemd/system/sysinit.target.wants/ossbot-nointerop.service
    mark_changed "enabled ossbot-nointerop.service"
  fi
  local f
  for f in /proc/sys/fs/binfmt_misc/WSLInterop*; do
    [[ -e $f ]] || continue
    echo -1 >"$f"
    mark_changed "removed binfmt entry ${f##*/}"
  done
  if [[ -d /run/WSL && $(stat -c %a /run/WSL) != 700 ]]; then
    chmod 0700 /run/WSL
    mark_changed "chmod 0700 /run/WSL"
  fi
}

wsl_check() {
  # The repo must be a drvfs mount with the §7.2 options and the only drvfs/9p mount there is.
  local fstype opts others
  fstype=$(findmnt -n -o FSTYPE --target "$OSSBOT_MOUNT" 2>/dev/null || true)
  opts=$(findmnt -n -o OPTIONS --mountpoint "$OSSBOT_MOUNT" 2>/dev/null || true)
  if [[ -z $opts ]] || [[ $fstype != 9p && $fstype != drvfs && $fstype != v9fs ]]; then
    printf 'wsl check: FAIL %s is not mounted (fstype=%s)\n' "$OSSBOT_MOUNT" "${fstype:-none}"
    return 3  # the only failure the fallback fstab forms can fix
  fi
  others=$(findmnt -rn -o TARGET,FSTYPE | awk -v m="$OSSBOT_MOUNT" '($2=="9p"||$2=="drvfs"||$2=="v9fs") && $1!=m && $1 !~ /^\/usr\/lib\/wsl/ && $1 !~ /^\/mnt\/wsl/ {print $1}')
  if [[ -n $others ]]; then
    printf 'wsl check: FAIL other Windows mounts present: %s\n' "$(tr '\n' ' ' <<<"$others")"
    return 1
  fi
  [[ -e /mnt/c ]] && { printf 'wsl check: FAIL /mnt/c exists\n'; return 1; }
  if compgen -G '/proc/sys/fs/binfmt_misc/WSLInterop*' >/dev/null; then
    printf 'wsl check: FAIL WSLInterop binfmt entry registered\n'
    return 1
  fi
  if [[ -d /run/WSL && $(stat -c %a /run/WSL) != 700 ]]; then
    printf 'wsl check: FAIL /run/WSL (interop sockets) is not root-only\n'
    return 1
  fi
  [[ -d $OSSBOT_MOUNT/scripts/lib ]] || { printf 'wsl check: FAIL repo content not visible\n'; return 1; }
  printf 'wsl check: PASS %s (%s %s)\n' "$OSSBOT_MOUNT" "$fstype" "$opts"
}

wsl_rollback() {
  [[ -e /etc/fstab.ossbot.bak ]] || die "no /etc/fstab.ossbot.bak to roll back to"
  cp -p /etc/fstab.ossbot.bak /etc/fstab
  if [[ -s /etc/wsl.conf.ossbot.bak ]]; then cp -p /etc/wsl.conf.ossbot.bak /etc/wsl.conf; fi
  printf 'wsl rollback: restored /etc/fstab (and /etc/wsl.conf) from the .ossbot.bak copies\n'
}

wsl_main() {
  exec </dev/null
  [[ $(id -u) == 0 ]] || die "wsl.sh must run as root"
  local cmd=${1:-}
  shift || true
  OSSBOT_STEP_CHANGED=0
  case $cmd in
    apply)
      wsl_apply "$@"
      if ((OSSBOT_STEP_CHANGED)); then printf 'wsl: changed\n'; else printf 'wsl: unchanged\n'; fi
      ;;
    check) wsl_check ;;
    rollback) wsl_rollback ;;
    *) printf 'usage: wsl.sh apply [--form N] | check | rollback\n' >&2; return 2 ;;
  esac
}
