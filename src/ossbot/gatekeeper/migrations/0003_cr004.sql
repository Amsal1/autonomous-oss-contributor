-- ossbot gatekeeper schema v3 (DESIGN §9.2, §9.5b, §9.14, §16.3; PLAN items 28-29 / FEAT-006).
-- Requests the gatekeeper holds for the runner's next scheduler tick (ossctl models refresh):
-- one row per kind, consumed (deleted) by the worker.
CREATE TABLE runner_requests (
  kind TEXT PRIMARY KEY CHECK (kind IN ('catalog_refresh')),
  requested_at TEXT NOT NULL, actor TEXT NOT NULL);
