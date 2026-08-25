from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OneCImportRun
from app.models_dismissal_lifecycle import DismissalDetailsSnapshot
from app.services.dismissal_details import DismissalDetailsService
from app.services.upcoming_dismissals import UpcomingDismissalService


logger = logging.getLogger(__name__)

POLL_SECONDS = 30
REFRESH_SECONDS = 5 * 60
RETRY_SECONDS = 60
SNAPSHOT_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DismissalDetailsCacheService:
    """Хранит результаты фоновой проверки и отдаёт их без внешних запросов."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @staticmethod
    def candidate_fingerprint(candidate: dict) -> str:
        organizations = []
        for organization in candidate.get("organizations") or []:
            raw_date = organization.get("dismissal_date")
            organizations.append(
                {
                    "source_id": str(organization.get("source_id") or ""),
                    "source_name": str(organization.get("source_name") or ""),
                    "dismissal_date": (
                        raw_date.isoformat()
                        if isinstance(raw_date, date)
                        else str(raw_date or "")
                    ),
                    "status": str(organization.get("status") or ""),
                    "is_present": bool(organization.get("is_present")),
                    "placements": list(organization.get("placements") or []),
                }
            )
        organizations.sort(
            key=lambda item: (
                item["source_id"],
                item["dismissal_date"],
                item["status"],
            )
        )
        payload = {
            "worker_key": str(candidate.get("worker_key") or ""),
            "fio": str(candidate.get("fio") or ""),
            "login": str(candidate.get("login") or ""),
            "email": str(candidate.get("email") or ""),
            "dismissal_date": candidate["dismissal_date"].isoformat(),
            "effective_block_date": candidate["effective_block_date"].isoformat(),
            "deferred": bool(candidate.get("deferred")),
            "preliminary": bool(candidate.get("preliminary")),
            "organizations": organizations,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _snapshot(self, candidate: dict) -> DismissalDetailsSnapshot | None:
        return self.db.scalar(
            select(DismissalDetailsSnapshot).where(
                DismissalDetailsSnapshot.worker_key == candidate["worker_key"],
                DismissalDetailsSnapshot.dismissal_date
                == candidate["dismissal_date"],
            )
        )

    @staticmethod
    def _retry_due(
        snapshot: DismissalDetailsSnapshot,
        now: datetime,
    ) -> bool:
        last_attempt = _aware_utc(snapshot.last_attempt_at)
        return (
            last_attempt is None
            or now - last_attempt >= timedelta(seconds=RETRY_SECONDS)
        )

    def needs_refresh(
        self,
        candidate: dict,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = _aware_utc(now) or utcnow()
        snapshot = self._snapshot(candidate)
        if snapshot is None:
            return True
        if not self._valid_rows(snapshot.payload_json):
            return self._retry_due(snapshot, now)
        if snapshot.candidate_fingerprint != self.candidate_fingerprint(candidate):
            return self._retry_due(snapshot, now)
        if snapshot.status != "ready":
            return self._retry_due(snapshot, now)
        checked_at = _aware_utc(snapshot.checked_at)
        return (
            checked_at is None
            or now - checked_at >= timedelta(seconds=REFRESH_SECONDS)
        )

    def _format_datetime(self, value: datetime | None) -> str:
        value = _aware_utc(value)
        if value is None:
            return ""
        try:
            zone = ZoneInfo(self.settings.app_timezone)
        except Exception:
            zone = timezone.utc
        return value.astimezone(zone).strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _valid_rows(payload_json: str) -> list[dict[str, str]]:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("version") != SNAPSHOT_VERSION:
            return []
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append(
                {
                    "label": str(row.get("label") or ""),
                    "value": str(row.get("value") or ""),
                    "state": str(row.get("state") or "neutral"),
                    "note": str(row.get("note") or ""),
                }
            )
        return result

    def refresh(self, candidate: dict) -> DismissalDetailsSnapshot:
        attempt_at = utcnow()
        fingerprint = self.candidate_fingerprint(candidate)
        try:
            details = DismissalDetailsService(
                self.settings,
                self.db,
            ).build(candidate)
            rows = self._valid_rows(
                json.dumps(
                    {
                        "version": SNAPSHOT_VERSION,
                        "rows": details.get("rows") or [],
                    },
                    ensure_ascii=False,
                )
            )
            payload_json = json.dumps(
                {"version": SNAPSHOT_VERSION, "rows": rows},
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception as exc:
            self.db.rollback()
            snapshot = self._snapshot(candidate)
            if snapshot is None:
                snapshot = DismissalDetailsSnapshot(
                    worker_key=candidate["worker_key"],
                    dismissal_date=candidate["dismissal_date"],
                    status="error",
                    last_error=str(exc),
                    last_attempt_at=attempt_at,
                )
                self.db.add(snapshot)
            else:
                snapshot.status = (
                    "stale" if self._valid_rows(snapshot.payload_json) else "error"
                )
                snapshot.last_error = str(exc)
                snapshot.last_attempt_at = attempt_at
            self.db.commit()
            self.db.refresh(snapshot)
            return snapshot

        snapshot = self._snapshot(candidate)
        if snapshot is None:
            snapshot = DismissalDetailsSnapshot(
                worker_key=candidate["worker_key"],
                dismissal_date=candidate["dismissal_date"],
            )
            self.db.add(snapshot)
        snapshot.candidate_fingerprint = fingerprint
        snapshot.payload_json = payload_json
        snapshot.status = "ready"
        snapshot.last_error = ""
        snapshot.checked_at = attempt_at
        snapshot.last_attempt_at = attempt_at
        self.db.commit()
        self.db.refresh(snapshot)
        return snapshot

    def view(self, candidate: dict) -> dict:
        """Вернуть только локальные данные; внешние клиенты здесь не вызываются."""
        snapshot = self._snapshot(candidate)
        rows = self._valid_rows(snapshot.payload_json) if snapshot else []
        state = snapshot.status if snapshot is not None else "pending"
        error = str(snapshot.last_error or "").strip() if snapshot else ""
        if (
            snapshot is not None
            and rows
            and snapshot.candidate_fingerprint
            != self.candidate_fingerprint(candidate)
        ):
            state = "stale"
            error = "Кадровые данные изменились. Снимок обновляется в фоне."
        if not rows and state == "stale":
            state = "error"
        if snapshot is not None and not rows and state == "ready":
            state = "error"
            error = "Сохранённый снимок недоступен. Проверка будет повторена."
        return {
            "fio": candidate["fio"],
            "dismissal_date": candidate["dismissal_date"],
            "organizations": candidate["organizations"],
            "preliminary": bool(candidate.get("preliminary")),
            "rows": rows,
            "snapshot_state": state,
            "snapshot_checked_at": self._format_datetime(
                snapshot.checked_at if snapshot else None
            ),
            "snapshot_error": error,
        }


class DismissalDetailsSnapshotWorker:
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
            name="dismissal-details-snapshots",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run_once(self) -> None:
        with self.session_factory() as db:
            try:
                import_running = db.scalar(
                    select(OneCImportRun.id)
                    .where(OneCImportRun.status == "running")
                    .limit(1)
                )
                if import_running:
                    return
                cache = DismissalDetailsCacheService(self.settings, db)
                candidates = UpcomingDismissalService(
                    self.settings,
                    db,
                ).list_upcoming(limit=1000)
                refreshed = 0
                for candidate in candidates:
                    if self._stop_event.is_set():
                        return
                    if not cache.needs_refresh(candidate):
                        continue
                    cache.refresh(candidate)
                    refreshed += 1
                if refreshed:
                    logger.info(
                        "Обновлены фоновые снимки увольнений: %s",
                        refreshed,
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка фоновой проверки подробностей увольнений")

    def _run_loop(self) -> None:
        self._run_once()
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
