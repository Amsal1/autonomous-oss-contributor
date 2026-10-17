#!/usr/bin/env bash
# probe entrypoint (DESIGN §10.2 snapshot post-check, §10.9 probes, §22.5 smoke tests).
# Never receives the Kiro key (filter Env row). Mode from OSSBOT_PROBE:
#   snapshot_check   print every entry of /work/repo that is not a regular file or directory
#                    (sandboxd requires empty output; the ro snapshot is mounted at /work/repo)
#   extract_fixture  untar /opt/ossbot/fixtures/probe-repo.tar into /work/repo, symlinks preserved
#   settings         `kiro-cli settings list` with the baked cli.json on the tmpfs HOME (sandbox-llm)
#   smoke            write /io/output/probe.json with the isolation facts of this container
# OSSBOT_WORK / OSSBOT_IO only relocate the fixed paths for tests.
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
MODE="${OSSBOT_PROBE:-}"

json_bool() { if "$@" >/dev/null 2>&1; then printf true; else printf false; fi; }

case "$MODE" in
  snapshot_check)
    find "$WORK/repo" -mindepth 1 ! -type f ! -type d -print
    ;;
  extract_fixture)
    mkdir -p "$WORK/repo" "$WORK/.home" "$WORK/cache"
    tar -xpf /opt/ossbot/fixtures/probe-repo.tar -C "$WORK/repo" --no-same-owner
    ;;
  settings)
    export HOME=/home/agent
    mkdir -p "$HOME/.kiro/settings" "$HOME/cwd" "$IO/output"
    cp /opt/ossbot/kiro/settings/cli.json "$HOME/.kiro/settings/cli.json"
    cd "$HOME/cwd"
    kiro-cli settings list >"$IO/output/settings.txt" 2>"$IO/output/settings.stderr"
    ;;
  smoke)
    mkdir -p "$IO/output"
    {
      printf '{"uid":%s,' "$(id -u)"
      printf '"cap_eff":"%s",' "$(awk '/^CapEff/ {print $2}' /proc/self/status)"
      printf '"root_writable":%s,' "$(json_bool touch /.ossbot-probe)"
      printf '"input_writable":%s,' "$(json_bool touch "$IO/input/.ossbot-probe")"
      printf '"output_writable":%s,' "$(json_bool touch "$IO/output/.ossbot-probe")"
      printf '"docker_sock":%s,' "$(json_bool test -e /var/run/docker.sock)"
      printf '"run_ossbot":%s,' "$(json_bool test -e /run/ossbot)"
      printf '"mnt_entries":%s,' "$(find /mnt -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)"
      printf '"resolves_example":%s,' "$(json_bool getent hosts example.com)"
      printf '"env_names":"%s"}\n' "$(env | cut -d= -f1 | sort | tr '\n' ' ')"
    } >"$IO/output/probe.json"
    ;;
  *)
    echo "probe: unknown OSSBOT_PROBE mode" >&2
    exit 2
    ;;
esac
