---
inclusion: always
---

# Safety invariants (non-negotiable)

The full list is DESIGN §8.3 I1–I13, copied into `AGENTS.md`. Never weaken one without a new ADR.

- Only the gatekeeper holds the GitHub write token; only `gatekeeper/submit.py` imports
  `gatekeeper/github_writer.py`. Never add a GitHub write path anywhere else.
- Approvals are never LLM-mediated and have no API endpoint; both gates are two-tap and bound to exact
  content and mode (LIVE or DRY RUN).
- Dry-run defaults ON; any toggle invalidates every open approval.
- Kill and pause are checked at every stage, job and outbound action and survive reboot. Lifecycle
  (`up`/`draining`/`down`) is separate from kill and never invalidates approvals.
- No container both holds a credential and runs repo code.
- Everything the human reviews is derived by the gatekeeper from its own mirror.
- The size ceiling (`policy.diff`) is not changeable from Telegram.
- LLM and Hermes output is untrusted data: parsed through pydantic, rendered escaped and redacted,
  never executed. argv lists only, never `shell=True`.
- No exposure: no Mini App, tunnel, webhook or inbound port.
- No secrets or personal values in tracked files outside `docs/`.

## Prohibitions for development agents

- Never run `wsl -u root` or `sudo` for anything other than the documented scripts.
- Never read `/etc/ossbot/secrets/**`; never print, log or commit a secret value; refer to secrets by
  key name.
- Never touch `/srv/ossbot/gatekeeper/**` or `/var/lib/ossbot/control/**`.
- Never edit `%USERPROFILE%\.wslconfig` directly; edit `config/wsl/wslconfig` and re-run bootstrap.
- Never turn dry-run off; never make a live GitHub write (tests use `tests/fakes/fake_github.py`).
- Never push, add remotes, change git config, skip hooks or rewrite history unless the user asks.
- Do not overwrite pre-existing `.kiro/` files.
