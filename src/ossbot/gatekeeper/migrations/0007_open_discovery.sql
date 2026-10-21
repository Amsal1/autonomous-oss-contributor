-- ossbot gatekeeper schema v7 (post-v1 open discovery, design §8.1, §13.3, §3.2): discovered targets,
-- contribution holds (egress-anomaly safety net) and the pending_input rebuild for 'open_topic_add'.
-- Token purposes added (ui_tokens.purpose is free text, no schema change): proposal_keep,
-- proposal_keep_confirm, hold_resume, hold_resume_confirm.

-- One row per repo found outside targets.yaml (DESIGN §9.4, §17.3); never written to tracked config.
-- The only writer is ossbot.gatekeeper.discovered_targets_writer (import-linter +
-- tests/unit/gatekeeper/test_discovered_targets_writer.py). source is set on insert and never changed.
CREATE TABLE discovered_targets (
  repo TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
  source TEXT NOT NULL CHECK (source IN ('search', 'hermes', 'owner')),
  status TEXT NOT NULL CHECK (status IN ('suggested', 'candidate', 'kept', 'never', 'skipped')),
  topic TEXT, stars INTEGER, language TEXT, license_spdx TEXT, pushed_at TEXT,
  default_branch_commit_at TEXT,         -- owner addition: the default branch's latest commit (activity gate)
  ci_on_pr TEXT CHECK (ci_on_pr IS NULL OR ci_on_pr IN ('yes', 'no', 'unknown')),
  repo_score REAL, skip_reason TEXT, suggested_issue INTEGER,
  egress_strict INTEGER NOT NULL DEFAULT 0 CHECK (egress_strict IN (0, 1)),
  first_seen TEXT NOT NULL, updated_at TEXT NOT NULL, tried_at TEXT, checked_at TEXT, cooldown_until TEXT,
  decided_by TEXT, decided_at TEXT);
CREATE INDEX discovered_targets_status ON discovered_targets (status, cooldown_until);

-- §13.3: an open hold (released_at NULL) stops every stage, /package and each submit write of the
-- contribution; only the human releases it. Not a state transition (I13 unchanged).
CREATE TABLE contribution_holds (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL REFERENCES contributions(id),
  reason TEXT NOT NULL CHECK (reason IN ('egress_anomaly')),
  detail_json TEXT NOT NULL, held_by TEXT NOT NULL, held_at TEXT NOT NULL,
  released_by TEXT, released_at TEXT);
CREATE UNIQUE INDEX contribution_holds_open ON contribution_holds (cid) WHERE released_at IS NULL;

-- §3.2: pending_input.kind gains 'open_topic_add' (/filters page "Open", add a topic). SQLite can't alter
-- a CHECK, so the table is rebuilt (the 0003 procedure; db.migrate runs with foreign keys off and
-- requires an empty foreign_key_check). Open rows (15 min TTL) are copied as is.
CREATE TABLE pending_input_v7 (         -- ForceReply correlation (§14.10.4 rule 3): 15 min TTL, single consumption
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('changes','edit_title','edit_body','reject_other','edit_comment',
                                     'comment','line_pick','review_send_text','body_file','feature_fix',
                                     'proposal_adjust','proposal_snooze_days','open_topic_add')),
  cid TEXT, approval_id TEXT, action_id TEXT, proposal_id TEXT, rev INTEGER,
  path TEXT, side TEXT CHECK (side IN ('old','new')), hunk INTEGER, line_candidates_json TEXT,
  prompt_message_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT);
INSERT INTO pending_input_v7 (
  id, kind, cid, approval_id, action_id, proposal_id, rev, path, side, hunk, line_candidates_json,
  prompt_message_id, chat_id, created_at, expires_at, consumed_at)
SELECT
  id, kind, cid, approval_id, action_id, proposal_id, rev, path, side, hunk, line_candidates_json,
  prompt_message_id, chat_id, created_at, expires_at, consumed_at
FROM pending_input;
DROP TABLE pending_input;
ALTER TABLE pending_input_v7 RENAME TO pending_input;
CREATE INDEX pending_input_open ON pending_input(chat_id, prompt_message_id) WHERE consumed_at IS NULL;
