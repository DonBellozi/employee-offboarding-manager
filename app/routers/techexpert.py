from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditLog
from app.models_techexpert import TechExpertActualizationRun
from app.security import (
    get_or_create_csrf,
    require_admin,
    require_operator,
    validate_csrf,
)
from app.services.ad import ActiveDirectoryService
from app.services.mailer import (
    CredentialMailer,
    ensure_domain_mail_profiles,
    get_domain_mail_profile,
    render_mail_template,
)
from app.services.techexpert_settings import (
    TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES,
    TECHEXPERT_TEMPLATE_VARIABLES,
    TechExpertSettingsService,
    build_techexpert_template_context,
    normalize_email,
)
from app.services.techexpert_registration import (
    TechExpertRegistrationService,
    preview_document,
)
from app.services.techexpert_access import TechExpertGroupAccessService
from app.services.techexpert_actualization import (
    TechExpertActualizationService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _redirect(*, message: str = "", error: str = "") -> RedirectResponse:
    parts = []
    if message:
        parts.append(f"message={quote_plus(message)}")
    if error:
        parts.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(parts)}" if parts else ""
    return RedirectResponse(f"/settings/techexpert{suffix}", status_code=303)


def _registration_redirect(
    *,
    registration_id: int = 0,
    record_id: int = 0,
    query: str = "",
    message: str = "",
    error: str = "",
) -> RedirectResponse:
    parts = []
    if registration_id:
        parts.append(f"registration_id={int(registration_id)}")
    if record_id:
        parts.append(f"record_id={int(record_id)}")
    if query:
        parts.append(f"q={quote_plus(query)}")
    if message:
        parts.append(f"message={quote_plus(message)}")
    if error:
        parts.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(parts)}" if parts else ""
    return RedirectResponse(f"/techexpert/registration{suffix}", status_code=303)


def _page_context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    error: str = "",
) -> dict[str, object]:
    current = require_operator(request)
    service = TechExpertSettingsService(settings, db)
    config = service.get()
    profiles = {
        profile.domain.strip().lower(): profile
        for profile in ensure_domain_mail_profiles(db, settings)
    }
    access_summary = {"access_count": 0}
    active_run = None
    active_run_details = None
    group_sync_summary: dict[str, object] = {}
    if config.source_domain:
        actualization = TechExpertActualizationService(settings, db, config)
        access_summary = actualization.access_summary()
        active_run = db.scalar(
            select(TechExpertActualizationRun)
            .where(
                TechExpertActualizationRun.source_id
                == config.source_domain.strip().lower(),
                TechExpertActualizationRun.status == "open",
            )
            .order_by(TechExpertActualizationRun.id.desc())
        )
        if active_run is not None:
            active_run_details = actualization.run_details(active_run.id)
        last_group_sync = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "techexpert_group_sync",
                AuditLog.target == config.source_domain.strip().lower(),
            )
            .order_by(AuditLog.id.desc())
        )
        if last_group_sync is not None:
            try:
                parsed = json.loads(last_group_sync.details or "{}")
                if isinstance(parsed, dict):
                    group_sync_summary = parsed
            except (TypeError, json.JSONDecodeError):
                group_sync_summary = {}
    history = list(
        db.scalars(
            select(TechExpertActualizationRun)
            .where(
                TechExpertActualizationRun.source_id
                == config.source_domain.strip().lower()
            )
            .order_by(TechExpertActualizationRun.id.desc())
            .limit(10)
        ).all()
    )
    return {
        "user": current,
        "is_admin": current.role == "admin",
        "csrf": get_or_create_csrf(request),
        "techexpert": config,
        "source_domains": service.available_domains(),
        "sender_profile": profiles.get(config.source_domain.strip().lower()),
        "template_variables": TECHEXPERT_TEMPLATE_VARIABLES,
        "registration_template_variables": (
            TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES
        ),
        "smtp_configured": bool(settings.smtp_host),
        "app_timezone": settings.app_timezone,
        "message": request.query_params.get("message", ""),
        "error": error or request.query_params.get("error", ""),
        "access_summary": access_summary,
        "group_sync_summary": group_sync_summary,
        "actualization_run": active_run,
        "actualization_details": active_run_details,
        "actualization_history": history,
    }


@router.get("/settings/techexpert")
def techexpert_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "techexpert.html",
        _page_context(request, settings=settings, db=db),
    )


@router.post("/settings/techexpert")
def techexpert_save(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    source_domain: str = Form(...),
    ad_group_dn: str = Form(...),
    recipient_email: str = Form(...),
    notification_time: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    registration_subject: str = Form(...),
    registration_body_html: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        TechExpertSettingsService(settings, db).save(
            enabled=enabled.strip().lower() in {"1", "true", "yes", "on"},
            source_domain=source_domain,
            ad_group_dn=ad_group_dn,
            recipient_email=recipient_email,
            notification_time=notification_time,
            subject=subject,
            body_html=body_html,
            registration_subject=registration_subject,
            registration_body_html=registration_body_html,
            actor=current.username,
        )
        return _redirect(message="Настройки Техэксперта сохранены")
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "techexpert.html",
            _page_context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/techexpert/check")
def techexpert_check(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        if not config.ad_group_dn.strip():
            raise ValueError("Сначала сохраните DN группы доступа AD")
        ActiveDirectoryService(settings).test_group(config.ad_group_dn)
        CredentialMailer(settings).test_connection()
        return _redirect(
            message="Группа AD найдена, SMTP-подключение работает"
        )
    except Exception as exc:
        return _redirect(error=f"Проверка не пройдена: {exc}")


@router.post("/settings/techexpert/test-email")
def techexpert_test_email(
    request: Request,
    csrf: str = Form(...),
    test_recipient: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        recipient = normalize_email(
            test_recipient,
            field_name="тестовый e-mail",
        )
        profile = get_domain_mail_profile(
            db,
            settings,
            config.source_domain,
        )
        context = build_techexpert_template_context(
            [
                {
                    "full_name": "Иванов Иван Иванович",
                    "corporate_email": f"ivanov@{config.source_domain}",
                    "organization": "Тестовая организация",
                    "department": "Центральный аппарат",
                    "dismissal_date": "20.08.2026",
                },
                {
                    "full_name": "Петрова Анна Сергеевна",
                    "corporate_email": f"petrova@{config.source_domain}",
                    "organization": "Тестовая организация",
                    "department": "ОП «Дирекция в Белгородской области»",
                    "dismissal_date": "20.08.2026",
                },
            ]
        )
        CredentialMailer(settings).send_html(
            recipient=recipient,
            subject=render_mail_template(
                config.subject,
                context,
                autoescape=False,
            ),
            body_html=render_mail_template(
                config.body_html,
                context,
                autoescape=True,
            ),
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
        )
        return _redirect(message=f"Тестовое письмо отправлено на {recipient}")
    except Exception as exc:
        return _redirect(error=f"Тестовое письмо не отправлено: {exc}")


@router.post("/settings/techexpert/test-registration-email")
def techexpert_test_registration_email(
    request: Request,
    csrf: str = Form(...),
    test_recipient: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        recipient = normalize_email(
            test_recipient,
            field_name="тестовый e-mail",
        )
        profile = get_domain_mail_profile(
            db,
            settings,
            config.source_domain,
        )
        context = {
            "full_name": "Иванов Иван Иванович",
            "position": "Ведущий инженер",
            "corporate_email": f"ivanov@{config.source_domain}",
            "mobile_phone": "+7 900 000-00-00",
            "department": "Центральный аппарат",
            "organization": "Тестовая организация",
        }
        CredentialMailer(settings).send_html(
            recipient=recipient,
            subject=render_mail_template(
                config.registration_subject,
                context,
                autoescape=False,
            ),
            body_html=render_mail_template(
                config.registration_body_html,
                context,
                autoescape=True,
            ),
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
        )
        return _redirect(
            message=f"Тестовое письмо о регистрации отправлено на {recipient}"
        )
    except Exception as exc:
        return _redirect(
            error=f"Тестовое письмо о регистрации не отправлено: {exc}"
        )


@router.get("/techexpert/registration")
def techexpert_registration_page(
    request: Request,
    q: str = "",
    record_id: int = 0,
    registration_id: int = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    current = require_operator(request)
    config = TechExpertSettingsService(settings, db).get()
    service = TechExpertRegistrationService(settings, db, config)
    error = request.query_params.get("error", "")
    results: list[dict[str, object]] = []
    selected = None
    registration = None
    try:
        if q.strip():
            if len(q.strip()) < 2:
                raise ValueError("Введите не менее двух символов ФИО")
            results = service.search(q)
        if record_id:
            selected = service.selected_record(record_id)
        if registration_id:
            registration = service.get_request(registration_id)
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "techexpert_registration.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
            "query": q,
            "results": results,
            "selected": selected,
            "registration": registration,
            "history": service.history(),
            "techexpert": config,
            "message": request.query_params.get("message", ""),
            "error": error,
        },
    )


@router.post("/techexpert/registration/prepare")
def techexpert_registration_prepare(
    request: Request,
    record_id: int = Form(...),
    placement_index: int = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        registration = TechExpertRegistrationService(
            settings,
            db,
            config,
        ).prepare(
            record_id=record_id,
            placement_index=placement_index,
            actor=current.username,
        )
        return _registration_redirect(
            registration_id=registration.id,
            message="Письмо подготовлено. Проверьте его перед отправкой.",
        )
    except Exception as exc:
        db.rollback()
        return _registration_redirect(
            record_id=record_id,
            error=f"Письмо не подготовлено: {exc}",
        )


@router.post("/techexpert/registration/{registration_id}/execute")
def techexpert_registration_execute(
    registration_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        registration = TechExpertRegistrationService(
            settings,
            db,
            config,
        ).execute(request_id=registration_id, actor=current.username)
        if registration.status == "sent":
            message = "Работник добавлен в группу, письмо отправлено."
            error = ""
        elif registration.status == "partial":
            message = ""
            error = (
                "Работник добавлен в группу, но письмо не отправлено. "
                "Исправьте SMTP и повторите отправку."
            )
        elif registration.status == "dry_run":
            message = "DRY_RUN: группа и почта не изменены."
            error = ""
        else:
            message = ""
            error = registration.last_error or "Запрос не выполнен"
        return _registration_redirect(
            registration_id=registration.id,
            message=message,
            error=error,
        )
    except Exception as exc:
        db.rollback()
        return _registration_redirect(
            registration_id=registration_id,
            error=f"Запрос не выполнен: {exc}",
        )


@router.get(
    "/techexpert/registration/{registration_id}/body",
    response_class=HTMLResponse,
)
def techexpert_registration_preview_body(
    registration_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    require_operator(request)
    config = TechExpertSettingsService(settings, db).get()
    registration = TechExpertRegistrationService(
        settings,
        db,
        config,
    ).get_request(registration_id)
    return HTMLResponse(preview_document(registration.body_html))


@router.post("/settings/techexpert/group-sync")
def techexpert_group_sync(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        result = TechExpertGroupAccessService(
            settings,
            db,
            config,
        ).sync(actor=current.username)
        return _redirect(
            message=(
                "Группа AD обновлена: "
                f"участников {result['members']}, "
                f"сопоставлено {result['matched']}, "
                f"требуют проверки {len(result['issues'])}"
            )
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Группа AD не обновлена: {exc}")


@router.post("/settings/techexpert/group-member/remove")
def techexpert_unmatched_group_member_remove(
    request: Request,
    ad_login: str = Form(...),
    ad_object_guid: str = Form(""),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        result = TechExpertGroupAccessService(
            settings,
            db,
            config,
        ).remove_unmatched_member(
            ad_login=ad_login,
            ad_object_guid=ad_object_guid,
            actor=current.username,
        )
        message = (
            "DRY_RUN: участник группы не изменён"
            if result["state"] == "dry_run"
            else f"{result['ad_login']} удалён из группы Техэксперта"
        )
        return _redirect(message=message)
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Участник группы не удалён: {exc}")


@router.post("/settings/techexpert/actualization/start")
def techexpert_actualization_start(
    request: Request,
    csrf: str = Form(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        run = TechExpertActualizationService(
            settings,
            db,
            config,
        ).create_run(actor=current.username, title=title)
        return _redirect(message=f"Пакет актуализации №{run.id} открыт")
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Пакет не создан: {exc}")


@router.post("/settings/techexpert/actualization/{run_id}/upload")
async def techexpert_actualization_upload(
    run_id: int,
    request: Request,
    file: UploadFile = File(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        result = TechExpertActualizationService(
            settings,
            db,
            config,
        ).add_file(
            run_id=run_id,
            filename=file.filename or "",
            data=await file.read(),
            actor=current.username,
        )
        return _redirect(
            message=(
                f"Файл «{result['department']}» обработан: "
                f"работают {result['working']}, "
                f"не работают {result['not_working']}, "
                f"проверить {result['review']}"
            )
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Файл не обработан: {exc}")


@router.post(
    "/settings/techexpert/actualization/{run_id}/reanalyze-not-working"
)
def techexpert_actualization_reanalyze_not_working(
    run_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        result = TechExpertActualizationService(
            settings,
            db,
            config,
        ).reanalyze_not_working(run_id=run_id, actor=current.username)
        return _redirect(
            message=(
                f"Повторно проверено: {result['checked']}; "
                f"теперь работают {result['working']}, "
                f"не работают {result['not_working']}, "
                f"проверить вручную {result['review']}"
            )
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Повторная проверка не выполнена: {exc}")


@router.post(
    "/settings/techexpert/actualization/{run_id}/items/{item_id}/resolve"
)
def techexpert_actualization_resolve(
    run_id: int,
    item_id: int,
    request: Request,
    record_id: int = Form(...),
    ad_login: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        TechExpertActualizationService(settings, db, config).resolve_item(
            run_id=run_id,
            item_id=item_id,
            record_id=record_id,
            ad_login=ad_login,
            actor=current.username,
        )
        return _redirect(message="Сопоставление сохранено")
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Сопоставление не сохранено: {exc}")


@router.post("/settings/techexpert/actualization/{run_id}/group")
def techexpert_actualization_group(
    run_id: int,
    request: Request,
    action: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        result = TechExpertActualizationService(
            settings,
            db,
            config,
        ).apply_group(run_id=run_id, action=action, actor=current.username)
        return _redirect(
            message=(
                f"Группа AD обработана: изменено {result['changed']}, "
                f"без изменений {result['skipped']}, ошибок {result['errors']}"
            )
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Группа AD не изменена: {exc}")


@router.post("/settings/techexpert/actualization/{run_id}/complete")
def techexpert_actualization_complete(
    run_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_operator(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        TechExpertActualizationService(
            settings,
            db,
            config,
        ).complete_run(run_id=run_id, actor=current.username)
        return _redirect(message=f"Пакет №{run_id} завершён")
    except Exception as exc:
        db.rollback()
        return _redirect(error=f"Пакет не завершён: {exc}")


@router.get("/settings/techexpert/actualization/{run_id}/not-working.xlsx")
def techexpert_actualization_not_working_export(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    require_operator(request)
    config = TechExpertSettingsService(settings, db).get()
    payload = TechExpertActualizationService(
        settings,
        db,
        config,
    ).export_not_working(run_id)
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="techexpert-not-working-{run_id}.xlsx"'
            )
        },
    )


@router.get("/settings/techexpert/current.xlsx")
def techexpert_current_export(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    require_operator(request)
    config = TechExpertSettingsService(settings, db).get()
    payload = TechExpertActualizationService(
        settings,
        db,
        config,
    ).export_current()
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                'attachment; filename="techexpert-current-users.xlsx"'
            ),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )
