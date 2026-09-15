"""Recover abandoned import history at startup, before any workers start.

This is not an age-based bypass of the HR interlock. Live imports continue to
block all consumers. The deployment runs one application/worker process.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, OneCImportRun


logger = logging.getLogger(__name__)
PROCESS_STARTED_AT = datetime.now(timezone.utc)


def running_import(db: Session) -> OneCImportRun | None:
    return db.scalar(select(OneCImportRun).where(
        OneCImportRun.status == "running",
    ).order_by(OneCImportRun.started_at, OneCImportRun.id).limit(1))


def recover_interrupted_imports(db: Session, *, started_before: datetime = PROCESS_STARTED_AT) -> int:
    """Startup only. Never call from a timer or an operator HTTP action."""
    rows = list(db.scalars(select(OneCImportRun).where(
        OneCImportRun.status == "running",
        OneCImportRun.started_at < started_before,
    ).order_by(OneCImportRun.id)))
    now = datetime.now(timezone.utc)
    for run in rows:
        previous_error = str(run.error_message or "")
        run.status = "failed"
        run.completed_at = run.completed_at or now
        run.error_message = (
            "Импорт не завершился до перезапуска приложения. "
            "Его результат не считается успешным. Проверьте следующую принятую выгрузку."
            + (f" Предыдущая ошибка: {previous_error}" if previous_error else "")
        )[:4000]
        db.add(AuditLog(
            actor="system", action="onec_import_interrupted_recovered",
            target=f"onec-import:{run.id}", result="failed",
            details=json.dumps({
                "import_id": run.id, "source_id": run.source_id,
                "started_at": run.started_at.isoformat(),
                "reason": "application_restart", "previous_error": previous_error,
            }, ensure_ascii=False),
        ))
    if rows:
        db.commit()
        logger.warning("При запуске отмечены прерванными незавершенные импорты 1С: %s", [row.id for row in rows])
    return len(rows)
