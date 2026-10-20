# Third-party notices

ossbot is licensed under the Apache License 2.0 (`LICENSE`); its attribution notice is in `NOTICE`.
This file lists the third-party components the repository contains or uses. Licenses are as stated by
each upstream project (checked 2026-10-20); the upstream license text is authoritative.

## Included in this repository

### hermes-kiro-provider (vendored fork)

- Path: `vendor/hermes-kiro-provider/`
- Upstream: https://github.com/tobenwarrior/hermes-kiro-provider, commit
  `02c4dd06585436751145961658a22b773f23a0ef` (`vendor/hermes-kiro-provider/UPSTREAM_COMMIT`)
- License: MIT, Copyright (c) 2026 tobenwarrior. The full text is kept in
  `vendor/hermes-kiro-provider/LICENSE` and applies to the upstream files.
- Changes: ossbot added `kiro_acp/` (the Kiro V3 ACP engine; its tests live in `tests/acp/`). The
  upstream V2 files stay byte-identical (`tests/acp/test_engine_select_v2.py` checks their hashes).

## Used, not bundled

These are installed or downloaded by provisioning at the pinned versions in `config/versions.lock.yaml`
(or by `uv sync` from `uv.lock`). None of them is copied into this repository.

| Component | Version | License | Upstream |
|---|---|---|---|
| Hermes Agent | v0.22.2 | MIT (Copyright (c) 2025 Nous Research) | https://github.com/NousResearch/hermes-agent |
| github-mcp-server | 2.0.2 | MIT (Copyright (c) 2025 GitHub) | https://github.com/github/github-mcp-server |
| Kiro CLI (`kiro-cli`) | 2.32.0 | proprietary; used under the Kiro / AWS terms of the user's own account | https://kiro.dev |
| uv | 0.13.1 | Apache-2.0 OR MIT | https://github.com/astral-sh/uv |
| gitleaks | 8.30.1 | MIT | https://github.com/gitleaks/gitleaks |
| PSScriptAnalyzer | 1.25.0 | MIT | https://github.com/PowerShell/PSScriptAnalyzer |
| mise | 2026.10.13 | MIT | https://github.com/jdx/mise |
| age | 1.3.2 | BSD-3-Clause | https://github.com/FiloSottile/age |
| Docker Engine (docker-ce) | 29.9.1 | Apache-2.0 | https://github.com/moby/moby |
| squid (Ubuntu package, proxy image) | 6.14 | GPL-2.0-or-later | https://www.squid-cache.org |
| poppler-utils (Ubuntu package, sandbox-full image) | 24.02.0 | GPL-2.0-or-later | https://poppler.freedesktop.org |
| Ubuntu 24.04 base image | pinned digest | various (Ubuntu package licenses) | https://hub.docker.com/_/ubuntu |
| Python dependencies (`pyproject.toml`, `uv.lock`) | pinned | each package's own license | PyPI |

## Acknowledgements (no code included)

- SpiralPool `scripts/windows/wsl2-shutdown-hook.ps1` (BSD-3-Clause, Copyright (c) 2026 Spiral Pool
  Contributors, https://github.com/SpiralPool/Spiral-Pool). The retired User32 1074 shutdown hook (6b) was
  modelled on its idea; no SpiralPool code is copied or adapted (ADR-0021).
