from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_notifications import HREmploymentDismissalEvent
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import (
    TechExpertNotification,
    TechExpertNotificationBatch,
    TechExpertNotificationBatchItem,
)
from app.services.ad import ActiveDirectoryService
from app.services.mailer import (
    CredentialMailer,
    get_domain_mail_profile,
    render_mail_template,
)
from app.services.techexpert_settings import (
    build_techexpert_template_context,
    ensure_techexpert_settings,
    parse_notification_time,
)
from app.services.techexpert_registration import (
    TechExpertRegistrationService,
)
from app.services.techexpert_access import (
    TechExpertGroupAccessService,
    placement_snapshot,
)


logger = logging.getLogger(__name__)
POLL_SECONDS = 60
RETRY_MINUTES = 15
ACTIVE_STATUSES = {"active"}
OPEN_STATUSES = {"pending", "deferred", "failed", "intervention"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str) -> str:
    normalized = normalize(value)
    return normalized.rsplit("@", 1)[1] if normalized.count("@") == 1 else ""


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TechExpertDataError(RuntimeError):
    """Ошибка данных, которую должен исправить оператор."""


@dataclass(frozen=True)
class TechExpertIdentity:
    corporate_email: str
    ad_login: str
    ad_object_guid: str


class TechExpertLifecycleService:
    """Закрывает доступ в группе AD и уведомляет внешний Техэксперт."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.config = ensure_techexpert_settings(db)

    @property
    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.app_timezone))

    @property
    def source_domain(self) -> str:
        return normalize(self.config.source_domain)

    def _configuration_error(self) -> str:
        missing = []
        if not self.source_domain:
            missing.append("организация")
        elif self.source_domain not in {
            normalize(domain) for domain in self.settings.zimbra_domains
        }:
            missing.append(
                "почтовый профиль выбранной организации"
            )
        if not str(self.config.ad_group_dn or "").strip():
            missing.append("маркерная группа AD")
        if not email_domain(self.config.recipient_email):
            missing.append("получатель уведомлений")
        if not str(self.settings.smtp_host or "").strip():
            missing.append("SMTP_HOST")
        try:
            parse_notification_time(self.config.notification_time)
        except ValueError:
            missing.append("время отправки")
        if not str(self.config.subject or "").strip():
            missing.append("тема письма")
        if not str(self.config.body_html or "").strip():
            missing.append("шаблон письма")
        return ", ".join(missing)

    def _local(self, value: datetime) -> datetime:
        return aware_utc(value).astimezone(ZoneInfo(self.settings.app_timezone))

    def _at_notification_time(self, value: date) -> datetime:
        local = datetime.combine(
            value,
            parse_notification_time(self.config.notification_time),
            tzinfo=ZoneInfo(self.settings.app_timezone),
        )
        return local.astimezone(timezone.utc)

    def _next_notification_time(self, confirmed_at: datetime) -> datetime:
        local_confirmation = self._local(confirmed_at)
        candidate = datetime.combine(
            local_confirmation.date(),
            parse_notification_time(self.config.notification_time),
            tzinfo=local_confirmation.tzinfo,
        )
        if candidate < local_confirmation:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def _deferral(
        self,
        worker_key: str,
        dismissal_date: date,
    ) -> DismissalDeferral | None:
        return self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == worker_key,
                DismissalDeferral.dismissal_date == dismissal_date,
            )
        )

    def _scheduled_for(
        self,
        *,
        event: HREmploymentDismissalEvent,
        state: HREmploymentState,
        deferral: DismissalDeferral | None,
    ) -> datetime:
        dismissal_date = event.current_dismissal_date
        if dismissal_date is None:
            raise TechExpertDataError("В кадровом событии отсутствует дата увольнения")

        confirmed_at = event.updated_at or event.created_at
        confirmation_date = self._local(confirmed_at).date()
        retroactive = (
            normalize(state.status_reason) == "absent_from_export"
            or dismissal_date < confirmation_date
        )
        if retroactive:
            candidate = self._next_notification_time(confirmed_at)
        else:
            candidate = self._at_notification_time(
                dismissal_date + timedelta(days=1)
            )

        if deferral is not None:
            candidate = max(
                candidate,
                self._at_notification_time(deferral.deferred_until),
            )
        return candidate

    def _employment_state(
        self,
        event: HREmploymentDismissalEvent,
    ) -> HREmploymentState | None:
        return self.db.scalar(
            select(HREmploymentState).where(
                HREmploymentState.worker_key == event.worker_key,
                HREmploymentState.source_id == self.source_domain,
            )
        )

    def _record_for_event(
        self,
        event: HREmploymentDismissalEvent,
    ) -> HRSourceRecord | None:
        return self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.worker_key == event.worker_key,
                HRSourceRecord.source_id == self.source_domain,
            )
        )

    def _audit(
        self,
        row: TechExpertNotification,
        *,
        action: str,
        result: str,
        details: str = "",
    ) -> None:
        payload = {
            "notification_id": row.id,
            "worker_key": row.worker_key,
            "fio": row.fio,
            "source_id": row.source_id,
            "corporate_email": row.corporate_email,
            "recipient_email": row.recipient_email,
            "dismissal_date": row.dismissal_date.isoformat(),
            "details": details,
        }
        self.db.add(
            AuditLog(
                actor="system",
                action=action,
                target=row.corporate_email or row.worker_key,
                result=result,
                details=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
        )

    def _mark_cancelled(
        self,
        row: TechExpertNotification,
        reason: str,
    ) -> None:
        if row.status not in OPEN_STATUSES:
            return
        row.status = "cancelled"
        row.cancelled_at = utcnow()
        row.next_attempt_at = None
        row.last_error = reason
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_notification_cancelled",
            result="cancelled",
            details=reason,
        )

    def _mark_attention_after_send(
        self,
        row: TechExpertNotification,
    ) -> None:
        if row.status != "sent" or row.attention_state:
            return
        row.attention_state = "hr_active_after_notification"
        row.attention_details = (
            "После отправки запроса на прекращение доступа работник снова "
            "активен в кадровом источнике. Автоматических действий нет; "
            "нужно связаться с Техэкспертом вручную."
        )
        row.attention_at = utcnow()
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_reactivation_attention",
            result="attention",
            details=row.attention_details,
        )

    def _ensure_notification(
        self,
        event: HREmploymentDismissalEvent,
        state: HREmploymentState | None,
    ) -> TechExpertNotification | None:
        row = self.db.scalar(
            select(TechExpertNotification).where(
                TechExpertNotification.employment_event_id == event.id
            )
        )

        event_is_current = (
            state is not None
            and event.current_dismissal_date is not None
            and normalize(state.status) not in ACTIVE_STATUSES
            and state.dismissal_date == event.current_dismissal_date
        )
        if not event_is_current:
            if row is not None:
                if normalize(getattr(state, "status", "")) in ACTIVE_STATUSES:
                    self._mark_attention_after_send(row)
                self._mark_cancelled(
                    row,
                    "Кадровая дата снята или работник снова активен в организации",
                )
            return row

        assert state is not None
        assert event.current_dismissal_date is not None
        deferral = self._deferral(event.worker_key, event.current_dismissal_date)
        event_updated_at = aware_utc(event.updated_at or event.created_at)
        hr_reason = normalize(state.status_reason)
        scheduled_for = self._scheduled_for(
            event=event,
            state=state,
            deferral=deferral,
        )
        record = self._record_for_event(event)
        corporate_email = normalize(
            getattr(record, "corporate_email", "")
        )
        departments = placement_snapshot(record)["top_departments"]
        department = ", ".join(departments)

        if row is None:
            row = TechExpertNotification(
                employment_event_id=event.id,
                worker_key=event.worker_key,
                source_id=self.source_domain,
                source_name=event.source_name,
                fio=event.fio,
                department=department,
                corporate_email=corporate_email,
                dismissal_date=event.current_dismissal_date,
                deferred_until=(deferral.deferred_until if deferral else None),
                hr_reason=hr_reason,
                event_updated_at=event_updated_at,
                recipient_email=normalize(
                    self.config.recipient_email
                ),
                scheduled_for=scheduled_for,
                status=(
                    "deferred"
                    if deferral is not None
                    and deferral.deferred_until > self.local_now.date()
                    else "pending"
                ),
            )
            self.db.add(row)
            return row

        material_change = any(
            (
                row.dismissal_date != event.current_dismissal_date,
                row.deferred_until != (
                    deferral.deferred_until if deferral else None
                ),
                normalize(row.hr_reason) != hr_reason,
                aware_utc(row.event_updated_at) != event_updated_at,
                aware_utc(row.scheduled_for) != scheduled_for,
            )
        )
        row.source_name = event.source_name
        row.fio = event.fio
        if row.status != "sent":
            row.department = department
        if row.status != "sent":
            row.corporate_email = corporate_email
            row.recipient_email = normalize(
                self.config.recipient_email
            )
        if material_change and row.status not in {"sent", "skipped"}:
            row.dismissal_date = event.current_dismissal_date
            row.deferred_until = deferral.deferred_until if deferral else None
            row.hr_reason = hr_reason
            row.event_updated_at = event_updated_at
            row.scheduled_for = scheduled_for
            row.next_attempt_at = None
            row.cancelled_at = None
            row.last_error = ""
            row.status = (
                "deferred"
                if deferral is not None
                and deferral.deferred_until > self.local_now.date()
                else "pending"
            )
        elif row.status == "cancelled":
            row.dismissal_date = event.current_dismissal_date
            row.deferred_until = deferral.deferred_until if deferral else None
            row.hr_reason = hr_reason
            row.event_updated_at = event_updated_at
            row.scheduled_for = scheduled_for
            row.cancelled_at = None
            row.last_error = ""
            row.status = "pending"
        row.updated_at = utcnow()
        return row

    def _resolve_identity(
        self,
        row: TechExpertNotification,
    ) -> TechExpertIdentity:
        record = self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.worker_key == row.worker_key,
                HRSourceRecord.source_id == self.source_domain,
            )
        )
        corporate_email = normalize(
            getattr(record, "corporate_email", "")
            or row.corporate_email
        )
        if not corporate_email or email_domain(corporate_email) != self.source_domain:
            raise TechExpertDataError(
                "Не найден корпоративный email работника в организации Техэксперта"
            )

        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == row.worker_key
                )
            ).all()
        )
        preferred = next(
            (
                item
                for item in mappings
                if normalize(item.source_domain) == self.source_domain
            ),
            None,
        )
        if preferred is None:
            identities = {
                (
                    normalize(item.ad_object_guid),
                    normalize(item.ad_login),
                )
                for item in mappings
                if normalize(item.ad_object_guid) or normalize(item.ad_login)
            }
            if len(identities) > 1:
                raise TechExpertDataError(
                    "Найдено несколько разных учетных записей AD для работника"
                )
            if identities:
                guid, login = next(iter(identities))
            else:
                guid, login = "", normalize(getattr(record, "login", ""))
        else:
            guid = normalize(preferred.ad_object_guid)
            login = normalize(preferred.ad_login)

        if not guid and not login:
            raise TechExpertDataError(
                "Не настроено сопоставление работника с учетной записью AD"
            )
        return TechExpertIdentity(corporate_email, login, guid)

    def _current_event_and_state(
        self,
        row: TechExpertNotification,
    ) -> tuple[HREmploymentDismissalEvent | None, HREmploymentState | None]:
        event = self.db.get(
            HREmploymentDismissalEvent,
            row.employment_event_id,
        )
        if event is None:
            return None, None
        return event, self._employment_state(event)

    def _still_due(
        self,
        row: TechExpertNotification,
        event: HREmploymentDismissalEvent | None,
        state: HREmploymentState | None,
    ) -> bool:
        return bool(
            event is not None
            and state is not None
            and normalize(event.source_id) == self.source_domain
            and event.current_dismissal_date == row.dismissal_date
            and state.dismissal_date == row.dismissal_date
            and normalize(state.status) not in ACTIVE_STATUSES
        )

    def _mark_send_failure(
        self,
        row: TechExpertNotification,
        exc: Exception,
    ) -> None:
        row.status = (
            "intervention"
            if isinstance(exc, TechExpertDataError)
            else "failed"
        )
        row.last_error = str(exc)[:4000]
        row.next_attempt_at = utcnow() + timedelta(minutes=RETRY_MINUTES)
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_notification_failed",
            result=row.status,
            details=row.last_error,
        )

    def _defer_if_needed(self, row: TechExpertNotification) -> bool:
        deferral = self._deferral(row.worker_key, row.dismissal_date)
        if deferral is None:
            return False
        deferral_time = self._at_notification_time(deferral.deferred_until)
        if deferral_time <= utcnow():
            return False
        row.status = "deferred"
        row.deferred_until = deferral.deferred_until
        row.scheduled_for = max(
            aware_utc(row.scheduled_for),
            deferral_time,
        )
        return True

    def _prepare_batch_candidate(self, row: TechExpertNotification) -> bool:
        event, state = self._current_event_and_state(row)
        if not self._still_due(row, event, state):
            self._mark_cancelled(row, "Повторная HR-проверка отменила письмо")
            return False

        if self._defer_if_needed(row):
            return False

        row.attempts = int(row.attempts or 0) + 1
        row.next_attempt_at = None
        row.updated_at = utcnow()
        try:
            identity = self._resolve_identity(row)
            row.corporate_email = identity.corporate_email
            row.ad_login = identity.ad_login
            row.ad_object_guid = identity.ad_object_guid

            ad = ActiveDirectoryService(self.settings)
            if row.group_removal_status not in {"removed", "already_absent"}:
                try:
                    is_member = ad.is_user_member_of_group(
                        identity.ad_login,
                        self.config.ad_group_dn,
                        object_guid=identity.ad_object_guid,
                    )
                except Exception:
                    row.membership_state = "error"
                    raise

                if is_member:
                    # membership_state является снимком доступа до удаления и
                    # остается member для повторной отправки SMTP.
                    row.membership_state = "member"
                    try:
                        removal = ad.remove_user_from_group(
                            identity.ad_login,
                            self.config.ad_group_dn,
                            object_guid=identity.ad_object_guid,
                        )
                    except Exception as exc:
                        row.group_removal_status = "failed"
                        row.group_removal_error = str(exc)[:4000]
                        raise
                    row.group_removal_status = (
                        "removed" if removal == "removed" else "already_absent"
                    )
                    row.group_removed_at = utcnow()
                    row.group_removal_error = ""
                    record = self._record_for_event(event)
                    if record is not None:
                        record.techexpert_access = False
                    self._audit(
                        row,
                        action="techexpert_group_access_removed",
                        result=row.group_removal_status,
                        details="Доступ удален после повторной кадровой проверки",
                    )
                elif row.membership_state == "member":
                    # Предыдущая попытка успела удалить членство, но процесс
                    # завершился до фиксации результата.
                    row.group_removal_status = "already_absent"
                    row.group_removed_at = utcnow()
                else:
                    row.membership_state = "not_member"
                    row.status = "skipped"
                    row.last_error = ""
                    row.updated_at = utcnow()
                    self._audit(
                        row,
                        action="techexpert_notification_skipped",
                        result="not_member",
                        details="Работник не входит в группу доступа AD",
                    )
                    return False
            return True
        except Exception as exc:
            self._mark_send_failure(row, exc)
            return False

    def _recheck_batch_candidate(self, row: TechExpertNotification) -> bool:
        event, state = self._current_event_and_state(row)
        if not self._still_due(row, event, state):
            self._mark_cancelled(
                row,
                "Повторная HR-проверка перед SMTP отменила письмо",
            )
            return False
        return not self._defer_if_needed(row)

    @staticmethod
    def _template_employee(row: TechExpertNotification) -> dict[str, str]:
        return {
            "full_name": row.fio or row.worker_key,
            "corporate_email": row.corporate_email,
            "organization": row.source_name or row.source_id,
            "department": row.department,
            "dismissal_date": row.dismissal_date.strftime("%d.%m.%Y"),
        }

    def _create_batch(
        self,
        rows: list[TechExpertNotification],
    ) -> tuple[
        TechExpertNotificationBatch,
        dict[int, TechExpertNotificationBatchItem],
    ]:
        batch = TechExpertNotificationBatch(
            source_id=self.source_domain,
            source_name=next(
                (
                    row.source_name
                    for row in rows
                    if str(row.source_name or "").strip()
                ),
                self.source_domain,
            ),
            recipient_email=normalize(self.config.recipient_email),
            status="processing",
            total_count=len(rows),
        )
        self.db.add(batch)
        self.db.flush()

        items: dict[int, TechExpertNotificationBatchItem] = {}
        for row in rows:
            item = TechExpertNotificationBatchItem(
                batch_id=batch.id,
                notification_id=row.id,
                worker_key=row.worker_key,
                fio=row.fio,
                department=row.department,
                corporate_email=row.corporate_email,
                ad_login=row.ad_login,
                dismissal_date=row.dismissal_date,
                membership_state=row.membership_state,
            )
            self.db.add(item)
            items[row.id] = item
        self.db.flush()
        return batch, items

    @staticmethod
    def _batch_item_reason(row: TechExpertNotification) -> str:
        if row.status == "skipped":
            return "Работник не состоит в маркерной группе AD"
        if row.status == "deferred" and row.deferred_until is not None:
            return (
                "Действует глобальная отсрочка до "
                + row.deferred_until.strftime("%d.%m.%Y")
            )
        if row.status == "cancelled":
            return row.last_error or "Кадровое событие больше не актуально"
        if row.status in {"failed", "intervention"}:
            return row.last_error or "Проверка завершилась ошибкой"
        return row.last_error

    def _record_batch_item(
        self,
        item: TechExpertNotificationBatchItem,
        row: TechExpertNotification,
        *,
        included: bool,
        result: str | None = None,
        reason: str = "",
    ) -> None:
        item.fio = row.fio
        item.department = row.department
        item.corporate_email = row.corporate_email
        item.ad_login = row.ad_login
        item.dismissal_date = row.dismissal_date
        item.membership_state = row.membership_state
        item.included = included
        item.result = result or row.status
        item.reason = reason or self._batch_item_reason(row)

    def _finalize_batch(
        self,
        batch: TechExpertNotificationBatch,
        items: list[TechExpertNotificationBatchItem],
    ) -> None:
        batch.total_count = len(items)
        batch.included_count = sum(bool(item.included) for item in items)
        batch.excluded_count = batch.total_count - batch.included_count
        sent_count = sum(item.result == "sent" for item in items)
        failed = [
            item
            for item in items
            if item.result in {"failed", "intervention"}
        ]
        if sent_count:
            batch.status = (
                "sent_with_exclusions"
                if batch.excluded_count
                else "sent"
            )
        elif failed:
            batch.status = "failed"
        else:
            batch.status = "no_send"
        errors = list(
            dict.fromkeys(item.reason for item in failed if item.reason)
        )
        batch.last_error = "\n".join(errors)
        batch.completed_at = utcnow()

    def _send_batch(
        self,
        rows: list[TechExpertNotification],
        batch: TechExpertNotificationBatch,
        batch_items: dict[int, TechExpertNotificationBatchItem],
    ) -> int:
        candidates: list[TechExpertNotification] = []
        for row in rows:
            if self._prepare_batch_candidate(row):
                candidates.append(row)
                self._record_batch_item(
                    batch_items[row.id],
                    row,
                    included=True,
                    result="ready",
                    reason=(
                        "HR подтверждён, работник состоит в маркерной группе AD"
                    ),
                )
            else:
                self._record_batch_item(
                    batch_items[row.id],
                    row,
                    included=False,
                )
        if not candidates:
            self._finalize_batch(batch, list(batch_items.values()))
            return 0

        # Кадровые данные перечитываются для всего списка непосредственно
        # после проверки группы AD и перед единственной SMTP-отправкой.
        self.db.flush()
        self.db.expire_all()
        final_rows: list[TechExpertNotification] = []
        for row in candidates:
            if self._recheck_batch_candidate(row):
                final_rows.append(row)
            else:
                self._record_batch_item(
                    batch_items[row.id],
                    row,
                    included=False,
                )
        if not final_rows:
            self._finalize_batch(batch, list(batch_items.values()))
            return 0

        final_rows.sort(
            key=lambda item: (
                normalize(item.fio),
                normalize(item.corporate_email),
                item.id or 0,
            )
        )
        context = build_techexpert_template_context(
            [self._template_employee(row) for row in final_rows]
        )
        try:
            profile = get_domain_mail_profile(
                self.db,
                self.settings,
                self.source_domain,
            )
            subject = render_mail_template(
                self.config.subject,
                context,
                autoescape=False,
            )
            batch.subject = subject
            CredentialMailer(self.settings).send_html(
                recipient=normalize(self.config.recipient_email),
                subject=subject,
                body_html=render_mail_template(
                    self.config.body_html,
                    context,
                    autoescape=True,
                ),
                sender_email=profile.sender_email,
                sender_name=profile.sender_name,
            )
        except Exception as exc:
            for row in final_rows:
                self._mark_send_failure(row, exc)
                self._record_batch_item(
                    batch_items[row.id],
                    row,
                    included=True,
                    result="failed",
                )
            self._finalize_batch(batch, list(batch_items.values()))
            return 0

        sent_at = utcnow()
        for row in final_rows:
            row.status = "sent"
            row.sent_at = sent_at
            row.last_error = ""
            row.next_attempt_at = None
            row.updated_at = sent_at
            self._audit(
                row,
                action="techexpert_notification_sent",
                result="success",
                details=(
                    "Пакетный запрос на прекращение доступа отправлен; "
                    f"сотрудников в письме: {len(final_rows)}"
                ),
            )
            self._record_batch_item(
                batch_items[row.id],
                row,
                included=True,
                result="sent",
                reason="Включён в отправленное письмо",
            )
        batch.sent_at = sent_at
        self._finalize_batch(batch, list(batch_items.values()))
        return len(final_rows)

    @staticmethod
    def _datetime_due(value: datetime | None, now: datetime) -> bool:
        if value is None:
            return True
        return aware_utc(value) <= now

    def process(self) -> dict[str, int | str]:
        group_sync: dict[str, object] = {}
        if self.source_domain and str(self.config.ad_group_dn or "").strip():
            try:
                group_sync = TechExpertGroupAccessService(
                    self.settings,
                    self.db,
                    self.config,
                ).sync(actor="system")
            except Exception as exc:
                # Ошибка синхронизации не стирает последние кадровые отметки.
                logger.warning("Техэксперт: группа AD не синхронизирована: %s", exc)
        if not self.config.enabled:
            return {
                "status": "disabled",
                "planned": 0,
                "sent": 0,
                "group_members": int(group_sync.get("members", 0) or 0),
            }
        if self.settings.dry_run:
            return {
                "status": "dry_run",
                "planned": 0,
                "sent": 0,
                "group_members": int(group_sync.get("members", 0) or 0),
            }
        configuration_error = self._configuration_error()
        if configuration_error:
            return {
                "status": "misconfigured",
                "missing": configuration_error,
                "planned": 0,
                "sent": 0,
            }

        stale_rows = list(
            self.db.scalars(
                select(TechExpertNotification).where(
                    TechExpertNotification.status.in_(OPEN_STATUSES),
                    TechExpertNotification.source_id != self.source_domain,
                )
            ).all()
        )
        for row in stale_rows:
            self._mark_cancelled(
                row,
                "Организация Техэксперта изменена в настройках",
            )

        events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent).where(
                    HREmploymentDismissalEvent.source_id == self.source_domain
                )
            ).all()
        )
        rows: list[TechExpertNotification] = []
        for event in events:
            row = self._ensure_notification(
                event,
                self._employment_state(event),
            )
            if row is not None:
                rows.append(row)
        self.db.commit()

        now = utcnow()
        due_rows = [
            row
            for row in rows
            if row.status in OPEN_STATUSES
            and self._datetime_due(row.scheduled_for, now)
            and self._datetime_due(row.next_attempt_at, now)
        ]
        if due_rows:
            batch, batch_items = self._create_batch(due_rows)
            sent = self._send_batch(due_rows, batch, batch_items)
        else:
            sent = 0
        self.db.commit()
        return {
            "status": "ok",
            "planned": len(rows),
            "sent": sent,
            "emails": 1 if sent else 0,
            "group_members": int(group_sync.get("members", 0) or 0),
        }


class TechExpertLifecycleWorker:
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
            name="techexpert-lifecycle",
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
                result = TechExpertLifecycleService(
                    self.settings,
                    db,
                ).process()
                if result.get("sent"):
                    logger.info(
                        "Техэксперт: отправлено уведомлений %s",
                        result["sent"],
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка уведомительного контура Техэксперта")
            try:
                config = ensure_techexpert_settings(db)
                queued_sent = TechExpertRegistrationService(
                    self.settings,
                    db,
                    config,
                ).process_due_queue(actor="system")
                if queued_sent:
                    logger.info(
                        "Техэксперт: отправлено заявок из очереди %s",
                        queued_sent,
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка очереди заявок Техэксперта")

    def _run_loop(self) -> None:
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
