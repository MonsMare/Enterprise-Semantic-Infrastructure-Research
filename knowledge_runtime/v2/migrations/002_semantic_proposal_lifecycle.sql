ALTER TABLE semantic_proposals
    ADD COLUMN IF NOT EXISTS extractor_version TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE semantic_proposals
    ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0;
ALTER TABLE semantic_proposals
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE semantic_proposals
    ADD COLUMN IF NOT EXISTS conflict_details_json JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE semantic_proposals
SET idempotency_key = proposal_id
WHERE idempotency_key IS NULL;

ALTER TABLE semantic_proposals
    ALTER COLUMN idempotency_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_proposals_idempotency
    ON semantic_proposals(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_semantic_proposals_status
    ON semantic_proposals(status, proposal_id);
