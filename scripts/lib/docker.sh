# shellcheck shell=bash
# Steps 'docker' and 'dockerfilter' (DESIGN §7.4, §10.8, §18.2). Requires common.sh.
# Docker Engine from Docker's apt repository, every package pinned from versions.lock.yaml and held;
# daemon.json of §7.4 plus "firewall-backend": "iptables" (§23 item 14, ADR-0005 addendum); the
# docker group stays empty (only root and the filter talk to /var/run/docker.sock).

OSSBOT_DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
OSSBOT_DOCKER_LIST=/etc/apt/sources.list.d/docker.list

docker_daemon_json() {
  cat <<'EOF'
{
  "data-root": "/srv/ossbot/docker",
  "log-driver": "local",
  "log-opts": {"max-size": "20m", "max-file": "3"},
  "iptables": true,
  "ip6tables": false,
  "userland-proxy": false,
  "live-restore": false,
  "bip": "10.211.0.1/24",
  "default-address-pools": [{"base": "10.214.0.0/16", "size": 24}],
  "features": {"buildkit": true},
  "firewall-backend": "iptables"
}
EOF
}

_docker_repo() {
  local fpr want_fpr tmp
  want_fpr=$(lock_get docker.gpg_fingerprint)
  if [[ ! -s $OSSBOT_DOCKER_KEYRING ]]; then
    tmp=$(mktemp)
    curl -fsSL --retry 3 --proto '=https' -o "$tmp" "$(lock_get docker.gpg_url)"
    fpr=$(gpg --show-keys --with-colons "$tmp" 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')
    [[ $fpr == "$want_fpr" ]] || { rm -f "$tmp"; die "Docker apt key fingerprint ${fpr:-none} != pinned $want_fpr"; }
    install -d -m 0755 /etc/apt/keyrings
    install -o root -g root -m 0644 "$tmp" "$OSSBOT_DOCKER_KEYRING"
    rm -f "$tmp"
    mark_changed "docker apt key $want_fpr"
  else
    fpr=$(gpg --show-keys --with-colons "$OSSBOT_DOCKER_KEYRING" 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')
    [[ $fpr == "$want_fpr" ]] || die "installed Docker apt key fingerprint ${fpr:-none} != pinned $want_fpr"
  fi
  local before=$OSSBOT_STEP_CHANGED
  OSSBOT_STEP_CHANGED=0
  ensure_file "$OSSBOT_DOCKER_LIST" root root 0644 <<EOF
deb [arch=amd64 signed-by=$OSSBOT_DOCKER_KEYRING] $(lock_get docker.apt_repo) noble stable
EOF
  if ((OSSBOT_STEP_CHANGED)); then
    DEBIAN_FRONTEND=noninteractive apt-get update -q
  fi
  ((before)) && OSSBOT_STEP_CHANGED=1
  return 0
}

step_docker() {
  _docker_repo
  local -A want=(
    [docker-ce]=$(lock_get docker.docker_ce)
    [docker-ce-cli]=$(lock_get docker.docker_ce_cli)
    [containerd.io]=$(lock_get docker.containerd_io)
    [docker-buildx-plugin]=$(lock_get docker.docker_buildx_plugin)
    [docker-compose-plugin]=$(lock_get docker.docker_compose_plugin)
  )
  local pkg have specs=()
  for pkg in "${!want[@]}"; do
    have=$(dpkg-query -W -f='${Status} ${Version}' "$pkg" 2>/dev/null || true)
    if [[ $have != "install ok installed ${want[$pkg]}" && $have != "hold ok installed ${want[$pkg]}" ]]; then
      specs+=("$pkg=${want[$pkg]}")
    fi
  done

  # daemon.json before the first start, so the data root and bip are right from the beginning.
  local before=$OSSBOT_STEP_CHANGED daemon_changed=0
  OSSBOT_STEP_CHANGED=0
  ensure_dir /etc/docker root root 0755
  docker_daemon_json | ensure_file /etc/docker/daemon.json root root 0644
  ((OSSBOT_STEP_CHANGED)) && daemon_changed=1
  ((before)) && OSSBOT_STEP_CHANGED=1

  if ((${#specs[@]})); then
    apt-mark unhold "${!want[@]}" >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends --allow-downgrades "${specs[@]}"
    apt-mark hold "${!want[@]}" >/dev/null
    mark_changed "docker packages: ${specs[*]}"
  fi
  if [[ $(apt-mark showhold | grep -c -E '^(docker-ce|docker-ce-cli|containerd.io|docker-buildx-plugin|docker-compose-plugin)$') != 5 ]]; then
    apt-mark hold "${!want[@]}" >/dev/null
    mark_changed "apt-mark hold docker packages"
  fi

  # The docker group exists (package postinst) but has no members (§7.3).
  if getent group docker >/dev/null && [[ -n $(getent group docker | cut -d: -f4) ]]; then
    gpasswd -M '' docker >/dev/null
    mark_changed "emptied group docker"
  fi
  if ((daemon_changed)) && systemctl is-active --quiet docker; then
    systemctl restart docker
    mark_changed "restarted docker (daemon.json)"
  fi
  if ! systemctl is-enabled --quiet docker 2>/dev/null; then
    systemctl enable docker.service containerd.service >/dev/null 2>&1
    mark_changed "enabled docker"
  fi
  if ! systemctl is-active --quiet docker; then
    systemctl start docker
    mark_changed "started docker"
  fi
  docker version --format '{{.Server.Version}}' >/dev/null || die "docker daemon not answering"
  local backend
  backend=$(docker info --format '{{json .}}' | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d.get("FirewallBackend",{}).get("Driver",""))' 2>/dev/null || true)
  if [[ -n $backend && $backend != iptables ]]; then
    die "docker firewall backend is '$backend', expected iptables (DOCKER-USER required)"
  fi
}

step_dockerfilter() {
  # The filter runs from the app venv (step app-deploy); here: its unit and sandboxd's Docker CLI
  # config dir (DOCKER_CONFIG, so the CLI never reads a user's ~/.docker).
  ensure_dir /srv/ossbot/sandboxd/docker-config ossbox ossbox 0700
  printf '{}\n' | ensure_file /srv/ossbot/sandboxd/docker-config/config.json ossbox ossbox 0600
  install_unit ossbot-dockerfilter.service
}
