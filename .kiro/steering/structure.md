---
inclusion: always
---

# Structure

| Path | Purpose |
|---|---|
| `src/ossbot/gatekeeper/` | trusted service: approvals, policy, audit, state machine, Telegram, the only GitHub writer (`submit.py` → `github_writer.py`) |
| `src/ossbot/runner/` | pipeline stages (`stages/`), scheduler; never imports the gatekeeper |
| `src/ossbot/sandboxd/` | sandbox jobs, profiles, Kiro settings, safety probes, key scan |
| `src/ossbot/dockerfilter/`, `src/ossbot/watchdog/` | root helpers |
| `src/ossbot/sandbox_tools/` | stdlib-only tools baked into images |
| `src/ossbot/ctl/` | `ossctl` (operator CLI, doctor, backup/restore, lints, docs) |
| `src/ossbot/common/` | config schemas, flags, snapshot; depends on no component |
| `hermes/`, `vendor/hermes-kiro-provider/` | Hermes plugin and config; the vendored V3 ACP fork |
| `sandbox/`, `systemd/`, `scripts/` | images, units, Windows entry points and `provision.sh` |
| `config/` | policy, targets, egress, models, telegram, `versions.lock.yaml`, `profile.example.yaml` |
| `prompts/`, `.kiro/agents/` | role prompts and one Kiro agent per pipeline role (`ossbot-*.json`); `harness-dev.json` for repo work |
| `tests/` | `unit/`, `telegram/`, `acp/`, `e2e/`, `sandbox/`, `system/`, `fakes/`, `fixtures/` |
| `docs/` | DESIGN, PLAN, TRACEABILITY, VERIFICATION, ADRs, SETUP, ARCHITECTURE |

Import boundaries are import-linter contracts in `pyproject.toml`. A new privileged writer gets its
own contract. Tests mirror the source tree (`tests/unit/<package>/test_<module>.py`).
