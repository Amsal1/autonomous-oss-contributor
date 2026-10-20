# Security policy

ossbot acts on GitHub from its owner's account, gated by two human approvals on Telegram. A bug that
lets anything else trigger a public action is the most serious kind of bug here.

## Reporting a vulnerability

Report privately through GitHub Security Advisories: open the repository's **Security** tab and choose
**Report a vulnerability**
(https://github.com/Amsal1/autonomous-oss-contributor/security/advisories/new). Please do not open a
public issue, pull request or discussion for a vulnerability.

Include what you found, the affected commit, the steps or a proof of concept, and the impact. Redact
real tokens, keys and personal data. The maintainer aims to acknowledge a report within 7 days and will
agree a disclosure date with you once a fix is ready; credit is given in the advisory unless you prefer
otherwise.

## Supported versions

Only the `main` branch is supported. There are no release branches.

## In scope

- **The approval path:** any way to approve, confirm or replay gate 1 (proposal) or gate 2 (public
  action) without both taps of the configured Telegram user, on the exact message, with a fresh token
  and the right mode (LIVE or DRY RUN); an approval that does not bind the exact content.
- **Bypasses of the invariants I1-I13** ([AGENTS.md](AGENTS.md)), for example a GitHub write outside
  `gatekeeper/submit.py`, a write while killed, paused or in dry-run, a toggle that leaves approvals
  open, or an audit chain that can be rewritten unnoticed.
- **Sandbox escape:** repo code or an LLM session reaching the host, a host mount, the Docker,
  Docker-filter or gatekeeper socket, or a credential.
- **Egress bypass:** a sandbox reaching a host outside its profile's allowlist, or the network without
  the proxy.
- **Key or token leaks:** the GitHub write or read token, the Telegram bot token, a model key or a
  Hermes key appearing in a log, a card, a bundle, an artifact or a model prompt.
- **Telegram authentication:** commands or callbacks accepted from another user or chat, or a
  privileged action reachable without the confirm step.
- **Exposure:** anything that opens an inbound port, a webhook, a tunnel or a Mini App.

What counts above all: **any way for LLM output, Hermes or upstream repository content (issues, code,
CONTRIBUTING files, prompt injection) to trigger a GitHub write or a "go" action (approve, resume,
dry-run off, config reload, raising a limit) without both human gates.**

## Out of scope

- Consequences of the owner's own configuration choices, such as turning dry-run off, approving a bad
  proposal, adding a hostile target, or editing `config/policy.yaml` limits.
- Vulnerabilities in third-party services and tools (GitHub, Telegram, Kiro, Hermes Agent, Docker,
  WSL, Windows) unless ossbot's use of them makes them exploitable; report those upstream.
- Attacks that need administrator access to the Windows host or root in the distro (the trusted
  computing base), or physical access to the laptop.
- Model quality issues that cannot cause a write or a go action.
