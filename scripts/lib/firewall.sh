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

# Phase 1 review P1-14 (ADR-0005 addendum): IORING_OP_SOCKET creates sockets without socket(2), so
# RestrictAddressFamilies (a seccomp filter on socket()) never sees AF_VSOCK, whose Hyper-V sockets
# reach the Windows host past every IP layer. io_uring is refused to every process (2 = root too);
# nothing in the stack uses it. systemd-sysctl applies the file at boot; 'ossbot-firewall --check'
# fails with firewall_drift:io_uring while the value is not 2. The non-root units also filter the
# io_uring syscalls themselves.
# F1c (build-env design §7, ADR-0005 addendum 2026-10-24): a TCP path-MTU black hole to Fastly
# (pypi.org, files.pythonhosted.org) stalled full-size segments with no ICMP frag-needed. The MSS
# clamp (mangle OSSBOT-MSS-*) fixes the known path; net.ipv4.tcp_mtu_probing = 1 (PLPMTUD) recovers
# any other black hole below it for the distro's own connections (the proxy netns sets it itself in
# ossbot-proxy-run). Doctor WARNs when it is not 1; it is not a floor, so --check ignores it.
apply_host_sysctls() {
  ensure_file /etc/sysctl.d/60-ossbot.conf root root 0644 <<'EOF'
# Managed by ossbot scripts/lib/firewall.sh (Phase 1 review P1-14; build-env F1c). Do not edit.
kernel.io_uring_disabled = 2
net.ipv4.tcp_mtu_probing = 1
EOF
  if [[ $(cat /proc/sys/kernel/io_uring_disabled 2>/dev/null) != 2 ]]; then
    sysctl -q -w kernel.io_uring_disabled=2 || die "sysctl kernel.io_uring_disabled=2 failed (kernel 6.6+ needed)"
    mark_changed "kernel.io_uring_disabled = 2"
  fi
  if [[ $(cat /proc/sys/net/ipv4/tcp_mtu_probing 2>/dev/null) != 1 ]]; then
    sysctl -q -w net.ipv4.tcp_mtu_probing=1 || die "sysctl net.ipv4.tcp_mtu_probing=1 failed"
    mark_changed "net.ipv4.tcp_mtu_probing = 1"
  fi
}

step_firewall() {
  mask_resolved
  apply_host_sysctls
  # The renderer lives in the app venv (step app-deploy); ossbot-firewall is the thin root wrapper.
  ensure_file /opt/ossbot/bin/ossbot-firewall root root 0755 <"$OSSBOT_REPO/scripts/bin/ossbot-firewall"
  install_unit ossbot-firewall.service
  install_unit ossbot-firewall-docker.service
  # '--check' (host part) also compares the live squid.conf with the render for the current egress
  # mode (§11.5). After a deploy of new renderer code, re-render it first (same snapshot, current
  # mode); the running proxy keeps its old inode until its restart in step enable-start. A snapshot
  # the new schema rejects is left alone: the gatekeeper restart writes a new one, the proxy restart
  # renders from it (and --check does not count the old one as drift meanwhile).
  local out
  if [[ -f /etc/ossbot/proxy/squid.conf && -L /srv/ossbot/config-snapshot/current ]]; then
    if out=$(/opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.render squid --out /etc/ossbot/proxy/squid.conf 2>&1); then
      [[ $out == unchanged* ]] || mark_changed "re-rendered /etc/ossbot/proxy/squid.conf"
    else
      warn "squid.conf not re-rendered now (${out:0:200}); the proxy re-renders after the gatekeeper restart"
    fi
  fi
  local part
  for part in host docker; do
    if ! /opt/ossbot/bin/ossbot-firewall --check --part "$part" >/dev/null 2>&1; then
      /opt/ossbot/bin/ossbot-firewall --apply --part "$part"
      mark_changed "applied firewall part $part"
    fi
  done
  /opt/ossbot/bin/ossbot-firewall --check || die "ossbot-firewall --check fails after apply"
}
