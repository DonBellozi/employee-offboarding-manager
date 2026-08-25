from __future__ import annotations

import json
from datetime import date, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    ADProvisioningOperation,
    AuditLog,
    BlockingOperation,
    BlockingQueueItem,
    DismissalSchedule,
    EmailLoginMapping,
    ProvisioningOperation,
)
from app.models_notifications import (
    DismissalEquipmentNotice,
    HREmploymentDismissalEvent,
)
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import TechExpertNotification
from app.models_zimbra_lifecycle import ZimbraEmploymentAction
from app.models_dismissal_lifecycle import (
    ADReactivationAlert,
    DismissalDetailsSnapshot,
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.routers.employees import (
    _ad_provisioning_journal_item,
    _blocking_journal_item,
    _dismissal_journal_item,
    _provisioning_journal_item,
)
from app.security import (
    get_current_user,
    get_or_create_csrf,
    require_operator,
    validate_csrf,
)
from app.services.ad_reactivation_alerts import ADReactivationAlertService
from app.services.upcoming_dismissals import (
    DEFERRAL_ACTION,
    UpcomingDismissalService,
)
from app.services.dismissal_details_cache import DismissalDetailsCacheService
from app.services.employee_arrivals import (
    NOT_REQUIRED_ACTION,
    EmployeeArrivalService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(
    Jinja2Templates(directory="app/templates")
)


def _context(request: Request, **kwargs):
    user = get_current_user(request)
    return {
        "user": user,
        "csrf": get_or_create_csrf(request),
        **kwargs,
    }


def _deferral_journal_item(event: AuditLog) -> dict[str, object]:
    try:
        payload = json.loads(event.details or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    dismissal_date = str(payload.get("dismissal_date") or "")
    deferred_until = str(payload.get("deferred_until") or "")
    previous_until = str(payload.get("previous_deferred_until") or "")

    def display_date(value: str) -> str:
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            return value

    organization_names = [
        str(item.get("source_name") or item.get("source_id") or "").strip()
        for item in payload.get("organizations") or []
        if isinstance(item, dict)
    ]

    details = [
        ("ФИО", str(payload.get("fio") or "")),
        ("Логин AD", str(payload.get("login") or "")),
        ("Корпоративная почта", str(payload.get("corporate_email") or "")),
        ("Дата окончательного увольнения", display_date(dismissal_date)),
        ("Блокировка отложена до", display_date(deferred_until)),
        ("Организации", ", ".join(name for name in organization_names if name)),
    ]
    if previous_until:
        details.insert(
            5,
            ("Предыдущая отсрочка", display_date(previous_until)),
        )

    return {
        "kind": "dismissal",
        "record_id": event.id,
        "created_at": event.created_at,
        "action": "Отсрочка блокировки",
        "subject": str(payload.get("fio") or "Работник"),
        "login": str(payload.get("login") or ""),
        "corporate_email": str(payload.get("corporate_email") or ""),
        "personal_email": "",
        "mail_domain": "",
        "operator": event.actor,
        "status_key": "success",
        "status_label": "Отложено на 7 дней",
        "details": details,
        "error_message": "",
        "completed_at": None,
    }


def _dismissal_notice_journal_item(
    notice: DismissalEquipmentNotice,
) -> dict[str, object]:
    try:
        recipients = json.loads(notice.recipients_json or "[]")
    except (TypeError, json.JSONDecodeError):
        recipients = []
    if not isinstance(recipients, list):
        recipients = []

    sent = [
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict) and item.get("sent")
    ]
    pending = [
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict) and not item.get("sent")
    ]
    corporate_addresses = [
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict)
        and item.get("kind") == "corporate"
        and str(item.get("email") or "").strip()
    ]
    personal_address = next(
        (
            str(item.get("email") or "").strip()
            for item in recipients
            if isinstance(item, dict)
            and item.get("kind") == "personal"
        ),
        "",
    )

    labels = {
        "pending": ("running", "Ожидает отправки"),
        "partial": ("partial", "Отправлено частично"),
        "failed": ("failed", "Ошибка отправки"),
        "sent": ("success", "Отправлено"),
        "cancelled": ("partial", "Отменено"),
    }
    status_key, status_label = labels.get(
        notice.status,
        ("running", notice.status),
    )

    details = [
        ("ФИО", notice.fio),
        (
            "Дата окончательного увольнения",
            notice.dismissal_date.strftime("%d.%m.%Y"),
        ),
        ("Домен отправителя", notice.sender_domain),
        ("Отправлено на", ", ".join(value for value in sent if value)),
    ]
    if pending:
        details.append(
            ("Еще не отправлено", ", ".join(value for value in pending if value))
        )
    details.append(("Попыток", str(int(notice.attempts or 0))))

    return {
        "kind": "dismissal",
        "record_id": notice.id,
        "created_at": notice.created_at,
        "action": "Уведомление о возврате оборудования",
        "subject": notice.fio or "Работник",
        "login": "",
        "corporate_email": ", ".join(corporate_addresses),
        "personal_email": personal_address,
        "mail_domain": notice.sender_domain,
        "operator": "Система",
        "status_key": status_key,
        "status_label": status_label,
        "details": details,
        "error_message": notice.last_error,
        "completed_at": notice.sent_at or notice.cancelled_at,
    }


def _arrival_not_required_journal_item(event: AuditLog) -> dict[str, object]:
    try:
        payload = json.loads(event.details or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    organizations = [
        str(item.get("source_name") or item.get("source_id") or "").strip()
        for item in payload.get("organizations") or []
        if isinstance(item, dict)
    ]
    fio = str(payload.get("fio") or event.target or "Работник")
    return {
        "kind": "arrival",
        "record_id": event.id,
        "created_at": event.created_at,
        "action": "Регистрация не требуется",
        "subject": fio,
        "login": "",
        "corporate_email": "",
        "personal_email": "",
        "mail_domain": "",
        "operator": event.actor,
        "status_key": "success",
        "status_label": "Не требуется",
        "details": [
            ("ФИО", fio),
            ("Организации", ", ".join(value for value in organizations if value)),
            ("Область решения", "Только текущий эпизод занятости"),
        ],
        "error_message": "",
        "completed_at": event.created_at,
    }


def _techexpert_journal_item(
    row: TechExpertNotification,
) -> dict[str, object]:
    labels = {
        "pending": ("running", "Запланировано"),
        "deferred": ("partial", "Отложено"),
        "failed": ("failed", "Ошибка отправки"),
        "intervention": ("failed", "Требует проверки"),
        "sent": ("success", "Отправлено"),
        "skipped": ("success", "Не требуется"),
        "cancelled": ("partial", "Отменено"),
    }
    status_key, status_label = labels.get(
        row.status,
        ("running", row.status),
    )
    membership_labels = {
        "not_checked": "Не проверено",
        "member": "Состоит в группе",
        "not_member": "Не состоит в группе",
        "error": "Ошибка проверки",
    }
    details = [
        ("ФИО", row.fio),
        ("Организация", row.source_name or row.source_id),
        ("Подразделение", row.department),
        ("Корпоративная почта", row.corporate_email),
        ("Получатель", row.recipient_email),
        ("Логин AD", row.ad_login),
        (
            "Доступ по группе AD",
            membership_labels.get(row.membership_state, row.membership_state),
        ),
        ("Дата увольнения", row.dismissal_date.strftime("%d.%m.%Y")),
        ("Попыток", str(int(row.attempts or 0))),
    ]
    removal_labels = {
        "not_started": "Не выполнялось",
        "removed": "Удален из группы",
        "already_absent": "Уже отсутствовал в группе",
        "failed": "Ошибка удаления из группы",
    }
    details.append(
        (
            "Группа AD",
            removal_labels.get(
                row.group_removal_status,
                row.group_removal_status,
            ),
        )
    )
    if row.deferred_until is not None:
        details.append(
            ("Отсрочка до", row.deferred_until.strftime("%d.%m.%Y"))
        )

    return {
        "kind": "dismissal",
        "record_id": row.id,
        "created_at": row.created_at,
        "action": "Уведомление Техэксперта",
        "subject": row.fio or "Работник",
        "login": row.ad_login,
        "corporate_email": row.corporate_email,
        "personal_email": "",
        "mail_domain": row.source_id,
        "operator": "Система",
        "status_key": status_key,
        "status_label": status_label,
        "details": details,
        "error_message": row.last_error,
        "completed_at": row.sent_at or row.cancelled_at,
    }


def _final_dismissal_block_journal_item(
    run: FinalDismissalBlockRun,
    targets: list[FinalDismissalBlockTarget],
    *,
    mapped_ad_login: str = "",
) -> dict[str, object]:
    labels = {
        "pending": ("running", "Ожидает"),
        "running": ("running", "Выполняется"),
        "partial": ("partial", "Выполнено частично"),
        "success": ("success", "Выполнено"),
        "intervention": ("failed", "Требует вмешательства"),
        "cancelled": ("partial", "Отменено"),
    }
    status_key, status_label = labels.get(
        run.status,
        ("running", run.status),
    )

    target_labels = {
        "pending": "Ожидает блокировки",
        "completed": "Заблокирована системой",
        "already_completed": "Уже была заблокирована",
        "intervention": "Требует вмешательства",
        "cancelled": "Отменено",
    }

    blocking_systems = []
    for target in targets:
        label = (
            "Active Directory"
            if target.system == "ad"
            else "Zimbra"
        )
        blocking_systems.append(
            {
                "label": label,
                "result": target_labels.get(
                    target.status,
                    target.status,
                ),
                "timestamp": (
                    target.completed_at
                    or target.last_attempt_at
                ),
            }
        )

    ad_identifier = str(mapped_ad_login or "").strip() or next(
        (
            item.target_identifier
            for item in targets
            if item.system == "ad"
            and item.target_identifier
        ),
        "",
    )
    details = [
        ("ФИО", run.fio),
        (
            "Дата окончательного увольнения",
            run.dismissal_date.strftime("%d.%m.%Y"),
        ),
        (
            "Дата автоматической блокировки",
            run.effective_block_date.strftime("%d.%m.%Y")
            + " 19:10",
        ),
        (
            "Целей AD",
            str(len(targets)),
        ),
    ]

    return {
        "kind": "blocking",
        "record_id": run.id,
        "created_at": run.created_at,
        "action": "Автоблокировка при увольнении",
        "subject": run.fio or "Работник",
        "login": ad_identifier,
        "corporate_email": "",
        "personal_email": "",
        "mail_domain": "",
        "operator": "Система",
        "status_key": status_key,
        "status_label": status_label,
        "details": details,
        "blocking_systems": blocking_systems,
        "equipment_snapshot": [],
        "error_message": run.last_error,
        "completed_at": run.completed_at or run.cancelled_at,
    }


def _completed_dismissal_journal_item(
    run: FinalDismissalBlockRun,
    targets: list[FinalDismissalBlockTarget],
    *,
    notice: DismissalEquipmentNotice | None,
    techexpert_notifications: list[TechExpertNotification],
    deferral_items: list[dict[str, object]],
    snapshot: DismissalDetailsSnapshot | None,
    mapped_ad_login: str = "",
) -> dict[str, object]:
    """Итоговая карточка одного завершенного объекта увольнения."""
    block_item = _final_dismissal_block_journal_item(
        run,
        targets,
        mapped_ad_login=mapped_ad_login,
    )
    components: list[dict[str, object]] = [*deferral_items]
    if notice is not None:
        components.append(_dismissal_notice_journal_item(notice))
    components.extend(
        _techexpert_journal_item(item)
        for item in techexpert_notifications
    )
    components.append(block_item)
    components.sort(
        key=lambda item: item.get("completed_at") or item["created_at"]
    )

    corporate_email = next(
        (
            str(item.get("corporate_email") or "").strip()
            for item in components
            if str(item.get("corporate_email") or "").strip()
        ),
        "",
    )
    personal_email = next(
        (
            str(item.get("personal_email") or "").strip()
            for item in components
            if str(item.get("personal_email") or "").strip()
        ),
        "",
    )
    human_operators = [
        str(item.get("operator") or "").strip()
        for item in reversed(components)
        if str(item.get("operator") or "").strip()
        and str(item.get("operator") or "").strip().casefold()
        != "система"
    ]
    operator = human_operators[0] if human_operators else "Система"

    system_rows = (
        DismissalDetailsCacheService._valid_rows(snapshot.payload_json)
        if snapshot is not None
        else []
    )

    def system_row(label: str) -> dict[str, str] | None:
        return next(
            (row for row in system_rows if row["label"] == label),
            None,
        )

    def set_system_row(
        label: str,
        value: str,
        *,
        state: str,
        note: str = "",
    ) -> None:
        row = system_row(label)
        if row is None:
            system_rows.append(
                {
                    "label": label,
                    "value": value,
                    "state": state,
                    "note": note,
                }
            )
            return
        row.update(value=value, state=state, note=note)

    if notice is not None:
        notice_labels = {
            "pending": ("Ожидает отправки", "pending"),
            "partial": ("Отправлено частично", "warning"),
            "failed": ("Ошибка отправки", "error"),
            "sent": ("Отправлено", "success"),
            "cancelled": ("Отменено", "warning"),
        }
        notice_value, notice_state = notice_labels.get(
            notice.status,
            (notice.status or "Неизвестно", "neutral"),
        )
        set_system_row(
            "Письмо о возврате оборудования",
            notice_value,
            state=notice_state,
            note=notice.last_error,
        )

    if techexpert_notifications:
        membership_states = {
            item.membership_state for item in techexpert_notifications
        }
        notification_statuses = {
            item.status for item in techexpert_notifications
        }
        if "member" in membership_states:
            value = "Есть"
        elif membership_states == {"not_member"}:
            value = "Нет"
        else:
            value = "Проверено"
        if notification_statuses <= {"sent", "skipped"}:
            state = "success"
            note = "Уведомление обработано"
        elif notification_statuses.intersection(
            {"failed", "intervention"}
        ):
            state = "error"
            note = "Требует проверки"
        else:
            state = "warning"
            note = "Обработано частично"
        set_system_row(
            "Техэксперт",
            value,
            state=state,
            note=note,
        )
    successful_ad = next(
        (
            target
            for target in targets
            if target.system == "ad"
            and target.status in {"completed", "already_completed"}
        ),
        None,
    )
    if successful_ad is not None:
        set_system_row(
            "AD",
            str(block_item.get("login") or "")
            or successful_ad.target_identifier
            or "Учетная запись",
            state="warning",
            note="Заблокирован",
        )
    set_system_row(
        "Автоблокировка при увольнении",
        "Выполнена",
        state="success",
    )

    steps = [
        {
            "record_id": item["record_id"],
            "action": item["action"],
            "status_key": item["status_key"],
            "status_label": item["status_label"],
            "operator": item["operator"],
            "timestamp": item.get("completed_at") or item["created_at"],
            "details": item.get("details") or [],
            "blocking_systems": item.get("blocking_systems") or [],
            "error_message": item.get("error_message") or "",
        }
        for item in components
    ]
    errors = [
        f"{item['action']}: {item['error_message']}"
        for item in components
        if str(item.get("error_message") or "").strip()
    ]
    has_warnings = any(
        item.get("status_key") in {"failed", "partial", "running"}
        for item in components
    )

    return {
        "kind": "dismissal",
        "record_id": run.id,
        "created_at": run.completed_at or run.updated_at,
        "action": "Увольнение",
        "subject": run.fio or "Работник",
        "login": str(block_item.get("login") or ""),
        "corporate_email": corporate_email,
        "personal_email": personal_email,
        "mail_domain": "",
        "operator": operator,
        "status_key": "partial" if has_warnings else "success",
        "status_label": (
            "Завершено с предупреждениями"
            if has_warnings
            else "Завершено"
        ),
        "details": [
            ("ФИО", run.fio),
            ("Дата увольнения", run.dismissal_date.strftime("%d.%m.%Y")),
            (
                "Автоблокировка выполнена",
                (run.completed_at or run.updated_at).strftime("%d.%m.%Y %H:%M"),
            ),
            ("Логин AD", str(block_item.get("login") or "")),
            ("Корпоративная почта", corporate_email),
            ("Личная почта", personal_email),
        ],
        "dismissal_date": run.dismissal_date,
        "dismissal_system_rows": system_rows,
        "dismissal_steps": steps,
        "blocking_systems": [],
        "equipment_snapshot": [],
        "error_message": "\n".join(errors),
        "completed_at": run.completed_at,
    }


def _organization_dismissal_journal_item(
    events: list[HREmploymentDismissalEvent],
    *,
    notice: DismissalEquipmentNotice | None,
    techexpert_notifications: list[TechExpertNotification],
    snapshot: DismissalDetailsSnapshot | None,
    mapped_ad_login: str = "",
) -> dict[str, object]:
    """Итог одной организации, когда общая AD-учетка остается активной."""
    event_date = max(
        event.current_dismissal_date or event.first_dismissal_date
        for event in events
    )
    fio = next((event.fio for event in events if event.fio), "Работник")
    organization_names = list(
        dict.fromkeys(
            str(event.source_name or event.source_id).strip()
            for event in events
            if str(event.source_name or event.source_id).strip()
        )
    )
    components: list[dict[str, object]] = []
    if notice is not None:
        components.append(_dismissal_notice_journal_item(notice))
    components.extend(
        _techexpert_journal_item(item)
        for item in techexpert_notifications
    )
    components.sort(key=lambda item: item.get("completed_at") or item["created_at"])
    completed_at = max(
        [
            *[event.updated_at for event in events],
            *[
                item.get("completed_at") or item["created_at"]
                for item in components
            ],
        ]
    )

    try:
        recipients = json.loads(notice.recipients_json or "[]") if notice else []
    except (TypeError, json.JSONDecodeError):
        recipients = []
    if not isinstance(recipients, list):
        recipients = []
    corporate_email = ", ".join(
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict)
        and item.get("kind") == "corporate"
        and str(item.get("email") or "").strip()
    )
    personal_email = next(
        (
            str(item.get("email") or "").strip()
            for item in recipients
            if isinstance(item, dict)
            and item.get("kind") == "personal"
            and str(item.get("email") or "").strip()
        ),
        "",
    )
    system_rows = (
        DismissalDetailsCacheService._valid_rows(snapshot.payload_json)
        if snapshot is not None
        else []
    )
    auto_row = next(
        (
            row
            for row in system_rows
            if row["label"] == "Автоблокировка при увольнении"
        ),
        None,
    )
    if auto_row is None:
        system_rows.append(
            {
                "label": "Автоблокировка при увольнении",
                "value": "Не требуется",
                "state": "neutral",
                "note": "Работа в другой организации продолжается",
            }
        )
    else:
        auto_row.update(
            value="Не требуется",
            state="neutral",
            note="Работа в другой организации продолжается",
        )

    steps = [
        {
            "record_id": event.id,
            "action": "Кадровое подтверждение",
            "status_key": "success",
            "status_label": "Подтверждено",
            "operator": "Система",
            "timestamp": event.updated_at,
            "details": [
                ("Организация", event.source_name or event.source_id),
                (
                    "Дата увольнения",
                    (
                        event.current_dismissal_date
                        or event.first_dismissal_date
                    ).strftime("%d.%m.%Y"),
                ),
            ],
            "blocking_systems": [],
            "error_message": "",
        }
        for event in events
    ]
    steps.extend(
        {
            "record_id": item["record_id"],
            "action": item["action"],
            "status_key": item["status_key"],
            "status_label": item["status_label"],
            "operator": item["operator"],
            "timestamp": item.get("completed_at") or item["created_at"],
            "details": item.get("details") or [],
            "blocking_systems": item.get("blocking_systems") or [],
            "error_message": item.get("error_message") or "",
        }
        for item in components
    )
    steps.sort(key=lambda item: item["timestamp"])
    errors = [
        f"{item['action']}: {item['error_message']}"
        for item in components
        if str(item.get("error_message") or "").strip()
    ]
    has_warnings = bool(errors) or any(
        item.get("status_key") in {"failed", "partial", "running"}
        for item in components
    )
    return {
        "kind": "dismissal",
        "record_id": events[0].id,
        "created_at": completed_at,
        "action": "Увольнение",
        "subject": fio,
        "login": mapped_ad_login,
        "corporate_email": corporate_email,
        "personal_email": personal_email,
        "mail_domain": "",
        "operator": "Система",
        "status_key": "partial" if has_warnings else "success",
        "status_label": (
            "Завершено с предупреждениями" if has_warnings else "Завершено"
        ),
        "details": [
            ("ФИО", fio),
            ("Дата увольнения", event_date.strftime("%d.%m.%Y")),
            ("Организации", ", ".join(organization_names)),
            ("Общая блокировка", "Не требуется"),
            ("Логин AD", mapped_ad_login),
            ("Корпоративная почта", corporate_email),
            ("Личная почта", personal_email),
        ],
        "dismissal_date": event_date,
        "dismissal_system_rows": system_rows,
        "dismissal_steps": steps,
        "blocking_systems": [],
        "equipment_snapshot": [],
        "error_message": "\n".join(errors),
        "completed_at": completed_at,
    }


def _preferred_mapped_ad_login(
    mappings: list[EmailLoginMapping],
    targets: (
        list[FinalDismissalBlockTarget]
        | tuple[FinalDismissalBlockTarget, ...]
    ) = (),
) -> str:
    """Вернуть подтвержденный AD-логин для строки журнала.

    Сначала учитывается objectGUID фактической цели. Это позволяет правильно
    показывать и старые операции, в которых target_identifier был заполнен
    кадровым логином до применения явного исключения.
    """

    target_guids = {
        str(target.stable_id or "").strip().casefold()
        for target in targets
        if target.system == "ad" and str(target.stable_id or "").strip()
    }
    exact_logins = {
        str(mapping.ad_login or "").strip().casefold()
        for mapping in mappings
        if str(mapping.ad_object_guid or "").strip().casefold()
        in target_guids
        and str(mapping.ad_login or "").strip()
    }
    if len(exact_logins) == 1:
        return next(iter(exact_logins))

    mapped_logins = {
        str(mapping.ad_login or "").strip().casefold()
        for mapping in mappings
        if str(mapping.ad_login or "").strip()
    }
    return next(iter(mapped_logins)) if len(mapped_logins) == 1 else ""

def _journal_items(
    db: Session,
    *,
    today: date | None = None,
    timezone_name: str = "UTC",
) -> list[dict[str, object]]:
    today = today or date.today()
    provisioning_operations = db.scalars(
        select(ProvisioningOperation)
        .order_by(desc(ProvisioningOperation.created_at))
        .limit(50)
    ).all()
    dismissal_operations = db.scalars(
        select(DismissalSchedule)
        .order_by(desc(DismissalSchedule.created_at))
        .limit(50)
    ).all()
    ad_provisioning_operations = db.scalars(
        select(ADProvisioningOperation)
        .order_by(desc(ADProvisioningOperation.created_at))
        .limit(50)
    ).all()
    blocking_operations = db.scalars(
        select(BlockingOperation)
        .order_by(desc(BlockingOperation.created_at))
        .limit(50)
    ).all()
    blocking_operation_ids = [item.id for item in blocking_operations]
    blocking_queue_items = (
        db.scalars(
            select(BlockingQueueItem).where(
                BlockingQueueItem.operation_id.in_(blocking_operation_ids)
            )
        ).all()
        if blocking_operation_ids
        else []
    )
    queue_by_operation: dict[int, list[BlockingQueueItem]] = {}
    for queue_item in blocking_queue_items:
        queue_by_operation.setdefault(
            queue_item.operation_id,
            [],
        ).append(queue_item)

    successful_final_block_runs = db.scalars(
        select(FinalDismissalBlockRun)
        .where(FinalDismissalBlockRun.status == "success")
        .order_by(
            desc(FinalDismissalBlockRun.completed_at),
            desc(FinalDismissalBlockRun.id),
        )
        .limit(200)
    ).all()
    arrival_not_required_events = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == NOT_REQUIRED_ACTION)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(50)
    ).all()
    try:
        local_zone = ZoneInfo(timezone_name)
    except Exception:
        local_zone = timezone.utc

    def completed_before_today(run: FinalDismissalBlockRun) -> bool:
        value = run.completed_at
        if value is None:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(local_zone).date() < today

    final_block_runs = [
        run
        for run in successful_final_block_runs
        if completed_before_today(run)
    ][:50]
    final_block_run_ids = [item.id for item in final_block_runs]
    final_block_targets = (
        db.scalars(
            select(FinalDismissalBlockTarget).where(
                FinalDismissalBlockTarget.run_id.in_(
                    final_block_run_ids
                )
            )
        ).all()
        if final_block_run_ids
        else []
    )
    final_targets_by_run: dict[
        int,
        list[FinalDismissalBlockTarget],
    ] = {}
    for target in final_block_targets:
        final_targets_by_run.setdefault(
            target.run_id,
            [],
        ).append(target)

    mappings_for_completed = (
        list(
            db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key.in_(
                        {item.worker_key for item in final_block_runs}
                    )
                )
            ).all()
        )
        if final_block_runs
        else []
    )
    mappings_by_worker: dict[str, list[EmailLoginMapping]] = {}
    for mapping in mappings_for_completed:
        mappings_by_worker.setdefault(mapping.worker_key, []).append(mapping)

    completed_keys = {
        (item.worker_key, item.dismissal_date)
        for item in final_block_runs
    }
    completed_worker_keys = {
        item.worker_key for item in final_block_runs
    }
    dismissal_notices = (
        list(
            db.scalars(
                select(DismissalEquipmentNotice)
                .where(
                    DismissalEquipmentNotice.worker_key.in_(
                        completed_worker_keys
                    )
                )
                .order_by(
                    desc(DismissalEquipmentNotice.created_at),
                    desc(DismissalEquipmentNotice.id),
                )
            ).all()
        )
        if completed_worker_keys
        else []
    )
    techexpert_notifications = (
        list(
            db.scalars(
                select(TechExpertNotification)
                .where(
                    TechExpertNotification.worker_key.in_(
                        completed_worker_keys
                    )
                )
                .order_by(
                    desc(TechExpertNotification.created_at),
                    desc(TechExpertNotification.id),
                )
            ).all()
        )
        if completed_worker_keys
        else []
    )
    deferral_events = (
        list(
            db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action == DEFERRAL_ACTION,
                    AuditLog.target.in_(completed_worker_keys),
                )
                .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
            ).all()
        )
        if completed_worker_keys
        else []
    )
    notice_by_key = {
        (item.worker_key, item.dismissal_date): item
        for item in dismissal_notices
        if (item.worker_key, item.dismissal_date) in completed_keys
    }
    linked_event_ids: set[int] = set()
    notice_event_ids: dict[int, list[int]] = {}
    for notice in dismissal_notices:
        try:
            raw_event_ids = json.loads(notice.event_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_event_ids = []
        event_ids = [
            int(value) for value in raw_event_ids if str(value).isdigit()
        ]
        notice_event_ids[notice.id] = event_ids
        linked_event_ids.update(event_ids)
    linked_events = (
        list(
            db.scalars(
                select(HREmploymentDismissalEvent).where(
                    HREmploymentDismissalEvent.id.in_(linked_event_ids)
                )
            ).all()
        )
        if linked_event_ids
        else []
    )
    event_by_id = {event.id: event for event in linked_events}
    for notice in dismissal_notices:
        for event_id in notice_event_ids.get(notice.id, []):
            event = event_by_id.get(event_id)
            if event is None:
                continue
            event_date = (
                event.current_dismissal_date
                or event.first_dismissal_date
            )
            key = (event.worker_key, event_date)
            if key in completed_keys:
                notice_by_key[key] = notice
    techexpert_by_key: dict[
        tuple[str, date],
        list[TechExpertNotification],
    ] = {}
    for item in techexpert_notifications:
        key = (item.worker_key, item.dismissal_date)
        if key in completed_keys:
            techexpert_by_key.setdefault(key, []).append(item)

    deferrals_by_key: dict[
        tuple[str, date],
        list[dict[str, object]],
    ] = {}
    for event in deferral_events:
        try:
            payload = json.loads(event.details or "{}")
            dismissal_date = date.fromisoformat(
                str(payload.get("dismissal_date") or "")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        key = (str(event.target or "").strip(), dismissal_date)
        if key in completed_keys:
            deferrals_by_key.setdefault(key, []).append(
                _deferral_journal_item(event)
            )

    snapshots = (
        list(
            db.scalars(
                select(DismissalDetailsSnapshot).where(
                    DismissalDetailsSnapshot.worker_key.in_(
                        completed_worker_keys
                    )
                )
            ).all()
        )
        if completed_worker_keys
        else []
    )
    snapshot_by_key = {
        (item.worker_key, item.dismissal_date): item
        for item in snapshots
        if (item.worker_key, item.dismissal_date) in completed_keys
    }
    completed_dismissal_items = [
        _completed_dismissal_journal_item(
            run,
            final_targets_by_run.get(run.id, []),
            notice=notice_by_key.get((run.worker_key, run.dismissal_date)),
            techexpert_notifications=techexpert_by_key.get(
                (run.worker_key, run.dismissal_date),
                [],
            ),
            deferral_items=deferrals_by_key.get(
                (run.worker_key, run.dismissal_date),
                [],
            ),
            snapshot=snapshot_by_key.get(
                (run.worker_key, run.dismissal_date)
            ),
            mapped_ad_login=_preferred_mapped_ad_login(
                mappings_by_worker.get(run.worker_key, []),
                final_targets_by_run.get(run.id, []),
            ),
        )
        for run in final_block_runs
    ]

    latest_events_by_source: dict[
        tuple[str, str],
        HREmploymentDismissalEvent,
    ] = {}
    for event in db.scalars(
        select(HREmploymentDismissalEvent).order_by(
            HREmploymentDismissalEvent.sequence,
            HREmploymentDismissalEvent.id,
        )
    ).all():
        latest_events_by_source[(event.worker_key, event.source_id)] = event
    active_worker_keys = set(
        db.scalars(
            select(HREmploymentState.worker_key).where(
                HREmploymentState.status == "active"
            )
        ).all()
    )
    organization_event_groups: dict[
        tuple[str, date],
        list[HREmploymentDismissalEvent],
    ] = {}
    for event in latest_events_by_source.values():
        event_date = event.current_dismissal_date
        if (
            event.worker_key not in active_worker_keys
            or event.status not in {"open", "absent"}
            or event_date is None
            or event_date >= today
        ):
            continue
        key = (event.worker_key, event_date)
        if key in completed_keys:
            continue
        organization_event_groups.setdefault(key, []).append(event)

    organization_dismissal_items: list[dict[str, object]] = []
    organization_worker_keys = {
        worker_key for worker_key, _ in organization_event_groups
    }
    missing_mapping_keys = organization_worker_keys.difference(
        mappings_by_worker
    )
    if missing_mapping_keys:
        for mapping in db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key.in_(missing_mapping_keys)
            )
        ).all():
            mappings_by_worker.setdefault(
                mapping.worker_key,
                [],
            ).append(mapping)
    for (worker_key, event_date), events in list(
        organization_event_groups.items()
    )[:50]:
        event_ids = {event.id for event in events}
        worker_notices = list(
            db.scalars(
                select(DismissalEquipmentNotice)
                .where(DismissalEquipmentNotice.worker_key == worker_key)
                .order_by(
                    desc(DismissalEquipmentNotice.created_at),
                    desc(DismissalEquipmentNotice.id),
                )
            ).all()
        )
        notice = None
        for candidate_notice in worker_notices:
            try:
                raw_ids = json.loads(candidate_notice.event_ids_json or "[]")
            except (TypeError, json.JSONDecodeError):
                raw_ids = []
            candidate_ids = {
                int(value) for value in raw_ids if str(value).isdigit()
            }
            if event_ids.intersection(candidate_ids):
                notice = candidate_notice
                break
        if notice is None:
            notice = next(
                (
                    candidate_notice
                    for candidate_notice in worker_notices
                    if candidate_notice.dismissal_date == event_date
                ),
                None,
            )
        organization_dismissal_items.append(
            _organization_dismissal_journal_item(
                events,
                notice=notice,
                techexpert_notifications=list(
                    db.scalars(
                        select(TechExpertNotification).where(
                            TechExpertNotification.worker_key == worker_key,
                            TechExpertNotification.dismissal_date == event_date,
                        )
                    ).all()
                ),
                snapshot=db.scalar(
                    select(DismissalDetailsSnapshot).where(
                        DismissalDetailsSnapshot.worker_key == worker_key,
                        DismissalDetailsSnapshot.dismissal_date == event_date,
                    )
                ),
                mapped_ad_login=_preferred_mapped_ad_login(
                    mappings_by_worker.get(worker_key, [])
                ),
            )
        )

    items = [
        *(
            _provisioning_journal_item(item)
            for item in provisioning_operations
        ),
        *(
            _ad_provisioning_journal_item(item)
            for item in ad_provisioning_operations
        ),
        *(
            _blocking_journal_item(
                item,
                queue_by_operation.get(item.id, []),
            )
            for item in blocking_operations
        ),
        *(
            _dismissal_journal_item(item)
            for item in dismissal_operations
        ),
        *(
            _arrival_not_required_journal_item(item)
            for item in arrival_not_required_events
        ),
        *completed_dismissal_items,
        *organization_dismissal_items,
    ]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:50]


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    dismissal_error = request.query_params.get("dismissal_error", "")
    upcoming_service = UpcomingDismissalService(settings, db)
    dashboard_today = upcoming_service.today
    try:
        upcoming = upcoming_service.list_upcoming(limit=20)
    except Exception as exc:
        db.rollback()
        upcoming = []
        dismissal_error = str(exc)

    employee_error = request.query_params.get("employee_error", "")
    try:
        new_employee_groups = EmployeeArrivalService(db).list_pending(limit=50)
    except Exception as exc:
        db.rollback()
        new_employee_groups = []
        employee_error = str(exc)

    ad_reactivation_alerts = list(
        db.scalars(
            select(ADReactivationAlert)
            .where(ADReactivationAlert.status == "open")
            .order_by(
                desc(ADReactivationAlert.updated_at),
                desc(ADReactivationAlert.id),
            )
        ).all()
    )
    for alert in ad_reactivation_alerts:
        try:
            candidates = json.loads(alert.candidates_json or "[]")
        except (TypeError, json.JSONDecodeError):
            candidates = []
        alert.candidate_rows = candidates if isinstance(candidates, list) else []
    zimbra_attention_actions = list(
        db.scalars(
            select(ZimbraEmploymentAction)
            .where(
                ZimbraEmploymentAction.status.in_(["intervention", "failed"])
            )
            .order_by(
                desc(ZimbraEmploymentAction.updated_at),
                desc(ZimbraEmploymentAction.id),
            )
        ).all()
    )
    techexpert_attention_notifications = list(
        db.scalars(
            select(TechExpertNotification)
            .where(
                (
                    TechExpertNotification.status.in_(
                        ["failed", "intervention"]
                    )
                )
                | (TechExpertNotification.attention_state != "")
            )
            .order_by(
                desc(TechExpertNotification.updated_at),
                desc(TechExpertNotification.id),
            )
        ).all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            journal_items=_journal_items(
                db,
                today=dashboard_today,
                timezone_name=settings.app_timezone,
            ),
            ad_reactivation_alerts=ad_reactivation_alerts,
            zimbra_attention_actions=zimbra_attention_actions,
            techexpert_attention_notifications=(
                techexpert_attention_notifications
            ),
            new_employee_groups=new_employee_groups,
            employee_message=request.query_params.get("employee_message", ""),
            employee_error=employee_error,
            attention_message=request.query_params.get(
                "attention_message",
                "",
            ),
            attention_error=request.query_params.get(
                "attention_error",
                "",
            ),
            upcoming_dismissals=upcoming,
            dismissal_message=request.query_params.get(
                "dismissal_message",
                "",
            ),
            dismissal_error=dismissal_error,
            dry_run=settings.dry_run,
        ),
    )


@router.get("/dismissals/upcoming/fragment")
def upcoming_dismissals_fragment(
    request: Request,
    message: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Обновить только виджет ближайших увольнений без перезагрузки журнала."""
    get_current_user(request)
    try:
        upcoming = UpcomingDismissalService(
            settings,
            db,
        ).list_upcoming(limit=20)
        dismissal_error = error
    except Exception as exc:
        db.rollback()
        upcoming = []
        dismissal_error = str(exc)

    return templates.TemplateResponse(
        request,
        "upcoming_dismissals_fragment.html",
        _context(
            request,
            upcoming_dismissals=upcoming,
            dismissal_message=message,
            dismissal_error=dismissal_error,
        ),
    )


@router.post("/ad-reactivation-alerts/{alert_id}/refresh")
def refresh_reactivated_ad(
    alert_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        alert, candidates = ADReactivationAlertService(
            settings,
            db,
        ).refresh(alert_id=alert_id, actor=current.username)
        message = (
            f"{alert.fio or alert.ad_login}: найдено учетных записей AD — "
            f"{len(candidates)}"
        )
        return RedirectResponse(
            f"/?attention_message={quote_plus(message)}#operator-attention",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/?attention_error={quote_plus(str(exc))}#operator-attention",
            status_code=303,
        )


@router.post("/ad-reactivation-alerts/{alert_id}/restore")
def restore_reactivated_ad(
    alert_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        alert = ADReactivationAlertService(
            settings,
            db,
        ).restore(alert_id=alert_id, actor=current.username)
        message = (
            "DRY_RUN: учетная запись AD не изменена"
            if settings.dry_run
            else f"AD восстановлен для {alert.fio or alert.ad_login}"
        )
        return RedirectResponse(
            f"/?attention_message={quote_plus(message)}#operator-attention",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/?attention_error={quote_plus(str(exc))}#operator-attention",
            status_code=303,
        )


@router.post("/ad-reactivation-alerts/{alert_id}/restore-candidate")
def restore_reactivated_ad_candidate(
    alert_id: int,
    request: Request,
    ad_login: str = Form(""),
    ad_object_guid: str = Form(""),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        alert = ADReactivationAlertService(
            settings,
            db,
        ).restore_candidate(
            alert_id=alert_id,
            ad_login=ad_login,
            ad_object_guid=ad_object_guid,
            actor=current.username,
        )
        message = (
            "DRY_RUN: учетная запись AD не изменена"
            if settings.dry_run
            else f"AD восстановлен для {alert.fio or alert.ad_login}"
        )
        return RedirectResponse(
            f"/?attention_message={quote_plus(message)}#operator-attention",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/?attention_error={quote_plus(str(exc))}#operator-attention",
            status_code=303,
        )


@router.post("/employees/arrivals/not-required")
def mark_employee_arrival_not_required(
    request: Request,
    arrival_event_ids: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    try:
        result = EmployeeArrivalService(db).mark_not_required(
            arrival_event_ids,
            operator=user.username,
        )
        message = f"Для {result['fio']} регистрация отмечена как ненужная"
        return RedirectResponse(
            f"/?employee_message={quote_plus(message)}#new-employees",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/?employee_error={quote_plus(str(exc))}#new-employees",
            status_code=303,
        )


@router.get("/dismissals/upcoming/details")
def upcoming_dismissal_details(
    request: Request,
    worker_key: str,
    dismissal_date: date,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Показать последний фоновый снимок без внешних запросов."""
    get_current_user(request)
    status_code = 200
    details = None
    details_error = ""
    try:
        candidate = UpcomingDismissalService(
            settings,
            db,
        ).get_upcoming(
            worker_key=worker_key,
            expected_dismissal_date=dismissal_date,
        )
        details = DismissalDetailsCacheService(settings, db).view(candidate)
    except Exception as exc:
        db.rollback()
        details_error = str(exc)
        status_code = 400

    return templates.TemplateResponse(
        request,
        "upcoming_dismissal_details.html",
        _context(
            request,
            details=details,
            details_error=details_error,
        ),
        status_code=status_code,
    )


@router.post("/dismissals/upcoming/defer")
def defer_upcoming_dismissal(
    request: Request,
    worker_key: str = Form(...),
    dismissal_date: date = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    try:
        result = UpcomingDismissalService(
            settings,
            db,
        ).defer(
            worker_key=worker_key,
            expected_dismissal_date=dismissal_date,
            operator_username=user.username,
        )
        if result.get("preliminary"):
            message = (
                f"Если увольнение {result['fio']} подтвердится, блокировка "
                f"будет отложена до "
                f"{result['deferred_until'].strftime('%d.%m.%Y')}"
            )
        else:
            message = (
                f"Блокировка для {result['fio']} отложена до "
                f"{result['deferred_until'].strftime('%d.%m.%Y')}"
            )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"ok": True, "message": message},
                status_code=200,
            )
        return RedirectResponse(
            f"/?dismissal_message={quote_plus(message)}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse(
            f"/?dismissal_error={quote_plus(str(exc))}",
            status_code=303,
        )
