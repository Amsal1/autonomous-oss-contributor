-- ossbot gatekeeper schema v3 (DESIGN §9.2, §9.5a, §9.5b, §9.14, §14.20.2, §16.3; PLAN items 28-29 /
-- FEAT-006): CR-004 proposal content and versions, outcomes, discovery_prefs, runner requests.
--
-- contributions is rebuilt: source gains 'user' (/work, 👤) and SQLite can't alter a CHECK. db.migrate
-- runs every migration with foreign keys off and requires an empty foreign_key_check before COMMIT
-- (SQLite's table-rebuild procedure); the references to contributions(id) name the rebuilt table.
CREATE TABLE contributions_v3 (
  id TEXT PRIMARY KEY,
  upstream TEXT NOT NULL,
  issue_number INTEGER, issue_url TEXT, issue_title TEXT,
  source TEXT NOT NULL CHECK (source IN ('discover','hermes_request','operator','user')),
  category TEXT, priority INTEGER, score REAL,
  state TEXT NOT NULL, state_reason TEXT,
  implement_attempts INTEGER NOT NULL DEFAULT 0,
  review_rounds INTEGER NOT NULL DEFAULT 0,
  change_rounds INTEGER NOT NULL DEFAULT 0,
  watch_rounds INTEGER NOT NULL DEFAULT 0,
  base_ref TEXT, base_sha TEXT, upstream_base_tip TEXT, head_sha TEXT, branch TEXT,
  artifact_rev INTEGER, package_ts_json TEXT NOT NULL DEFAULT '{}',
  pr_title TEXT, pr_body TEXT,
  pr_title_override TEXT, pr_body_override TEXT,
  commit_message TEXT,
  patch_sha256 TEXT, commit_message_sha256 TEXT, bundle_sha256 TEXT,
  diffstat_json TEXT, plan_json TEXT, validation_json TEXT, review_json TEXT, vetting_json TEXT,
  risk TEXT, confidence REAL, models_json TEXT,
  pr_number INTEGER, pr_url TEXT,
  import_head_ref TEXT, import_bundle_sha256 TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  -- CR-004
  work_url TEXT, work_ref TEXT CHECK (work_ref IN ('issue','pull','repo')),   -- /work target (source='user')
  notes TEXT,                                  -- /work notes (≤ 1000, owner text, shown on every card)
  issue_hash TEXT,                             -- sha256(title, body, labels) at discovery (Skip rule)
  kind TEXT,                                   -- proposal kind (bug, docs, ...), from the newest proposal
  proposal_version INTEGER,                    -- newest stored version
  est_class TEXT, approved_class TEXT,         -- newest estimate; class bound by Start work
  proposal_feedback_json TEXT,                 -- pending Adjust / Make it smaller / Split for scout
  declined_at TEXT, declined_reason TEXT,      -- Skip/Later/Never (Undo window, §14.11.15)
  snooze_until TEXT,                           -- Later
  size_report_json TEXT);                      -- /package measurement vs the approved estimate
INSERT INTO contributions_v3 (
  id, upstream, issue_number, issue_url, issue_title, source, category, priority, score, state, state_reason,
  implement_attempts, review_rounds, change_rounds, watch_rounds, base_ref, base_sha, upstream_base_tip,
  head_sha, branch, artifact_rev, package_ts_json, pr_title, pr_body, pr_title_override, pr_body_override,
  commit_message, patch_sha256, commit_message_sha256, bundle_sha256, diffstat_json, plan_json,
  validation_json, review_json, vetting_json, risk, confidence, models_json, pr_number, pr_url,
  import_head_ref, import_bundle_sha256, created_at, updated_at)
SELECT
  id, upstream, issue_number, issue_url, issue_title, source, category, priority, score, state, state_reason,
  implement_attempts, review_rounds, change_rounds, watch_rounds, base_ref, base_sha, upstream_base_tip,
  head_sha, branch, artifact_rev, package_ts_json, pr_title, pr_body, pr_title_override, pr_body_override,
  commit_message, patch_sha256, commit_message_sha256, bundle_sha256, diffstat_json, plan_json,
  validation_json, review_json, vetting_json, risk, confidence, models_json, pr_number, pr_url,
  import_head_ref, import_bundle_sha256, created_at, updated_at
FROM contributions;
DROP TABLE contributions;
ALTER TABLE contributions_v3 RENAME TO contributions;
-- M6 + CR-004: PROPOSAL_REJECTED is terminal (unless the human Undo)
CREATE UNIQUE INDEX contributions_issue_active ON contributions(upstream, issue_number)
  WHERE issue_number IS NOT NULL AND state NOT IN
  ('SKIPPED','ABANDONED','REJECTED','EXPIRED','FAILED','DRY_RUN_DONE','MERGED','CLOSED','PROPOSAL_REJECTED');
CREATE INDEX contributions_issue ON contributions(upstream, issue_number, updated_at);
CREATE INDEX contributions_state ON contributions(state, updated_at);

-- Scout output, one row per version; proposal_approvals references (cid, version).
CREATE TABLE proposals (
  cid TEXT NOT NULL REFERENCES contributions(id), version INTEGER NOT NULL,
  proposal_json TEXT NOT NULL,                 -- canonical JSON of the validated Proposal
  proposal_sha256 TEXT NOT NULL, est_class TEXT NOT NULL,
  feedback TEXT,                               -- the owner's Adjust text this version answers
  created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (cid, version));

-- Track record (§9.14): proposal -> PR -> merge per repo, kind and size class.
CREATE TABLE outcomes (
  cid TEXT PRIMARY KEY REFERENCES contributions(id),
  upstream TEXT NOT NULL, kind TEXT, size_class TEXT, source TEXT NOT NULL,
  proposed_at TEXT, started_at TEXT, pr_opened_at TEXT, finished_at TEXT,
  result TEXT CHECK (result IN ('declined','abandoned','failed','expired','dry_run','merged','closed')),
  measured_class TEXT, updated_at TEXT NOT NULL);
CREATE INDEX outcomes_class ON outcomes(size_class, result);

-- §14.14.4 / CR-004: written only by gatekeeper/discovery_prefs.py; scope '*' = global, else 'owner/repo'.
-- filters_json holds the /filters overrides and, per repo, {"never": true} (Never this repo).
CREATE TABLE discovery_prefs (
  scope TEXT PRIMARY KEY, autonomous_max_class TEXT CHECK (autonomous_max_class IN ('xs','s','m','l','xl')),
  preferred_classes_json TEXT, filters_json TEXT, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
-- 60 s undo of the last change of a scope: the previous row (NULL = no row)
CREATE TABLE discovery_prefs_undo (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, previous_json TEXT, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, used_at TEXT);

-- Requests the gatekeeper holds for the runner's next scheduler tick (ossctl models refresh):
-- one row per kind, consumed (deleted) by the worker.
CREATE TABLE runner_requests (
  kind TEXT PRIMARY KEY CHECK (kind IN ('catalog_refresh')),
  requested_at TEXT NOT NULL, actor TEXT NOT NULL);
