-- 020_status_audits.sql -- durable post-draft status audit log.
-- One row per (tenant_id, turn_id): a turn is audited at most once.  The row
-- records the customer status, the agent commitment (context only, never proof
-- of outcome), the evidence source, the model used, and any error.  The unique
-- key makes the DBOS workflow idempotent across replays.

CREATE TABLE IF NOT EXISTS genius_next.status_audits (
    id                 bigserial PRIMARY KEY,
    tenant_id          text NOT NULL,
    turn_id            text NOT NULL,
    phone              text NOT NULL,
    customer_status    text NOT NULL CHECK (customer_status IN ('new', 'win', 'loss', 'off')),
    agent_commitment   text,
    confidence         double precision NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0.0 AND 1.0),
    evidence_source    text NOT NULL,
    model              text,
    error              text,
    audited_at         timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS status_audits_tenant_turn_key
    ON genius_next.status_audits (tenant_id, turn_id);

CREATE INDEX IF NOT EXISTS status_audits_tenant_phone_audited_idx
    ON genius_next.status_audits (tenant_id, phone, audited_at DESC);

COMMENT ON TABLE genius_next.status_audits IS
    'One idempotent post-draft status audit per turn; agent reply is context/commitment, never proof of win/loss/off.';
