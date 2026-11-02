"""Closed, record-only intake for loss candidates entering Genius Rescue.

This module deliberately records an auditable case only.  It imports neither
providers nor the outbox, so recording a candidate can never send a message.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text as _sql_text

from app.db import sql_session, transaction
from app.identity_primitives import stable_id


class RescueIntakeSnapshot(BaseModel):
    """Locked domain facts used to decide one rescue intake."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    handoff_key: str
    candidate_status: str
    lifecycle_state: str
    client_status: str
    automation_state: str
    has_newer_inbound: bool


class RescueIntakeDecision(BaseModel):
    """Closed outcome: record once, block, or replay without side effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["recorded", "blocked", "noop"]
    create_case: bool
    candidate_status: Literal["intake_recorded", "blocked", "queued"]
    delivery_mode: Literal["record_only"] | None = None


def decide_rescue_intake(snapshot: RescueIntakeSnapshot) -> RescueIntakeDecision:
    """Fail closed unless the candidate is still the exact loss handoff."""
    if snapshot.candidate_status in {"intake_recorded", "blocked"}:
        return RescueIntakeDecision(
            status="noop", create_case=False, candidate_status=snapshot.candidate_status
        )
    eligible = (
        snapshot.candidate_status == "queued"
        and snapshot.lifecycle_state == "rescue_queued"
        and snapshot.client_status == "loss"
        and snapshot.automation_state == "open"
        and not snapshot.has_newer_inbound
    )
    if not eligible:
        return RescueIntakeDecision(
            status="blocked", create_case=False, candidate_status="blocked"
        )
    return RescueIntakeDecision(
        status="recorded",
        create_case=True,
        candidate_status="intake_recorded",
        delivery_mode="record_only",
    )


_LOCK_RESCUE_CANDIDATE_SQL = _sql_text(
    """
    /* rescue.intake_lock */
    SELECT c.tenant_id, c.handoff_key, c.status AS candidate_status,
           l.state AS lifecycle_state, cl.status AS client_status,
           cl.automation_state,
           EXISTS (
               SELECT 1
               FROM genius_next.events AS e
               WHERE e.tenant_id = l.tenant_id
                 AND e.phone = l.phone
                 AND e.direction = 'inbound'
                 AND e.conversation_sequence > l.origin_sequence
           ) AS has_newer_inbound
    FROM genius_next.follow_up_candidates AS c
    JOIN genius_next.follow_up_lifecycles AS l
      ON l.tenant_id = c.tenant_id AND l.origin_turn_id = c.origin_turn_id
    JOIN genius_next.clients AS cl
      ON cl.tenant_id = l.tenant_id AND cl.phone = l.phone
    WHERE c.tenant_id = :tenant_id
      AND c.handoff_key = :handoff_key
    FOR UPDATE OF c, l, cl
    """
)

_INSERT_RESCUE_CASE_SQL = _sql_text(
    """
    /* rescue.intake_case */
    INSERT INTO genius_next.rescue_cases
        (tenant_id, handoff_key, status, delivery_mode, audit)
    VALUES (:tenant_id, :handoff_key, 'recorded', 'record_only',
            jsonb_build_object('source', 'followup_v2'))
    ON CONFLICT (tenant_id, handoff_key) DO NOTHING
    """
)

_SET_CANDIDATE_STATUS_SQL = _sql_text(
    """
    /* rescue.intake_candidate_status */
    UPDATE genius_next.follow_up_candidates
    SET status = :status,
        audit = COALESCE(audit, '{}'::jsonb) || jsonb_build_object(
            'rescue_intake', :status,
            'delivery_mode', 'record_only'
        )
    WHERE tenant_id = :tenant_id
      AND handoff_key = :handoff_key
      AND status = 'queued'
    """
)


@transaction(isolation_level="SERIALIZABLE")
def record_rescue_intake_txn(
    tenant_id: str,
    origin_turn_id: str,
) -> RescueIntakeDecision:
    """Lock, revalidate, and record one local Rescue case with no delivery."""
    handoff_key = stable_id("rescue-handoff", tenant_id, origin_turn_id)
    row = sql_session().execute(
        _LOCK_RESCUE_CANDIDATE_SQL,
        {"tenant_id": tenant_id, "handoff_key": handoff_key},
    ).mappings().one_or_none()
    if row is None:
        return RescueIntakeDecision(
            status="noop", create_case=False, candidate_status="queued"
        )
    decision = decide_rescue_intake(RescueIntakeSnapshot.model_validate(dict(row)))
    if decision.status == "noop":
        return decision
    if decision.create_case:
        sql_session().execute(
            _INSERT_RESCUE_CASE_SQL,
            {"tenant_id": tenant_id, "handoff_key": handoff_key},
        )
    sql_session().execute(
        _SET_CANDIDATE_STATUS_SQL,
        {
            "tenant_id": tenant_id,
            "handoff_key": handoff_key,
            "status": decision.candidate_status,
        },
    )
    return decision
