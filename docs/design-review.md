# Design review: docs/DESIGN.md (Draft v1)

Reviewer: design-review step (fresh context) · Date: 2026-10-17
Scope: DESIGN.md Parts I–III plus ADR-0001..0012, checked against the three verbatim user messages and two later user decisions relayed during the review (V3 ACP as primary for Hermes; commit identity "Amsal Khan").

Verdict: **CHANGES_REQUESTED** (7 HIGH, 15 MEDIUM, 12 NIT)

## Overall assessment

The skeleton is sound. Approvals live in a deterministic gatekeeper. Only the human can "go". Approval tokens are bound to the message ID and are single-use with two-tap confirm. The GitHub write token sits only in the gatekeeper's env file. Sandboxes get no tokens. Dry-run is ON by default. The single-bot Telegram decision is well argued: one `getUpdates` consumer per token, so whoever polls sees every Approve tap, and that process has to be the gatekeeper and not Hermes. The `[VERIFY]` discipline and Section 23 are good.

The blocking problems are in four places:

- What the human approves vs what gets pushed. The diff the human sees comes from the sandbox, not from the objects the gatekeeper pushes. Dry-run state is not bound to the approval.
- Two real exfiltration and escalation paths: the Kiro key next to hostile code, and kill flags that non-human components can delete.
- Threat-model claims that are not true on WSL, about Windows processes and I2.
- Two user decisions the document does not reflect yet: V3 ACP as primary, and the "Amsal Khan" identity.

---

## Findings

### HIGH

**H1. Hermes must use Kiro V3 ACP as primary (user decision); the design pins V2.**
Where: §15.2, §23 items 9–11, §18.1 (`hermes/vendor/hermes-kiro-provider/`), ADR-0002 "Fallbacks", ADR-0011 "Hermes's ACP subprocess pins the V2 ACP server".
Problem: The design makes V2 ACP (`acp --agent ossbot-chat --model …`) primary and V3 a fallback. The user has made V3 primary. I checked kiro.dev/docs/cli/v3/acp-migration live on 2026-10-17. V3 rejects `--agent --model --effort --trust-all-tools --trust-tools` on `acp`. V3 does **not** implement `session/set_model`. The model is set through `session/set_config_option {configId:"model"}`. Agent and mode are set via `session/set_mode` or `_meta.kiro.modeId` on `session/new`. By default auth uses the CLI login; with `--auth-method=client`, the client must answer `_kiro/auth/getAccessToken`. The page does not mention `KIRO_API_KEY` for ACP.
Fix: rewrite §15.2 and ADR-0002/0011 as follows:
- Vendor a fork of `tobenwarrior/hermes-kiro-provider@02c4dd06585436751145961658a22b773f23a0ef` at `vendor/hermes-kiro-provider/` (the path the user named; update §18.1). Record `UPSTREAM_COMMIT` and keep the fork's own commit history.
- Launch: `kiro-cli acp --agent-engine=v3` with no `--agent/--model/--effort/--trust-*`. Optionally add `--auth-method=client`, see auth below.
- Handshake: `initialize`. Read `agentCapabilities` and `_meta.kiro.extensionMethods`, and never infer support from the version. Then `session/new {cwd: /home/hermes/.hermes/workspace, mcpServers: [], _meta.kiro: {modeId: "ossbot-chat", modelId: <models.yaml roles.chat>, effortLevel: <effort>}}`. Then `session/set_config_option configId=model` whenever models.yaml changes. That makes the chat-model switch live, so the watchdog "restart Hermes on chat-model change" in §7.5/§16.1 is only the V2 fallback.
- Permissions: the shim must answer every `session/request_permission` with the reject option, as deterministic code. The global V3 permission config for user `hermes` denies `shell`, writes, and reads of `~/.kiro/**`, `/proc/**`, `/etc/**`. Remove the old claim that "no permission request ever reaches the auto-approval path". Make the shim deny regardless.
- Auth order: (a) `KIRO_API_KEY` in env, with the default auth method, verified by a live probe; (b) `--auth-method=client`, where the shim answers `_kiro/auth/getAccessToken` from `/etc/ossbot/secrets/hermes.env`, never logs the token, and redaction is tested; (c) device-flow login as `hermes`.
- Tests: a `tests/fakes/fake_acp_v3.py` stdio NDJSON JSON-RPC server, plus contract tests for initialize/capabilities, session/new with `_meta.kiro`, set_config_option model, that request_permission gets rejected, getAccessToken, cancel, and rejecting the v2-only flags. Add a live `ossctl doctor` round-trip ("reply pong") that runs once `KIRO_API_KEY` exists.
- At build time, run `kiro-cli acp --agent-engine=v3` for real (initialize + session/new, which may work without auth) and record the transcript and results in ADR-0002.
- V2 ACP (`acp --agent ossbot-chat --model …`, original plugin) becomes Fallback 1; the per-turn `kiro-cli chat --no-interactive` provider stays Fallback 2.
- Upstreaming: see M15.

**H2. Commit identity must be "Amsal Khan &lt;md.amsalkhan@gmail.com&gt;" for author, committer and DCO (user decision); the design uses "Amsal1".**
Where: §2 table, §9.11 step 2, §12.5/I6, §18.3 identity.yaml, §24 A3, §25, ADR-0006.
Problem: DCO checkers compare the `Signed-off-by` name/email with the commit author, and the user has fixed the name to "Amsal Khan". The GitHub login stays `Amsal1`. The design mixes the two concepts in one value.
Fix: split them in `config/identity.yaml`:
```yaml
github_login: Amsal1            # API identity; checked via GET /user
commit_name: "Amsal Khan"       # author == committer == sign-off name
commit_email: md.amsalkhan@gmail.com
signoff: true
forbidden_logins: [<work-login>]  # plus a hard-coded floor, see M5
```
- Package uses `GIT_AUTHOR_NAME=GIT_COMMITTER_NAME=commit_name` and appends `Signed-off-by: Amsal Khan <md.amsalkhan@gmail.com>`.
- Gatekeeper policy (I6, criterion 15) checks that author name/email == committer == the last `Signed-off-by` trailer == identity.yaml.
- setup-secrets pre-fills "Amsal Khan" / md.amsalkhan@gmail.com.
- §2 says "Same identity for this harness repo". Replace it with: "harness repo uses repo-local git config (`Amsal Khan <md.amsalkhan@gmail.com>`), commit normally, no `GIT_AUTHOR_*`/`GIT_COMMITTER_*` overrides."
- Drop §24 A3 ("Amsal1 acceptable as DCO name").
- Keep §23 item 14 (email verified on Amsal1).

**H3. The coder (`agent` profile) holds the Kiro key in the same container as hostile code, and "Kiro hosts give an attacker no readable channel" is unverified and probably false.**
Where: §10.3, §10.4, §8.4 T1/T2, §15.3 coder agent, ADR-0005.
Problem: Hostile repo code runs as the same UID as kiro-cli, so it can read `/proc/<kiro pid>/environ` or its own env. The image ships `kiro-cli`. With the key, hostile code can call Kiro directly through the allowed hosts. If any Kiro feature does network I/O on the attacker's behalf, the egress allowlist is bypassed and the key leaves. Candidates: server-side `web_search`/`web_fetch` (§23 item 6 admits this is unknown), remote MCP, or cloud/Web sessions (the design lists "Kiro Web/Crew cloud sessions" as existing). The shell deny patterns (`curl *`) do not constrain hostile test code at all.
Fix: specify the split-execution coder as the **default**, not as a conditional:
- Container `agent-llm` holds the key. It runs `kiro-cli chat --v3 …` with the built-in `shell` tool **removed** from `ossbot-coder`, `fs_write` limited to `/work/repo/**`, and egress to Kiro hosts only. It never executes repo code.
- Container `agent-exec` (no key, `--network none`, same task volume) runs a tiny deterministic MCP stdio server (`ossbot-exec`), connected to kiro-cli over a sandboxd-created socket on a shared tmpfs volume. Its tools are `run(cmd, cwd, timeout≤1200s)` and `apply_patch` if needed. Commands run there with `env -i` plus a fixed env.
- The coder agent's tools become `read`, `write`, `@ossbot-exec/run`.
- Keep the current single-container layout only as an explicit, logged downgrade (`sandbox.coder_mode: single`), allowed only if §23 items 6 and 7 prove that no Kiro feature reachable with the key performs attacker-directed egress, and `kiro-cli --sandbox` scrubs env.
- Add sandbox test S-key-1: in `agent-exec`, `cat /proc/*/environ | grep -c KIRO_API_KEY` == 0, and `getent`/`curl` to Kiro hosts fail.

**H4. The diff the human approves is produced inside the sandbox, not derived by the gatekeeper from the objects it pushes; `base_sha` is never shown to be upstream history.**
Where: §9.11 step 2 (`change.patch`, numstat made in the `package` container), §14.3 diff view and `.patch`, §12.4 binding, §12.5 step 2.
Problem: The package container runs on a volume that hostile code and the coder have written to. `/work/repo/.git/config` is coder-writable (`fs_write /work/repo/**`), so filters, fsmonitor and hooks run on `git add`. That container can emit a benign `change.patch`/diffstat while the bundle contains something else. The binding covers `bundle_sha256`/`head_sha`, but nothing ties the *displayed* diff to them. The gatekeeper also only checks `is-ancestor base_sha head_sha`, not that `base_sha` is in upstream history. A crafted base, for example from an earlier contribution's `refs/ossbot/*` in the shared mirror, would show a tiny diff while the push carries extra history.
Fix: add to §12.4/§12.5 and call it from `/package`:
1. On `/package`, the gatekeeper imports the bundle into a **per-contribution** bare repo `/srv/ossbot/gatekeeper/mirrors/<owner>__<repo>/<cid>.git`, so no cross-contribution refs exist. It runs a fresh `git fetch https://github.com/<upstream>.git +refs/heads/<base_ref>:refs/upstream/<base_ref>`.
2. It requires `git merge-base --is-ancestor <base_sha> refs/upstream/<base_ref>`, `git rev-list --count base..head == 1` (squash invariant), and `head^ == base_sha`.
3. The gatekeeper **itself** generates `git diff --numstat`, `git format-patch --stdout base..head`, the commit message, and author/committer from the mirror. These are the only artifacts rendered on the card, in View diff, and in the `.patch` document. Sandbox-supplied patch/diffstat are discarded, or compared and cause a mismatch failure.
4. Add `patch_sha256` (of the gatekeeper-generated patch) and `commit_message_sha256` to the `open_pr`/`push_update` binding.
5. Run every git command in the gatekeeper with `-c core.hooksPath=/dev/null -c protocol.file.allow=never`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`.
6. Test: `test_submit.py::test_patch_mismatch_rejected` and `::test_fabricated_base_rejected`.

**H5. Non-human components can delete the kill/pause flags, which violates I9 at the filesystem level.**
Where: §7.3 (ossrunner and ossbox are in `osscontrol`), §7.3 directories (`/var/lib/ossbot/control/` gatekeeper:osscontrol **2770**), §13.2 "Writers: gatekeeper and root".
Problem: Group-writable without a sticky bit means `ossrunner` and `ossbox` can `unlink killed`/`paused`. The runner checks the file before each stage. So a runner bug or exploit, driven by untrusted LLM output, can "resume" itself, and the gatekeeper notices only on restart.
Fix:
- `/var/lib/ossbot/control` becomes `gatekeeper:osscontrol 0750`, files `0640`, so group members can read but not write or delete.
- Root (enforcer, `ossbot-kill`) writes as root.
- Runner and sandboxd request pause/kill only via the gatekeeper API (`worker(anomaly)` already exists). sandboxd needs no write path.
- Add `tests/system/test_control_perms.sh`: as `ossrunner` and as `ossbox`, `rm`/`mv`/`touch` in the control dir all fail with EACCES.
- Watchdog invariant: if `stat` of the dir or files differs from expected → kill + alert.

**H6. Dry-run is not bound to approvals: an approval tapped on a "DRY RUN" card can execute live after dry-run is turned off.**
Where: §12.4 common binding `dry_run_at_creation`, §12.4 "recomputes the binding from the current contribution row", §12.9, §9.2 invalidation rules, §22.1.
Problem: It is not stated whether the recompute uses the creation-time flag or the current one. With the creation-time flag, a card the user approved casually, because it said "nothing will be published", publishes for real.
Fix:
- Replace `dry_run_at_creation` with `dry_run` and recompute it from **current** control state at confirm and at execution.
- Any dry-run toggle, in either direction, invalidates every approval in `pending|armed|confirmed`, in the same transaction as the toggle, and re-issues fresh cards labelled with the new mode.
- The Confirm text must state the mode: "Confirm: LIVE — open PR on X as Amsal1" vs "Confirm: DRY RUN — nothing will be published".
- Add `test_approvals.py::test_dry_run_toggle_invalidates` and an e2e step: approve under dry-run, toggle off, tap the old Confirm → "superseded", zero writes.

**H7. T7 and the secrets-on-ext4 rationale claim protections that WSL does not provide: any Windows process running as the user can do `wsl.exe -d ossbot -u root`.**
Where: §8.4 T7 ("an AI agent on Windows running `wsl.exe ossctl ...` cannot approve"), §18.3 and ADR-0010 ("so Windows processes, editors, and IDE agents cannot read them"), §12.9 ("ossadmin, interactive TTY").
Problem: Root in the distro is one command away for any process in the Windows user session, including IDE agents such as this one. From there it can read `gatekeeper.env` (the write PAT and bot token), edit the SQLite DB, and push to GitHub directly. Also, `ossadmin` is the default WSL user, so a plain `wsl.exe` already carries the `operator` role. The "interactive TTY" check is defeatable with `script -qc`. The NFR-2 isolation direction (Linux → Windows) is fine. The reverse claims are false.
Fix:
- Restate T7 in §8.2/§8.4 and ADR-0010: "Processes running as the Windows user are root-equivalent over the distro. ossbot does not defend against them. Ext4 secrets defend only against passive exposure (workspace indexing, IDE agents reading files, sync/backup tools, accidental git add). Telegram-only approval defends against accidental or unprompted approval by local agents, not against a hostile local process."
- Hardening to add, all cheap: (a) `[user] default=ossnobody`, an unprivileged user that is not in `ossapi`, so `wsl.exe` without `-u` gets no API role; ossadmin is used explicitly. (b) `.kiro/steering/safety-invariants.md` and `AGENTS.md` forbid `wsl -u root`, reading `/etc/ossbot/secrets`, or touching `/srv/ossbot/gatekeeper` from dev agents. (c) Gatekeeper audits any `operator` call whose process tree is not under an interactive `sshd`/`login` session (best-effort) and sends a Telegram FYI for every operator "go" action, so a local resume is always visible on the phone.
- Remove the word "cannot" from both claims.

### MEDIUM

**M1. State-machine transitions have no actor ownership.**
Where: §9.2, §12.2 `POST /v1/contributions/{id}/transition` (worker).
Problem: Nothing stops the worker from requesting `AWAITING_APPROVAL→APPROVED`, `APPROVED→SUBMITTING`, `→SUBMITTED`, `→REJECTED`, `SUBMIT_FAILED→AWAITING_APPROVAL`, or `→DRY_RUN_DONE`. I1 still blocks the write, but approval-related state becomes forgeable by a non-human process.
Fix: make `ALLOWED: dict[State, dict[State, Actor]]` with `Actor ∈ {worker, gatekeeper_internal, human}`. Transitions out of or into `AWAITING_APPROVAL`, `APPROVED`, `SUBMITTING`, `SUBMITTED`, `SUBMIT_FAILED`, `DRY_RUN_DONE`, `REJECTED` and `CHANGES_REQUESTED` are `gatekeeper_internal` or `human` only. The worker API returns 403 for them. Test: `test_state_machine.py::test_worker_cannot_drive_approval_states` (parametrized over every such edge).

**M2. `/package` semantics: allowed states, TOCTOU with submit, and loss of human title/body edits.**
Where: §9.11 step 5, §12.2 `/package`, §14.4 Edit title/body.
Fix:
- `/package` is accepted only when the contribution is in `PACKAGING`; otherwise 409.
- A new rev atomically invalidates approvals.
- `submit.py` loads title, body and SHA **once** into an immutable snapshot, hashes that snapshot, compares it to the binding, and posts exactly those bytes.
- Human edits are stored as `pr_title_override`/`pr_body_override` columns and survive later package revs (Request changes) until the user clears them. The card shows "title/body edited by you".

**M3. Watch-stage flows and claim gating are not in the state machine; approval expiry vs EXPIRED is undefined.**
Where: §9.2 vs §9.12 and §9.5.
Problem: `WATCHING` can only reach `MERGED|CLOSED`, but §9.12 re-enters implement for fixes and creates `comment` actions. `VETTED→FETCHING` has no guard for `requires_claim_comment`. Approvals expire at 72 h, contributions at 7 days, and what happens in between is undefined.
Fix:
- Add `WATCHING→IMPLEMENTING` (fix loop, then `push_update` action) and `WATCHING→WATCHING` (comment action pending) to the table, with a `watch_rounds` counter (max 3).
- Guard: `VETTED→FETCHING` requires `issue_claim` action status `done` when `requires_claim_comment`; if the claim is rejected → `SKIPPED`.
- On approval expiry the contribution stays `AWAITING_APPROVAL`. `/approve <cid>` re-runs live checks and issues a fresh approval and card. After 7 days with no fresh approval → `EXPIRED`.

**M4. Telegram review completeness: PR body rendering, commit message, and long bodies.**
Where: §14.4 message 2, §14.3, §12.6 text checks.
Fix:
- Message 2 renders the PR body **verbatim**: `html.escape` inside `<pre>`, never through the markdown converter. Hidden constructs (`<!-- -->`, `<details>`, images, links) must be visible as source.
- The card shows "body: N chars, K parts". If K > 3 or the body is > 10 000 chars, also send `pr_body.md` via `sendDocument`.
- Approve is enabled only after all body parts are delivered. Outbox success for every part is required, otherwise the Approve button is replaced by "⏳ delivering…".
- The card and View diff show the gatekeeper-derived commit message (H4).
- The commit message is scanned by the same secret, confidentiality-denylist and length checks as title/body.

**M5. Security-relevant config is read from the Windows-writable drvfs mount with unspecified load semantics.**
Where: §7.2 (repo mount writable by any Windows process), §16.1 (models.yaml read fresh), §17.1 `egress_extra`, §11.3 egress.yaml, §18.3 identity.yaml `forbidden_logins`, §18.2 `config/` symlink into `/opt/ossbot/app`.
Problem: A Windows-side edit can silently add egress hosts, set `ai_policy_override`/`cla_signed`, or change identity or forbidden logins. The gatekeeper checks commit identity against the same mutable file.
Fix: policy, targets, egress and identity load only at service start or on a human-confirmed `/reload`. The gatekeeper copies the validated files to `/srv/ossbot/gatekeeper/config-snapshot/<sha256>/`, records the hash in audit, and serves it to runner, sandboxd and squid rendering via `GET /v1/config/snapshot`. Those components never read drvfs config directly. `models.yaml` may stay live-read, since it carries no safety impact beyond model choice. Hard-code a non-configurable floor in `policy.py`: forbidden logins `{"<work-login>"}`, denylist `{"<work-login>", "@<work-domain>"}`, forbidden paths `.github/workflows/**`.

**M6. The proxy and egress topology as written does not work, and gatekeeper egress is not what the text claims.**
Where: §7.4 daemon.json, §11.2, §11.4.
Problems:
(a) With `default-address-pools` set and no `bip`, `docker0` takes the first /24 from 10.212.0.0/16, so `ossbot-egress`'s gateway is not guaranteed to be 10.212.0.1.
(b) A port published with `userland-proxy:false` is DNATed in `nat OUTPUT` before `filter OUTPUT`, so `-d 10.212.0.1 --dport 3140` never matches. Host services get REJECTed.
(c) Runner and Hermes both come from the bridge IP, so squid cannot give them different allowlists. "host" is a single merged ACL.
(d) Gatekeeper rules allow **any** TCP 443 and UDP 53, while §11.3 claims "api.telegram.org, api.github.com, github.com … enforced by owner-match rules".
Fix:
- Set `"bip": "10.211.0.1/24"`. Create `ossbot-egress` with `--subnet 10.212.0.0/24 --gateway 10.212.0.1` and run the proxy with `--ip 10.212.0.2`, with no published port.
- Host services connect to `10.212.0.2:3140` (runner) and `10.212.0.2:3141` (Hermes). iptables: `--uid-owner ossrunner -d 10.212.0.2 -p tcp --dport 3140 -j ACCEPT`, `--uid-owner hermes -d 10.212.0.2 -p tcp --dport 3141 -j ACCEPT`. Squid maps port 3140 to `github_read` only and 3141 to `kiro_core + web_public`.
- Gatekeeper: port 3150, ACL `{api.telegram.org, api.github.com, github.com}`, with iptables owner rule `-d 10.212.0.2 --dport 3150` only and no direct 443/53.
- Soft kill no longer stops `ossbot-proxy`. Sandboxes and their networks are gone anyway, and runner/Hermes are stopped, so the gatekeeper keeps Telegram reachable.
- Add a doctor and test probe for each uid/port pair.

**M7. DNS exfiltration fallback for `--internal` networks is unspecified.**
Where: §10.3 "[VERIFY on Docker 29]", criterion 12.
Problem: If Docker's embedded resolver forwards external queries for internal networks, the agent profile can leak the key as `<key>.attacker.tld`. The design names the probe but has no fallback.
Fix: always run sandboxes with `--dns 127.0.0.1 --dns-search .`. The embedded resolver's upstream then points at nothing inside the container netns, and squid does all external resolution. Keep criterion 12's `getent hosts example.com` probe and add `dig +short TXT x.example.com @127.0.0.11` → no answer.

**M8. The read-only task volume breaks kiro-cli HOME and stage outputs in `research` profiles.**
Where: §10.2 template (`HOME=/work/.home` on the volume), §9.1 "repo ro", §10.5, §15.3 (`> /work/output/...`).
Fix: for ro profiles, mount `ossbot-task-<cid>` read-only at `/work`, add `--tmpfs /work/.home:rw,nosuid,nodev,size=512m`, and mount a per-run volume `ossbot-out-<run_id>` rw at `/work/output`, which sandboxd copies to artifacts. Specify this in the argv template, with a unit test on the generated argv.

**M9. Conflicting profile for the summarizer.**
Where: §9.1 row 8 and §9.11 ("`agent` profile, tools read only") vs §8.4 T1 research row and ADR-0005 (summarizer in `research`).
Fix: summarizer runs in `research` with `tools: [read]` and no web tools. It needs no web and no shell, and `research` has no hostile code execution. Update §9.1, §9.11 and §15.3 accordingly.

**M10. I2 ("only the gatekeeper can read the write token, kernel-enforced") contradicts §7.3: `ossbox` is in the `docker` group.**
Problem: `ossbox` is root-equivalent, so `docker run -v /etc/ossbot:/x` reads `gatekeeper.env`.
Fix: choose one and write it into §7.3, §8.3 and ADR-0005.
(a) Preferred: `ossbox` is **not** in `docker`. A root-run `ossbot-docker-filter` UDS proxy (about 200 lines, deterministic, unit-tested) forwards only these Docker API calls:
- create, with HostConfig matching the template: no Binds, `Privileged=false`, empty CapAdd, NetworkMode `ossbot-t-*|none`, required labels;
- start, wait, logs, kill, remove for containers labelled `ossbot.sandbox=1`;
- network create, connect and remove for `ossbot-t-*`;
- volume create and remove for `ossbot-task-*|ossbot-out-*`.
(b) Minimum: amend I2 to "gatekeeper and the root-equivalent sandboxd" and list sandboxd as TCB in §12.1 and AGENTS.md.

**M11. The Kiro key cannot be literally scanned by the gatekeeper, and the regex relies on an unverified prefix.**
Where: §14.3 redaction ("literal values … known to the gatekeeper"), §12.6 `ksk_` pattern, §18.3 "[VERIFY prefix]".
Fix: sandboxd holds the key, so before publishing to `/srv/ossbot/artifacts`, sandboxd scans every artifact and every archived log for the literal key and for its base64, hex, URL-encoded and reversed forms. A hit fails the stage, raises the anomaly "secret in output" (pause), and redacts logs. Also give the gatekeeper `KIRO_KEY_SHA256` and hash-check every `[A-Za-z0-9_\-]{20,}` token in the diff/body/commit message.

**M12. The audit hash chain is not tamper-evident against its own writer.**
Where: §8.3 I11, §12.7, §22.1 `test_audit.py`.
Problem: The gatekeeper owns the DB, so it can drop triggers and recompute the whole chain.
Fix: every 5 minutes and on every outbound write, the root watchdog appends `{seq, row_hash, ts}` to `/srv/ossbot/logs/audit-anchors.log`, a root-owned file with `chattr +a`. `ossctl audit verify` checks the chain against the anchors. The Telegram `/status` shows the current head hash prefix. Test: recompute the chain after editing → verify fails against the anchors.

**M13. `kill.ps1` and the boot path: killing a stopped distro boots it with services racing the flag, and the watchdog may restart Hermes while killed.**
Where: §13.3, §7.5, §7.5 watchdog "restarts Hermes on chat-model change".
Fix:
- Add a Windows-writable kill marker `D:\Work\autonomous-oss-contributor\.ossbot-local\KILL`, gitignored. `kill.ps1` writes it **before** calling `wsl.exe`.
- A root `ossbot-boot-gate.service` (`Before=` all ossbot units, `DefaultDependencies=no` after the mount) converts the marker's presence into the ext4 `killed` flag. Absence never resumes. Only `ossbot-resume` deletes the marker and the flag together, after the human confirm.
- `kill.ps1` detects "not running" via `wsl.exe -l --running -q` and reports it, but still writes the marker.
- The watchdog must not start or restart any unit while `killed` or `paused` exists.

**M14. The test plan does not exercise several safety properties.**
Add:
- `tests/system/test_kill.ps1`: `/kill` and `kill.ps1` → no `ossbot.sandbox=1` containers within 10 s, units stopped, gatekeeper 423 on outbound; reboot (`wsl --terminate` + start) → still killed; marker path from M13.
- Control-dir permissions (H5).
- Dry-run toggle invalidation (H6).
- Gatekeeper-derived patch vs sandbox patch mismatch, and fabricated base (H4).
- Worker-forbidden transitions (M1).
- Secret files unreadable: `sudo -u {ossrunner,ossbox,hermes} cat /etc/ossbot/secrets/gatekeeper.env` → EACCES.
- A live `doctor` probe in which the research agent is told to read `/proc/self/environ` and web_fetch a URL with it, asserting the permission denial in stream-json (the §23 item 5 deny-rule claim).
- V3 ACP fake-server contract tests and the shim's request_permission rejection (H1).
- Per-uid egress probes (M6).
- Split-coder key absence (H3).
- Gatekeeper egress limited to its 3 hosts.

**M15. The first candidate contribution (the V3 fork upstreamed to `tobenwarrior/hermes-kiro-provider`) has no pipeline entry path.**
Where: §9.4 (issue-driven discovery only; proactive limited to docs-typo/broken-link), criterion 15 ("repo not an enabled target" → refuse), §9.5 (unclear AI policy → SKIPPED).
Fix:
- Add `ossctl contribution import --repo tobenwarrior/hermes-kiro-provider --from-bundle <path> --base <sha> --title-file … --body-file …` (operator; source `operator`; `issue_number` NULL). It creates a contribution that enters at `VALIDATING`, then goes through validate, review and package (identity re-applied), the gatekeeper-derived preview (H4), and normal two-tap approval.
- Add `tobenwarrior/hermes-kiro-provider` to targets.yaml (category `agent-runtime`, priority 1, `ai_policy_override: allowed` only after the human taps it on `/repos`).
- State explicitly that the workflow never pushes it directly.

### NIT

- N1. §10.2 `--security-opt seccomp=builtin` is unverified as a `docker run` value. Drop it, since the default profile applies when the option is omitted, or tag it [VERIFY].
- N2. Missing [VERIFY] tags: Hermes config keys `approvals.mode`, `cron_mode`, `unattended_mode` (§15.1); "compose v5" (§18.1); the `/model` id regex `^[a-z0-9][a-z0-9.\-]{1,63}$` (§20) may reject real ids. Accept any exact catalog id and apply the regex only to `--force`.
- N3. §9.3/§9.11: the package timestamp "stage start" changes on retry, so the SHA is not stable. Persist `package_ts` per rev in the contributions row and reuse it on retries.
- N4. §18.2 step 3 says provision.sh "runs from the mount", which contradicts §7.2 (stream via stdin). Pick stdin for both.
- N5. §10.2: the sandboxd socket group is `ossrunner`, which is not defined in §7.3. Define group `osssbx` with member ossrunner. Specify `prompt_file` regex `^[a-z0-9_-]{1,64}\.md$` and /28 subnet allocation (first free index in the profile pool, persisted in the sandboxd DB).
- N6. §12.6 Telegram token pattern `[0-9]{8,10}:AA…{33}` is inconsistent with the §18.3 entry regex. Use `\d{6,12}:[A-Za-z0-9_-]{30,}` in both.
- N7. §12.3 disclosure says "I reviewed the full diff". Either show Approve only after View diff has been opened to its last page, or reword to "I reviewed and approved this change".
- N8. §20: the discovery URL regex accepts `/pull/N`, but the pipeline is issue-based. Either drop `pull` or define it ("review-only, no contribution").
- N9. §13: if the gatekeeper hangs, Telegram `/kill` is unavailable. Add a watchdog rule: `/v1/health` fails 4 ticks (2 min) in a row → root writes `paused` and logs.
- N10. §7.6: the Hermes API on `127.0.0.1:8642` is forwarded to Windows. Bind it to `127.0.0.2` if Hermes allows, which WSL does not forward [VERIFY], and keep the bearer key.
- N11. FR-4 asks for "run the project" when cheap. State in §9.7/§9.9 that the planner should propose a T1s runtime smoke (build + `--help`/sample run) in the `validate` profile whenever the project ships a CLI or binary.
- N12. §6/§25 keep "no remotes for now". When the user does push the harness repo as a private GitHub repo (message 3), use a separate credential, never the gatekeeper's classic PAT (`public_repo` cannot reach private repos anyway). Add `tools/bin/` and `.ossbot-local/` to `.gitignore` now.

---

## Verified assumptions

These were checked live on 2026-10-17 or against the workspace:

- Kiro CLI release manifest (`desktop-release.q.us-east-1.amazonaws.com/latest/manifest.json`) reports **2.32.0**, with headless `tarGz` x86_64 at `2.32.0/kirocli-x86_64-linux.tar.gz` and per-package sha256 (`05989f9b…d312`).
- kiro.dev V3 ACP migration doc:
  - launch is `kiro-cli acp --agent-engine=v3`;
  - v2-only flags `--agent --model --effort --trust-all-tools --trust-tools` are rejected;
  - the model is set via `session/set_config_option configId:"model"`, and `session/set_model` is **not** implemented;
  - `_meta.kiro.modeId/modelId/effortLevel` are accepted on `session/new`;
  - `--auth-method=client` triggers `_kiro/auth/getAccessToken`;
  - `session/request_permission` is used when the client presents tool approvals;
  - capabilities must be negotiated, not inferred from the version.
- `Amsal1/langfuse` and `Amsal1/n8n` exist as forks with parents `langfuse/langfuse` and `n8n-io/n8n` (§12.5 step 3).
- `tobenwarrior/hermes-kiro-provider` exists, is MIT, has 0 stars, and commit `02c4dd06585436751145961658a22b773f23a0ef` exists (2026-09-17).
- `NousResearch/hermes-agent` latest release is `v0.22.2`, published 2026-10-15.
- Workspace: the VHD is at `.wsl/ossbot/ext4.vhdx` inside the repo. `.gitignore` excludes `.wsl/`, `.env*`, `secrets/`. There are no git remotes. Repo-local git identity is `Amsal Khan <md.amsalkhan@gmail.com>`. ADRs 0001–0012 exist and match the design.
- Design-internal: the GitHub write path is confined to `gatekeeper/submit.py` (import-linter). There is no approve endpoint in the UDS API. Approve requires two taps plus a message-ID-bound single-use 128-bit token. The private-chat and user-ID check is present. Hermes output is rendered without buttons. Kill and pause are deterministic code plus systemd path units, with no LLM involvement. Dry-run defaults ON. Resume does not change dry-run.

## Unverified or wrong assumptions

Wrong as written:
- §8.4 T7 and ADR-0010: Windows processes "cannot" approve or read secrets. False, because `wsl.exe -u root` is available to any user-session process (H7).
- I2 "kernel-enforced, only gatekeeper": false while `ossbox` is in `docker` (M10).
- I9 "only the human may go": violated by the 2770 group-writable control dir (H5).
- §11.3 "gatekeeper egress … enforced by owner-match rules" to 3 hosts: the rules allow any 443 and 53 (M6d).
- §11.2 proxy at `10.212.0.1:3140` with an OUTPUT `-d 10.212.0.1` match: the gateway IP is not guaranteed, and post-DNAT matching fails (M6a, M6b).
- §2/§9.11/ADR-0006 DCO identity "Amsal1": superseded by the user's decision (H2).
- §15.2/ADR-0002/0011 V2 ACP primary: superseded by the user's decision (H1). The design's own V3 fallback sketch is consistent with the live doc.

Unverified and load-bearing (must be probed before dependent code):
- §10.4: "Kiro service hosts … give an attacker no readable channel". This depends on server-side web tools and cloud features (H3).
- `KIRO_API_KEY` honoured by `kiro-cli acp` (V2 or V3). The V3 doc only describes CLI-login and client-supplied auth.
- `--internal` network embedded-DNS behaviour on Docker 29 (M7). Docker 29 firewall backend on WSL kernel 6.18.
- `--security-opt seccomp=builtin` (N1).
- V3 headless honouring permissions deny rules under `--trust-all-tools` (§23 item 5).
- The fine-grained PAT limitation on contributing to non-member public repos (§0 table). This is plausible and consistent with GitHub docs as I know them, but I did not re-fetch it.
- Telegram Bot API facts (64-byte `callback_data`, 4096 chars, single `getUpdates` consumer → Conflict) match my knowledge but were not re-fetched. Bot API version 10.3 was not checked.
- WSL: drvfs sub-folder fstab source, `instanceIdleTimeout` and keepalive behaviour, localhost forwarding scope. All are correctly tagged [VERIFY] in §23.
- Hermes config keys (`approvals.mode`, `cron_mode`, `unattended_mode`), toolset names, installer ref pinning (N2, §23 item 12).
