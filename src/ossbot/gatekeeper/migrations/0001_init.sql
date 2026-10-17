-- ossbot gatekeeper schema v1 (DESIGN §12.7, verbatim). Applied by db.migrate in one transaction.
CREATE TABLE control_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL CHECK (state IN ('running','paused','killed')),
  dry_run INTEGER NOT NULL DEFAULT 1,
  config_hash TEXT NOT NULL,
  reason TEXT, changed_by TEXT NOT NULL, changed_at TEXT NOT NULL,
  resume_log_offset INTEGER NOT NULL DEFAULT 0);   -- N4: bytes of resume.log already announced

CREATE TABLE policy_floor (               -- monotonic personal floors (Section 7.9)
  kind TEXT NOT NULL CHECK (kind IN ('forbidden_login','confidentiality')),
  value TEXT NOT NULL, added_at TEXT NOT NULL, removed_at TEXT, removed_by TEXT,
  PRIMARY KEY (kind, value));

CREATE TABLE config_snapshots (
  hash TEXT PRIMARY KEY, files_json TEXT NOT NULL, loaded_at TEXT NOT NULL, loaded_by TEXT NOT NULL);

CREATE TABLE contributions (
  id TEXT PRIMARY KEY,                 -- 'c_' + 10 char base32 (ULID-derived), sortable
  upstream TEXT NOT NULL,              -- 'owner/repo'
  issue_number INTEGER, issue_url TEXT, issue_title TEXT,
  source TEXT NOT NULL CHECK (source IN ('discover','hermes_request','operator')),
  category TEXT, priority INTEGER, score REAL,
  state TEXT NOT NULL, state_reason TEXT,
  implement_attempts INTEGER NOT NULL DEFAULT 0,
  review_rounds INTEGER NOT NULL DEFAULT 0,
  change_rounds INTEGER NOT NULL DEFAULT 0,
  watch_rounds INTEGER NOT NULL DEFAULT 0,
  base_ref TEXT, base_sha TEXT, upstream_base_tip TEXT, head_sha TEXT, branch TEXT,
  artifact_rev INTEGER, package_ts_json TEXT NOT NULL DEFAULT '{}',   -- {"<rev>": "2026-10-17T10:00:00Z"}
  pr_title TEXT, pr_body TEXT,                     -- packaged values
  pr_title_override TEXT, pr_body_override TEXT,   -- human edits; survive later revs until cleared
  commit_message TEXT,                             -- gatekeeper-derived
  patch_sha256 TEXT, commit_message_sha256 TEXT, bundle_sha256 TEXT,
  diffstat_json TEXT, plan_json TEXT, validation_json TEXT, review_json TEXT, vetting_json TEXT,
  risk TEXT, confidence REAL, models_json TEXT,
  pr_number INTEGER, pr_url TEXT,
  import_head_ref TEXT, import_bundle_sha256 TEXT,   -- M3: operator import only
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- M6: at most one non-terminal contribution per issue; terminal rows never block a re-attempt
-- (re-discovery rules in 9.4 decide whether one is allowed).
CREATE UNIQUE INDEX contributions_issue_active ON contributions(upstream, issue_number)
  WHERE issue_number IS NOT NULL AND state NOT IN
  ('SKIPPED','ABANDONED','REJECTED','EXPIRED','FAILED','DRY_RUN_DONE','MERGED','CLOSED');
CREATE INDEX contributions_issue ON contributions(upstream, issue_number, updated_at);

CREATE TABLE stage_runs (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL REFERENCES contributions(id),
  stage TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','interrupted','cancelled')),
  profile TEXT, coder_mode TEXT, model TEXT, model_actual TEXT, effort TEXT,
  started_at TEXT, heartbeat_at TEXT, finished_at TEXT, exit_code INTEGER,
  error_class TEXT, error_message TEXT, log_path TEXT);

CREATE TABLE outbound_actions (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL REFERENCES contributions(id),
  kind TEXT NOT NULL CHECK (kind IN ('open_pr','push_update','comment','issue_claim')),
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending_approval','approved','executing','done','dry_run_done','failed','cancelled')),
  error_class TEXT,                    -- e.g. interrupted, branch_moved (M5)
  result_json TEXT, created_at TEXT NOT NULL, executed_at TEXT);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,                 -- 'a_' + 10 char
  action_id TEXT NOT NULL REFERENCES outbound_actions(id),
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  dry_run_shown INTEGER NOT NULL,      -- mode printed on the card (display only; binding recomputes)
  status TEXT NOT NULL CHECK (status IN ('pending','armed','confirmed','consumed','invalidated','expired','rejected')),
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  body_parts_total INTEGER NOT NULL, body_parts_delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  armed_at TEXT, confirmed_at TEXT, consumed_at TEXT,
  invalidated_reason TEXT, decided_by_tg_user INTEGER);
CREATE INDEX approvals_action ON approvals(action_id, status);

CREATE TABLE ui_tokens (                -- opaque callback tokens (callback_data = 't:' + token)
  token TEXT PRIMARY KEY,              -- 16 random bytes, base64url (22 chars)
  purpose TEXT NOT NULL,               -- approve|confirm|cancel|reject|reject_reason|changes|diff_page|logs|edit_title|edit_body|clear_edits|resume_confirm|dryrun_off_confirm|reload_confirm|model_set|repo_toggle|ai_allow|retry|give_up_fix|stop_watch|stop_watch_confirm
  approval_id TEXT, cid TEXT, arg TEXT,
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  single_use INTEGER NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);

CREATE TABLE change_requests (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT);

CREATE TABLE repo_facts (
  upstream TEXT PRIMARY KEY, facts_json TEXT NOT NULL, fetched_at TEXT NOT NULL);

CREATE TABLE repo_overrides (
  upstream TEXT PRIMARY KEY, enabled INTEGER, ai_policy_override TEXT, cooldown_until TEXT,
  updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE model_catalog (
  model_id TEXT PRIMARY KEY, display_name TEXT, raw_json TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
  forced INTEGER NOT NULL DEFAULT 0, forced_by TEXT, forced_at TEXT);   -- forced = set via ossctl --force (N6)

CREATE TABLE tg_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, payload_json TEXT NOT NULL,
  dedupe_key TEXT UNIQUE, approval_id TEXT, part_index INTEGER,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL, last_error TEXT);

CREATE TABLE watch_state (
  cid TEXT PRIMARY KEY, last_event_at TEXT, seen_event_ids_json TEXT NOT NULL DEFAULT '[]');

CREATE TABLE audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, actor TEXT NOT NULL,   -- 'tg:<user_id>' | 'uid:<n>/<role>' | 'system'
  event TEXT NOT NULL, cid TEXT, data_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL);   -- row_hash = sha256(prev_hash || canonical(seq,ts,actor,event,cid,data))
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
