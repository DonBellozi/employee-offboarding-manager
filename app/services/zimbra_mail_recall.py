from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog
from app.models_zimbra_recall import ZimbraMailRecallRun
from app.services.zimbra import ZimbraService
from app.services.zimbra_mail_cleanup import (
    SEARCH_LIMIT,
    ZimbraMailCleanupService,
    normalize_email,
    utcnow,
)


ACTIVE_STATUSES = {"queued", "running"}
CANDIDATE_LIMIT = 100
DELETE_PASSES = 3
ALLOWED_WINDOWS = {15, 30, 60, 120}
HEADER_VALUE_RE = re.compile(
    r"(?im)^[ \t]*(?P<name>[A-Za-z][A-Za-z0-9-]*):[ \t]*(?P<value>[^\r\n]*)"
)


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
            or "@" not in raw
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
        sent_date: date,
        approximate_time: str,
        time_window_minutes: int,
        subject_hint: str,
        message_id: str,
        actor: str,
    ) -> ZimbraMailRecallRun:
        if self.settings.dry_run:
            raise RuntimeError(
                "Глобальный DRY_RUN запрещает удаление сообщений"
            )
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")

        sender = normalize_email(sender_email, field_name="адрес автора")
        recipient = normalize_email(
            recipient_email,
            field_name="адрес получателя или группы",
        )
        sender_domain = sender.rsplit("@", 1)[1]
        if sender_domain not in self._allowed_domains():
            raise ValueError(
                "Автор должен находиться в одном из настроенных доменов Zimbra"
            )
        normalized_time = self._parse_time(approximate_time)
        window = int(time_window_minutes)
        if window not in ALLOWED_WINDOWS:
            raise ValueError("Выберите допустимый интервал времени")
        normalized_message_id = (
            self.normalize_message_id(message_id) if message_id.strip() else ""
        )
        subject = str(subject_hint or "").strip()[:512]
        if self.active_run() is not None:
            raise RuntimeError("Другой отзыв письма уже выполняется")

        run = ZimbraMailRecallRun(
            status="queued",
            initiated_by=str(actor or "")[:256],
            sender_email=sender,
            recipient_email=recipient,
            subject_hint=subject,
            sent_date=sent_date,
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
                "sent_date": sent_date.isoformat(),
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
        if self.active_run() is not None:
            raise RuntimeError("Другой отзыв письма уже выполняется")

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
        values: dict[str, list[str]] = {}
        for match in HEADER_VALUE_RE.finditer(str(output or "")):
            values.setdefault(match.group("name").lower(), []).append(
                match.group("value").strip()
            )
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

        def first(*names: str) -> str:
            for name in names:
                for value in headers.get(name.lower(), []):
                    if value:
                        return value
            return ""

        message_id = self.normalize_message_id(
            first("message-id", "messageid")
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
            # В старых версиях zmmailbox дата в verbose-ответе может быть
            # локализована необычным образом. Такой вариант нельзя молча
            # отбросить: оператор увидит его при неоднозначности.
            return True
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
    ) -> list[RecallCandidate]:
        # Пересобираем также запросы, поставленные в очередь старой версией.
        run.source_search_query = self.build_source_query(
            sender_email=run.sender_email,
            recipient_email=run.recipient_email,
            sent_date=run.sent_date,
            subject_hint=run.subject_hint,
        )
        self.db.commit()
        client = zimbra._client()
        try:
            output = zimbra.execute_mailbox_command(
                client,
                run.author_mailbox,
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
                try:
                    verbose = zimbra.execute_mailbox_command(
                        client,
                        run.author_mailbox,
                        ["getMessage", "-v", local_id],
                        timeout=180,
                    )
                    try:
                        candidate = self.parse_candidate(local_id, verbose)
                    except ValueError:
                        # Некоторые выпуски Zimbra в verbose-режиме выводят
                        # только служебные метаданные без MIME Message-ID.
                        # Обычный getMessage возвращает исходное письмо; оно
                        # используется в памяти только для чтения заголовков.
                        raw_message = zimbra.execute_mailbox_command(
                            client,
                            run.author_mailbox,
                            ["getMessage", local_id],
                            timeout=180,
                        )
                        candidate = self.parse_candidate(
                            local_id,
                            raw_message,
                        )
                    if not self._candidate_matches_time(run, candidate):
                        continue
                    if candidate.message_id in by_message_id:
                        continue
                    by_message_id.add(candidate.message_id)
                    candidates.append(candidate)
                except Exception as exc:
                    errors.append(f"{local_id}: {str(exc)[:500]}")
            if not candidates and errors:
                raise RuntimeError(
                    "Не удалось прочитать служебные заголовки найденных "
                    f"писем: {'; '.join(errors[:3])}"
                )
            return candidates
        finally:
            client.close()

    def _process_mailbox(
        self,
        zimbra: ZimbraService,
        mailbox: str,
        *,
        author_mailbox: str,
        message_id: str,
    ) -> RecallMailboxResult:
        started = time.monotonic()
        preserve_sent = mailbox == author_mailbox
        query = self.build_message_query(
            message_id,
            preserve_sent=preserve_sent,
        )
        found_ids: set[str] = set()
        deleted_ids: set[str] = set()
        remaining = 0
        verification_complete = False
        error = ""
        try:
            client = zimbra._client()
        except Exception as exc:
            return RecallMailboxResult(
                mailbox=mailbox,
                found=0,
                deleted=0,
                remaining=0,
                sent_excluded=preserve_sent,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=str(exc)[:2000],
            )

        try:
            for _ in range(DELETE_PASSES):
                output = zimbra.execute_mailbox_command(
                    client,
                    mailbox,
                    [
                        "search",
                        "-t",
                        "message",
                        "-l",
                        str(SEARCH_LIMIT),
                        query,
                    ],
                    timeout=180,
                )
                batch = ZimbraMailCleanupService.parse_search_output(output)
                current_ids = set(batch.message_ids)
                found_ids.update(current_ids)
                if not current_ids:
                    remaining = 0
                    break
                zimbra.execute_mailbox_command(
                    client,
                    mailbox,
                    ["deleteMessage", ",".join(batch.message_ids)],
                    timeout=180,
                    mutating=True,
                )
                deleted_ids.update(current_ids)
            if found_ids:
                verification_output = zimbra.execute_mailbox_command(
                    client,
                    mailbox,
                    [
                        "search",
                        "-t",
                        "message",
                        "-l",
                        str(SEARCH_LIMIT),
                        query,
                    ],
                    timeout=180,
                )
                verification = ZimbraMailCleanupService.parse_search_output(
                    verification_output
                )
                remaining = len(verification.message_ids)
                if verification.more:
                    remaining = max(remaining, SEARCH_LIMIT + 1)
                verification_complete = True
        except Exception as exc:
            error = str(exc)[:2000]
        finally:
            client.close()

        return RecallMailboxResult(
            mailbox=mailbox,
            found=len(found_ids),
            deleted=(
                max(0, len(deleted_ids) - remaining)
                if verification_complete
                else 0
            ),
            remaining=remaining,
            sent_excluded=preserve_sent,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )

    def execute_run(self, run_id: int) -> ZimbraMailRecallRun:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Запуск отзыва не найден")
        if run.status != "queued":
            raise ValueError("Запуск уже обработан")
        if not self._run_lock.acquire(blocking=False):
            return self._fail(
                run,
                RuntimeError("Другой отзыв письма уже выполняется"),
                started=time.monotonic(),
            )

        started = time.monotonic()
        try:
            run.status = "running"
            run.started_at = run.started_at or utcnow()
            run.progress_at = utcnow()
            self.db.commit()

            zimbra = ZimbraService(self.settings)
            author = zimbra.administrative_account_by_address(
                run.sender_email
            )
            if author is None:
                raise RuntimeError(
                    "Не удалось однозначно определить основной ящик автора. "
                    "Удаление не начато, чтобы сохранить папку «Отправленные»"
                )
            run.author_mailbox = author.primary_email
            self.db.commit()

            if not run.message_id:
                candidates = self._find_source_candidates(run, zimbra)
                candidate_rows = [
                    candidate.as_dict(self.settings.app_timezone)
                    for candidate in candidates
                ]
                run.candidates_json = json.dumps(
                    candidate_rows,
                    ensure_ascii=False,
                )
                if not candidates:
                    raise RuntimeError(
                        "В «Отправленных» автора не найдено письмо в указанную "
                        "дату и время. Проверьте получателя, тему и интервал"
                    )
                if len(candidates) > 1:
                    run.status = "needs_selection"
                    run.error_message = (
                        "Найдено несколько разных писем. Выберите нужное — "
                        "после выбора удаление начнётся сразу"
                    )
                    run.duration_ms = int(
                        (time.monotonic() - started) * 1000
                    )
                    run.progress_at = utcnow()
                    self._audit(
                        run.initiated_by,
                        "zimbra_mail_recall_needs_selection",
                        f"recall:{run.id}",
                        {"candidate_count": len(candidates)},
                    )
                    self.db.commit()
                    self.db.refresh(run)
                    return run
                selected = candidates[0]
                run.message_id = selected.message_id
                run.source_local_id = selected.local_id
                run.source_subject = selected.subject
                run.source_sent_at = selected.sent_at
                self.db.commit()

            with zimbra._query_lock:
                mailboxes = zimbra.list_user_mailboxes()
            if run.author_mailbox not in mailboxes:
                raise RuntimeError(
                    "Основной ящик автора отсутствует в безопасном списке "
                    "пользовательских ящиков. Удаление не начато"
                )

            run.total_mailboxes = len(mailboxes)
            run.processed_mailboxes = 0
            run.progress_at = utcnow()
            self.db.commit()

            results: list[RecallMailboxResult] = []
            workers = max(
                1,
                min(
                    int(self.settings.zimbra_mail_cleanup_workers),
                    len(mailboxes) or 1,
                ),
            )
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="zimbra-mail-recall",
            ) as pool:
                futures = [
                    pool.submit(
                        self._process_mailbox,
                        zimbra,
                        mailbox,
                        author_mailbox=run.author_mailbox,
                        message_id=run.message_id,
                    )
                    for mailbox in mailboxes
                ]
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    run.processed_mailboxes += 1
                    run.matched_mailboxes += int(result.found > 0)
                    run.found_messages += result.found
                    run.deleted_messages += result.deleted
                    run.remaining_messages += result.remaining
                    run.error_count += int(bool(result.error))
                    run.duration_ms = int(
                        (time.monotonic() - started) * 1000
                    )
                    run.progress_at = utcnow()
                    if result.found or result.remaining or result.error:
                        run.details_json = json.dumps(
                            [
                                item.as_dict()
                                for item in sorted(
                                    results,
                                    key=lambda item: item.mailbox,
                                )
                                if item.found or item.remaining or item.error
                            ],
                            ensure_ascii=False,
                        )
                    self.db.commit()

            run.completed_at = utcnow()
            run.progress_at = run.completed_at
            run.duration_ms = int((time.monotonic() - started) * 1000)
            if run.error_count or run.remaining_messages:
                run.status = "warning" if run.deleted_messages else "failed"
                run.error_message = (
                    "Не все найденные копии удалось удалить. "
                    "Откройте результат и проверьте ошибки"
                )
            elif run.found_messages == 0:
                run.status = "warning"
                run.error_message = (
                    "Исходное письмо найдено, но его копий в пользовательских "
                    "ящиках уже нет"
                )
            else:
                run.status = "success"
                run.error_message = ""
            self._audit(
                run.initiated_by,
                "zimbra_mail_recall_completed",
                f"recall:{run.id}",
                {
                    "status": run.status,
                    "author_mailbox": run.author_mailbox,
                    "checked_mailboxes": run.processed_mailboxes,
                    "matched_mailboxes": run.matched_mailboxes,
                    "found_messages": run.found_messages,
                    "deleted_messages": run.deleted_messages,
                    "remaining_messages": run.remaining_messages,
                    "error_count": run.error_count,
                },
            )
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            run = self.get_run(run_id)
            return self._fail(run, exc, started=started)
        finally:
            self._run_lock.release()

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
        if runs:
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
