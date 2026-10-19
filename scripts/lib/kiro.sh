# shellcheck shell=bash
# kiro-cli pins (DESIGN §15.3, ADR-0011). Requires common.sh.
# kiro-cli is baked into the sandbox-llm image (images step) and installed for uid hermes by the
# 'kiro-cli-hermes' step (scripts/lib/hermes.sh, FEAT-007), both from the same pinned tarball.

# kiro_llm_build_args: the --build-arg list for sandbox/images/llm/Dockerfile from versions.lock.yaml.
kiro_llm_build_args() {
  local version url sha
  version=$(lock_get kiro_cli.version)
  url=$(lock_get kiro_cli.headless_tar_gz_url)
  sha=$(lock_get kiro_cli.headless_tar_gz_sha256)
  [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "kiro_cli.version '$version' is not a version"
  [[ $url == https://* ]] || die "kiro_cli.headless_tar_gz_url must be https"
  [[ $sha =~ ^[0-9a-f]{64}$ ]] || die "kiro_cli.headless_tar_gz_sha256 is not pinned"
  printf '%s\n' --build-arg "KIRO_CLI_VERSION=$version" --build-arg "KIRO_CLI_URL=$url" \
    --build-arg "KIRO_CLI_SHA256=$sha"
}

# kiro_manifest_check: WARN (never fail) when the release manifest's latest differs from the pin.
kiro_manifest_check() {
  local latest pin
  pin=$(lock_get kiro_cli.version)
  latest=$(curl -fsSL --max-time 15 --proto '=https' "$(lock_get kiro_cli.manifest)" 2>/dev/null | jq -r '.version // empty' 2>/dev/null || true)
  if [[ -n $latest && $latest != "$pin" ]]; then
    warn "kiro-cli manifest latest is $latest, pinned $pin (re-verify and update versions.lock.yaml)"
  fi
}
