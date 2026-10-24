# shellcheck shell=bash
# Proxy-unaware tools in repo-code sandboxes (build-env design §3.2, §3.3; ADR-0025). Sourced by fetch.sh,
# validate.sh and exec.sh; not a job entrypoint.
#
# ossbot_netshim_start   start /opt/ossbot/bin/ossbot-netshim (stub DNS + 80/443 listeners that turn every
#                        connection into an ordinary request to the proxy) when the container has a network
#                        and HTTPS_PROXY is set. A bind failure is one stderr line; the job continues and
#                        proxy-aware tools are unaffected. It runs as this uid and can do nothing the repo
#                        code can't do by talking to the proxy itself.
# ossbot_gitconfig       a fresh $HOME/.gitconfig (rm -f, then 0644) whose url.insteadOf sends ssh GitHub
#                        URLs (submodules) over https: there is no ssh path out of a sandbox.

NETSHIM_BIN=/opt/ossbot/bin/ossbot-netshim

ossbot_netshim_start() {
  local nic networked=0
  [[ -n "${HTTPS_PROXY:-}" && -x "$NETSHIM_BIN" ]] || return 0
  for nic in /sys/class/net/*; do
    [[ ${nic##*/} == lo ]] || networked=1
  done
  ((networked)) || return 0
  if ! "$NETSHIM_BIN" --daemon --pidfile /tmp/netshim.pid --log /tmp/netshim.log; then
    echo "netshim: not started (could not bind 127.0.0.1:53, :80 or :443); proxy-unaware tools have no route" >&2
  fi
  return 0
}

ossbot_gitconfig() {
  mkdir -p "$HOME"
  rm -f "$HOME/.gitconfig"
  printf '[url "https://github.com/"]\n\tinsteadOf = git@github.com:\n\tinsteadOf = ssh://git@github.com/\n' \
    >"$HOME/.gitconfig"
  chmod 0644 "$HOME/.gitconfig"
}
