---
inclusion: always
---

# Tech

- Python 3.12 with uv (`uv run --frozen`); dependencies pinned with `==` and locked in `uv.lock`.
- pydantic v2 with `extra="forbid"` for every config file and every LLM or Hermes payload; YAML through
  `ossbot.common.yamlio` (ruamel, safe loader, duplicate keys rejected).
- Gatekeeper: python-telegram-bot 22.8 with the typed Bot API 10.1–10.3 adapter (`telegram/rawapi.py`),
  SQLite, a Unix-socket API. No Pillow in the gatekeeper.
- Models only through the Kiro CLI (headless and V3 ACP); every model id lives in `config/models.yaml`.
- Docker Engine inside the distro behind a root API filter; squid egress proxy; iptables owner rules.
- Lint gate: `ruff check`, `ruff format --check`, `mypy` (strict for gatekeeper, dockerfilter, common),
  `lint-imports`; `shellcheck` for shell; gitleaks for secrets.
- Tests: pytest markers `unit`, `telegram`, `acp`, `e2e` (hermetic), `sandbox`, `e2e_docker` (root,
  Docker), shell system tests under `tests/system/`, the Windows isolation script. Per-test timeout 120 s.
- External versions and checksums are pinned in `config/versions.lock.yaml` after a web or live check.
- Windows side: PowerShell 5.1 scripts in `scripts/` (CRLF); everything else LF.
