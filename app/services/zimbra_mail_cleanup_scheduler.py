from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.config import Settings
from app.services.zimbra_mail_cleanup import ZimbraMailCleanupService


logger = logging.getLogger(__name__)


class ZimbraMailCleanupScheduler:
    """Независимо запускает недельные политики хранения почты."""

    POLL_SECONDS = 30

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="zimbra-mail-cleanup",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        with self.session_factory() as db:
            service = ZimbraMailCleanupService(self.settings, db)
            decision = service.weekly_schedule_decision(now=now)
            if not decision.due:
                service.update_scheduler_state(
                    status="waiting" if decision.enabled else "manual",
                    message=decision.reason,
                    next_run_at=decision.next_run_at,
                    checked_at=decision.current,
                )
                return 0

            service.update_scheduler_state(
                status="running",
                message=decision.reason,
                next_run_at=decision.due_at,
                checked_at=decision.current,
            )
            try:
                runs = service.scheduled_cleanup(
                    actor="system",
                    now=decision.current,
                )
            except Exception as exc:
                db.rollback()
                service.update_scheduler_state(
                    status="error",
                    message=f"Автозапуск не выполнен: {str(exc)[:1800]}",
                    next_run_at=decision.due_at,
                )
                raise

            failed = sum(row.status == "failed" for row in runs)
            skipped = all(row.status == "skipped" for row in runs)
            next_decision = service.weekly_schedule_decision(
                now=decision.current,
            )
            if skipped:
                message = runs[0].error_message
            else:
                message = (
                    f"Недельный запуск завершён: правил {len(runs)}, "
                    f"удалено {sum(row.deleted_messages for row in runs)}, "
                    f"ошибок {sum(row.error_count for row in runs)}"
                )
            service.update_scheduler_state(
                status="error" if failed else "completed",
                message=message,
                next_run_at=next_decision.next_run_at,
            )
            logger.info(
                "Недельная очистка почты Zimbra: rules=%s deleted=%s errors=%s",
                len(runs),
                sum(row.deleted_messages for row in runs),
                sum(row.error_count for row in runs),
            )
            return len(runs)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Фоновая очистка почты Zimbra завершилась ошибкой")
            if self._stop_event.wait(self.POLL_SECONDS):
                break
