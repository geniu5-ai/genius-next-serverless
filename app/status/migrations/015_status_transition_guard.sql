-- 015_status_transition_guard.sql -- storage-layer enforcement of terminal status.
-- Closes a real gap: the CASE guard in _CLIENT_APPLY_DECISION_SQL only protects
-- the LLM classifier path. The operator control path (_CONTROL_APPLY_CLIENT_SQL)
-- and any future/ad-hoc SQL had zero protection against off->new or win/loss->new.
-- This trigger makes the rule storage-native: it holds no matter which code path,
-- future refactor, or direct DB session attempts the write.
--
-- Reversal is a break-glass admin action only, never a product feature: it
-- requires set_config('genius_next.admin_override', 'true') in the SAME
-- transaction as the UPDATE. There is no application code path that sets this;
-- it is deliberately reserved for a manual, audited, one-off correction.

CREATE OR REPLACE FUNCTION genius_next.guard_client_status_transition()
RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'off' AND NEW.status <> 'off'
       AND COALESCE(current_setting('genius_next.admin_override', true), 'false') <> 'true' THEN
        RAISE EXCEPTION 'IRREVERSIBLE_STATUS: off->% blocked for phone % (tenant %)',
            NEW.status, OLD.phone, OLD.tenant_id
            USING HINT = 'Use set_config(''genius_next.admin_override'', ''true'') for an audited manual override.';
    END IF;

    IF OLD.status IN ('win', 'loss') AND NEW.status = 'new'
       AND COALESCE(current_setting('genius_next.admin_override', true), 'false') <> 'true' THEN
        RAISE EXCEPTION 'TERMINAL_REGRESSION: %->new blocked for phone % (tenant %)',
            OLD.status, OLD.phone, OLD.tenant_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clients_status_transition_guard
    BEFORE UPDATE OF status ON genius_next.clients
    FOR EACH ROW
    EXECUTE FUNCTION genius_next.guard_client_status_transition();

COMMENT ON FUNCTION genius_next.guard_client_status_transition() IS
    'Enforces status transition rules at the storage layer: off is irreversible, win/loss cannot regress to new, unless genius_next.admin_override session var is set in the same transaction (manual break-glass only).';
