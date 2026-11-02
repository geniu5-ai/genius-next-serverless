-- Durable, idempotent result of the post-draft status audit.
CREATE TABLE IF NOT EXISTS genius_next.status_audits (
    id                bigserial PRIMARY KEY,
    tenant_id         text NOT NULL,
    turn_id           text NOT NULL,
    phone             text NOT NULL,
    source_sequence   bigint NOT NULL DEFAULT 0 CHECK (source_sequence >= 0),
    customer_status   text NOT NULL CHECK (customer_status IN ('new', 'win', 'loss', 'off')),
    agent_commitment  text,
    confidence        double precision NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    evidence_source   text NOT NULL,
    model             text,
    audited_at        timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, turn_id)
);

CREATE INDEX IF NOT EXISTS status_audits_client_sequence_idx
    ON genius_next.status_audits (tenant_id, phone, source_sequence DESC);
