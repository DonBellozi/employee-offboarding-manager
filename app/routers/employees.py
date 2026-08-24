from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import date
from urllib.parse import quote_plus

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
    ProvisioningOperation,
)
from app.time_utils import register_datetime_filters
from app.security import get_current_user, get_or_create_csrf, validate_csrf
from app.services.ad import ActiveDirectoryService
from app.services.blocking import BlockingService
from app.services.employee_arrivals import EmployeeArrivalService
from app.services.employee_arrival_accounts import (
    EmployeeArrivalAccountService,
)
from app.services.hr_registry import HRRegistryService
from app.services.names import build_login_candidates, parse_two_line_input, validate_person_name
from app.services.provisioning import ProvisioningInput, ProvisioningService
from app.services.zimbra import BackgroundLoginCheckCancelled, ZimbraService

router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))
LOGIN_RE = re.compile(r"^[a-z][a-z0-9.-]{0,19}$")
MAX_BACKGROUND_CANDIDATES = 12


def _context(request: Request, **kwargs):
    user = get_current_user(request)
    return {"user": user, "csrf": get_or_create_csrf(request), **kwargs}


def _domains(settings: Settings) -> list[str]:
    return settings.zimbra_domains or (
        [settings.zimbra_primary_domain] if settings.zimbra_primary_domain else []
    )


def _login_candidates(last_name: str, first_name: str, middle_name: str) -> list[str]:
    try:
        return build_login_candidates(last_name, first_name, middle_name)[:MAX_BACKGROUND_CANDIDATES]
    except Exception:
        return []


def _yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def _provisioning_journal_item(operation: ProvisioningOperation) -> dict[str, object]:
    full_name = " ".join(
        part
        for part in [
            operation.last_name,
            operation.first_name,
            operation.middle_name,
        ]
        if part
    )
    status_labels = {
        "draft": "Черновик",
        "running": "Выполняется",
        "partial": "Частично выполнено",
        "success": "Успешно",
        "failed": "Ошибка",
    }
    status_key = operation.status.value

    return {
        "kind": "provision",
        "record_id": operation.id,
        "created_at": operation.created_at,
        "action": "Создание учетных записей",
        "subject": full_name,
        "login": operation.login,
        "corporate_email": operation.corporate_email,
        "personal_email": operation.personal_email,
        "mail_domain": operation.mail_domain,
        "operator": operation.operator_username,
        "status_key": status_key,
        "status_label": status_labels.get(status_key, status_key),
        "details": [
            ("ФИО", full_name),
            ("Логин AD", operation.login),
            ("Корпоративная почта", operation.corporate_email),
            ("Личный адрес", operation.personal_email),
            ("Почтовый домен", operation.mail_domain),
            ("Учетная запись AD создана", _yes_no(operation.ad_created)),
            ("Учетная запись AD включена", _yes_no(operation.ad_enabled)),
            ("Ящик Zimbra создан", _yes_no(operation.zimbra_created)),
            (
                "Получатель реквизитов почты",
                operation.personal_email or operation.corporate_email,
            ),
            (
                "Реквизиты почты отправлены",
                _yes_no(operation.personal_mail_sent),
            ),
            (
                "Реквизиты AD отправлены на корпоративную почту",
                _yes_no(operation.corporate_mail_sent),
            ),
        ],
        "error_message": operation.error_message,
        "completed_at": operation.completed_at,
    }


def _ad_provisioning_journal_item(
    operation: ADProvisioningOperation,
) -> dict[str, object]:
    status_labels = {
        "draft": "Черновик",
        "running": "Выполняется",
        "partial": "Частично выполнено",
        "success": "Успешно",
        "failed": "Ошибка",
    }
    status_key = operation.status.value
    return {
        "kind": "ad-provision",
        "record_id": operation.id,
        "created_at": operation.created_at,
        "action": "Создание AD для существующей почты",
        "subject": operation.full_name,
        "login": operation.login,
        "corporate_email": operation.corporate_email,
        "personal_email": "",
        "mail_domain": (
            operation.corporate_email.rsplit("@", 1)[1]
            if "@" in operation.corporate_email
            else ""
        ),
        "operator": operation.operator_username,
        "status_key": status_key,
        "status_label": status_labels.get(status_key, status_key),
        "details": [
            ("ФИО", operation.full_name),
            ("Логин AD", operation.login),
            ("Корпоративная почта", operation.corporate_email),
            ("Учетная запись AD создана", _yes_no(operation.ad_created)),
            ("Учетная запись AD включена", _yes_no(operation.ad_enabled)),
            (
                "Реквизиты AD отправлены",
                _yes_no(operation.credentials_mail_sent),
            ),
            (
                "Кадровый реестр обновлен",
                _yes_no(operation.registry_updated),
            ),
        ],
        "error_message": operation.error_message,
        "completed_at": operation.completed_at,
    }


def _blocking_journal_item(
    operation: BlockingOperation,
    queue_items: list[BlockingQueueItem] | tuple[BlockingQueueItem, ...] = (),
) -> dict[str, object]:
    status_key = operation.status.value
    status_labels = {
        "running": "Ожидает завершения",
        "partial": "Частично выполнено",
        "success": "Выполнено",
        "failed": "Требует вмешательства",
    }
    status_label = "DRY RUN" if operation.dry_run else status_labels.get(
        status_key,
        status_key,
    )
    queue_by_system = {item.system: item for item in queue_items}
    ad_item = queue_by_system.get("ad")
    zimbra_item = queue_by_system.get("zimbra")
    ad_login = (
        str(ad_item.target_identifier or "").strip()
        if ad_item is not None
        else ""
    ) or operation.login

    def queue_result(item: BlockingQueueItem | None, system: str) -> str:
        if item is None:
            return "Нет данных"
        labels = {
            "ad": {
                "pending": "Ожидает блокировки",
                "completed": "Заблокирована системой",
                "already_completed": "На момент выполнения уже была заблокирована",
                "intervention": "Требует вмешательства",
                "dry_run": "DRY RUN",
            },
            "zimbra": {
                "pending": "Ожидает закрытия",
                "completed": "Закрыта системой",
                "already_completed": "На момент выполнения уже была закрыта",
                "intervention": "Требует вмешательства",
                "dry_run": "DRY RUN",
            },
        }
        return labels.get(system, {}).get(item.status, item.status)

    try:
        equipment_snapshot = json.loads(operation.equipment_snapshot_json or "[]")
    except (TypeError, json.JSONDecodeError):
        equipment_snapshot = []
    if not isinstance(equipment_snapshot, list):
        equipment_snapshot = []

    return {
        "kind": "blocking",
        "record_id": operation.id,
        "created_at": operation.created_at,
        "action": "Блокировка учетных записей",
        "subject": operation.full_name,
        "login": ad_login,
        "corporate_email": operation.corporate_email,
        "personal_email": "",
        "mail_domain": "",
        "operator": operation.operator_username,
        "status_key": status_key,
        "status_label": status_label,
        "details": [
            ("ФИО", operation.full_name),
            ("Логин AD", ad_login),
            ("Корпоративная почта", operation.corporate_email),
            ("IT Invent проверен", _yes_no(operation.itinvent_checked)),
            ("Имущества в IT Invent", str(operation.equipment_count)),
            ("Режим DRY RUN", _yes_no(operation.dry_run)),
        ],
        "blocking_systems": [
            {
                "label": "Active Directory",
                "result": queue_result(ad_item, "ad"),
                "timestamp": (
                    ad_item.completed_at or ad_item.last_attempt_at
                    if ad_item is not None
                    else None
                ),
            },
            {
                "label": "Zimbra",
                "result": queue_result(zimbra_item, "zimbra"),
                "timestamp": (
                    zimbra_item.completed_at or zimbra_item.last_attempt_at
                    if zimbra_item is not None
                    else None
                ),
            },
        ],
        "equipment_snapshot": equipment_snapshot,
        "error_message": operation.error_message,
        "completed_at": operation.completed_at,
    }


def _dismissal_journal_item(schedule: DismissalSchedule) -> dict[str, object]:
    if schedule.ad_expiration_set and schedule.zimbra_note_set:
        status_key = "success"
        status_label = "Успешно"
    elif schedule.ad_expiration_set or schedule.zimbra_note_set:
        status_key = "partial"
        status_label = "Частично выполнено"
    elif schedule.error_message:
        status_key = "failed"
        status_label = "Ошибка"
    else:
        status_key = "running"
        status_label = "Выполняется"

    return {
        "kind": "dismissal",
        "record_id": schedule.id,
        "created_at": schedule.created_at,
        "action": "Пометка на увольнение",
        "subject": schedule.corporate_email or schedule.login,
        "login": schedule.login,
        "corporate_email": schedule.corporate_email,
        "personal_email": "",
        "mail_domain": "",
        "operator": schedule.operator_username,
        "status_key": status_key,
        "status_label": status_label,
        "details": [
            ("Логин AD", schedule.login),
            ("Корпоративная почта", schedule.corporate_email),
            ("Дата увольнения", schedule.dismissal_date.strftime("%d.%m.%Y")),
            (
                "Срок действия учетной записи AD установлен",
                _yes_no(schedule.ad_expiration_set),
            ),
            (
                "Дата записана в zimbraNotes",
                _yes_no(schedule.zimbra_note_set),
            ),
        ],
        "error_message": schedule.error_message,
        "completed_at": None,
    }


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
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
    blocking_queue_by_operation: dict[int, list[BlockingQueueItem]] = {}
    for queue_item in blocking_queue_items:
        blocking_queue_by_operation.setdefault(queue_item.operation_id, []).append(
            queue_item
        )

    journal_items = [
        *(_provisioning_journal_item(item) for item in provisioning_operations),
        *(
            _ad_provisioning_journal_item(item)
            for item in ad_provisioning_operations
        ),
        *(
            _blocking_journal_item(
                item,
                blocking_queue_by_operation.get(item.id, []),
            )
            for item in blocking_operations
        ),
        *(_dismissal_journal_item(item) for item in dismissal_operations),
    ]
    journal_items.sort(key=lambda item: item["created_at"], reverse=True)
    journal_items = journal_items[:50]

    registry_service = HRRegistryService(settings, db)
    registry_summary = registry_service.summary()
    registry_issues = registry_service.list_rows(status="issues", limit=10)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            journal_items=journal_items,
            registry_summary=registry_summary,
            registry_issues=registry_issues,
            dry_run=settings.dry_run,
        ),
    )


@router.get("/employees/registry")
def employee_registry(
    request: Request,
    q: str = "",
    status: str = "all",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    if status not in {"all", "issues", "ok", "checked", "not_checked"}:
        status = "all"
    service = HRRegistryService(settings, db)
    return templates.TemplateResponse(
        request,
        "hr_registry.html",
        _context(
            request,
            rows=service.list_rows(query=q, status=status, limit=1000),
            summary=service.summary(),
            query=q,
            selected_status=status,
        ),
    )


@router.post("/employees/registry/{record_id}/checked")
def mark_registry_worker_checked(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    return_to: str = Form("/employees/registry"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    HRRegistryService(settings, db).mark_accounts_not_required(
        record_id,
        user.username,
        user.source,
    )
    target = return_to.strip()
    if not target.startswith("/") or target.startswith("//"):
        target = "/employees/registry"
    return RedirectResponse(target, status_code=303)


@router.get("/employees/registry/{record_id}/create-ad")
def create_ad_for_existing_mailbox_form(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    service = ProvisioningService(settings)
    try:
        preflight = service.prepare_ad_for_existing_mailbox(db, record_id)
        return templates.TemplateResponse(
            request,
            "ad_only_confirm.html",
            _context(
                request,
                preflight=preflight,
                error="",
                dry_run=settings.dry_run,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "ad_only_confirm.html",
            _context(
                request,
                preflight=None,
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.post("/employees/registry/{record_id}/confirm-ad")
def confirm_existing_ad_candidate(
    record_id: int,
    request: Request,
    ad_login: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    service = ProvisioningService(settings)

    try:
        confirmation = service.confirm_ad_candidate(
            db,
            user.username,
            record_id,
            ad_login,
        )
        return templates.TemplateResponse(
            request,
            "ad_only_confirm.html",
            _context(
                request,
                preflight=None,
                confirmation=confirmation,
                error="",
                dry_run=settings.dry_run,
            ),
        )
    except Exception as exc:
        try:
            preflight = service.prepare_ad_for_existing_mailbox(
                db,
                record_id,
            )
        except Exception:
            preflight = None
        return templates.TemplateResponse(
            request,
            "ad_only_confirm.html",
            _context(
                request,
                preflight=preflight,
                confirmation=None,
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.post("/employees/registry/{record_id}/create-ad")
def create_ad_for_existing_mailbox(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    confirm_name_candidates: str = Form("false"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    confirmed = confirm_name_candidates.strip().lower() == "true"
    service = ProvisioningService(settings)

    try:
        credentials = service.provision_ad_for_existing_mailbox(
            db,
            user.username,
            record_id,
            confirm_name_candidates=confirmed,
        )
        response = templates.TemplateResponse(
            request,
            "ad_only_result.html",
            _context(
                request,
                credentials=credentials,
                error="",
            ),
        )
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, private"
        )
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception as exc:
        try:
            preflight = service.prepare_ad_for_existing_mailbox(
                db,
                record_id,
            )
        except Exception:
            preflight = None
        return templates.TemplateResponse(
            request,
            "ad_only_confirm.html",
            _context(
                request,
                preflight=preflight,
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.get("/employees/new")
def new_employee(
    request: Request,
    fio: str = "",
    arrival_event_ids: str = "",
    force_new: bool = False,
    account_error: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    domains = _domains(settings)
    if arrival_event_ids.strip():
        try:
            arrival = EmployeeArrivalService(db).registration_context(
                arrival_event_ids
            )
            raw_input = str(arrival["fio"])
            if arrival["personal_email"]:
                raw_input += f"\n{arrival['personal_email']}"
            parsed = parse_two_line_input(raw_input)
            candidates = build_login_candidates(
                parsed.last_name,
                parsed.first_name,
                parsed.middle_name,
            )[:MAX_BACKGROUND_CANDIDATES]
            if not candidates:
                raise RuntimeError("Не удалось сформировать логин из ФИО")
            hr_login = next(iter(arrival["logins"]), "")
            if LOGIN_RE.fullmatch(hr_login) and hr_login not in candidates:
                candidates.insert(0, hr_login)
                candidates = candidates[:MAX_BACKGROUND_CANDIDATES]
            account_state = EmployeeArrivalAccountService(
                settings,
                db,
            ).inspect(arrival["event_ids_value"])
            preferred_domain = str(arrival["preferred_domain"])
            return templates.TemplateResponse(
                request,
                "employee_form.html",
                _context(
                    request,
                    domains=domains,
                    parsed=parsed,
                    candidates=candidates,
                    selected_login=(
                        hr_login
                        if LOGIN_RE.fullmatch(hr_login)
                        else candidates[0]
                    ),
                    selected_domain=(
                        preferred_domain if preferred_domain in domains else ""
                    ),
                    error=account_error,
                    raw_input=raw_input,
                    domain_mode=settings.zimbra_domain_mode,
                    no_email_confirmed=False,
                    arrival_event_ids=arrival["event_ids_value"],
                    arrival_accounts=account_state,
                    force_new=force_new,
                ),
            )
        except Exception as exc:
            db.rollback()
            return templates.TemplateResponse(
                request,
                "employee_form.html",
                _context(
                    request,
                    domains=domains,
                    parsed=None,
                    candidates=[],
                    error=str(exc),
                    raw_input=fio.strip(),
                    domain_mode=settings.zimbra_domain_mode,
                    arrival_event_ids="",
                    arrival_accounts=None,
                    force_new=False,
                ),
                status_code=400,
            )
    return templates.TemplateResponse(
        request,
        "employee_form.html",
        _context(
            request,
            domains=domains,
            parsed=None,
            candidates=[],
            error="",
            raw_input=fio.strip(),
            domain_mode=settings.zimbra_domain_mode,
            arrival_event_ids="",
            arrival_accounts=None,
            force_new=False,
        ),
    )


@router.post("/employees/arrivals/accounts/resolve")
def resolve_employee_arrival_accounts(
    request: Request,
    arrival_event_ids: str = Form(...),
    ad_login: str = Form(...),
    zimbra_email: str = Form(...),
    action: str = Form("confirm"),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    try:
        if action not in {"confirm", "restore"}:
            raise ValueError("Неизвестное действие с учетными записями")
        result = EmployeeArrivalAccountService(settings, db).resolve(
            raw_event_ids=arrival_event_ids,
            ad_login=ad_login,
            zimbra_email=zimbra_email,
            actor=user.username,
            restore_closed=action == "restore",
        )
        return templates.TemplateResponse(
            request,
            "employee_arrival_accounts_result.html",
            _context(
                request,
                result=result,
                error="",
            ),
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            "/employees/new?"
            f"arrival_event_ids={quote_plus(arrival_event_ids)}&"
            f"account_error={quote_plus(str(exc))}",
            status_code=303,
        )


@router.post("/employees/parse")
def parse_employee(
    request: Request,
    raw_input: str = Form(...),
    arrival_event_ids: str = Form(""),
    confirm_no_personal_email: str = Form("false"),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    try:
        parsed = parse_two_line_input(raw_input)
        no_email_confirmed = (
            confirm_no_personal_email.strip().lower() == "true"
        )
        if not parsed.personal_email and not no_email_confirmed:
            raise ValueError(
                "ФИО введено без личного email. Подтвердите, "
                "что оба письма с реквизитами будут отправлены "
                "на создаваемую корпоративную почту."
            )

        candidates = build_login_candidates(
            parsed.last_name,
            parsed.first_name,
            parsed.middle_name,
        )[:MAX_BACKGROUND_CANDIDATES]
        if not candidates:
            raise RuntimeError("Не удалось сформировать логин из ФИО")

        # Внешние системы здесь больше не проверяются. Следующий экран
        # открывается сразу, а AD и Zimbra проверяются отдельными запросами.
        selected_login = candidates[0]

        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=parsed,
                candidates=candidates,
                selected_login=selected_login,
                error="",
                raw_input=raw_input,
                domain_mode=settings.zimbra_domain_mode,
                no_email_confirmed=no_email_confirmed,
                arrival_event_ids=arrival_event_ids,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=None,
                candidates=[],
                error=str(exc),
                raw_input=raw_input,
                domain_mode=settings.zimbra_domain_mode,
                arrival_event_ids=arrival_event_ids,
            ),
            status_code=400,
        )


@router.get("/employees/check-login/{source}")
def check_login_source(
    source: str,
    request: Request,
    login: str,
    fresh: bool = False,
    settings: Settings = Depends(get_settings),
):
    # Запрос доступен только авторизованному оператору.
    get_current_user(request)

    normalized_login = login.strip().lower()
    if not LOGIN_RE.fullmatch(normalized_login):
        return JSONResponse(
            {
                "ok": False,
                "source": source,
                "error": "Некорректный формат логина",
            },
            status_code=400,
        )

    started = time.perf_counter()
    try:
        if source == "ad":
            enabled = settings.ad_check_enabled
            occupied = (
                ActiveDirectoryService(settings).login_exists(normalized_login)
                if enabled
                else False
            )
            label = "Active Directory"
        elif source == "zimbra":
            enabled = settings.zimbra_check_enabled
            occupied = (
                ZimbraService(settings).login_exists_any_domain(
                    normalized_login,
                    force_refresh=fresh,
                )
                if enabled
                else False
            )
            label = "Zimbra"
        else:
            return JSONResponse(
                {
                    "ok": False,
                    "source": source,
                    "error": "Неизвестный источник проверки",
                },
                status_code=404,
            )

        return {
            "ok": True,
            "source": source,
            "label": label,
            "login": normalized_login,
            "enabled": enabled,
            "occupied": occupied,
            "free": not occupied,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "source": source,
                "login": normalized_login,
                "error": str(exc),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            },
            status_code=503,
        )


@router.post("/employees/check-candidates")
def check_login_candidates(
    request: Request,
    logins_json: str = Form(...),
    csrf: str = Form(...),
    force_refresh: bool = Form(False),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    get_current_user(request)

    try:
        raw_logins = json.loads(logins_json)
        if not isinstance(raw_logins, list):
            raise ValueError("Список кандидатов имеет неверный формат")

        logins: list[str] = []
        for value in raw_logins[:MAX_BACKGROUND_CANDIDATES]:
            login = str(value).strip().lower()
            if not LOGIN_RE.fullmatch(login):
                continue
            if login not in logins:
                logins.append(login)

        if not logins:
            raise ValueError("Не переданы корректные варианты логина")

        started = time.perf_counter()
        items = ProvisioningService(settings).check_logins(
            logins,
            force_refresh=force_refresh,
            background=True,
        )
        return {
            "ok": True,
            "items": items,
            "checked": len(items),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except BackgroundLoginCheckCancelled:
        return JSONResponse(
            {
                "ok": False,
                "cancelled": True,
                "error": "Фоновая проверка альтернатив отменена",
            },
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=503,
        )


@router.post("/employees/provision")
def provision_employee(
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(""),
    personal_email: str = Form(""),
    confirm_no_personal_email: str = Form("false"),
    login: str = Form(...),
    mail_domain: str = Form(...),
    arrival_event_ids: str = Form(""),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    login = login.strip().lower()
    personal_email = personal_email.strip()

    try:
        from email_validator import EmailNotValidError, validate_email

        arrival_service = EmployeeArrivalService(db)
        if arrival_event_ids.strip():
            # Повторно читаем кадровое состояние непосредственно перед
            # внешними действиями: исчезнувший или уже обработанный эпизод
            # не должен запустить создание учетных записей.
            arrival_service.registration_context(arrival_event_ids)

        if not LOGIN_RE.fullmatch(login):
            raise ValueError("Логин должен начинаться с латинской буквы и содержать не более 20 символов")

        last_name, first_name, middle_name = validate_person_name(
            last_name,
            first_name,
            middle_name,
        )
        no_email_confirmed = (
            confirm_no_personal_email.strip().lower() == "true"
        )
        if personal_email:
            try:
                personal_email = validate_email(
                    personal_email,
                    check_deliverability=False,
                ).normalized
            except EmailNotValidError as exc:
                raise ValueError(
                    f"Некорректный личный email: {exc}"
                ) from exc
        elif not no_email_confirmed:
            raise ValueError(
                "Личный email не указан. Подтвердите создание "
                "учетных записей с отправкой обоих писем "
                "на корпоративную почту."
            )

        if settings.zimbra_domains and mail_domain not in settings.zimbra_domains:
            raise ValueError("Выбран неизвестный почтовый домен")

        data = ProvisioningInput(
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            personal_email=personal_email,
            login=login,
            mail_domain=mail_domain,
        )
        credentials = ProvisioningService(settings).provision(db, user.username, data)
        if (
            arrival_event_ids.strip()
            and credentials.ad_created
            and credentials.zimbra_created
        ):
            try:
                EmployeeArrivalAccountService(settings, db).resolve(
                    raw_event_ids=arrival_event_ids,
                    ad_login=credentials.ad_login,
                    zimbra_email=credentials.corporate_email,
                    actor=user.username,
                    restore_closed=False,
                    provisioning_operation_id=credentials.operation_id,
                )
            except Exception as mapping_exc:
                db.rollback()
                db.add(
                    AuditLog(
                        actor=user.username,
                        action="new_employment_created_mapping_failed",
                        target=arrival_event_ids,
                        result="error",
                        details=str(mapping_exc)[:4000],
                    )
                )
                db.commit()
                credentials = replace(
                    credentials,
                    status="partial",
                    warnings=(
                        *credentials.warnings,
                        "Учетные записи созданы, но кадровое событие осталось "
                        "открытым: не удалось сохранить сопоставление — "
                        f"{mapping_exc}",
                    ),
                )
        response = templates.TemplateResponse(
            request,
            "result.html",
            _context(request, credentials=credentials, error=""),
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception as exc:
        parsed = type(
            "Parsed",
            (),
            {
                "last_name": last_name.strip(),
                "first_name": first_name.strip(),
                "middle_name": middle_name.strip(),
                "personal_email": personal_email,
            },
        )()
        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=parsed,
                candidates=_login_candidates(last_name, first_name, middle_name),
                error=str(exc),
                raw_input="",
                domain_mode=settings.zimbra_domain_mode,
                selected_login=login,
                selected_domain=mail_domain,
                no_email_confirmed=(
                    confirm_no_personal_email.strip().lower() == "true"
                ),
                arrival_event_ids=arrival_event_ids,
            ),
            status_code=400,
        )


@router.get("/blocking")
def blocking_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    query = q.strip()
    rows: list[dict] = []
    error = ""
    if query:
        if len(query) < 2:
            error = "Введите не менее двух символов для поиска работника."
        else:
            try:
                rows = BlockingService(settings, db).search(query)
            except Exception as exc:
                error = str(exc)
    return templates.TemplateResponse(
        request,
        "blocking.html",
        _context(
            request,
            query=query,
            rows=rows,
            card=None,
            result=None,
            error=error,
            dry_run=settings.dry_run,
        ),
    )


@router.get("/blocking/{record_id}")
def blocking_card(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    try:
        service = BlockingService(settings, db)
        card = service.card(record_id)
        latest_result = service.latest_result_for_record(record_id)
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=card,
                result=latest_result,
                error="",
                dry_run=settings.dry_run,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=None,
                result=None,
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.post("/blocking/{record_id}/itinvent/refresh")
def refresh_blocking_itinvent(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    get_current_user(request)
    try:
        result = BlockingService(settings, db).refresh_itinvent(record_id)
        if result.state == "error":
            return JSONResponse(
                {"ok": False, "error": result.error},
                status_code=503,
            )
        equipment = []
        if result.itinvent is not None:
            equipment = [
                {
                    "type": item.equipment_type,
                    "inventory_number": item.inventory_number,
                    "name": item.equipment_name,
                    "serial_number": item.serial_number,
                    "accounting_inventory_number": (
                        item.accounting_inventory_number
                    ),
                }
                for item in result.itinvent.equipment
            ]
        return {
            "ok": True,
            "state": result.state,
            "effective_login": result.effective_login,
            "checked_at": result.checked_at,
            "owner_found": bool(
                result.itinvent is not None
                and result.itinvent.owner_found
            ),
            "owner_display_name": (
                result.itinvent.owner_display_name
                if result.itinvent is not None
                else ""
            ),
            "owner_login": (
                result.itinvent.owner_login
                if result.itinvent is not None
                else result.effective_login
            ),
            "equipment": equipment,
            "equipment_count": len(equipment),
        }
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/blocking/{record_id}")
def block_employee_accounts(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    service = BlockingService(settings, db)
    try:
        result = service.block(record_id, user.username)
        try:
            card = service.card(record_id)
        except Exception:
            card = None
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=card,
                result=result,
                error="",
                dry_run=settings.dry_run,
            ),
        )
    except Exception as exc:
        try:
            card = service.card(record_id)
        except Exception:
            card = None
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=card,
                result=None,
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.post("/blocking/operations/{operation_id}/retry")
def retry_blocking_operation(
    operation_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    get_current_user(request)
    operation = db.get(BlockingOperation, operation_id)
    if operation is None:
        return RedirectResponse("/blocking", status_code=303)

    service = BlockingService(settings, db)
    try:
        result = service.retry_operation(operation_id)
        card = service.card(operation.source_record_id)
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=card,
                result=result,
                error="",
                dry_run=settings.dry_run,
            ),
        )
    except Exception as exc:
        try:
            card = service.card(operation.source_record_id)
        except Exception:
            card = None
        return templates.TemplateResponse(
            request,
            "blocking.html",
            _context(
                request,
                query="",
                rows=[],
                card=card,
                result=service.operation_result(operation_id),
                error=str(exc),
                dry_run=settings.dry_run,
            ),
            status_code=400,
        )


@router.get("/dismissals/new")
def dismissal_form_legacy():
    return RedirectResponse("/blocking", status_code=303)


@router.post("/dismissals")
def schedule_dismissal(
    request: Request,
    login: str = Form(...),
    corporate_email: str = Form(...),
    dismissal_date: date = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    schedule = DismissalSchedule(
        login=login.strip().lower(),
        corporate_email=corporate_email.strip().lower(),
        dismissal_date=dismissal_date,
        operator_username=user.username,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    try:
        ActiveDirectoryService(settings).set_account_expiration(schedule.login, schedule.dismissal_date)
        schedule.ad_expiration_set = True
        db.commit()
        ZimbraService(settings).set_dismissal_note(schedule.corporate_email, schedule.dismissal_date)
        schedule.zimbra_note_set = True
        db.add(AuditLog(actor=user.username, action="schedule_dismissal", target=schedule.corporate_email))
        db.commit()
        return templates.TemplateResponse(
            request,
            "dismissal_form.html",
            _context(request, error="", success="Срок действия AD и дата в zimbraNotes установлены"),
        )
    except Exception as exc:
        schedule.error_message = str(exc)[:4000]
        db.add(
            AuditLog(
                actor=user.username,
                action="schedule_dismissal",
                target=schedule.corporate_email,
                result="partial",
                details=str(exc)[:1000],
            )
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "dismissal_form.html",
            _context(request, error=str(exc), success=""),
            status_code=400,
        )
