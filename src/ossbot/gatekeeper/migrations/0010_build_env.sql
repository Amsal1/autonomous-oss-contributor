-- ossbot gatekeeper schema v10 (post-v1 build-env, ADR-0025): a second hold reason and the egress notes.

-- A floor reach attempt holds the contribution in every mode (reason 'egress_floor', next to the volume
-- anomaly). SQLite can't alter a CHECK, so the table is rebuilt (the 0003/0007 procedure; db.migrate runs
-- with foreign keys off and requires an empty foreign_key_check). Every row is copied unchanged, open and
-- released alike, and the partial unique index (one open hold per cid) is re-created.
CREATE TABLE contribution_holds_v10 (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL REFERENCES contributions(id),
  reason TEXT NOT NULL CHECK (reason IN ('egress_anomaly', 'egress_floor')),
  detail_json TEXT NOT NULL, held_by TEXT NOT NULL, held_at TEXT NOT NULL,
  released_by TEXT, released_at TEXT);
INSERT INTO contribution_holds_v10 (id, cid, reason, detail_json, held_by, held_at, released_by, released_at)
SELECT id, cid, reason, detail_json, held_by, held_at, released_by, released_at FROM contribution_holds;
DROP TABLE contribution_holds;
ALTER TABLE contribution_holds_v10 RENAME TO contribution_holds;
CREATE UNIQUE INDEX contribution_holds_open ON contribution_holds (cid) WHERE released_at IS NULL;

-- Notes from sandboxd's meter (ADR-0025): a web job of sandbox_full over a volume limit (it ran on), or a
-- job's floor notes (loopback names, the HashiCorp beacon). Data for the daily digest only: no transition,
-- no invalidation, no message at write time. One row per job run (idempotent). The only writer is
-- ossbot.gatekeeper.egress_notes (import-linter). Kept like the contribution's holds (no row is deleted).
CREATE TABLE egress_notes (
  id INTEGER PRIMARY KEY,
  cid TEXT NOT NULL REFERENCES contributions(id),
  run_id TEXT NOT NULL UNIQUE,
  stage TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('volume', 'floor_note')),
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE INDEX egress_notes_created ON egress_notes (created_at);
