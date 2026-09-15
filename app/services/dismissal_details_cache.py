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
from app.services.onec_import_recovery import PROCESS_STARTED_AT, running_import


logger = logging.getLogger(__name__)

POLL_SECONDS = 30
REFRESH_SECONDS = 5 * 60
RETRY_SECONDS = 60
SNAPSHOT_VERSION = 1
DELAY_SECONDS = 10 * 60


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

    def active_import(self) -> OneCImportRun | None:
        # Same interlock as mail/lifecycle consumers. Startup recovery repairs
        # the history once, rather than having snapshots ignore it alone.
        return running_import(self.db)

    def enqueue(self, candidate: dict) -> DismissalDetailsSnapshot:
        snapshot = self._snapshot(candidate)
        if snapshot is None:
            snapshot = DismissalDetailsSnapshot(
                worker_key=candidate["worker_key"],
                dismissal_date=candidate["dismissal_date"],
            )
            self.db.add(snapshot)
            self.db.commit()
        return snapshot

    def record_error(self, candidate: dict, error: Exception,
                     attempted_at: datetime | None = None) -> DismissalDetailsSnapshot:
        self.db.rollback()
        snapshot = self.enqueue(candidate)
        snapshot.status = "stale" if self._valid_rows(snapshot.payload_json) else "error"
        snapshot.last_error = str(error)[:2000]
        snapshot.last_attempt_at = attempted_at or utcnow()
        self.db.commit()
        return snapshot

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
        try:
            fingerprint = self.candidate_fingerprint(candidate)
            snapshot = self.enqueue(candidate)
            snapshot.status = "refreshing"
            snapshot.last_attempt_at = attempt_at
            snapshot.last_error = ""
            self.db.commit()
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
            if not rows:
                raise ValueError("Проверка вернула пустой результат. Попытка будет повторена")
            payload_json = json.dumps(
                {"version": SNAPSHOT_VERSION, "rows": rows},
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception as exc:
            logger.warning("Не удалось обновить снимок увольнения %s", candidate.get("worker_key"), exc_info=True)
            return self.record_error(candidate, exc, attempt_at)

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
        snapshot.checked_at = utcnow()
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
        now = utcnow()
        last_activity = _aware_utc(
            snapshot.last_attempt_at or snapshot.created_at
        ) if snapshot else None
        if state in {"pending", "refreshing"} and last_activity and (
            now - last_activity >= timedelta(seconds=DELAY_SECONDS)
        ):
            state = "stale" if rows else "delayed"
            error = "Обновление задержалось более 10 минут. Проверьте журнал приложения"
        checked_at = _aware_utc(snapshot.checked_at) if snapshot else None
        if state == "ready" and checked_at and now - checked_at >= timedelta(seconds=DELAY_SECONDS):
            state = "stale"
            error = "Сохранённые сведения устарели; фоновая проверка задерживается"
        if state != "ready":
            active_import = self.active_import()
            if active_import is not None:
                state = "stale" if rows else "waiting_import"
                error = (
                    f"Ожидаем завершения импорта 1С #{active_import.id} "
                    f"(начат {self._format_datetime(active_import.started_at)}). "
                    "Сведения об учетных записях пока не обновляются"
                )
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
            "snapshot_attempt_at": self._format_datetime(snapshot.last_attempt_at if snapshot else None),
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
        self._stop_event.clear()
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
                cache = DismissalDetailsCacheService(self.settings, db)
                if cache.active_import() is not None:
                    return
                candidates = UpcomingDismissalService(
                    self.settings,
                    db,
                ).list_upcoming(limit=None)
                # Persist pending work before external calls. An exception in
                # one person must not restart the scan at the same person.
                due = []
                for candidate in candidates:
                    if self._stop_event.is_set():
                        return
                    try:
                        snapshot = cache.enqueue(candidate)
                        if cache.needs_refresh(candidate):
                            due.append((
                                bool(cache._valid_rows(snapshot.payload_json)),
                                _aware_utc(snapshot.last_attempt_at) or datetime.min.replace(tzinfo=timezone.utc),
                                candidate,
                            ))
                    except Exception as exc:
                        db.rollback()
                        logger.exception("Не удалось подготовить снимок %s", candidate.get("worker_key"))
                        try:
                            cache.record_error(candidate, exc)
                        except Exception:
                            db.rollback()
                            logger.exception("Не удалось сохранить ошибку снимка")
                refreshed = 0
                for _, _, candidate in sorted(due, key=lambda item: item[:2]):
                    if self._stop_event.is_set():
                        return
                    if cache.active_import() is not None:
                        return
                    try:
                        cache.refresh(candidate)
                        refreshed += 1
                    except Exception as exc:
                        db.rollback()
                        logger.exception("Ошибка сохранения снимка %s; продолжаем остальные", candidate.get("worker_key"))
                        try:
                            cache.record_error(candidate, exc)
                        except Exception:
                            db.rollback()
                            logger.exception("Не удалось сохранить ошибку снимка")
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
