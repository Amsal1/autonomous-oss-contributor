---
inclusion: always
---

# Product

ossbot is a self-hosted bot on one Windows laptop (inside the dedicated WSL distro `ossbot`) that finds,
proposes, implements, validates and, only after explicit human approval on Telegram, publishes
open-source contributions from the owner's personal GitHub account. It is also the owner's general
multimodal assistant (Hermes) in the same Telegram bot, kept apart from the approval path.

- Two human gates: gate 1 approves a scouted proposal (plan and size estimate) before implementation;
  gate 2 approves the public action against a gatekeeper-derived preview. Everything else is autonomous.
- Dry-run is the default; the owner alone turns it off.
- Size is a preference for autonomous picks (`/size`, `/filters`); the hard ceiling in
  `config/policy.yaml` is a runaway guard that Telegram cannot raise. User-directed `/work` skips the
  preference but not the gates or the ceiling.
- No exposure: long polling only, no webhook, tunnel, Mini App or inbound port.

The spec is `docs/DESIGN.md` (revision 5). User docs: `README.md`, `docs/SETUP.md`.
