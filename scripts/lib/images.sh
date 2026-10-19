# shellcheck shell=bash
# Step 'images' (DESIGN §10.1, §10.9, §18.2): build ossbot/sandbox-full, ossbot/sandbox-llm and
# ossbot/proxy as root on the real socket (context = repo root, filtered by .dockerignore), write
# their digests and the probe inputs (agent_configs_sha256, settings_sha256) back into
# config/versions.lock.yaml, and install the root-owned copy /etc/ossbot/versions.lock.yaml that the
# Docker filter and sandboxd read. Requires common.sh, kiro.sh and the app venv (app-deploy).
# Idempotence: each image is tagged ossbot/<name>:in-<inputs-hash12> and labelled with the full
# hash of its Dockerfile, build args and every file it copies; an existing tag means unchanged.

OSSBOT_LOCK_COPY=/etc/ossbot/versions.lock.yaml

# _inputs_hash BUILDARGS PATH...: sha256 over the build args and the sorted file contents.
_inputs_hash() {
  local args=$1; shift
  {
    printf 'args:%s\n' "$args"
    (cd "$OSSBOT_REPO" && find "$@" -type f -o -type l | LC_ALL=C sort | while IFS= read -r f; do
      if [[ -L $f ]]; then printf 'L %s %s\n' "$f" "$(readlink "$f")"; else printf 'F %s %s\n' "$f" "$(sha256sum <"$f" | cut -d' ' -f1)"; fi
    done)
  } | sha256sum | cut -d' ' -f1
}

# _build NAME DOCKERFILE "BUILDARGS..." INPUT_PATH...  -> sets OSSBOT_IMAGE_ID (sha256:<hex>), OSSBOT_IMAGE_TAG
_build() {
  local name=$1 dockerfile=$2 args=$3; shift 3
  local hash tag
  hash=$(_inputs_hash "$args" "$dockerfile" "$@")
  tag="ossbot/$name:in-${hash:0:12}"
  if ! docker image inspect "$tag" >/dev/null 2>&1; then
    log "building $tag (full log /srv/ossbot/logs/build-$name.log)"
    local -a argv=()
    [[ -n $args ]] && read -r -a argv <<<"$args"
    local blog="/srv/ossbot/logs/build-$name.log" started=$SECONDS rc line
    # Every build step, its cache hit and its duration go to the provision output as they happen.
    (cd "$OSSBOT_REPO" && DOCKER_BUILDKIT=1 docker build --progress=plain --pull=false \
      -f "$dockerfile" -t "$tag" --label "ossbot.inputs=$hash" --label "ossbot.image=$name" "${argv[@]}" .
      echo "$?" >"$blog.rc") 2>&1 |
      tee "$blog" | {
        grep --line-buffered -E '^#[0-9]+ (\[[^]]*\] |DONE |CACHED|ERROR)' |
          while IFS= read -r line; do log "  $name ${line:0:150}"; done
        true
      }
    rc=$(cat "$blog.rc" 2>/dev/null || echo 1)
    rm -f "$blog.rc"
    if [[ $rc -ne 0 ]]; then
      tail -n 40 "$blog" >&2
      die "docker build $name failed (log $blog)"
    fi
    mark_changed "built $tag in $((SECONDS - started)) s"
  fi
  OSSBOT_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$tag")
  OSSBOT_IMAGE_TAG=$tag
  # Older input tags of this image are superseded (an image still used by a container stays).
  local old
  for old in $(docker images --format '{{.Repository}}:{{.Tag}}' "ossbot/$name" | grep -vx "$tag" || true); do
    docker rmi "$old" >/dev/null 2>&1 && mark_changed "removed superseded $old"
  done
  return 0
}

# _lock_set_images FULL LLM PROXY AGENTS SETTINGS KIROVER: rewrite the images: block values in place.
_lock_set_images() {
  python3 -I - "$OSSBOT_REPO/config/versions.lock.yaml" "$@" <<'PY'
import re, sys
path, full, llm, proxy, agents, settings, kiro = sys.argv[1:8]
want = {
    ("sandbox_full", "digest"): full, ("sandbox_llm", "digest"): llm, ("proxy", "digest"): proxy,
    ("sandbox_llm", "agent_configs_sha256"): agents, ("sandbox_llm", "settings_sha256"): settings,
    ("sandbox_llm", "kiro_cli_version"): kiro,
}
lines = open(path, encoding="utf-8").read().split("\n")
in_images, image, changed = False, None, False
for i, line in enumerate(lines):
    if re.match(r"^images:\s*$", line):
        in_images = True
        continue
    if in_images and re.match(r"^\S", line):
        in_images = False
    if not in_images:
        continue
    m = re.match(r"^  ([a-z_]+):\s*$", line)
    if m:
        image = m.group(1)
        continue
    m = re.match(r"^(    )([a-z_0-9]+):\s*(.*)$", line)
    if m and (image, m.group(2)) in want:
        new = f'{m.group(1)}{m.group(2)}: "{want[(image, m.group(2))]}"'
        if new != line:
            lines[i] = new
            changed = True
if changed:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
print("changed" if changed else "unchanged")
PY
}

step_images() {
  kiro_manifest_check
  local full_args llm_args proxy_args
  full_args="--build-arg UV_VERSION=$(lock_get uv.version) --build-arg UV_SHA256=$(lock_get uv.sha256)"
  full_args+=" --build-arg MISE_VERSION=$(lock_get mise.version) --build-arg MISE_SHA256=$(lock_get mise.sha256)"
  full_args+=" --build-arg TOFU_VERSION=$(lock_get tofu.version) --build-arg KIND_VERSION=$(lock_get kind.version)"
  full_args+=" --build-arg GITLEAKS_VERSION=$(lock_get gitleaks.version)"
  llm_args=$(kiro_llm_build_args | paste -sd' ' -)
  proxy_args="--build-arg SQUID_VERSION=$(lock_get squid.apt_version)"

  local full llm proxy base
  # The toolchains (rarely changed) and the thin top with our scripts (FEAT-007: a script change
  # rebuilds only the top; the base tag also survives a build-cache prune).
  _build sandbox-full-base sandbox/images/full-base/Dockerfile "$full_args" sandbox/images/full/code-requirements.txt
  base=$OSSBOT_IMAGE_TAG
  _build sandbox-full sandbox/images/full/Dockerfile "--build-arg BASE=$base" \
    src/ossbot/sandbox_tools/numstat2json.py src/ossbot/sandbox_tools/execd.py sandbox/entrypoints sandbox/fixtures
  full=$OSSBOT_IMAGE_ID
  _build sandbox-llm sandbox/images/llm/Dockerfile "$llm_args" \
    src/ossbot/sandbox_tools/exec_mcp.py sandbox/entrypoints sandbox/kiro .kiro/agents prompts sandbox/probes
  llm=$OSSBOT_IMAGE_ID
  _build proxy sandbox/images/proxy/Dockerfile "$proxy_args"
  proxy=$OSSBOT_IMAGE_ID
  local d
  for d in "$full" "$llm" "$proxy"; do
    [[ $d =~ ^sha256:[0-9a-f]{64}$ ]] || die "unexpected image id '$d'"
  done
  # The pinned reference form the filter allows must resolve locally (containerd image store).
  docker image inspect "ossbot/sandbox-full@$full" >/dev/null 2>&1 || die "ossbot/sandbox-full@$full does not resolve"

  local agents settings res
  agents=$("$OSSBOT_APP/.venv/bin/python" -I -m ossbot.sandboxd.probes hash-agents "$OSSBOT_REPO/.kiro/agents" \
    "$OSSBOT_REPO/sandbox/kiro/settings/permissions.yaml")
  settings=$("$OSSBOT_APP/.venv/bin/python" -I -m ossbot.sandboxd.probes hash-settings "$OSSBOT_REPO/sandbox/kiro/settings/cli.json")
  res=$(_lock_set_images "$full" "$llm" "$proxy" "$agents" "$settings" "$(lock_get kiro_cli.version)")
  [[ $res == changed ]] && mark_changed "image digests written to config/versions.lock.yaml"
  ensure_file "$OSSBOT_LOCK_COPY" root root 0644 <"$OSSBOT_REPO/config/versions.lock.yaml"
}
