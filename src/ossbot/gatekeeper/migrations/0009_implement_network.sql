-- ossbot gatekeeper schema v9 (post-v1 sandbox network, ADR-0024): the owner's implement_network setting,
-- the per-contribution network-rerun counter and grant, and the rerun hint of a sandbox-gap failure.

-- Only the human changes it (Telegram /egress confirm, ossctl TTY confirm); control.py is its only writer.
-- 'off' on a fresh DB, after a DB reset and on upgrade (this default), like dry_run (I7).
ALTER TABLE control_state ADD COLUMN implement_network TEXT NOT NULL DEFAULT 'off'
  CHECK (implement_network IN ('off', 'on_request', 'auto'));

-- The H-only edge FAILED -> PROPOSAL_APPROVED (reason network_rerun) counts here, bounded by
-- policy.limits.network_reruns_max; it sets the grant, which every move into RESCOPE_REQUIRED, PACKAGING,
-- AWAITING_APPROVAL or a terminal state clears.
ALTER TABLE contributions ADD COLUMN network_reruns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contributions ADD COLUMN network_grant INTEGER NOT NULL DEFAULT 0 CHECK (network_grant IN (0, 1));
-- The proposal version the grant was tapped for (carried criterion D17): a grant counts only while it equals
-- contributions.proposal_version; NULL whenever the grant is cleared.
ALTER TABLE contributions ADD COLUMN network_grant_version INTEGER;
-- 1 when the item failed in the validation loop with sandbox gaps reported (evidence sandbox_gap: true).
ALTER TABLE contributions ADD COLUMN rerun_hint INTEGER NOT NULL DEFAULT 0 CHECK (rerun_hint IN (0, 1));

-- The gate-2 provenance line reads the implement_egress_decided events of one cid after the round start.
-- The append-only triggers cover UPDATE and DELETE only, so an index is fine.
CREATE INDEX IF NOT EXISTS audit_events_cid_seq ON audit_events (cid, seq);
