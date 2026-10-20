# ADR-0015: Open-source readiness: personal profile file, monotonic personal floors, README/AGENTS, CI
Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.9, §18.5, §18.8, §18.9

## Context
The repo now has a private remote and may be published later. v1 put personal values in tracked config (`identity.yaml`, IdC start URL in `egress.yaml`, `@Amsal1` in templates) and hard-coded `<work-login>` / `@<work-domain>` floors in `policy.py`.

## Decision
- All personal values (GitHub login, commit name/email, forbidden logins, confidentiality strings, Telegram user ID, IdC start URL/region, backup recipients, interests) live in the gitignored `config/profile.yaml`; `config/profile.example.yaml` with placeholders is committed. `profile.yaml` is loaded through the human-confirmed config snapshot like policy and targets. Templates use `{{ profile.* }}`; tests use a fixture profile.
- Personal floors are monotonic instead of hard-coded: every forbidden login and confidentiality string ever loaded is kept in the gatekeeper's `policy_floor` table; removal needs an operator TTY command plus a Telegram confirm. An empty `forbidden_logins` on a fresh DB fails closed. The non-personal workflow-path floor stays in code.
- `ossctl lint personal` (pre-commit, after gitleaks) fails if any local profile value appears in tracked files outside `docs/`; CI checks the example holds placeholders.
- README.md (safety model, mermaid diagram, quickstart, generated command table, limitations, AI-disclosure statement, License: TBD) and AGENTS.md (architecture map, conventions, invariants, how-to recipes, testing) are specified deliverables.
- `.github/workflows/ci.yml`: lint, hermetic tests, gitleaks (pinned binary, full history); `permissions: contents: read`; actions pinned by SHA; no secrets.

## Alternatives
- Keep identity in tracked config: rejected (publishing would leak personal data and the work account name).
- Keep hard-coded floors: rejected (personal values in code); monotonic DB floor preserves the "Windows edit cannot silently weaken" property from review M5.

## Consequences
A fresh clone does not work until the profile is created or restored (SETUP step 4/7). `docs/` may still name the owner's deployment; that is intentional and excluded from the lint.

## Implementation addendum (2026-10-21): README showcase media

The README banner, badges, demo GIF, MP4 and stills (`docs/media`) are generated, never drawn by hand, so they stay true as the cards change.

- **Truthfulness.** `tests/e2e/test_demo_capture.py` drives the hermetic e2e dry-run flow (real gatekeeper, runner and sandboxd on FakeRuntime, `tests/fakes/fake_github.py`, the fictional upstream `acme/widgets`, `tests/fixtures/profile.test.yaml`) through the real Telegram handlers over a recording fake Bot API, plus the fake Hermes stream, and writes every Bot API call to a transcript. Fixed clocks and ids make two captures byte-identical. `scripts/dev/demo/demokit` renders pages only from that transcript; `assert_faithful` checks every message text, button label and payload hash against the captured payload, on the generated HTML and again on the DOM Chrome dumped. Any other page text must sit in a `data-chrome` element (status bar, header, times, caption).
- **Personal values.** `scripts/dev/demo/personal_check.py` runs the `ossctl lint personal` values of the local profile, the redaction secret patterns and a host-path denylist over the transcript before anything is rendered and over the pages after, because the PNG and GIF outputs are binary and skipped by the lint.
- **Tools (dev-only, nothing in `uv.lock` or the runtime).** Screenshots: the system Google Chrome in new headless mode (`--screenshot`, `--window-size`, `--dump-dom`; new headless since 132.0.6793.0). It auto-updates, so it is a floor (`chrome_headless.min_major`) plus the exact version recorded in `docs/media/demo.manifest.json`, not a pin. Encoding and pixel digests: the Gyan ffmpeg 9.0.2 essentials build for Windows, pinned by URL and sha256 in `config/versions.lock.yaml` (`ffmpeg_windows`), downloaded into the gitignored `tools\bin\ffmpeg` and verified before extraction; never installed in the distro or an image and never redistributed. Pillow is not used anywhere (ADR-0020); GIF dimensions are read with the stdlib, pixel comparison uses `ffmpeg -f framemd5`.
- **Rejected.** Playwright (a dev dependency in `uv.lock` plus a browser download without a published checksum, and extra libraries and egress in the distro, for static pages); gifski (one more binary to pin; ffmpeg `palettegen`/`paletteuse` suffices for flat UI frames); shields.io badges (a third-party request, and the CI badge URL would carry the owner login). The CI badge says `manual run` because the workflow is `workflow_dispatch` only.
- **Not verifiable before a push.** GitHub renders mermaid client-side; `tests/unit/test_readme_media.py` checks the relative media paths, alt texts, sizes and mermaid syntax hygiene, and the owner eyeballs the rendered README after the next push.

Sources (checked 2026-10-21): https://developer.chrome.com/docs/automation-and-testing/headless-cli, https://developer.chrome.com/docs/chromium/headless, https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2 (release API asset digest), https://www.gyan.dev/ffmpeg/builds/, https://pypi.org/project/playwright.
