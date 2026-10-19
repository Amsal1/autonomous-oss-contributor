-- ossbot gatekeeper schema v4 (DESIGN §14.20.2, §15.5, §15.6; PLAN items 36-37 / FEAT-007):
-- the skill gate's staged reviews (imported from the root hasher) and the assistant's chat turns.
CREATE TABLE skill_reviews (
  id TEXT PRIMARY KEY,                    -- 'r_' + 20 hex: sha256(pending id, content digest), from the root hasher
  pending_id TEXT NOT NULL,               -- Hermes ~/.hermes/pending/skills/<id>.json (skills.write_approval)
  skill TEXT NOT NULL, rel_dir TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create','update','delete')),
  version INTEGER NOT NULL,               -- 1 + the skill's activated versions
  files_json TEXT NOT NULL,               -- {relative path: sha256} of the proposed tree ({} for delete)
  content_sha256 TEXT NOT NULL,           -- digest of files_json: what the approval binds and root activates
  declared_tools_json TEXT NOT NULL DEFAULT '[]',
  tier TEXT NOT NULL,                     -- the tier when it was staged
  summary TEXT, diff TEXT, origin TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged','activating','activated','rejected','superseded','invalid')),
  reason TEXT,
  created_at TEXT NOT NULL, decided_at TEXT);
CREATE INDEX skill_reviews_skill ON skill_reviews(skill, status);
CREATE TABLE chat_turns (
  turn_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, thread_id INTEGER, user_message_id INTEGER NOT NULL,
  run_id TEXT, status TEXT NOT NULL CHECK (status IN ('streaming','done','stopped','failed')),
  started_at TEXT NOT NULL, finished_at TEXT, model TEXT, answer_message_id INTEGER,
  origin TEXT NOT NULL DEFAULT 'typed' CHECK (origin IN ('typed','followup_tap','media','retry')),
  user_text TEXT,                         -- only for the tg-<uid>-reviews session (Use as comment), <= 4,000; GC after 24 h
  answer_text TEXT,                       -- the persisted final answer, <= 200 KB, redacted before it is stored
  tools_json TEXT NOT NULL DEFAULT '[]',  -- tool names used in the turn (<= 64 chars each)
  had_attachments INTEGER NOT NULL DEFAULT 0,
  feature_cards INTEGER NOT NULL DEFAULT 0);
CREATE INDEX chat_turns_session ON chat_turns(session_key, status, finished_at);
CREATE TABLE chat_sessions (tg_user_id INTEGER PRIMARY KEY, n INTEGER NOT NULL, updated_at TEXT NOT NULL);
