# CR-002: Research egress (github.io, gists) and configurable Hermes capability tiers

Status: Required for Phase 2 (user request, 2026-10-17). Where it conflicts with DESIGN.md, this CR wins and DESIGN.md/ADRs must be updated to match.

## Context
- The current design already lets `research` sandboxes and Hermes (proxy port 3141) reach `web_public` (any public host, minus private ranges and the always-denied names). Pipeline LLM work that needs the web runs in the `research` profile. The code-running profiles (`fetch`, `validate`, `agent-llm`) stay on narrow allowlists.
- `config/egress.yaml` `always_denied.domains` blocks `.github.io` and `gist.github.com` for **every** profile, including research and Hermes. The user wants those readable, because a lot of technical knowledge lives on GitHub Pages blogs/docs and in gists.
- Hermes is locked to the `memory`, `todo` and `session_search` tools plus the `ossbot` plugin. Web access comes only through the Kiro session's `web_search`/`web_fetch`. `skills`, `terminal`, `code_execution`, `browser`, `delegation` and `cronjob` are off. The user wants this configurable so Hermes can later research more and self-improve, without weakening the safety invariants.

## Requirements
1. **Readable GitHub user content for research only.**
   - Move `.github.io`, `gist.github.com` and `gist.githubusercontent.com` out of `always_denied`. Put them in a new category `github_user_content`, allowed for `research` and `hermes` (3141) by default.
   - They stay denied for `fetch`, `validate`, `agent-llm`, `catalog`, `runner` and `gatekeeper`.
   - `uploads.github.com` stays always-denied (it is a write endpoint).
   - Add tests: research profile and hermes get 200 on a known `*.github.io` page and a public gist (raw too); the code-running profiles get 403.
   - Also review the other always-denied names and document why each stays.
2. **Research egress modes** in config (`config/egress.yaml` or a policy key), switchable from Telegram (`/egress`, two-tap confirm) and `ossctl egress mode`:
   - `allowlist`: only named categories, no `web_public`.
   - `research` (default): today's behaviour plus requirement 1.
   - `open`: `web_public` also for `agent-llm`. The code-running `fetch`/`validate` profiles are still never given `web_public`.

   Private and link-local ranges, metadata IPs, IP literals, the vsock/io_uring/DNS controls and the Hyper-V layer apply in **every** mode. No mode ever widens gatekeeper or runner egress. Every mode change is audited and FYI'd on Telegram.
3. **Hermes capability tiers**, one config key (`hermes.capability_tier`), switchable via `/hermes tier` (two-tap) and `ossctl hermes tier`:
   - `locked` (default today): current allowlist.
   - `research`: adds Hermes-native `web` tools if they route through proxy 3141. Keep the Kiro session's web tools.
   - `self_improve`: also enables Hermes `skills` (skill_manage/skill_view/skills_list), with a deterministic gate.
     - New or changed skills are written to a staging directory and **inactive** until approved.
     - The gatekeeper sends a Telegram card with the skill diff and Approve/Reject buttons, using the same two-tap, single-use-token flow as PRs.
     - Approved skills are copied by root into a read-only active dir and hashed. Doctor detects drift.
     - Skills can never grant tools above the tier.
     - Memory stays on in all tiers.

   `terminal`, `code_execution`, `file`, `browser`, `delegation` and `cronjob` stay OFF in every tier. Hermes runs as the uid that can read its Kiro key. If Hermes needs to run code, it requests a `research` or `agent-llm` sandbox job through the `ossbot` plugin, which the gatekeeper schedules. Changing tier re-runs the Hermes surface probe (§15.2). The gatekeeper's `/v1/toolsets` check uses the tier's allowlist, and a mismatch is still a soft kill.
4. **Verify Hermes research actually works** with the live key. Record the results in VERIFICATION.md: a Telegram→Hermes question that needs current web info returns a sourced answer, the Kiro `web_search`/`web_fetch` calls show up in the proxy log via 3141, and a github.io page and a gist can be fetched. If Kiro's web tools run server-side (not through our proxy), document that and what it means for egress control.
5. **Docs:** add an "Egress and capability tiers" section to README/AGENTS.md: what each mode/tier allows, the invariants no mode can relax, and how to switch.

## Amendment A (user decisions, 2026-10-17). Supersedes the defaults above.
6. **Defaults:** `hermes.capability_tier: self_improve` and egress mode `open`.
   - **Why `open`:** the original brief requires every agent, the coder included, to web-search unknowns because model knowledge is stale. That needs the web from `agent-llm`.
   - **What `open` still never relaxes:**
     - `fetch`/`validate` (untrusted repo code) stay on package/GitHub allowlists.
     - No mode reaches private, link-local, metadata or IP-literal targets, vsock/io_uring, or the Windows host.
     - Gatekeeper and runner egress are unchanged.
     - Kiro key-exfil probes and output key-scans stay mandatory and fail closed.
   - **Self-written skills:** under `self_improve` they still activate only after Telegram approval (requirement 3).
7. **Hermes is the user's general-purpose assistant**, not only the OSS chat front-end.
   - The user will use the bot for general Q&A and keep adding features. Examples: reverse image search, estimating protein intake from a food photo.
   - Design for that without touching the OSS safety boundary. The gatekeeper still owns the bot, the deterministic commands and every public GitHub write.
   - **Telegram routing:** commands go to the deterministic gatekeeper handlers. Everything else goes to Hermes:
     - free text
     - photos and albums, documents, voice notes, locations
     - replies to earlier bot messages, which carry the quoted context
   - **Multimodal relay:**
     - The gatekeeper downloads Telegram files (size cap configurable, default 20 MB; type allowlist: images, PDF, text, audio).
     - It strips EXIF/GPS from images by default (config toggle).
     - It stores attachments under a per-conversation dir that Hermes can read, then passes them to Hermes.
     - Hermes sends images to the model as ACP image content blocks. **[VERIFY]** Kiro V3 ACP `promptCapabilities.image`/`audio` and which models in config/models.yaml accept images. Fall back to a vision-capable model per role (add a `vision` role to models.yaml).
     - Voice: transcribe, or pass audio if supported. Otherwise reply that voice isn't supported yet.
   - **UX:**
     - Typing indicator while working.
     - Streamed replies, by editing one message progressively and respecting edit rate limits.
     - Long answers split cleanly, with Markdown or HTML rendered safely.
     - Sources listed as links when the web was used.
     - `/new` starts a fresh conversation. Memory persists across conversations.
     - `/model` already exists, and the `vision` role is switchable too.
   - **Feature extensibility:** new assistant features ship as Hermes skills (self-written or authored by hand). When a feature needs structured data or code, it ships as `ossbot` plugin tools backed by a small per-feature SQLite store; example: a nutrition log with daily totals. Code execution goes to a sandbox job through the plugin, never in-process.
     - Reverse image search may need an external API key (e.g. SerpAPI/Bing/Google Lens provider). Leave a documented hook in setup-secrets plus a skill stub. Don't implement a specific provider without a user decision.
   - **Separation:** OSS notifications and approval cards stay visually distinct from assistant chat (prefix/emoji header). `/quiet` and `/notify` control OSS FYI verbosity so assistant use isn't drowned out.
   - **Tests:** relay routing unit tests (command vs assistant, all message types), the attachment cap and type allowlist, EXIF stripping, and a fake-Hermes streaming test. Also a live check: send a food photo and get back an estimate (record the result; accuracy isn't graded).
