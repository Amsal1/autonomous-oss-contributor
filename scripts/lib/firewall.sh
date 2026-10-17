# shellcheck shell=bash
# Steps 'networks' and 'firewall' (DESIGN §11.2, §11.4). Requires common.sh and services.sh.

step_networks() {
  local subnet
  if docker network inspect ossbot-egress >/dev/null 2>&1; then
    subnet=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}' ossbot-egress)
    [[ $subnet == "10.212.0.0/24 10.212.0.1" ]] || die "network ossbot-egress has '$subnet', expected 10.212.0.0/24 10.212.0.1"
    return 0
  fi
  docker network create --driver bridge --subnet 10.212.0.0/24 --gateway 10.212.0.1 \
    --label ossbot.role=egress ossbot-egress >/dev/null
  mark_changed "network ossbot-egress 10.212.0.0/24"
}

step_firewall() {
  # The renderer lives in the app venv (step app-deploy); ossbot-firewall is the thin root wrapper.
  ensure_file /opt/ossbot/bin/ossbot-firewall root root 0755 <"$OSSBOT_REPO/scripts/bin/ossbot-firewall"
  install_unit ossbot-firewall.service
  install_unit ossbot-firewall-docker.service
  local part
  for part in host docker; do
    if ! /opt/ossbot/bin/ossbot-firewall --check --part "$part" >/dev/null 2>&1; then
      /opt/ossbot/bin/ossbot-firewall --apply --part "$part"
      mark_changed "applied firewall part $part"
    fi
  done
  /opt/ossbot/bin/ossbot-firewall --check || die "ossbot-firewall --check fails after apply"
}
