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

# Phase 1 review P1-6 (ADR-0005 addendum): systemd-resolved answers any uid over its 0666 varlink
# socket and over D-Bus and sends the query upstream as systemd-resolve, which the owner-match
# rules do not cover. Nothing here needs it: /etc/resolv.conf is the WSL file naming the resolver
# directly and nsswitch is "files dns". Masked, it also cannot be D-Bus-activated.
# 'ossbot-firewall --check' (every unit's ExecStartPre, the watchdog, doctor) fails while it is
# not masked or still listening.
mask_resolved() {
  local unit=systemd-resolved.service
  if [[ $(systemctl is-enabled "$unit" 2>/dev/null || true) != masked ]]; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit" >/dev/null 2>&1 || die "systemctl mask $unit failed"
    mark_changed "masked $unit (DNS for every uid over varlink/D-Bus)"
  fi
  if systemctl is-active --quiet "$unit"; then
    systemctl stop "$unit" || die "systemctl stop $unit failed"
    mark_changed "stopped $unit"
  fi
}

step_firewall() {
  mask_resolved
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
