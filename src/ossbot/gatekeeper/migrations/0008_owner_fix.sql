-- ossbot gatekeeper schema v8 (post-v1 owner fix rounds, design §9.2, §9.12; ADR-0003 addendum 2026-10-23):
-- the owner fix-round counter, the explicit fixes-disabled flag (M4), the change-request source columns
-- (Part B's review-bot findings use them; no later migration needed) and the pending_input rebuild for 'pr_fix'.
-- Token purposes added (ui_tokens.purpose is free text, no schema change): pr_fix, pr_fix_confirm,
-- pr_fix_cancel (Part A); pr_bot_fix, pr_bot_fix_confirm, pr_bot_back, pr_bot_ignore (Part B).

-- The human edge WATCHING->IMPLEMENTING (owner /fix) counts here, never in watch_rounds: watch_rounds_max
-- guards LLM-started rounds, and an owner round must not spend it (policy.limits.owner_fix_rounds_max).
ALTER TABLE contributions ADD COLUMN owner_fix_rounds INTEGER NOT NULL DEFAULT 0;

-- M4: branch_moved disables every fix round, the worker's and the owner's. Until v7 it was encoded only as
-- watch_rounds := watch_rounds_max, which the separate owner cap would bypass. A closed set of reasons.
ALTER TABLE contributions ADD COLUMN fixes_disabled_reason TEXT
  CHECK (fixes_disabled_reason IS NULL OR fixes_disabled_reason IN ('branch_moved'));

-- Backfill from the audit log (read only; the chain is never rewritten): the transition evidence carries
-- error_class branch_moved at every site that set it (the /package redirect, submit, reconcile), and it
-- survives later state_reason overwrites.
UPDATE contributions SET fixes_disabled_reason = 'branch_moved'
WHERE pr_number IS NOT NULL AND EXISTS (
  SELECT 1 FROM audit_events a
  WHERE a.cid = contributions.id AND a.event = 'transition'
    AND json_extract(a.data_json, '$.evidence.error_class') = 'branch_moved');

-- Who asked: 'owner' (Request changes, /fix notes; the trusted <human_instruction> channel) or 'review_bot'
-- (a re-fetched review-bot finding; its text is untrusted data and never a human instruction).
ALTER TABLE change_requests ADD COLUMN source TEXT NOT NULL DEFAULT 'owner'
  CHECK (source IN ('owner', 'review_bot'));
-- review_bot only: JSON {comment_id, path, line, author} of the finding the gatekeeper re-fetched.
ALTER TABLE change_requests ADD COLUMN source_ref TEXT;
-- review_bot only: the re-fetched finding body, wrapped <untrusted> in the coder prompt.
ALTER TABLE change_requests ADD COLUMN untrusted_text TEXT;

-- pending_input.kind gains 'pr_fix' (status card 🔧 Fix with notes). SQLite can't alter a CHECK, so the table
-- is rebuilt as in 0007 (db.migrate runs with foreign keys off and requires an empty foreign_key_check).
-- Open rows (15 min TTL) are copied as is.
CREATE TABLE pending_input_v8 (         -- ForceReply correlation (§14.10.4 rule 3): 15 min TTL, single consumption
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('changes','edit_title','edit_body','reject_other','edit_comment',
                                     'comment','line_pick','review_send_text','body_file','feature_fix',
                                     'proposal_adjust','proposal_snooze_days','open_topic_add','pr_fix')),
  cid TEXT, approval_id TEXT, action_id TEXT, proposal_id TEXT, rev INTEGER,
  path TEXT, side TEXT CHECK (side IN ('old','new')), hunk INTEGER, line_candidates_json TEXT,
  prompt_message_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT);
INSERT INTO pending_input_v8 (
  id, kind, cid, approval_id, action_id, proposal_id, rev, path, side, hunk, line_candidates_json,
  prompt_message_id, chat_id, created_at, expires_at, consumed_at)
SELECT
  id, kind, cid, approval_id, action_id, proposal_id, rev, path, side, hunk, line_candidates_json,
  prompt_message_id, chat_id, created_at, expires_at, consumed_at
FROM pending_input;
DROP TABLE pending_input;
ALTER TABLE pending_input_v8 RENAME TO pending_input;
CREATE INDEX pending_input_open ON pending_input(chat_id, prompt_message_id) WHERE consumed_at IS NULL;
