-- ossbot gatekeeper schema v5 (DESIGN §14.20.2, PLAN items 44-45 / FEAT-009): system topics, ForceReply
-- correlation, the draft review, the outbox priority/thread/coalescing columns and the UX preferences.
-- New ui_tokens purposes (free text; §14.18.2 lists them): nav|file_pick|comment_hunk|comment_line_pick|
-- review_send|review_send_now|review_discard|comment_undo|body_file|show_card|stale_retry|
-- stale_show_current|queue_open|queue_sort|picker|confirm_cmd|danger_second_tap|comment_replace|
-- comment_replace_cancel|use_as_comment|quiet_set|notify_toggle|settings_set|size_set|size_pref_toggle|
-- size_repo_pick|filters_toggle|filters_page|tune_undo|work_on_link|work_cancel|link_dismiss|
-- proposal_skip|proposal_adjust|proposal_smaller|proposal_never|proposal_never_confirm|proposal_later|
-- proposal_details|proposal_ask_first|proposal_undo|ceiling_recheck|ceiling_split|rescope_continue|
-- item_drop|item_drop_confirm|edit_title|edit_body|clear_edits|reject_pick|dash_*.
CREATE TABLE tg_topics (name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE pending_input (            -- ForceReply correlation (§14.10.4 rule 3): 15 min TTL, single consumption
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('changes','edit_title','edit_body','reject_other','edit_comment',
                                     'comment','line_pick','review_send_text','body_file','feature_fix',
                                     'proposal_adjust','proposal_snooze_days')),
  cid TEXT, approval_id TEXT, action_id TEXT, proposal_id TEXT, rev INTEGER,
  path TEXT, side TEXT CHECK (side IN ('old','new')), hunk INTEGER, line_candidates_json TEXT,
  prompt_message_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT);
CREATE INDEX pending_input_open ON pending_input(chat_id, prompt_message_id) WHERE consumed_at IS NULL;

CREATE TABLE review_comments (          -- the draft review (§14.11.5), keyed by (cid, rev)
  id TEXT PRIMARY KEY, cid TEXT NOT NULL, rev INTEGER NOT NULL, approval_id TEXT,
  source TEXT NOT NULL CHECK (source IN ('telegram','mirror')), path TEXT, side TEXT CHECK (side IN ('old','new')),
  line_start INTEGER, line_end INTEGER, quote TEXT, text TEXT NOT NULL,
  tg_message_id INTEGER, gh_comment_id INTEGER, outdated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, change_request_id TEXT, discarded_at TEXT);
CREATE INDEX review_comments_draft ON review_comments(cid, rev) WHERE change_request_id IS NULL AND discarded_at IS NULL;

ALTER TABLE change_requests ADD COLUMN comments_json TEXT;           -- structured comments (§14.11.5)

ALTER TABLE tg_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 2;   -- P0..P3 (§14.13.1)
ALTER TABLE tg_outbox ADD COLUMN thread_key TEXT;                    -- topic name, resolved at send time
ALTER TABLE tg_outbox ADD COLUMN coalesce_key TEXT;                  -- '<cid>:<event_kind>' (§14.13.3)
ALTER TABLE tg_outbox ADD COLUMN merged_into INTEGER;                -- batch row id when status = 'merged'
-- tg_outbox.status has no CHECK (0001_init.sql); new values: 'held' (quiet hours / batch window), 'merged'
CREATE INDEX tg_outbox_coalesce ON tg_outbox(coalesce_key, status);

CREATE TABLE ux_prefs (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
