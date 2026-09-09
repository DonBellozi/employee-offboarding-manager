from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import HeaderParser
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog
from app.models_zimbra_recall import ZimbraMailRecallBatch, ZimbraMailRecallRun
from app.services.zimbra import ZimbraService
from app.services.zimbra_mail_cleanup import (
    ZimbraMailCleanupService,
    normalize_email,
    utcnow,
)


ACTIVE_STATUSES = {"queued", "running", "stopping"}
BATCH_LIMIT = 50
CANDIDATE_LIMIT = 100
DELETE_PASSES = 3
ALLOWED_WINDOWS = {15, 30, 60, 120}


class RecallCancelled(Exception):
    pass


@dataclass(frozen=True)
class RecallTarget:
    run_id: int
    message_id: str
    author_mailbox: str


@dataclass(frozen=True)
class RecallCandidate:
    local_id: str
    message_id: str
    subject: str
    recipient: str
    sent_at: datetime | None

    def as_dict(self, timezone_name: str) -> dict[str, object]:
        local_time = None
        if self.sent_at is not None:
            local_time = self.sent_at.astimezone(ZoneInfo(timezone_name))
        return {
            "local_id": self.local_id,
            "message_id": self.message_id,
            "subject": self.subject,
            "recipient": self.recipient,
            "sent_at": self.sent_at.isoformat() if self.sent_at else "",
            "sent_at_label": (
                local_time.strftime("%d.%m.%Y %H:%M:%S")
                if local_time is not None
                else "Время не распознано"
            ),
        }


@dataclass(frozen=True)
class RecallMailboxResult:
    mailbox: str
    found: int
    deleted: int
    remaining: int
    sent_excluded: bool
    duration_ms: int
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "mailbox": self.mailbox,
            "found": self.found,
            "deleted": self.deleted,
            "remaining": self.remaining,
            "sent_excluded": self.sent_excluded,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class ZimbraMailRecallService:
    """Срочно удалить копии одного письма, сохранив Sent автора."""

    _run_lock = threading.Lock()
    _events_lock = threading.Lock()
    _cancel_events: dict[int, threading.Event] = {}

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def _allowed_domains(self) -> set[str]:
        return {
            str(value or "").strip().lower()
            for value in self.settings.zimbra_domains
            if str(value or "").strip()
        }

    @staticmethod
    def _parse_time(value: str) -> str:
        raw = str(value or "").strip()
        try:
            parsed = datetime.strptime(raw, "%H:%M").time()
        except ValueError as exc:
            raise ValueError("Укажите примерное время в формате ЧЧ:ММ") from exc
        return parsed.strftime("%H:%M")

    @staticmethod
    def normalize_message_id(value: str) -> str:
        raw = str(value or "").strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1].strip()
        if (
            not raw
            or len(raw) > 998
            or raw.count("@") != 1
            or raw.startswith("@") or raw.endswith("@")
            or "<" in raw or ">" in raw
            or any(character.isspace() for character in raw)
            or any(character in raw for character in ('"', "'", "\\"))
            or any(ord(character) < 32 for character in raw)
        ):
            raise ValueError("Некорректный Message-ID письма")
        return raw

    @staticmethod
    def _safe_search_text(value: str) -> str:
        cleaned = re.sub(r"[\x00-\x1f\"\\]+", " ", str(value or ""))
        return " ".join(cleaned.split())

    @classmethod
    def build_message_query(
        cls,
        message_id: str,
        *,
        preserve_sent: bool,
    ) -> str:
        normalized = cls.normalize_message_id(message_id)
        sent_filter = " -in:sent" if preserve_sent else ""
        return f'is:anywhere{sent_filter} msgid:"{normalized}"'

    @classmethod
    def build_source_query(
        cls,
        *,
        sender_email: str,
        recipient_email: str,
        sent_date: date,
        subject_hint: str,
    ) -> str:
        # Поиск разбирает mailboxd; LC_ALL SSH-сеанса не задаёт ему локаль.
        # Штатный формат абсолютных дат для zmmailbox: MM/DD/YYYY.
        # Календарную дату и время затем проверяет _candidate_matches_time
        # в часовом поясе приложения.
        previous_day = (sent_date - timedelta(days=1)).strftime("%m/%d/%Y")
        next_day = (sent_date + timedelta(days=1)).strftime("%m/%d/%Y")
        parts = [
            "in:sent",
            f'from:"{sender_email}"',
            f'tocc:"{recipient_email}"',
            f"after:{previous_day}",
            f"before:{next_day}",
        ]
        safe_subject = cls._safe_search_text(subject_hint)
        if safe_subject:
            parts.append(f'subject:"{safe_subject}"')
        return " ".join(parts)

    def prepare_run(
        self,
        *,
        sender_email: str,
        recipient_email: str,
        sent_date: date | None,
        approximate_time: str,
        time_window_minutes: int,
        subject_hint: str,
        message_id: str,
        actor: str,
        lookup_mailbox: str = "",
        draft: bool = False,
    ) -> ZimbraMailRecallRun:
        if self.settings.dry_run:
            raise RuntimeError(
                "Глобальный DRY_RUN запрещает удаление сообщений"
            )
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")

        sender = normalize_email(sender_email, field_name="адрес автора")
        sender_domain = sender.rsplit("@", 1)[1]
        if sender_domain not in self._allowed_domains():
            raise ValueError(
                "Автор должен находиться в одном из настроенных доменов Zimbra"
            )
        normalized_message_id = (
            self.normalize_message_id(message_id) if message_id.strip() else ""
        )
        if normalized_message_id:
            recipient, normalized_time, window = "", "", 30
            lookup_mailbox = ""
        else:
            recipient = normalize_email(recipient_email, field_name="адрес получателя или группы")
            if not isinstance(sent_date, date):
                raise ValueError("Укажите дату отправки")
            normalized_time = self._parse_time(approximate_time)
            window = int(time_window_minutes)
            if window not in ALLOWED_WINDOWS:
                raise ValueError("Выберите допустимый интервал времени")
            if lookup_mailbox.strip():
                lookup_mailbox = normalize_email(lookup_mailbox, field_name="ящик получателя")
                if lookup_mailbox.rsplit("@", 1)[1] not in self._allowed_domains():
                    raise ValueError("Ящик получателя должен быть в настроенном домене Zimbra")
        subject = str(subject_hint or "").strip()[:512]
        if normalized_message_id and self.db.scalar(select(ZimbraMailRecallRun.id).where(
            ZimbraMailRecallRun.message_id == normalized_message_id,
            ZimbraMailRecallRun.status.in_({"draft", "needs_selection", *ACTIVE_STATUSES}),
        ).limit(1)):
            raise ValueError("Этот Message-ID уже есть в пакете или выполняющемся запросе")

        run = ZimbraMailRecallRun(
            status="draft" if draft else "queued",
            initiated_by=str(actor or "")[:256],
            lookup_mailbox=lookup_mailbox.strip(),
            sender_email=sender,
            recipient_email=recipient,
            subject_hint=subject,
            # Legacy column is NOT NULL in deployed databases. For direct ID
            # this is only a storage default: never a search filter or UI date.
            sent_date=sent_date if not normalized_message_id else utcnow().date(),
            approximate_time=normalized_time,
            time_window_minutes=window,
            message_id=normalized_message_id,
            source_search_query=(
                ""
                if normalized_message_id
                else self.build_source_query(
                    sender_email=sender,
                    recipient_email=recipient,
                    sent_date=sent_date,
                    subject_hint=subject,
                )
            ),
            progress_at=utcnow(),
        )
        self.db.add(run)
        self.db.flush()
        self._audit(
            actor,
            "zimbra_mail_recall_queued",
            f"recall:{run.id}",
            {
                "sender_email": sender,
                "recipient_email": recipient,
                "sent_date": sent_date.isoformat() if not normalized_message_id else None,
                "approximate_time": normalized_time,
                "time_window_minutes": window,
                "direct_message_id": bool(normalized_message_id),
            },
        )
        self.db.commit()
        self.db.refresh(run)
        return run

    def select_candidate(
        self,
        run_id: int,
        candidate_index: int,
        *,
        actor: str,
    ) -> ZimbraMailRecallRun:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Запуск отзыва не найден")
        if run.status != "needs_selection":
            raise ValueError("Выбор исходного письма уже недоступен")
        candidates = self.candidates(run)
        if candidate_index < 0 or candidate_index >= len(candidates):
            raise ValueError("Выбранное письмо не найдено")

        candidate = candidates[candidate_index]
        run.message_id = self.normalize_message_id(
            str(candidate.get("message_id") or "")
        )
        run.source_local_id = str(candidate.get("local_id") or "")[:64]
        run.source_subject = str(candidate.get("subject") or "")[:998]
        sent_at = str(candidate.get("sent_at") or "")
        run.source_sent_at = (
            datetime.fromisoformat(sent_at) if sent_at else None
        )
        run.status = "queued"
        run.batch_id = 0
        run.error_message = ""
        run.completed_at = None
        run.progress_at = utcnow()
        self._audit(
            actor,
            "zimbra_mail_recall_candidate_selected",
            f"recall:{run.id}",
            {
                "candidate_index": candidate_index,
                "message_id": run.message_id,
            },
        )
        self.db.commit()
        self.db.refresh(run)
        return run

    @staticmethod
    def _header_values(output: str) -> dict[str, list[str]]:
        # Only the RFC822 header block, not lookalike lines in the body.
        block = re.split(r"\r?\n\r?\n", str(output or ""), maxsplit=1)[0]
        if len(block) > 262144:
            raise ValueError("Слишком большой блок заголовков письма")
        message = HeaderParser().parsestr(block, headersonly=True)
        values: dict[str, list[str]] = {}
        for name, value in message.items():
            values.setdefault(name.lower(), []).append(re.sub(r"\r?\n[ \t]+", " ", value).strip())
        return values

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @staticmethod
    def _parse_message_datetime(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is None:
            for fmt in (
                "%d.%m.%Y %H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%m/%d/%y %H:%M",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def parse_candidate(
        self,
        local_id: str,
        output: str,
    ) -> RecallCandidate:
        headers = self._header_values(output)
        if len(headers.get("message-id", [])) != 1:
            raise ValueError("Исходное письмо не содержит единственный заголовок Message-ID")

        def first(*names: str) -> str:
            for name in names:
                for value in headers.get(name.lower(), []):
                    if value:
                        return value
            return ""

        message_id = self.normalize_message_id(
            first("message-id")
        )
        return RecallCandidate(
            local_id=str(local_id),
            message_id=message_id,
            subject=self._decode_header(first("subject"))[:998],
            recipient=self._decode_header(first("to"))[:998],
            sent_at=self._parse_message_datetime(first("date")),
        )

    def _candidate_matches_time(
        self,
        run: ZimbraMailRecallRun,
        candidate: RecallCandidate,
    ) -> bool:
        if candidate.sent_at is None:
            raise ValueError("Не удалось проверить дату и время найденного письма")
        local = candidate.sent_at.astimezone(
            ZoneInfo(self.settings.app_timezone)
        )
        if local.date() != run.sent_date:
            return False
        requested = datetime.combine(
            run.sent_date,
            dt_time.fromisoformat(run.approximate_time),
            tzinfo=ZoneInfo(self.settings.app_timezone),
        )
        return abs((local - requested).total_seconds()) <= (
            int(run.time_window_minutes) * 60
        )

    def _find_source_candidates(
        self,
        run: ZimbraMailRecallRun,
        zimbra: ZimbraService,
        cancel: threading.Event | None = None,
    ) -> list[RecallCandidate]:
        # Пересобираем также запросы, поставленные в очередь старой версией.
        run.source_search_query = self.build_source_query(
            sender_email=run.sender_email,
            recipient_email=run.recipient_email,
            sent_date=run.sent_date,
            subject_hint=run.subject_hint,
        )
        if run.lookup_mailbox:
            run.source_search_query = run.source_search_query.replace("in:sent", "is:anywhere -in:sent", 1)
        self.db.commit()
        client = zimbra._client()
        try:
            output = zimbra.execute_mailbox_command(
                client,
                run.lookup_mailbox or run.author_mailbox,
                [
                    "search",
                    "-t",
                    "message",
                    "-l",
                    str(CANDIDATE_LIMIT),
                    run.source_search_query,
                ],
                timeout=180,
            )
            batch = ZimbraMailCleanupService.parse_search_output(output)
            if batch.more:
                raise RuntimeError(
                    "Найдено более 100 исходных писем. Уточните тему, дату "
                    "или укажите Message-ID"
                )
            candidates: list[RecallCandidate] = []
            by_message_id: set[str] = set()
            errors: list[str] = []
            for local_id in batch.message_ids:
                if cancel is not None and cancel.is_set():
                    raise RecallCancelled()
                try:
                    candidate = self._read_candidate(
                        zimbra, client, run.lookup_mailbox or run.author_mailbox, local_id,
                    )
                    if not self._candidate_matches_time(run, candidate):
                        continue
                    if candidate.message_id in by_message_id:
                        continue
                    by_message_id.add(candidate.message_id)
                    candidates.append(candidate)
                except Exception as exc:
                    errors.append(f"{local_id}: {str(exc)[:500]}")
            # An unreadable second hit must not make the first hit "unique".
            if errors:
                raise RuntimeError(
                    "Не удалось прочитать служебные заголовки найденных "
                    f"писем: {'; '.join(errors[:3])}"
                )
            return candidates
        finally:
            client.close()

    def _read_candidate(self, zimbra, client, mailbox: str, local_id: str) -> RecallCandidate:
        if not re.fullmatch(r"[1-9][0-9]*", str(local_id)):
            raise ValueError("Некорректный внутренний номер письма")
        # getMessage (including -v) is a presentation/metadata command.
        # REST Get Item with no fmt returns original MIME RFC822 headers.
        # https://wiki.zimbra.com/wiki/Zimbra_REST_API_Reference:Get_Item
        raw = zimbra.execute_mailbox_command(
            client, mailbox, ["getRestURL", f"/?id={local_id}"], timeout=180,
        )
        return self.parse_candidate(local_id, raw)

    def execute_run(self, run_id: int) -> ZimbraMailRecallRun:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Запрос отзыва не найден")
        if run.status != "queued":
            raise ValueError("Запрос уже обработан")
        if not run.batch_id:
            self.queue_batch([run.id], actor=run.initiated_by, allowed_status="queued")
        self.execute_queue()
        self.db.refresh(run)
        return run

    def execute_queue(self) -> None:
        from app.services.zimbra_recall_batch import execute_queue
        execute_queue(self)

    def queue_batch(self, run_ids: list[int], *, actor: str,
                    allowed_status: str = "draft") -> ZimbraMailRecallBatch:
        ids = sorted(set(int(value) for value in run_ids))
        if not ids or len(ids) > BATCH_LIMIT:
            raise ValueError(f"Выберите от 1 до {BATCH_LIMIT} запросов")
        batch = ZimbraMailRecallBatch(initiated_by=actor)
        self.db.add(batch)
        self.db.flush()
        claimed = self.db.execute(update(ZimbraMailRecallRun).where(
            ZimbraMailRecallRun.id.in_(ids),
            ZimbraMailRecallRun.status == allowed_status,
            ZimbraMailRecallRun.batch_id == 0,
        ).values(status="queued", batch_id=batch.id))
        if claimed.rowcount != len(ids):
            self.db.rollback()
            raise ValueError("Часть запросов уже запущена или отменена. Обновите страницу")
        self._audit(actor, "zimbra_mail_recall_batch_queued", f"recall-batch:{batch.id}",
                    {"run_ids": ids})
        self.db.commit()
        return batch

    def draft_runs(self) -> list[ZimbraMailRecallRun]:
        return list(self.db.scalars(select(ZimbraMailRecallRun).where(
            ZimbraMailRecallRun.status == "draft",
        ).order_by(ZimbraMailRecallRun.id)))

    def batch_runs(self, batch_id: int) -> list[ZimbraMailRecallRun]:
        return list(self.db.scalars(select(ZimbraMailRecallRun).where(
            ZimbraMailRecallRun.batch_id == batch_id,
        ).order_by(ZimbraMailRecallRun.id)))

    def batches(self, *, active_only: bool = False) -> list[ZimbraMailRecallBatch]:
        query = select(ZimbraMailRecallBatch)
        if active_only:
            query = query.where(ZimbraMailRecallBatch.status.in_(ACTIVE_STATUSES))
        return list(self.db.scalars(query.order_by(desc(ZimbraMailRecallBatch.id)).limit(30)))

    def cancel_batch(self, batch_id: int, *, actor: str) -> None:
        batch = self.db.get(ZimbraMailRecallBatch, batch_id)
        if batch is None:
            raise ValueError("Пакет не найден")
        # CAS prevents a late click from overwriting a completed result.
        cancelled = self.db.execute(update(ZimbraMailRecallBatch).where(
            ZimbraMailRecallBatch.id == batch_id,
            ZimbraMailRecallBatch.status == "queued",
        ).values(status="cancelled", completed_at=utcnow()))
        if cancelled.rowcount:
            self.db.execute(update(ZimbraMailRecallRun).where(
                ZimbraMailRecallRun.batch_id == batch_id,
                ZimbraMailRecallRun.status == "queued",
            ).values(status="cancelled", completed_at=utcnow()))
        else:
            self.db.execute(update(ZimbraMailRecallBatch).where(
                ZimbraMailRecallBatch.id == batch_id,
                ZimbraMailRecallBatch.status == "running",
            ).values(status="stopping"))
        self._audit(actor, "zimbra_mail_recall_cancel_requested", f"recall-batch:{batch_id}", {})
        self.db.commit()
        with self._events_lock:
            event = self._cancel_events.get(batch_id)
            if event is not None:
                event.set()

    def cancel_draft(self, run_id: int, *, actor: str) -> None:
        changed = self.db.execute(update(ZimbraMailRecallRun).where(
            ZimbraMailRecallRun.id == run_id,
            ZimbraMailRecallRun.status.in_({"draft", "needs_selection"}),
        ).values(status="cancelled", completed_at=utcnow()))
        if not changed.rowcount:
            raise ValueError("Запрос уже выполняется; прервите его пакет")
        self._audit(actor, "zimbra_mail_recall_request_cancelled", f"recall:{run_id}", {})
        self.db.commit()

    def _fail(
        self,
        run: ZimbraMailRecallRun,
        error: Exception,
        *,
        started: float,
    ) -> ZimbraMailRecallRun:
        run.status = "failed"
        run.error_count = max(1, int(run.error_count or 0))
        run.error_message = str(error)[:4000]
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.completed_at = utcnow()
        run.progress_at = run.completed_at
        self._audit(
            run.initiated_by,
            "zimbra_mail_recall_failed",
            f"recall:{run.id}",
            {"error": run.error_message},
        )
        self.db.commit()
        self.db.refresh(run)
        return run

    def recover_interrupted_runs(self) -> int:
        self.db.execute(update(ZimbraMailRecallBatch).where(
            ZimbraMailRecallBatch.status.in_(ACTIVE_STATUSES),
        ).values(status="failed", completed_at=utcnow(), error_message=(
            "Пакет прерван перезапуском приложения. Автоматического повторного удаления не будет"
        )))
        runs = list(
            self.db.scalars(
                select(ZimbraMailRecallRun).where(
                    ZimbraMailRecallRun.status.in_(ACTIVE_STATUSES)
                )
            ).all()
        )
        now = utcnow()
        for run in runs:
            run.status = "failed"
            run.error_count = max(1, int(run.error_count or 0))
            run.error_message = (
                "Запуск был прерван перезапуском приложения. "
                "Повторите отзыв: уже удалённые копии повторно не появятся"
            )
            run.completed_at = now
            run.progress_at = now
        self.db.commit()
        return len(runs)

    def active_run(self) -> ZimbraMailRecallRun | None:
        return self.db.scalars(
            select(ZimbraMailRecallRun)
            .where(ZimbraMailRecallRun.status.in_(ACTIVE_STATUSES))
            .order_by(
                desc(ZimbraMailRecallRun.created_at),
                desc(ZimbraMailRecallRun.id),
            )
            .limit(1)
        ).first()

    def get_run(self, run_id: int) -> ZimbraMailRecallRun | None:
        return self.db.get(ZimbraMailRecallRun, int(run_id))

    def recent_runs(self, *, limit: int = 30) -> list[ZimbraMailRecallRun]:
        return list(
            self.db.scalars(
                select(ZimbraMailRecallRun)
                .order_by(
                    desc(ZimbraMailRecallRun.created_at),
                    desc(ZimbraMailRecallRun.id),
                )
                .limit(max(1, int(limit)))
            ).all()
        )

    @staticmethod
    def _json_list(value: str) -> list[dict[str, object]]:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    @classmethod
    def candidates(
        cls,
        run: ZimbraMailRecallRun | None,
    ) -> list[dict[str, object]]:
        return cls._json_list(run.candidates_json) if run else []

    @classmethod
    def details(
        cls,
        run: ZimbraMailRecallRun | None,
    ) -> list[dict[str, object]]:
        return cls._json_list(run.details_json) if run else []

    def _audit(
        self,
        actor: str,
        action: str,
        target: str,
        details: dict[str, object],
    ) -> None:
        self.db.add(
            AuditLog(
                actor=str(actor or "")[:256],
                action=action,
                target=target,
                result="success",
                details=json.dumps(
                    details,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
