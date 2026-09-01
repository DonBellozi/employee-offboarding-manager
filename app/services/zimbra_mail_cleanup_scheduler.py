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
            runs = ZimbraMailCleanupService(
                self.settings,
                db,
            ).run_weekly_if_due(now=now)
            if runs:
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
