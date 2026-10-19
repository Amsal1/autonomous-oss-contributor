# shellcheck shell=bash
# Steps 'kiro-cli-hermes' and 'hermes' (DESIGN §15.1, §15.2, §15.4; ADR-0002, ADR-0011 addenda).
# Requires common.sh. Root never runs code from, or writes through a link inside, the hermes-writable
# home: files hermes owns are written by uid hermes itself (runuser) from root-owned staging copies;
# the few root-owned paths inside ~/.hermes (active skills, hooks, .env) are created only after every
# hermes process is stopped and every path involved has been checked for links (R4-N4).

HERMES_USER_HOME=/home/hermes
HERMES_DIR=$HERMES_USER_HOME/.hermes
HERMES_CODE=$HERMES_DIR/hermes-agent
HERMES_STAMP=/var/lib/ossbot/hermes.stamp
HERMES_UPGRADE_LOCK=/run/ossbot/hermes-upgrade.lock
HERMES_PROXY_URL=http://10.212.0.2:3141
HERMES_MANAGED=/etc/hermes
# Extras installed from the tag's uv.lock (no lazy installs at run time: the unit sets
# HERMES_DISABLE_LAZY_INSTALLS=1). messaging = aiohttp for the API server; exa, firecrawl and
# parallel-web = the SDKs of the keyless web-tool vendors (research tier). No browser or computer use.
HERMES_EXTRAS=(messaging exa firecrawl parallel-web)
OSSBOT_HERMES_QUIESCED=0

# as_hermes CMD...: run as uid hermes from cwd / (root's cwd may be unreadable to hermes).
as_hermes() { (cd / && runuser -u hermes -- "$@"); }

refuse_link() {
  local p
  for p in "$@"; do
    if [[ -L $p ]]; then die "refusing: $p is a symlink"; fi
  done
}

# hermes_quiesce: stop Hermes and its probe socket and kill every hermes process, once per run.
hermes_quiesce() {
  ((OSSBOT_HERMES_QUIESCED)) && return 0
  systemctl stop ossbot-hermes-probe.socket ossbot-hermes.service >/dev/null 2>&1 || true
  pkill -KILL -u hermes || true
  sleep 1
  if pgrep -u hermes >/dev/null; then die "refusing: hermes processes still running"; fi  # not `! pgrep` (set -e)
  OSSBOT_HERMES_QUIESCED=1
  log "  stopped Hermes and every hermes process"
}

# hermes_resume: start Hermes again after a quiesce, unless killed or paused (the probe re-gates chat).
hermes_resume() {
  ((OSSBOT_HERMES_QUIESCED)) || return 0
  OSSBOT_HERMES_QUIESCED=0
  if [[ -e /var/lib/ossbot/control/killed || -e /var/lib/ossbot/control/paused ]]; then
    log "  killed or paused: Hermes stays stopped"
    return 0
  fi
  if [[ -e /etc/systemd/system/ossbot-hermes.service ]]; then
    systemctl start ossbot-hermes.service ossbot-hermes-probe.socket || warn "could not start Hermes again"
  fi
}

# hermes_proxy_env: the env for hermes-side downloads. The hermes uid reaches only squid port 3141 once
# the firewall is applied; on the first provisioning (no OSSBOT-OUT chain yet) downloads go direct.
hermes_proxy_env() {
  if iptables -S OSSBOT-OUT >/dev/null 2>&1; then
    systemctl is-active --quiet ossbot-proxy.service || die "the firewall is up but ossbot-proxy is not running"
    printf '%s\n' "HTTPS_PROXY=$HERMES_PROXY_URL" "https_proxy=$HERMES_PROXY_URL" \
      "HTTP_PROXY=$HERMES_PROXY_URL" "http_proxy=$HERMES_PROXY_URL"
  fi
}

# hermes_install_file SRC DEST MODE: DEST (hermes-owned) gets exactly SRC's bytes, written by hermes.
hermes_install_file() {
  local src=$1 dest=$2 mode=$3 stage
  if [[ -f $dest && ! -L $dest ]] && cmp -s "$src" "$dest"; then return 0; fi
  hermes_quiesce
  refuse_link "$dest" "$(dirname "$dest")"
  stage=$(mktemp -d /var/lib/ossbot/hermes-stage.XXXXXX)
  chmod 0755 "$stage"
  install -o root -g root -m 0644 "$src" "$stage/file"
  as_hermes install -D -m "$mode" "$stage/file" "$dest"
  rm -rf "$stage"
  mark_changed "file $dest"
}

# hermes_dir PATH MODE: a hermes-owned directory (created by hermes).
hermes_dir() {
  local path=$1 mode=$2
  refuse_link "$path"
  if [[ -d $path && $(stat -c '%U:%G %a' "$path") == "hermes:hermes ${mode#0}" ]]; then return 0; fi
  hermes_quiesce
  if [[ -e $path ]] && [[ $(stat -c %U "$path") != hermes ]]; then die "$path is not owned by hermes"; fi
  as_hermes install -d -m "$mode" "$path"
  as_hermes chmod "$mode" "$path"
  mark_changed "dir $path"
}

# hermes_root_dir PATH: root:hermes 0750 inside the hermes home (read-only for Hermes).
hermes_root_dir() {
  local path=$1
  refuse_link "$path"
  if [[ -d $path && $(stat -c '%U:%G %a' "$path") == "root:hermes 750" ]]; then return 0; fi
  hermes_quiesce
  refuse_link "$path" "$(dirname "$path")"
  if [[ -e $path && ! -d $path ]]; then die "$path is not a directory"; fi
  # an existing hermes-made tree becomes root-owned; nothing inside it may be a link or special file
  if [[ -d $path ]] && find "$path" -mindepth 1 \( -type l -o ! -type f ! -type d \) -print -quit | grep -q .; then
    die "refusing: $path contains a link or special file"
  fi
  install -d -o root -g hermes -m 0750 "$path"
  chown -R --no-dereference root:hermes "$path"
  find "$path" -type d -exec chmod 0750 {} + -o -type f -exec chmod 0640 {} +
  mark_changed "root-owned dir $path"
}

# hermes_tree SRC DEST: DEST (hermes-owned) mirrors the root-owned SRC (bytecode caches aside).
hermes_tree() {
  local src=$1 dest=$2
  refuse_link "$dest"
  if [[ -d $dest ]] && diff -rq --no-dereference -x __pycache__ "$src" "$dest" >/dev/null 2>&1; then return 0; fi
  hermes_quiesce
  refuse_link "$dest" "$(dirname "$dest")"
  as_hermes mkdir -p "$dest"
  as_hermes rsync -a --delete --no-links --exclude '__pycache__/' "$src/" "$dest/"
  mark_changed "tree $dest"
}

# --------------------------------------------------------------------------- kiro-cli-hermes
step_kiro_cli_hermes() {
  local version url sha have tmp bindir=$HERMES_USER_HOME/.local/bin f
  version=$(lock_get kiro_cli.version)
  url=$(lock_get kiro_cli.headless_tar_gz_url)
  sha=$(lock_get kiro_cli.headless_tar_gz_sha256)
  [[ -d $HERMES_USER_HOME && ! -L $HERMES_USER_HOME ]] || die "$HERMES_USER_HOME missing (step users)"
  hermes_dir "$HERMES_USER_HOME/.local" 0700
  hermes_dir "$bindir" 0700
  have=""
  if [[ -f $bindir/kiro-cli && ! -L $bindir/kiro-cli ]]; then
    have=$( (as_hermes "$bindir/kiro-cli" --version 2>/dev/null || true) | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
  fi
  local before=${have:-absent}
  if [[ $have != "$version" ]]; then
    tmp=$(mktemp -d /var/lib/ossbot/kiro-hermes.XXXXXX)
    chmod 0755 "$tmp"
    download_verified "$url" "$sha" "$tmp/kiro.tar.gz"
    mkdir -p "$tmp/x"
    tar -xzf "$tmp/kiro.tar.gz" -C "$tmp/x" --no-same-owner
    local src
    src=$(dirname "$(find "$tmp/x" -type f -name kiro-cli -perm -u+x | head -n 1)")
    [[ -x $src/kiro-cli ]] || die "kiro-cli not found in the headless tarball"
    chmod -R a+rX "$tmp"
    hermes_quiesce
    refuse_link "$bindir"
    for f in "$src"/*; do
      [[ -f $f && ! -L $f ]] || continue
      refuse_link "$bindir/${f##*/}"
      as_hermes install -m 0755 "$f" "$bindir/${f##*/}"
    done
    rm -rf "$tmp"
    have=$( (as_hermes "$bindir/kiro-cli" --version 2>/dev/null || true) | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
    [[ $have == "$version" ]] || die "kiro-cli for hermes reports '$have', expected $version"
    mark_changed "kiro-cli for hermes $before -> $version"
  fi
  hermes_dir "$HERMES_USER_HOME/.kiro" 0700
  hermes_dir "$HERMES_USER_HOME/.kiro/settings" 0700
  hermes_dir "$HERMES_USER_HOME/.kiro/agents" 0700
  hermes_install_file "$OSSBOT_REPO/sandbox/kiro/settings/cli.json" "$HERMES_USER_HOME/.kiro/settings/cli.json" 0600
  hermes_install_file "$OSSBOT_REPO/hermes/config/permissions.yaml" \
    "$HERMES_USER_HOME/.kiro/settings/permissions.yaml" 0600
  hermes_install_file "$OSSBOT_REPO/.kiro/agents/ossbot-chat.json" "$HERMES_USER_HOME/.kiro/agents/ossbot-chat.json" 0600
  hermes_resume
}

# ------------------------------------------------------------------------------------ hermes
_hermes_head() {  # the checked-out commit, read as data (root never runs git inside the hermes tree)
  local head=$HERMES_CODE/.git/HEAD
  [[ -f $head && ! -L $head && ! -L $HERMES_CODE && ! -L $HERMES_CODE/.git ]] || return 0
  head -c 64 "$head" | tr -d '[:space:]'
}

_hermes_installed() {
  local commit=$1
  [[ $(_hermes_head) == "$commit" ]] || return 1
  [[ -f $HERMES_STAMP && $(cat "$HERMES_STAMP") == "$commit ${HERMES_EXTRAS[*]}" ]] || return 1
  as_hermes test -x "$HERMES_CODE/.venv/bin/hermes" || return 1
}

_hermes_install() {
  local tag=$1 tag_object=$2 commit=$3 repo=$4
  local H=$HERMES_DIR D=$HERMES_CODE S=/var/lib/ossbot/staging/hermes-$tag
  hermes_quiesce
  if [[ $(_hermes_head) == "$commit" ]] && as_hermes test -x "$D/.venv/bin/hermes"; then
    _hermes_sync "$D" || die "uv sync for Hermes $tag failed (tree kept as is)"
    printf '%s\n' "$commit ${HERMES_EXTRAS[*]}" >"$HERMES_STAMP"
    chmod 0644 "$HERMES_STAMP"
    mark_changed "Hermes $tag dependencies (extras ${HERMES_EXTRAS[*]})"
    return 0
  fi
  refuse_link "$HERMES_USER_HOME" "$H" "$D" "$D.prev" "$D.failed"
  [[ $(stat -c %U:%F "$H") == "hermes:directory" ]] || die "$H must be a directory owned by hermes"
  ensure_dir /var/lib/ossbot/staging root root 0700
  rm -rf --one-file-system "$S"
  # root clones only into root-owned staging: a fresh clone runs no repository code (no hooks, no filters)
  git -c core.hooksPath=/dev/null -c advice.detachedHead=false clone --quiet --depth 1 --branch "$tag" "$repo" "$S"
  [[ $(git -C "$S" rev-parse "refs/tags/$tag") == "$tag_object" ]] || { rm -rf "$S"; die "hermes tag object mismatch"; }
  [[ $(git -C "$S" rev-parse HEAD) == "$commit" ]] || { rm -rf "$S"; die "hermes commit mismatch"; }
  chown -R --no-dereference hermes:hermes "$S"
  rm -rf --one-file-system "$D.prev"
  if [[ -e $D ]]; then mv -T "$D" "$D.prev"; fi
  mv -T "$S" "$D"
  # uv builds the venv at its final path (venvs are not relocatable); only the tag's uv.lock is used
  if ! _hermes_sync "$D"; then
    pkill -KILL -u hermes || true
    sleep 1
    refuse_link "$D" "$D.prev" "$D.failed"
    rm -rf --one-file-system "$D.failed"
    mv -T "$D" "$D.failed"
    if [[ -e $D.prev ]]; then mv -T "$D.prev" "$D"; fi
    die "uv sync for Hermes $tag failed; the previous tree was restored"
  fi
  printf '%s\n' "$commit ${HERMES_EXTRAS[*]}" >"$HERMES_STAMP"
  chmod 0644 "$HERMES_STAMP"
  mark_changed "Hermes $tag ($commit)"
}

# _hermes_sync DIR: uv sync --frozen as hermes, with the pinned uv and a managed CPython.
_hermes_sync() {
  local dir=$1 e line
  local -a env=(HOME="$HERMES_USER_HOME" UV_CACHE_DIR="$HERMES_USER_HOME/.cache/uv" UV_NO_CONFIG=1
    UV_PYTHON_INSTALL_DIR="$HERMES_USER_HOME/.local/share/uv/python" UV_PYTHON_PREFERENCE=only-managed)
  local -a extras=()
  for e in "${HERMES_EXTRAS[@]}"; do extras+=(--extra "$e"); done
  while IFS= read -r line; do [[ -n $line ]] && env+=("$line"); done < <(hermes_proxy_env)
  as_hermes env "${env[@]}" /opt/ossbot/bin/uv sync --frozen --no-dev "${extras[@]}" --project "$dir" --quiet
}

# _github_mcp_host: GHMCP-4, the read-only GitHub MCP server for Hermes: the pinned release binary
# (sha256-verified) and the ossbot-github-mcp wrapper (fixed --read-only flags + startup check), both
# root-owned 0755 in /opt/ossbot/bin; Hermes starts only the wrapper (managed config, tiers research+).
_github_mcp_host() {
  local version url sha bin=/opt/ossbot/bin/github-mcp-server wrap=/opt/ossbot/bin/ossbot-github-mcp have tmp
  local src=/opt/ossbot/app/src/ossbot/sandbox_tools/github_mcp.py
  version=$(lock_get github_mcp_server.version)
  url=$(lock_get github_mcp_server.url)
  sha=$(lock_get github_mcp_server.sha256)
  [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $sha =~ ^[0-9a-f]{64}$ ]] || die "github_mcp_server not pinned"
  [[ $url == https://github.com/github/github-mcp-server/releases/download/* ]] || die "github_mcp_server.url"
  ensure_dir /opt/ossbot/bin root root 0755
  have=""
  if [[ -f $bin && ! -L $bin ]]; then
    have=$( (as_hermes "$bin" --version 2>/dev/null || true) | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
  fi
  if [[ $have != "$version" ]]; then
    tmp=$(mktemp -d /var/lib/ossbot/ghmcp.XXXXXX)
    download_verified "$url" "$sha" "$tmp/ghmcp.tar.gz"
    tar -xzf "$tmp/ghmcp.tar.gz" -C "$tmp" --no-same-owner github-mcp-server
    refuse_link "$bin"
    install -o root -g root -m 0755 "$tmp/github-mcp-server" "$bin"
    rm -rf "$tmp"
    mark_changed "github-mcp-server ${have:-absent} -> $version"
  fi
  [[ -f $src && ! -L $src ]] || die "app-deploy first ($src missing)"
  if [[ ! -f $wrap || -L $wrap ]] || ! cmp -s "$src" "$wrap"; then
    refuse_link "$wrap"
    install -o root -g root -m 0755 "$src" "$wrap"
    mark_changed "file $wrap"
  fi
}

step_hermes() {
  local tag tag_object commit repo lockfd
  tag=$(lock_get hermes.tag)
  tag_object=$(lock_get hermes.tag_object)
  commit=$(lock_get hermes.peeled_commit)
  repo=$(lock_get hermes.repo)
  [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "hermes.tag '$tag' is not a version tag"
  [[ $tag_object =~ ^[0-9a-f]{40}$ && $commit =~ ^[0-9a-f]{40}$ ]] || die "hermes tag object/commit not pinned"
  [[ $repo == https://github.com/* ]] || die "hermes.repo must be a GitHub https URL"
  [[ -d /opt/ossbot/app/vendor/hermes-kiro-provider ]] || die "app-deploy first (vendored provider missing)"
  # never re-mode an existing /run/ossbot (the tmpfiles entry owns it: 1775 + ACL; the watchdog checks it)
  [[ -d /run/ossbot ]] || mkdir -p /run/ossbot
  # R5-N2: the whole step holds the upgrade lock; the probe reports "Hermes is upgrading" meanwhile.
  exec {lockfd}>"$HERMES_UPGRADE_LOCK"
  flock -x "$lockfd"
  hermes_dir "$HERMES_DIR" 0700  # Hermes keeps its home 0700 itself
  if ! _hermes_installed "$commit"; then
    _hermes_install "$tag" "$tag_object" "$commit" "$repo"
  fi
  # setup-hermes.sh step 4 (ADR-0002 addendum): the launcher in ~/.local/bin
  hermes_dir "$HERMES_USER_HOME/.local" 0700
  hermes_dir "$HERMES_USER_HOME/.local/bin" 0700
  if [[ $(readlink "$HERMES_USER_HOME/.local/bin/hermes" 2>/dev/null) != "$HERMES_CODE/.venv/bin/hermes" ]]; then
    hermes_quiesce
    as_hermes ln -sfn "$HERMES_CODE/.venv/bin/hermes" "$HERMES_USER_HOME/.local/bin/hermes"
    mark_changed "launcher ~hermes/.local/bin/hermes"
  fi
  hermes_dir "$HERMES_DIR/kiro-cwd" 0700
  hermes_dir "$HERMES_DIR/workspace" 0700
  hermes_dir "$HERMES_DIR/plugins" 0750
  hermes_dir "$HERMES_DIR/plugins/model-providers" 0750
  # R4-M2: the hermes-side copy comes from the root-owned deployed copy; the probe hashes both.
  hermes_tree /opt/ossbot/app/vendor/hermes-kiro-provider "$HERMES_DIR/plugins/model-providers/kiro-acp"
  hermes_tree /opt/ossbot/app/hermes/plugins/ossbot "$HERMES_DIR/plugins/ossbot"
  # read-only for Hermes: the active skills (root activates approved skills, §15.5) and gateway hooks
  hermes_root_dir "$HERMES_DIR/skills"
  hermes_root_dir "$HERMES_DIR/hooks"
  # ~/.hermes/.env is loaded with override and could redirect HERMES_MANAGED_DIR: root-owned, fixed.
  local envfile=$HERMES_DIR/.env
  refuse_link "$envfile"
  if [[ ! -f $envfile || $(stat -c '%U:%G %a' "$envfile") != "root:hermes 640" ]] \
     || ! cmp -s "$envfile" "$OSSBOT_REPO/hermes/config/dotenv"; then
    hermes_quiesce
    refuse_link "$envfile"
    rm -f "$envfile"
    install -o root -g hermes -m 0640 "$OSSBOT_REPO/hermes/config/dotenv" "$envfile"
    mark_changed "file $envfile"
  fi
  _github_mcp_host
  # the tool surface: Hermes' managed scope, rendered by root for the active tier
  ensure_dir "$HERMES_MANAGED" root root 0755
  local out
  out=$(/opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_render) || die "hermes_render failed"
  log "  $out"
  if [[ $out == *": changed "* ]]; then
    mark_changed "$HERMES_MANAGED/config.yaml"
    if systemctl is-active --quiet ossbot-hermes.service; then
      systemctl restart ossbot-hermes.service
      mark_changed "restarted ossbot-hermes (new managed config)"
    fi
  fi
  hermes_resume
  flock -u "$lockfd"
  exec {lockfd}>&-
}
