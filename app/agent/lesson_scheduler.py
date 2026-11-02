"""Relogio de 12 h que extrai licoes das conversas (Procrastinate, UTC)."""

from __future__ import annotations

import logging

from app.agent.lesson_extractor import run as extract_lessons_batch
from app.jobs import CRON_QUEUE, jobs

logger = logging.getLogger(__name__)


@jobs.periodic(cron="0 */12 * * *")
@jobs.task(name="lesson_extraction", queue=CRON_QUEUE, retry=3)
def lesson_extraction_scheduled(timestamp: int) -> None:
    logger.info("lesson_extraction_scheduled: starting batch")
    extract_lessons_batch(dry_run=False, tenant_id=None)
    logger.info("lesson_extraction_scheduled: done")
