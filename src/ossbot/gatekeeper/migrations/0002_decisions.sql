-- ossbot gatekeeper schema v2 (DESIGN §14.20.2, PLAN item 24 / FEAT-005): the proposal and skill
-- decision services (§14.18.4.2), kit views and thread counters (§14.18.2, §14.11.3), and the
-- tg_outbox columns Outbox.mark_sent needs to bind kit tokens in its own transaction (delta 6).
-- New ui_tokens purposes (free text; §14.18.2 lists them): explore|files|interdiff|edit_text_pick
-- (card view purposes, multi-use) | proposal_start|proposal_confirm|proposal_cancel (p_ ids) |
-- skill_approve|skill_confirm|skill_cancel|skill_reject|skill_diff|skill_ask (s_ ids).
CREATE TABLE tg_views (
  message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('dashboard','card','explorer','file','interdiff','draft_review','status',
                                      'digest','feature','picker','progress','chat_answer',
                                      'body_part','commit_msg','fyi','confirm_prompt',
                                      'proposal','proposal_details','rescope','ceiling','work_request','link_card',
                                      'skill_card','skill_part')),
  approval_id TEXT, cid TEXT, rev INTEGER,
  action_id TEXT,                                -- outbound action the view belongs to (§14.18.2); NULL for read-only explorers
  path TEXT, page INTEGER,
  state_json TEXT NOT NULL DEFAULT '{}', content_sha256 TEXT, updated_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);

CREATE TABLE tg_thread_seq (thread_key TEXT PRIMARY KEY, n INTEGER NOT NULL);   -- §14.11.3: messages seen per thread

CREATE TABLE proposal_approvals (               -- §14.18.4.2; CR-004 owns the proposal content, referenced by (cid, version)
  id TEXT PRIMARY KEY,                          -- 'p_' + 10 Crockford chars (new_id("p"))
  cid TEXT NOT NULL REFERENCES contributions(id), version INTEGER NOT NULL,
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','armed','confirmed','consumed','invalidated','expired','rejected')),
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, armed_at TEXT, confirmed_at TEXT, consumed_at TEXT,
  invalidated_reason TEXT, decided_by_tg_user INTEGER,
  rejected_reason TEXT CHECK (rejected_reason IN ('skipped','snoozed','never_repo','never_kind')));   -- §14.11.15
CREATE INDEX proposal_approvals_open ON proposal_approvals(cid, version, status);

CREATE TABLE skill_approvals (                  -- §14.15, §14.18.4.2; skill_reviews (0004_hermes.sql, PLAN item 36) keeps the staged content
  id TEXT PRIMARY KEY,                          -- 's_' + 10 Crockford chars (new_id("s"))
  skill_review_id TEXT NOT NULL,                -- skill_reviews.id (any format PLAN item 36 ships)
  skill TEXT NOT NULL, version INTEGER NOT NULL,
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','armed','confirmed','consumed','invalidated','expired','rejected')),
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  body_parts_total INTEGER NOT NULL, body_parts_delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, armed_at TEXT, confirmed_at TEXT, consumed_at TEXT,
  invalidated_reason TEXT, decided_by_tg_user INTEGER);
CREATE INDEX skill_approvals_open ON skill_approvals(skill, status);

ALTER TABLE tg_outbox ADD COLUMN result_message_id INTEGER;          -- written inside mark_sent's transaction
ALTER TABLE tg_outbox ADD COLUMN bind_tokens_json TEXT;              -- tokens to bind to result_message_id (§14.18.2)
ALTER TABLE tg_outbox ADD COLUMN view_json TEXT;                     -- tg_views row to insert at send (§14.18.2)
