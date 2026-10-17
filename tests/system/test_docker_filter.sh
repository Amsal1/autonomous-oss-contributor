#!/usr/bin/env bash
# DESIGN §22.6 test_docker_filter.sh: as ossbox through the filter socket, escalations are refused
# with 403 (bind mount, bind-type volume, exec, build, unfiltered list); the real Docker socket is
# not reachable (EACCES). A template-conformant listing is allowed.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

full=$(awk '/^  sandbox_full:/{f=1} f && /digest:/{gsub(/"/,"",$2); print $2; exit}' /etc/ossbot/versions.lock.yaml)
ref="ossbot/sandbox-full@$full"
D=(env "DOCKER_HOST=unix://$FILTER_SOCK" DOCKER_CONFIG=/srv/ossbot/sandboxd/docker-config docker)

denied() {  # denied DESCRIPTION CMD...: the command fails and the filter's 403 message is shown
  local d=$1 out; shift
  out=$(as ossbox "$@" 2>&1)
  if [[ $? -ne 0 && $out == *"ossbot-dockerfilter: denied"* ]]; then pass "$d"; else fail "$d: ${out:0:200}"; fi
}
denied "run -v /etc:/x refused" "${D[@]}" run --rm --name ossbot-x -v /etc:/x "$ref" true
denied "volume create type=none,o=bind,device=/etc refused" "${D[@]}" volume create --opt type=none --opt o=bind --opt device=/etc ossbot-task-x
denied "privileged run refused" "${D[@]}" run --rm --name ossbot-x --privileged "$ref" true
denied "exec into the proxy refused" "${D[@]}" exec ossbot-proxy true
denied "unlabelled container list refused" "${D[@]}" ps -a
denied "image list refused" "${D[@]}" images
check "labelled container list allowed" as ossbox "${D[@]}" ps -a --filter label=ossbot.sandbox=1
out=$(as ossbox curl -s --max-time 5 --unix-socket /var/run/docker.sock http://d/_ping 2>&1; echo "rc=$?")
if [[ $out == *"rc=7"* || $out == *"Permission denied"* ]]; then pass "ossbox gets EACCES on /var/run/docker.sock"; else fail "docker.sock reachable by ossbox: $out"; fi
check "docker group has no members" test -z "$(getent group docker | cut -d: -f4)"
refuse "ossbox is not in the docker group" bash -c "id -nG ossbox | tr ' ' '\n' | grep -qx docker"
finish
