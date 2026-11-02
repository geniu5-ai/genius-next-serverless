-- 005_status_simplify.sql — elimina status 'lead' do enum customer_status.
-- O contrato AgentDecision.customer_status agora é "new", "win", "loss", "off".
-- "lead" era um artefato de funil que nunca foi populado com semântica distinta
-- de "new" — todos os registros com status='lead' viram 'new' neste backfill.

-- 1) Backfill antes de qualquer alteração de constraint.
UPDATE genius_next.clients
SET status = 'new'
WHERE status = 'lead';

-- 2) Remove 'lead' da constraint CHECK de status.
ALTER TABLE genius_next.clients
    DROP CONSTRAINT IF EXISTS clients_status_check;

ALTER TABLE genius_next.clients
    ADD CONSTRAINT clients_status_check
    CHECK (status IN ('new', 'win', 'loss', 'off'));

COMMENT ON COLUMN genius_next.clients.status IS
    'Status do cliente: new (sem sinal explícito), win (compra/agendamento confirmado), loss (recusa explícita/silêncio após follow-ups), off (pediu para não ser contatado)';
