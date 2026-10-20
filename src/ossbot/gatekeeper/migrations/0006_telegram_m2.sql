-- FEAT-010 (PLAN items 50-52, DESIGN §14.20.2): digest runs, reminders, the opt-in review mirror and
-- Telegraph pages. The tables match the DESIGN schema block exactly.
CREATE TABLE digest_runs (kind TEXT NOT NULL, period_start TEXT NOT NULL, sent_at TEXT, stats_json TEXT NOT NULL,
  PRIMARY KEY (kind, period_start));
CREATE TABLE reminders (id TEXT PRIMARY KEY, due_at TEXT NOT NULL, text TEXT NOT NULL, created_by TEXT NOT NULL,
  thread_id INTEGER, fired_at TEXT, cancelled_at TEXT);
CREATE TABLE review_mirror (
  cid TEXT NOT NULL, rev INTEGER NOT NULL, branch TEXT NOT NULL, pr_number INTEGER, mirror_head TEXT NOT NULL,
  tree_equal INTEGER NOT NULL, pushed_at TEXT, closed_at TEXT, last_error TEXT, PRIMARY KEY (cid, rev));
CREATE TABLE telegraph_pages (path TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, turn_id INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL, published_at TEXT NOT NULL, unpublished_at TEXT);   -- audit event on publish/unpublish
