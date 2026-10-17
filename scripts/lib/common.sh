# shellcheck shell=bash
# Shared helpers for provision.sh steps. Sourced, never executed directly.
# Every ensure_* helper is idempotent and calls mark_changed only when it modified something.
# shellcheck disable=SC2034  # OSSBOT_STEP_CHANGED is read by provision.sh

OSSBOT_STEP_CHANGED=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { log "WARN: $*" >&2; }
die() { log "ERROR: $*" >&2; exit 1; }

mark_changed() {
  OSSBOT_STEP_CHANGED=1
  log "  changed: $*"
}

# ensure_group NAME [GID]
ensure_group() {
  local name=$1 gid=${2:-}
  if getent group "$name" >/dev/null; then
    if [[ -n $gid ]] && [[ $(getent group "$name" | cut -d: -f3) != "$gid" ]]; then
      die "group $name exists with gid $(getent group "$name" | cut -d: -f3), expected $gid"
    fi
    return 0
  fi
  if [[ -n $gid ]]; then
    groupadd --gid "$gid" "$name"
  else
    groupadd --system "$name"
  fi
  mark_changed "group $name"
}

# ensure_user NAME UID|system HOME CREATE_HOME(yes|no) SHELL
ensure_user() {
  local name=$1 uid=$2 home=$3 create_home=$4 shell=$5
  local -a args=(--home-dir "$home" --shell "$shell" --user-group)
  if getent passwd "$name" >/dev/null; then
    if [[ $uid != system ]] && [[ $(id -u "$name") != "$uid" ]]; then
      die "user $name exists with uid $(id -u "$name"), expected $uid"
    fi
    return 0
  fi
  if [[ $uid == system ]]; then args+=(--system); else args+=(--uid "$uid"); fi
  if [[ $create_home == yes ]]; then args+=(--create-home); else args+=(--no-create-home); fi
  useradd "${args[@]}" "$name"
  mark_changed "user $name"
}

# ensure_user_groups NAME [GROUP...]: supplementary groups are exactly the given set.
ensure_user_groups() {
  local name=$1; shift
  local want have primary
  primary=$(id -gn "$name")
  want=$(printf '%s\n' "$@" | sort -u | paste -sd, -)
  have=$(id -nG "$name" | tr ' ' '\n' | grep -vx "$primary" | sort -u | paste -sd, - || true)
  if [[ $want != "$have" ]]; then
    usermod -G "$want" "$name"
    mark_changed "groups of $name: ${have:-none} -> ${want:-none}"
  fi
}

# ensure_dir PATH OWNER GROUP MODE (mode as 4 octal digits, e.g. 0750)
ensure_dir() {
  local path=$1 owner=$2 group=$3 mode=$4
  if [[ -L $path ]]; then die "$path is a symlink"; fi
  if [[ ! -d $path ]]; then
    install -d -o "$owner" -g "$group" -m "$mode" "$path"
    mark_changed "dir $path"
    return 0
  fi
  local cur
  cur=$(stat -c '%U:%G %a' "$path")
  if [[ $cur != "$owner:$group ${mode#0}" ]]; then
    chown "$owner:$group" "$path"
    chmod "$mode" "$path"
    mark_changed "dir $path ($cur -> $owner:$group $mode)"
  fi
}

# ensure_file PATH OWNER GROUP MODE < content
ensure_file() {
  local path=$1 owner=$2 group=$3 mode=$4 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f $path && ! -L $path ]] && cmp -s "$tmp" "$path" \
     && [[ $(stat -c '%U:%G %a' "$path") == "$owner:$group ${mode#0}" ]]; then
    rm -f "$tmp"
    return 0
  fi
  install -o "$owner" -g "$group" -m "$mode" "$tmp" "$path"
  rm -f "$tmp"
  mark_changed "file $path"
}

# ensure_managed_block FILE OWNER GROUP MODE < block body
# Replaces the lines between '# BEGIN ossbot' and '# END ossbot' (appends the block if absent).
ensure_managed_block() {
  local file=$1 owner=$2 group=$3 mode=$4 body new
  body=$(cat)
  new=$(mktemp)
  if [[ -f $file ]]; then
    awk '/^# BEGIN ossbot$/{skip=1; next} /^# END ossbot$/{skip=0; next} !skip' "$file" >"$new"
  fi
  printf '# BEGIN ossbot\n%s\n# END ossbot\n' "$body" >>"$new"
  ensure_file "$file" "$owner" "$group" "$mode" <"$new"
  rm -f "$new"
}

# lock_get KEY.PATH: read a scalar from config/versions.lock.yaml (nested 2-space mappings).
lock_get() {
  local key=$1
  python3 -I - "$OSSBOT_REPO/config/versions.lock.yaml" "$key" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
stack = []
with open(path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = (len(line) - len(line.lstrip(" "))) // 2
        name, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith('"'):
            value = value[1:value.index('"', 1)]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        del stack[indent:]
        stack.append(name.strip())
        if ".".join(stack) == key and value:
            print(value)
            sys.exit(0)
sys.exit(f"versions.lock.yaml: key {key} not found")
PY
}

# download_verified URL SHA256 DEST
download_verified() {
  local url=$1 sha=$2 dest=$3
  [[ $sha =~ ^[0-9a-f]{64}$ ]] || die "no valid sha256 pinned for $url"
  curl -fsSL --retry 3 --proto '=https' -o "$dest" "$url"
  if [[ $(sha256sum "$dest" | cut -d' ' -f1) != "$sha" ]]; then
    rm -f "$dest"
    die "sha256 mismatch for $url"
  fi
}
