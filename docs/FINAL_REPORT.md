# ossbot v1: final report

The full report is written by FEAT-011 (Stage 4). Until then this file only holds what needs the owner.

## User action needed

Key and account problems found by the live checks are listed here and never worked around.

- None so far. FEAT-007 (2026-10-19): the Hermes key (`KIRO_API_KEY_HERMES`) works for headless `--v3` and for V3 ACP with the default auth method; the sandbox key, the read token and the bot token work as recorded in docs/VERIFICATION.md.

## Owner decisions pending

- Reverse image search provider (SerpAPI, Bing Visual Search, Google Lens, ...): the key hook and a skill stub exist; nothing is wired until a provider is chosen (DESIGN §15.6, TRACEABILITY CR3-IH-11).
