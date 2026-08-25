from __future__ import annotations

import base64
import email
import hashlib
import html
import imaplib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_notifications import (
    DismissalEquipmentNotice,
    HREmploymentDismissalEvent,
)
from app.models_onec_sources import HREmploymentState, OneCAdditionalSource
from app.models_preliminary_dismissals import (
    PreliminaryDismissalItem,
    PreliminaryDismissalMessage,
    PreliminaryDismissalSettings,
)
from app.services.dismissal_notifications import DismissalNotificationService


ITEM_LINE_RE = re.compile(
    r"^\s*(\d{2}\.\d{2}\.\d{4})\s+-\s+(.+?)\s+-\s+(.+?)\s*$"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_fio(value: str) -> str:
    text = " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").split())
    return text.casefold()


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            parts.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(str(part))
    return "".join(parts)


class _HTMLText(HTMLParser):
    BREAK_TAGS = {"br", "div", "p", "li", "tr", "h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self.BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return html.unescape("".join(self.parts))


@dataclass(frozen=True)
class ParsedPreliminaryDismissal:
    dismissal_date: date
    fio: str
    position: str
    departments: tuple[str, ...]


@dataclass(frozen=True)
class PreliminaryMail:
    uid: str
    message_id: str
    message_date: str
    sender: str
    subject: str
    body: str
    body_hash: str


@dataclass(frozen=True)
class PreliminaryMailScan:
    max_uid: str
    messages: tuple[PreliminaryMail, ...]


class PreliminaryDismissalSecretBox:
    """Шифрует пароль IMAP ключом, производным от APP_SECRET_KEY."""

    def __init__(self, app_secret_key: str):
        secret = str(app_secret_key or "").encode("utf-8")
        if not secret:
            raise RuntimeError("APP_SECRET_KEY не задан")
        digest = hashlib.sha256(secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        try:
            return self._fernet.decrypt(text.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise RuntimeError(
                "Не удалось расшифровать пароль IMAP. "
                "Проверьте, что APP_SECRET_KEY не менялся."
            ) from exc


def _split_position_and_departments(value: str) -> tuple[str, tuple[str, ...]]:
    parts = [" ".join(part.split()) for part in str(value or "").split("/")]
    position = parts[0] if parts else ""
    departments = tuple(part for part in parts[1:] if part)
    return position, departments


def parse_preliminary_dismissals(body: str) -> list[ParsedPreliminaryDismissal]:
    """Разобрать одно или несколько увольнений, включая переносы строк Outlook."""
    raw_items: list[tuple[str, str, str]] = []
    current: list[str] | None = None

    for raw_line in str(body or "").replace("\u00a0", " ").splitlines():
        line = " ".join(raw_line.split())
        normalized_line = line.casefold()
        reply_boundary = (
            line == "---"
            or normalized_line.startswith(
                (
                    "с уважением",
                    "от:",
                    "from:",
                    "-----original message-----",
                    "________________________________",
                )
            )
        )
        if reply_boundary:
            if current is not None:
                raw_items.append((current[0], current[1], " ".join(current[2:])))
                current = None
            break
        match = ITEM_LINE_RE.match(line)
        if match:
            if current is not None:
                raw_items.append((current[0], current[1], " ".join(current[2:])))
            current = [match.group(1), match.group(2), match.group(3)]
            continue
        if current is None or not line:
            continue
        if normalized_line.startswith("добрый день"):
            continue
        current.append(line)

    if current is not None:
        raw_items.append((current[0], current[1], " ".join(current[2:])))

    result: list[ParsedPreliminaryDismissal] = []
    for raw_date, raw_fio, remainder in raw_items:
        try:
            dismissal_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
        except ValueError:
            continue
        fio = " ".join(raw_fio.split())
        if len(fio.split()) < 2:
            continue
        position, departments = _split_position_and_departments(remainder)
        result.append(
            ParsedPreliminaryDismissal(
                dismissal_date=dismissal_date,
                fio=fio,
                position=position,
                departments=departments,
            )
        )
    return result


class PreliminaryDismissalImapService:
    """Read-only чтение доверенных предварительных кадровых писем."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        use_ssl: bool,
        username: str,
        password: str,
        lookback_days: int,
    ):
        self.host = str(host or "").strip()
        self.port = int(port)
        self.use_ssl = bool(use_ssl)
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.lookback_days = max(1, int(lookback_days))

    @staticmethod
    def _uid_number(value: str | bytes | None) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _connect(self):
        if not (self.host and self.username and self.password):
            raise RuntimeError("Для организации не настроено IMAP-подключение")
        client_cls = imaplib.IMAP4_SSL if self.use_ssl else imaplib.IMAP4
        client = client_cls(self.host, self.port)
        client.login(self.username, self.password)
        return client

    @staticmethod
    def _fetch_raw(imap, uid: str) -> bytes:
        status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not data:
            raise RuntimeError(f"Не удалось прочитать письмо IMAP UID {uid}")
        for item in data:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise RuntimeError(f"Письмо IMAP UID {uid} получено без содержимого")

    @staticmethod
    def _message_body(message) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        parts = message.walk() if message.is_multipart() else (message,)
        for part in parts:
            if part.get_content_disposition() == "attachment":
                continue
            content_type = str(part.get_content_type() or "").casefold()
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                value = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if not isinstance(value, str):
                continue
            if content_type == "text/plain":
                plain_parts.append(value)
            else:
                parser = _HTMLText()
                parser.feed(value)
                html_parts.append(parser.text())
        return "\n".join(plain_parts or html_parts)

    @classmethod
    def _from_raw(cls, uid: str, raw: bytes) -> PreliminaryMail:
        message = email.message_from_bytes(raw, policy=policy.default)
        body = cls._message_body(message)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return PreliminaryMail(
            uid=uid,
            message_id=decode_mime(str(message.get("Message-ID") or "")).strip(),
            message_date=decode_mime(str(message.get("Date") or "")),
            sender=decode_mime(str(message.get("From") or "")),
            subject=decode_mime(str(message.get("Subject") or "")),
            body=body,
            body_hash=body_hash,
        )

    def scan(
        self,
        *,
        after_uid: str,
        folder: str,
        sender_filter: str,
        subject_filter: str,
    ) -> PreliminaryMailScan:
        sender_filter = sender_filter.strip().casefold()
        subject_filter = subject_filter.strip().casefold()
        if not sender_filter or not subject_filter:
            raise RuntimeError("Укажите фильтры отправителя и темы письма")
        after_number = self._uid_number(after_uid)
        selected_folder = folder.strip() or "INBOX"

        with self._connect() as imap:
            status, _ = imap.select(selected_folder, readonly=True)
            if status != "OK":
                raise RuntimeError(
                    f"Не удалось открыть папку {selected_folder} в режиме read-only"
                )
            since = (
                datetime.now()
                - timedelta(days=self.lookback_days)
            ).strftime("%d-%b-%Y")
            criteria: list[str] = []
            if after_number:
                criteria.extend(["UID", f"{after_number + 1}:*"])
            criteria.extend(["SINCE", since])
            status, data = imap.uid("search", None, *criteria)
            if status != "OK":
                raise RuntimeError("Ошибка поиска предварительных писем по IMAP")

            uid_values = data[0].split() if data and data[0] else []
            uid_values = [
                value for value in uid_values if self._uid_number(value) > after_number
            ]
            if not uid_values:
                return PreliminaryMailScan(str(after_number) if after_number else "", ())

            messages: list[PreliminaryMail] = []
            for uid_bytes in uid_values:
                uid = uid_bytes.decode()
                item = self._from_raw(uid, self._fetch_raw(imap, uid))
                if sender_filter not in item.sender.casefold():
                    continue
                if subject_filter not in item.subject.casefold():
                    continue
                messages.append(item)
            max_uid = max(self._uid_number(value) for value in uid_values)
            return PreliminaryMailScan(str(max_uid), tuple(messages))


class PreliminaryDismissalService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.secret_box = PreliminaryDismissalSecretBox(settings.app_secret_key)

    @property
    def today(self) -> date:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).date()

    def _migrate_legacy_settings(self) -> None:
        """Перенести прежнее общее IMAP-подключение в единственное старое правило."""
        rows = list(
            self.db.scalars(
                select(PreliminaryDismissalSettings).order_by(
                    PreliminaryDismissalSettings.id
                )
            ).all()
        )
        changed = False
        for row in rows:
            if not row.source_id or row.imap_host or row.imap_username:
                continue
            row.imap_host = str(self.settings.onec_imap_host or "").strip()
            row.imap_port = int(self.settings.onec_imap_port or 993)
            row.imap_ssl = bool(self.settings.onec_imap_ssl)
            row.imap_username = str(self.settings.onec_imap_username or "").strip()
            row.imap_lookback_days = max(
                1,
                int(self.settings.onec_imap_lookback_days or 7),
            )
            legacy_password = str(self.settings.onec_imap_password or "")
            if legacy_password and not row.imap_password_encrypted:
                row.imap_password_encrypted = self.secret_box.encrypt(legacy_password)
            row.config_key = self._config_key(
                source_id=row.source_id,
                imap_host=row.imap_host,
                imap_port=row.imap_port,
                imap_ssl=row.imap_ssl,
                imap_username=row.imap_username,
                imap_folder=row.imap_folder,
                imap_lookback_days=row.imap_lookback_days,
                sender_filter=row.sender_filter,
                subject_filter=row.subject_filter,
            )
            changed = True
        if changed:
            self.db.commit()

    def list_settings(self) -> list[PreliminaryDismissalSettings]:
        self._migrate_legacy_settings()
        return list(
            self.db.scalars(
                select(PreliminaryDismissalSettings).order_by(
                    PreliminaryDismissalSettings.id
                )
            ).all()
        )

    def get_settings(
        self,
        rule_id: int | None = None,
        *,
        create: bool = False,
    ) -> PreliminaryDismissalSettings | None:
        """Совместимый доступ к первому правилу; новые вызовы передают rule_id."""
        self._migrate_legacy_settings()
        if rule_id is not None:
            row = self.db.get(PreliminaryDismissalSettings, rule_id)
        else:
            row = self.db.scalar(
                select(PreliminaryDismissalSettings)
                .order_by(PreliminaryDismissalSettings.id)
                .limit(1)
            )
        if row is None and create:
            row = PreliminaryDismissalSettings()
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    @staticmethod
    def _config_key(
        *,
        source_id: str,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        imap_username: str,
        imap_folder: str,
        imap_lookback_days: int,
        sender_filter: str,
        subject_filter: str,
    ) -> str:
        raw = "\n".join(
            (
                source_id,
                imap_host.casefold(),
                str(imap_port),
                "ssl" if imap_ssl else "plain",
                imap_username.casefold(),
                imap_folder,
                str(imap_lookback_days),
                sender_filter.casefold(),
                subject_filter.casefold(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save_settings(
        self,
        *,
        rule_id: int | None,
        enabled: bool,
        source_id: str,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        imap_username: str,
        imap_password: str,
        imap_folder: str,
        imap_lookback_days: int,
        sender_filter: str,
        subject_filter: str,
        operator: str,
    ) -> PreliminaryDismissalSettings:
        source_id = source_id.strip().lower()
        imap_host = imap_host.strip()
        imap_username = imap_username.strip()
        imap_folder = imap_folder.strip() or "INBOX"
        sender_filter = sender_filter.strip()
        subject_filter = subject_filter.strip()
        try:
            imap_port = int(imap_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("Порт IMAP должен быть числом") from exc
        if not 1 <= imap_port <= 65535:
            raise ValueError("Порт IMAP должен быть от 1 до 65535")
        try:
            imap_lookback_days = int(imap_lookback_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("Глубина поиска должна быть числом дней") from exc
        if not 1 <= imap_lookback_days <= 365:
            raise ValueError("Глубина поиска должна быть от 1 до 365 дней")
        if any(
            char in value
            for value in (imap_host, imap_username, imap_folder)
            for char in ("\r", "\n", "\0")
        ):
            raise ValueError("Параметры IMAP содержат недопустимые символы")
        source = self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.mail_domain == source_id
            )
        ) if source_id else None
        if source is None:
            raise ValueError("Выберите организацию предварительных уведомлений")
        duplicate = self.db.scalar(
            select(PreliminaryDismissalSettings).where(
                PreliminaryDismissalSettings.source_id == source_id,
                PreliminaryDismissalSettings.id != int(rule_id or 0),
            )
        )
        if duplicate is not None:
            raise ValueError("Для этой организации правило уже создано")
        if not imap_host:
            raise ValueError("Укажите IMAP-сервер")
        if not imap_username:
            raise ValueError("Укажите почтовый ящик или логин IMAP")
        if enabled and not sender_filter:
            raise ValueError("Укажите доверенного отправителя")
        if enabled and "@" not in sender_filter:
            raise ValueError("Укажите полный e-mail доверенного отправителя")
        if enabled and not subject_filter:
            raise ValueError("Укажите часть темы кадрового письма")

        row = self.get_settings(rule_id, create=False) if rule_id else None
        if rule_id and row is None:
            raise ValueError("Настройка организации не найдена")
        if row is None:
            row = PreliminaryDismissalSettings()
            self.db.add(row)
        if imap_password:
            row.imap_password_encrypted = self.secret_box.encrypt(imap_password)
        if enabled and not row.imap_password_encrypted:
            raise ValueError("Укажите пароль IMAP")
        new_key = self._config_key(
            source_id=source_id,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_ssl=imap_ssl,
            imap_username=imap_username,
            imap_folder=imap_folder,
            imap_lookback_days=imap_lookback_days,
            sender_filter=sender_filter,
            subject_filter=subject_filter,
        )
        if row.config_key and row.config_key != new_key:
            row.last_scanned_uid = ""
            row.last_status = "reset"
            row.last_error = "Правило изменено; почтовый курсор сброшен"
        row.enabled = enabled
        row.source_id = source_id
        row.imap_host = imap_host
        row.imap_port = imap_port
        row.imap_ssl = imap_ssl
        row.imap_username = imap_username
        row.imap_folder = imap_folder
        row.imap_lookback_days = imap_lookback_days
        row.sender_filter = sender_filter
        row.subject_filter = subject_filter
        row.config_key = new_key
        row.updated_by = operator
        row.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def _source(self, source_id: str) -> OneCAdditionalSource | None:
        return self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.mail_domain == source_id
            )
        )

    def _matching_records(
        self,
        *,
        source_id: str,
        normalized_fio: str,
    ) -> list[HRSourceRecord]:
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(HRSourceRecord.source_id == source_id)
            ).all()
        )
        return [record for record in records if normalize_fio(record.fio) == normalized_fio]

    def _match_worker(
        self,
        *,
        source_id: str,
        normalized_fio: str,
    ) -> tuple[str, str]:
        records = self._matching_records(
            source_id=source_id,
            normalized_fio=normalized_fio,
        )
        present_keys = {record.worker_key for record in records if record.is_present}
        keys = present_keys or {record.worker_key for record in records}
        if len(keys) == 1:
            return next(iter(keys)), ""
        if not keys:
            return "", "ФИО не найдено в выбранной организации"
        return "", "В выбранной организации найдено несколько работников с таким ФИО"

    def _current_hr_state(self, worker_key: str, source_id: str):
        if not worker_key:
            return None
        return self.db.scalar(
            select(HREmploymentState).where(
                HREmploymentState.worker_key == worker_key,
                HREmploymentState.source_id == source_id,
            )
        )

    @staticmethod
    def _message_key(source_id: str, item: PreliminaryMail) -> str:
        identity = item.message_id.strip() or (
            f"{item.sender}\n{item.subject}\n{item.message_date}\n{item.body_hash}"
        )
        return hashlib.sha256(f"{source_id}\n{identity}".encode("utf-8")).hexdigest()

    def _latest_item(self, source_id: str, normalized_fio: str):
        return self.db.scalar(
            select(PreliminaryDismissalItem)
            .where(
                PreliminaryDismissalItem.source_id == source_id,
                PreliminaryDismissalItem.normalized_fio == normalized_fio,
            )
            .order_by(desc(PreliminaryDismissalItem.sequence))
            .limit(1)
        )

    def _move_deferral(
        self,
        *,
        worker_key: str,
        previous_date: date,
        current_date: date,
    ) -> None:
        """Сохранить относительную отсрочку при переносе даты увольнения."""
        if not worker_key or previous_date == current_date:
            return
        previous = self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == worker_key,
                DismissalDeferral.dismissal_date == previous_date,
            )
        )
        if previous is None:
            return

        offset_days = max(
            0,
            (previous.deferred_until - previous_date).days,
        )
        migrated_until = current_date + timedelta(days=offset_days)
        current = self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == worker_key,
                DismissalDeferral.dismissal_date == current_date,
            )
        )
        if current is not None and current.id != previous.id:
            if migrated_until >= current.deferred_until:
                current.deferred_until = migrated_until
                current.operator_username = previous.operator_username
            current.deferral_count = max(
                int(current.deferral_count or 0),
                int(previous.deferral_count or 0),
            )
            current.updated_at = utcnow()
            self.db.delete(previous)
            return

        previous.dismissal_date = current_date
        previous.deferred_until = migrated_until
        previous.updated_at = utcnow()

    def _discard_unconfirmed_deferral(
        self,
        item: PreliminaryDismissalItem,
    ) -> None:
        if not item.worker_key:
            return
        deferral = self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == item.worker_key,
                DismissalDeferral.dismissal_date == item.dismissal_date,
            )
        )
        if deferral is not None:
            self.db.delete(deferral)

    def _upsert_item(
        self,
        *,
        source: OneCAdditionalSource,
        parsed: ParsedPreliminaryDismissal,
        message: PreliminaryMail,
        message_key: str,
    ) -> PreliminaryDismissalItem:
        normalized = normalize_fio(parsed.fio)
        worker_key, match_error = self._match_worker(
            source_id=source.source_id,
            normalized_fio=normalized,
        )
        latest = self._latest_item(source.source_id, normalized)
        current_state = self._current_hr_state(
            latest.worker_key if latest is not None else worker_key,
            source.source_id,
        )
        new_episode = bool(
            latest is not None
            and (
                (latest.status == "confirmed" and (
                    current_state is None or current_state.dismissal_date is None
                ))
                or (
                    worker_key
                    and latest.worker_key
                    and worker_key != latest.worker_key
                )
            )
        )
        if latest is None or new_episode:
            item = PreliminaryDismissalItem(
                source_id=source.source_id,
                source_name=source.name,
                sequence=(1 if latest is None else latest.sequence + 1),
                worker_key=worker_key,
                normalized_fio=normalized,
                fio=parsed.fio,
                dismissal_date=parsed.dismissal_date,
                position=parsed.position,
                departments_json=json.dumps(
                    list(parsed.departments), ensure_ascii=False
                ),
                status="active" if worker_key else "unmatched",
                match_error=match_error,
                latest_message_key=message_key,
                latest_message_uid=message.uid,
            )
            self.db.add(item)
            self.db.flush()
            return item

        latest.source_name = source.name
        latest.fio = parsed.fio
        self._move_deferral(
            worker_key=latest.worker_key or worker_key,
            previous_date=latest.dismissal_date,
            current_date=parsed.dismissal_date,
        )
        latest.dismissal_date = parsed.dismissal_date
        latest.position = parsed.position
        latest.departments_json = json.dumps(
            list(parsed.departments), ensure_ascii=False
        )
        if worker_key:
            latest.worker_key = worker_key
        if latest.status != "confirmed":
            latest.status = "active" if latest.worker_key else "unmatched"
        latest.match_error = "" if latest.worker_key else match_error
        latest.latest_message_key = message_key
        latest.latest_message_uid = message.uid
        latest.latest_notified_at = utcnow()
        latest.expired_at = None
        latest.updated_at = utcnow()
        return latest

    def _ingest_message(
        self,
        source: OneCAdditionalSource,
        item: PreliminaryMail,
    ) -> tuple[int, int]:
        message_key = self._message_key(source.source_id, item)
        existing = self.db.scalar(
            select(PreliminaryDismissalMessage).where(
                PreliminaryDismissalMessage.message_key == message_key
            )
        )
        if existing is not None:
            return 0, 0

        parsed_items = parse_preliminary_dismissals(item.body)
        row = PreliminaryDismissalMessage(
            message_key=message_key,
            source_id=source.source_id,
            imap_uid=item.uid,
            message_id=item.message_id,
            message_date=item.message_date,
            sender=item.sender,
            subject=item.subject,
            body_hash=item.body_hash,
            status="processed" if parsed_items else "ignored",
            items_count=len(parsed_items),
            error=("В письме не найдены строки увольнений" if not parsed_items else ""),
        )
        self.db.add(row)
        matched = 0
        for parsed in parsed_items:
            saved = self._upsert_item(
                source=source,
                parsed=parsed,
                message=item,
                message_key=message_key,
            )
            if saved.worker_key:
                matched += 1
        row.matched_count = matched
        self.db.commit()
        return len(parsed_items), matched

    @staticmethod
    def _event_ids(notice: DismissalEquipmentNotice) -> list[int]:
        try:
            values = json.loads(notice.event_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        return [int(value) for value in values if str(value).isdigit()]

    def _link_hr_event(self, item: PreliminaryDismissalItem) -> None:
        if not item.worker_key or not item.equipment_notice_id:
            return
        event = self.db.scalar(
            select(HREmploymentDismissalEvent)
            .where(
                HREmploymentDismissalEvent.worker_key == item.worker_key,
                HREmploymentDismissalEvent.source_id == item.source_id,
                HREmploymentDismissalEvent.current_dismissal_date.is_not(None),
            )
            .order_by(desc(HREmploymentDismissalEvent.sequence))
            .limit(1)
        )
        notice = self.db.get(DismissalEquipmentNotice, item.equipment_notice_id)
        if event is None or notice is None:
            return
        event_ids = list(dict.fromkeys([*self._event_ids(notice), event.id]))
        notice.event_ids_json = json.dumps(event_ids)
        event.noticed_at = event.noticed_at or notice.created_at or utcnow()
        notice.updated_at = utcnow()

    def reconcile(self) -> None:
        items = list(
            self.db.scalars(
                select(PreliminaryDismissalItem).where(
                    PreliminaryDismissalItem.status.in_(
                        ["active", "unmatched", "expired", "confirmed"]
                    )
                )
            ).all()
        )
        now = utcnow()
        for item in items:
            if not item.worker_key:
                worker_key, error = self._match_worker(
                    source_id=item.source_id,
                    normalized_fio=item.normalized_fio,
                )
                if worker_key:
                    item.worker_key = worker_key
                    item.match_error = ""
                else:
                    item.status = "unmatched"
                    item.match_error = error
                    item.updated_at = now
                    continue

            state = self._current_hr_state(item.worker_key, item.source_id)
            if state is not None and state.dismissal_date is not None:
                self._move_deferral(
                    worker_key=item.worker_key,
                    previous_date=item.dismissal_date,
                    current_date=state.dismissal_date,
                )
                item.dismissal_date = state.dismissal_date
                item.status = "confirmed"
                item.confirmed_at = item.confirmed_at or now
                item.expired_at = None
            elif item.status != "confirmed":
                if item.dismissal_date < self.today:
                    self._discard_unconfirmed_deferral(item)
                    item.status = "expired"
                    item.expired_at = item.expired_at or now
                else:
                    item.status = "active"
                    item.expired_at = None
            self._link_hr_event(item)
            item.updated_at = now
        self.db.commit()

    def _candidate(self, item: PreliminaryDismissalItem) -> dict:
        return {
            "worker_key": item.worker_key,
            "fio": item.fio,
            "dismissal_date": item.dismissal_date,
            "organizations": [
                {
                    "source_id": item.source_id,
                    "source_name": item.source_name or item.source_id,
                    "dismissal_date": item.dismissal_date,
                }
            ],
        }

    def _queue_equipment_notices(self) -> tuple[int, int]:
        service = DismissalNotificationService(self.settings, self.db)
        queued = 0
        sent = 0
        items = list(
            self.db.scalars(
                select(PreliminaryDismissalItem)
                .where(
                    PreliminaryDismissalItem.worker_key != "",
                    PreliminaryDismissalItem.status.in_(
                        ["active", "confirmed", "expired"]
                    ),
                )
                .order_by(PreliminaryDismissalItem.id)
            ).all()
        )
        for item in items:
            previous_notice = (
                self.db.get(DismissalEquipmentNotice, item.equipment_notice_id)
                if item.equipment_notice_id
                else None
            )
            previous_status = previous_notice.status if previous_notice is not None else ""
            notice, status = service.queue_preliminary_notice(
                candidate=self._candidate(item),
                equipment_notice_id=item.equipment_notice_id,
            )
            if notice is None:
                item.match_error = status
                item.updated_at = utcnow()
                self.db.commit()
                continue
            if item.equipment_notice_id is None:
                queued += 1
            item.equipment_notice_id = notice.id
            if status == "sent" and previous_status != "sent":
                sent += 1
            if status == "sent":
                item.match_error = ""
            else:
                item.match_error = notice.last_error or status
            item.updated_at = utcnow()
            self._link_hr_event(item)
            self.db.commit()
        return queued, sent

    @staticmethod
    def _status_label(value: str) -> str:
        return {
            "never": "Еще не проверялось",
            "reset": "Правило изменено",
            "success": "Проверено",
            "warning": "Требует проверки",
            "failed": "Ошибка",
        }.get(value, value or "Неизвестно")

    def _safe_rule_error(self, exc: Exception, password: str = "") -> str:
        message = str(exc)[:4000] or exc.__class__.__name__
        if password:
            message = message.replace(password, "***")
        return message

    def _scan_rule(self, config: PreliminaryDismissalSettings) -> dict[str, int | str]:
        config_id = config.id
        source = self._source(config.source_id)
        if source is None:
            config.last_status = "failed"
            config.last_error = "Выбранная организация больше не существует"
            config.last_checked_at = utcnow()
            self.db.commit()
            return {
                "rule_id": config_id,
                "status": "failed",
                "messages": 0,
                "items": 0,
                "matched": 0,
            }

        password = ""
        try:
            password = self.secret_box.decrypt(config.imap_password_encrypted)
            config.last_checked_at = utcnow()
            self.db.commit()
            scan = PreliminaryDismissalImapService(
                host=config.imap_host,
                port=config.imap_port,
                use_ssl=config.imap_ssl,
                username=config.imap_username,
                password=password,
                lookback_days=config.imap_lookback_days,
            ).scan(
                after_uid=config.last_scanned_uid,
                folder=config.imap_folder,
                sender_filter=config.sender_filter,
                subject_filter=config.subject_filter,
            )
            parsed_count = 0
            matched_count = 0
            for message in scan.messages:
                parsed, matched = self._ingest_message(source, message)
                parsed_count += parsed
                matched_count += matched

            config = self.db.get(PreliminaryDismissalSettings, config_id)
            assert config is not None
            warnings: list[str] = []
            if scan.messages and parsed_count == 0:
                warnings.append("В найденных письмах нет строк увольнений нужного формата")
            if matched_count < parsed_count:
                warnings.append(
                    f"Не сопоставлено с кадровым реестром: {parsed_count - matched_count}"
                )
            config.last_scanned_uid = scan.max_uid
            config.last_status = "warning" if warnings else "success"
            config.last_error = ". ".join(warnings)
            config.last_checked_at = utcnow()
            config.last_success_at = utcnow()
            self.db.commit()
            return {
                "rule_id": config_id,
                "status": config.last_status,
                "messages": len(scan.messages),
                "items": parsed_count,
                "matched": matched_count,
            }
        except Exception as exc:
            self.db.rollback()
            config = self.db.get(PreliminaryDismissalSettings, config_id)
            if config is not None:
                config.last_status = "failed"
                config.last_error = self._safe_rule_error(exc, password)
                config.last_checked_at = utcnow()
                self.db.commit()
            return {
                "rule_id": config_id,
                "status": "failed",
                "messages": 0,
                "items": 0,
                "matched": 0,
            }

    def process(
        self,
        *,
        force: bool = False,
        rule_id: int | None = None,
    ) -> dict[str, int | str]:
        notification_service = DismissalNotificationService(self.settings, self.db)
        notification_service._sync_employment_events()
        self.reconcile()

        if rule_id is not None:
            selected = self.get_settings(rule_id, create=False)
            configs = [selected] if selected is not None else []
        else:
            configs = [row for row in self.list_settings() if row.enabled]
        configs = [row for row in configs if row is not None and (row.enabled or force)]
        if not configs:
            return {
                "status": "not_found" if rule_id is not None else "disabled",
                "rules": 0,
                "failed": 0,
                "messages": 0,
                "items": 0,
                "matched": 0,
                "queued": 0,
                "sent": 0,
            }

        results = [self._scan_rule(config) for config in configs]
        self.reconcile()
        queued, sent = self._queue_equipment_notices()
        self.reconcile()

        for result in results:
            if result["status"] == "failed":
                continue
            config = self.db.get(PreliminaryDismissalSettings, int(result["rule_id"]))
            if config is None:
                continue
            delivery_errors = int(
                self.db.scalar(
                    select(func.count(PreliminaryDismissalItem.id)).where(
                        PreliminaryDismissalItem.source_id == config.source_id,
                        PreliminaryDismissalItem.status.in_(["active", "confirmed"]),
                        PreliminaryDismissalItem.worker_key != "",
                        PreliminaryDismissalItem.match_error != "",
                    )
                )
                or 0
            )
            if delivery_errors:
                message = (
                    "Письмо о возврате оборудования ожидает отправки: "
                    f"{delivery_errors}"
                )
                config.last_status = "warning"
                config.last_error = ". ".join(
                    part for part in (config.last_error, message) if part
                )
                result["status"] = "warning"
        self.db.commit()

        failed = sum(result["status"] == "failed" for result in results)
        warnings = sum(result["status"] == "warning" for result in results)
        overall_status = (
            "failed"
            if failed == len(results)
            else "partial"
            if failed
            else "warning"
            if warnings
            else "success"
        )
        return {
            "status": overall_status,
            "rules": len(results),
            "failed": failed,
            "messages": sum(int(result["messages"]) for result in results),
            "items": sum(int(result["items"]) for result in results),
            "matched": sum(int(result["matched"]) for result in results),
            "queued": queued,
            "sent": sent,
        }

    def summary(self) -> dict[str, object]:
        rules: list[dict[str, object]] = []
        for config in self.list_settings():
            source = self._source(config.source_id) if config.source_id else None
            counts = {
                status: int(
                    self.db.scalar(
                        select(func.count(PreliminaryDismissalItem.id)).where(
                            PreliminaryDismissalItem.source_id == config.source_id,
                            PreliminaryDismissalItem.status == status,
                        )
                    )
                    or 0
                )
                for status in ("active", "confirmed", "expired", "unmatched")
            }
            rules.append(
                {
                    "settings": {
                        "id": config.id,
                        "enabled": config.enabled,
                        "source_id": config.source_id,
                        "imap_host": config.imap_host,
                        "imap_port": config.imap_port,
                        "imap_ssl": config.imap_ssl,
                        "imap_username": config.imap_username,
                        "imap_folder": config.imap_folder,
                        "imap_lookback_days": config.imap_lookback_days,
                        "sender_filter": config.sender_filter,
                        "subject_filter": config.subject_filter,
                        "last_status": config.last_status,
                        "last_error": config.last_error,
                        "last_checked_at": config.last_checked_at,
                    },
                    "source_name": source.name if source is not None else config.source_id,
                    "password_configured": bool(config.imap_password_encrypted),
                    "counts": counts,
                    "status_label": self._status_label(config.last_status),
                    "messages": int(
                        self.db.scalar(
                            select(func.count(PreliminaryDismissalMessage.id)).where(
                                PreliminaryDismissalMessage.source_id == config.source_id
                            )
                        )
                        or 0
                    ),
                }
            )
        return {
            "rules": rules,
            "messages": int(
                self.db.scalar(select(func.count(PreliminaryDismissalMessage.id))) or 0
            ),
            "counts": {
                status: int(
                    self.db.scalar(
                        select(func.count(PreliminaryDismissalItem.id)).where(
                            PreliminaryDismissalItem.status == status
                        )
                    )
                    or 0
                )
                for status in ("active", "confirmed", "expired", "unmatched")
            },
            "new_rule": {
                "imap_host": str(self.settings.onec_imap_host or ""),
                "imap_port": int(self.settings.onec_imap_port or 993),
                "imap_ssl": bool(self.settings.onec_imap_ssl),
                "imap_username": str(self.settings.onec_imap_username or ""),
                "imap_folder": "INBOX",
                "imap_lookback_days": max(
                    1,
                    int(self.settings.onec_imap_lookback_days or 7),
                ),
            },
        }

    def source_options(self) -> list[OneCAdditionalSource]:
        return list(
            self.db.scalars(
                select(OneCAdditionalSource)
                .where(OneCAdditionalSource.enabled.is_(True))
                .order_by(
                    OneCAdditionalSource.is_primary.desc(),
                    OneCAdditionalSource.name,
                )
            ).all()
        )
