from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import HRSourceRecord
from app.models_dismissal_lifecycle import FinalDismissalBlockRun
from app.models_notifications import (
    DismissalEquipmentNotice,
    HREmploymentDismissalEvent,
)
from app.models_techexpert import TechExpertSettings
from app.services.ad import ActiveDirectoryService
from app.services.blocking import BlockingCard, BlockingService
from app.services.dismissal_notifications import DismissalNotificationService


class DismissalDetailsService:
    """Собирает read-only состояние систем для фонового снимка увольнения."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def _format_datetime(self, value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            zone = ZoneInfo(self.settings.app_timezone)
        except Exception:
            zone = timezone.utc
        return value.astimezone(zone).strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _row(
        label: str,
        value: str,
        *,
        state: str = "neutral",
        note: str = "",
    ) -> dict[str, str]:
        return {
            "label": label,
            "value": value,
            "state": state,
            "note": note,
        }

    @staticmethod
    def _recipient_timestamps(notice: DismissalEquipmentNotice) -> list[datetime]:
        try:
            recipients = json.loads(notice.recipients_json or "[]")
        except (TypeError, json.JSONDecodeError):
            recipients = []
        result: list[datetime] = []
        for recipient in recipients if isinstance(recipients, list) else []:
            if not isinstance(recipient, dict):
                continue
            raw = str(recipient.get("sent_at") or "").strip()
            if not raw:
                continue
            try:
                result.append(datetime.fromisoformat(raw))
            except ValueError:
                continue
        return result

    @staticmethod
    def _notice_event_ids(notice: DismissalEquipmentNotice) -> set[int]:
        try:
            values = json.loads(notice.event_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        return {
            int(value)
            for value in values if str(value).strip().isdigit()
        }

    def _equipment_notice(self, candidate: dict) -> dict[str, str]:
        source_ids = {
            str(item.get("source_id") or "").strip().lower()
            for item in candidate.get("organizations") or []
        }
        events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent)
                .where(
                    HREmploymentDismissalEvent.worker_key
                    == candidate["worker_key"]
                )
                .order_by(
                    HREmploymentDismissalEvent.source_id,
                    desc(HREmploymentDismissalEvent.sequence),
                )
            ).all()
        )
        latest_event_ids: set[int] = set()
        seen_sources: set[str] = set()
        for event in events:
            source_id = str(event.source_id or "").strip().lower()
            if source_id not in source_ids or source_id in seen_sources:
                continue
            seen_sources.add(source_id)
            latest_event_ids.add(event.id)

        notices = list(self.db.scalars(
            select(DismissalEquipmentNotice)
            .where(
                DismissalEquipmentNotice.worker_key
                == candidate["worker_key"]
            )
            .order_by(
                desc(DismissalEquipmentNotice.created_at),
                desc(DismissalEquipmentNotice.id),
            )
        ).all())
        notice = next(
            (
                item
                for item in notices
                if latest_event_ids.intersection(self._notice_event_ids(item))
            ),
            None,
        )
        if notice is None:
            notice = next(
                (
                    item
                    for item in notices
                    if item.dismissal_date == candidate["dismissal_date"]
                ),
                None,
            )
        if notice is None:
            status = DismissalNotificationService(
                self.settings,
                self.db,
            ).notice_creation_status(candidate)
            return self._row(
                "Письмо о возврате оборудования",
                status["value"],
                state=status["state"],
                note=status["note"],
            )

        sent_times = self._recipient_timestamps(notice)
        sent_at = notice.sent_at or (max(sent_times) if sent_times else None)
        timestamp = self._format_datetime(sent_at)
        labels = {
            "pending": ("Ожидает отправки", "pending"),
            "partial": ("Отправлено частично", "warning"),
            "failed": ("Ошибка отправки", "error"),
            "sent": ("Отправлено", "success"),
            "cancelled": ("Отменено", "warning"),
        }
        value, state = labels.get(
            notice.status,
            (notice.status or "Неизвестно", "neutral"),
        )
        if timestamp:
            value = f"{value} {timestamp}"
        return self._row(
            "Письмо о возврате оборудования",
            value,
            state=state,
            note=(notice.last_error or "").strip(),
        )

    def _preferred_record(self, worker_key: str) -> HRSourceRecord | None:
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == worker_key
                )
            ).all()
        )
        records.sort(
            key=lambda item: (
                not item.is_present,
                not bool(item.login.strip()),
                not bool(item.corporate_email.strip()),
                item.id,
            )
        )
        return records[0] if records else None

    def _blocking_card(self, worker_key: str) -> tuple[BlockingCard | None, str]:
        record = self._preferred_record(worker_key)
        if record is None or not record.is_present:
            return None, "Работник отсутствует в текущем кадровом реестре"
        try:
            return BlockingService(self.settings, self.db).card(
                record.id,
                remember_itinvent=False,
            ), ""
        except Exception as exc:
            self.db.rollback()
            return None, str(exc)

    def _itinvent(self, card: BlockingCard | None, error: str) -> dict[str, str]:
        if card is None:
            return self._row("IT Invent", "Не проверено", state="warning", note=error)
        if card.itinvent_state in {"found", "stale"} and card.itinvent is not None:
            count = len(card.itinvent.equipment)
            value = f"Есть — {count} шт." if count else "Отсутствует"
            note = ""
            if card.itinvent_state == "stale":
                note = "Показан последний успешный результат проверки"
            if card.itinvent_checked_at:
                note = " · ".join(
                    part for part in (note, f"Проверено {card.itinvent_checked_at}") if part
                )
            return self._row(
                "IT Invent",
                value,
                state="success" if count else "neutral",
                note=note,
            )
        if card.itinvent_state == "owner_not_found":
            return self._row("IT Invent", "Отсутствует")
        if card.itinvent_state == "not_configured":
            return self._row("IT Invent", "Не настроено")
        if card.itinvent_state == "no_login":
            return self._row("IT Invent", "Нет логина", state="warning")
        return self._row(
            "IT Invent",
            "Ошибка проверки",
            state="error",
            note=card.itinvent_error,
        )

    def _ad(self, card: BlockingCard | None, error: str) -> dict[str, str]:
        if not self.settings.ad_check_enabled:
            return self._row("AD", "Проверка отключена")
        if card is None:
            return self._row("AD", "Не проверено", state="warning", note=error)
        if card.ad_error:
            return self._row(
                "AD",
                "Ошибка проверки",
                state="error",
                note=card.ad_error,
            )
        if card.ad_user is None:
            return self._row("AD", "Отсутствует")
        return self._row(
            "AD",
            card.ad_user.username,
            state="warning" if card.ad_is_blocked else "success",
            note="Заблокирован" if card.ad_is_blocked else "Активен",
        )

    def _mail(self, card: BlockingCard | None, error: str) -> dict[str, str]:
        if not self.settings.zimbra_check_enabled:
            return self._row("Почта", "Проверка отключена")
        if card is None:
            return self._row("Почта", "Не проверено", state="warning", note=error)
        if card.zimbra_error:
            return self._row(
                "Почта",
                "Ошибка проверки",
                state="error",
                note=card.zimbra_error,
            )
        if card.zimbra is None:
            return self._row("Почта", "Отсутствует")
        status = (card.zimbra.account_status or "").strip().lower()
        status_label = {
            "active": "Активна",
            "locked": "Заблокирована",
            "closed": "Закрыта",
            "maintenance": "Обслуживание",
        }.get(status, status or "Существует")
        return self._row(
            "Почта",
            card.zimbra.login or card.zimbra.primary_email,
            state="success" if status == "active" else "warning",
            note=status_label,
        )

    def _techexpert(
        self,
        candidate: dict,
        card: BlockingCard | None,
        error: str,
    ) -> dict[str, str]:
        config = self.db.get(TechExpertSettings, 1)
        if config is None or not config.enabled or not config.ad_group_dn.strip():
            return self._row("Техэксперт", "Не настроено")

        source_ids = {
            str(item.get("source_id") or "").strip().lower()
            for item in candidate.get("organizations") or []
        }
        if config.source_domain.strip().lower() not in source_ids:
            return self._row(
                "Техэксперт",
                "Нет",
                note="Работник не относится к организации Техэксперта",
            )
        if not self.settings.ad_check_enabled:
            return self._row("Техэксперт", "Проверка отключена")
        if card is None:
            return self._row(
                "Техэксперт",
                "Не проверено",
                state="warning",
                note=error,
            )
        if card.ad_user is None:
            return self._row(
                "Техэксперт",
                "Нет",
                note=card.ad_error or "Учетная запись AD отсутствует",
            )
        try:
            is_member = ActiveDirectoryService(
                self.settings
            ).is_user_member_of_group(
                card.ad_user.username,
                config.ad_group_dn,
                object_guid=card.ad_user.object_guid,
            )
        except Exception as exc:
            return self._row(
                "Техэксперт",
                "Ошибка проверки",
                state="error",
                note=str(exc),
            )
        return self._row(
            "Техэксперт",
            "Есть" if is_member else "Нет",
            state="success" if is_member else "neutral",
            note="Состоит в маркерной группе AD" if is_member else "Не состоит в маркерной группе AD",
        )

    def _automatic_blocking(self, candidate: dict) -> dict[str, str]:
        if candidate.get("preliminary"):
            planned = (
                candidate["effective_block_date"].strftime("%d.%m.%Y")
                + " 19:10"
            )
            return self._row(
                "Автоблокировка при увольнении",
                "Ожидает кадрового подтверждения",
                state="pending",
                note=(
                    f"После подтверждения — не ранее {planned}. "
                    "До появления увольнения в кадровой выгрузке "
                    "учетные записи не блокируются."
                ),
            )
        if not candidate.get("blocking_required", True):
            return self._row(
                "Автоблокировка при увольнении",
                "Не требуется",
                note="У работника остается активная занятость в другой организации",
            )
        run = self.db.scalar(
            select(FinalDismissalBlockRun)
            .where(
                FinalDismissalBlockRun.worker_key == candidate["worker_key"],
                FinalDismissalBlockRun.dismissal_date == candidate["dismissal_date"],
            )
            .order_by(desc(FinalDismissalBlockRun.id))
            .limit(1)
        )
        planned = (
            candidate["effective_block_date"].strftime("%d.%m.%Y")
            + " 19:10"
        )
        if run is None:
            return self._row(
                "Автоблокировка при увольнении",
                f"Запланирована {planned}",
                state="pending",
            )
        if run.status == "success":
            completed = self._format_datetime(run.completed_at)
            return self._row(
                "Автоблокировка при увольнении",
                f"Выполнена {completed or planned}",
                state="success",
            )
        labels = {
            "pending": "Запланирована",
            "running": "Выполняется",
            "partial": "Выполнена частично",
            "intervention": "Требует вмешательства",
            "cancelled": "Отменена",
        }
        return self._row(
            "Автоблокировка при увольнении",
            f"{labels.get(run.status, run.status or 'Неизвестно')} {planned}",
            state=(
                "error"
                if run.status == "intervention"
                else "warning" if run.status in {"partial", "cancelled"} else "pending"
            ),
            note=(run.last_error or "").strip(),
        )

    def build(self, candidate: dict) -> dict:
        card, card_error = self._blocking_card(candidate["worker_key"])
        return {
            "fio": candidate["fio"],
            "dismissal_date": candidate["dismissal_date"],
            "organizations": candidate["organizations"],
            "rows": [
                self._equipment_notice(candidate),
                self._itinvent(card, card_error),
                self._ad(card, card_error),
                self._mail(card, card_error),
                self._techexpert(candidate, card, card_error),
                self._row(
                    "1С ДО",
                    "Разрабатывается",
                    state="pending",
                ),
                self._automatic_blocking(candidate),
            ],
        }
