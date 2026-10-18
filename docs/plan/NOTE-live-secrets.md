# NOTE: live secrets are configured (2026-10-17)

The user ran `scripts/setup-secrets.ps1`. Every secret is set (checked by key name only, values never read):

- `gatekeeper.env`: GITHUB_WRITE_TOKEN (classic, `public_repo`), TELEGRAM_BOT_TOKEN, HERMES_API_KEY, OSSBOT_TOKEN_PEPPER, KIRO_KEY_SHA256.
- `hermes.env`: KIRO_API_KEY (key "ossbot-hermes"), API server key.
- `runner.env`: GITHUB_READ_TOKEN (fine-grained, public read-only).
- `sandboxd.env`: KIRO_API_KEY (key "ossbot-sandbox").
- `config/profile.yaml`: `telegram.user_id` is set.

What this means for the remaining steps:
- Run the live checks that were previously marked "pending live probe" and record the results in docs/VERIFICATION.md:
  - `ossctl doctor` live checks.
  - Kiro headless `--v3` round-trip with each key.
  - The real `kiro-cli acp --agent-engine=v3` session through the forked hermes-kiro-provider.
  - Telegram getMe + a test message to the allowlisted user only.
  - GitHub `GET /user` == Amsal1 with `X-OAuth-Scopes` `public_repo` (read-only call).
  - Fine-grained read token can read public repos.
- **Hard limits while building:**
  - Dry-run stays ON.
  - No live GitHub write of any kind: no fork, push, PR, comment, issue claim or star. Live write-path tests use the fake upstream only.
  - Telegram messages go only to the configured user ID.
  - Never print, log or commit secret values. Gitleaks must stay clean.
- If a live check fails because of the user's token or key (wrong scope, API keys not enabled for the Kiro org, and so on), record it under "user action needed" in FINAL_REPORT.md. Don't work around it.
