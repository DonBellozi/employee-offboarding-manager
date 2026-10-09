from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from sqlalchemy import select

from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.services.account_requirement import AccountRequirementService
from app.services.employee_arrival_accounts import EmployeeArrivalAccountService

logger = logging.getLogger(__name__)


class AccountRequirementWorker:
    """Read-only AD/Zimbra discovery; never creates or restores accounts."""
    def __init__(self, settings, session_factory):
        self.settings, self.session_factory = settings, session_factory
        self._stop_event = threading.Event()
        self._thread = None
        self._checked = {}

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, name="account-requirement", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_once(self):
        with self.session_factory() as db:
            service = AccountRequirementService(db)
            try:
                service.assert_import_idle()
            except ValueError:
                return
            from app.services.account_requirement_pre_registration import PreRegistrationRequirementService
            PreRegistrationRequirementService(db).reconcile_confirmed()
            service.cancel_stale()
            grouped = defaultdict(list)
            for row in db.scalars(select(HREmploymentArrivalEvent).where(
                HREmploymentArrivalEvent.status == "pending", HREmploymentArrivalEvent.ended_at.is_(None),
            ).order_by(HREmploymentArrivalEvent.id)).all():
                grouped[row.worker_key].append(row.id)
            # Old failures cannot starve newer arrivals. Bound external work per tick.
            for key in sorted(grouped, key=lambda key: self._checked.get(key, 0))[:10]:
                if self._stop_event.is_set():
                    return
                if time.monotonic() - self._checked.get(key, -600) < 300:
                    continue
                self._checked[key] = time.monotonic()
                raw_ids = ",".join(map(str, grouped[key]))
                try:
                    state = EmployeeArrivalAccountService(self.settings, db).inspect(raw_ids)
                    db.expire_all()
                    service.observe(raw_ids, state)
                except Exception:
                    db.rollback()
                    # Avoid dumping HR data / directory exception text into logs.
                    logger.warning("Не удалось проверить новый кадровый эпизод; проверка будет повторена")
            self._checked = {key: value for key, value in self._checked.items() if key in grouped}

    def _run_loop(self):
        while not self._stop_event.wait(60):
            try:
                self._run_once()
            except Exception:
                logger.warning("Фоновый сбор решений будет повторен после ошибки")
