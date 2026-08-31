from __future__ import annotations

import json
from pathlib import Path


from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db import get_db
from app.time_utils import register_datetime_filters
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.ad import ActiveDirectoryService
from app.services.email_login_mapping import EmailLoginMappingService
from app.services.hr_registry import HRRegistryService
from app.services.itinvent import ITInventService
from app.services.itinvent_control import ITInventControlService
from app.services.mailer import CredentialMailer
from app.services.onec_import import OneCImportService
from app.services.onec_scheduler import schedule_info
from app.services.zimbra import ZimbraService
from sqlalchemy.orm import Session

router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def _set_not_set(value: str) -> str:
    return "задан" if str(value or "").strip() else "не задан"


def _file_state(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return "не задан"
    return "найден" if Path(path).is_file() else "не найден"


def _integration_overview(settings: Settings) -> dict[str, dict[str, object]]:
    ad_configured = bool(
        settings.ad_server
        and settings.ad_bind_dn
        and settings.ad_bind_password
        and settings.ad_base_dn
    )
    zimbra_auth = settings.zimbra_ssh_auth
    zimbra_secret_present = bool(
        settings.zimbra_ssh_password
        or settings.zimbra_ssh_password_file
        or settings.zimbra_ssh_private_key
    )
    zimbra_configured = bool(
        settings.zimbra_ssh_host
        and settings.zimbra_ssh_user
        and zimbra_secret_present
        and settings.zimbra_backend != "disabled"
    )
    smtp_configured = bool(settings.smtp_host)
    onec_configured = bool(
        settings.onec_imap_host
        and settings.onec_imap_username
        and settings.onec_imap_password
        and settings.onec_attachment_filename
    )
    itinvent_configured = bool(
        settings.itinvent_enabled
        and settings.itinvent_db_host
        and settings.itinvent_db_name
        and settings.itinvent_db_username
        and settings.itinvent_db_password
    )

    if settings.smtp_ssl:
        smtp_mode = "SSL/TLS"
    elif settings.smtp_starttls:
        smtp_mode = "STARTTLS"
    else:
        smtp_mode = "Без шифрования"

    return {
        "ad": {
            "configured": ad_configured,
            "badge": "Используется" if ad_configured else "Не настроено",
            "server": settings.ad_server or "–",
            "port": settings.ad_port,
            "ssl": _yes_no(settings.ad_use_ssl),
            "verify_tls": _yes_no(settings.ad_verify_tls),
            "base_dn": settings.ad_base_dn or "–",
            "users_ou": settings.ad_users_ou or "–",
            "bind_dn": settings.ad_bind_dn or "–",
            "password": _set_not_set(settings.ad_bind_password),
            "ca_file": _file_state(settings.ad_ca_cert_file),
            "check_enabled": _yes_no(settings.ad_check_enabled),
        },
        "zimbra": {
            "configured": zimbra_configured,
            "badge": "Используется" if zimbra_configured else "Не настроено",
            "server": settings.zimbra_ssh_host or "–",
            "port": settings.zimbra_ssh_port,
            "user": settings.zimbra_ssh_user or "–",
            "auth": zimbra_auth,
            "password": _set_not_set(
                settings.zimbra_ssh_password
                or settings.zimbra_ssh_password_file
            ),
            "private_key": _file_state(settings.zimbra_ssh_private_key)
            if zimbra_auth in {"key", "auto"}
            else "не используется",
            "known_hosts": _file_state(settings.zimbra_ssh_known_hosts),
            "backend": settings.zimbra_backend,
            "domains": ", ".join(settings.zimbra_domains) or "–",
            "check_enabled": _yes_no(settings.zimbra_check_enabled),
        },
        "smtp": {
            "configured": smtp_configured,
            "badge": "Используется" if smtp_configured else "Не настроено",
            "server": settings.smtp_host or "–",
            "port": settings.smtp_port,
            "mode": smtp_mode,
            "username": settings.smtp_username or "–",
            "password": _set_not_set(settings.smtp_password),
            "timeout": f"{settings.smtp_timeout_seconds} сек.",
            "retries": settings.smtp_retry_attempts,
        },
        "itinvent": {
            "configured": itinvent_configured,
            "badge": (
                "Используется"
                if itinvent_configured
                else "Отключено"
                if not settings.itinvent_enabled
                else "Не настроено"
            ),
            "enabled": _yes_no(settings.itinvent_enabled),
            "server": settings.itinvent_db_host or "–",
            "port": settings.itinvent_db_port,
            "database": settings.itinvent_db_name or "ITInvent",
            "username": settings.itinvent_db_username or "–",
            "password": _set_not_set(settings.itinvent_db_password),
            "mode": "Только чтение",
        },
        "onec": {
            "configured": onec_configured,
            "badge": "Настроено" if onec_configured else "Не настроено",
            "server": settings.onec_imap_host or "–",
            "port": settings.onec_imap_port,
            "ssl": _yes_no(settings.onec_imap_ssl),
            "username": settings.onec_imap_username or "–",
            "password": _set_not_set(settings.onec_imap_password),
            "folder": settings.onec_imap_folder or "INBOX",
            "sender": settings.onec_imap_from_contains or "без фильтра",
            "lookback": f"{settings.onec_imap_lookback_days} дн.",
            "filename": settings.onec_attachment_filename or "–",
            "data_dir": settings.onec_data_dir,
            "hash_secret": (
                "ONEC_WORKER_HASH_SECRET"
                if settings.onec_worker_hash_secret.strip()
                else "APP_SECRET_KEY (временно)"
            ),
            "source_domain": (
                settings.onec_source_domain.strip().lower()
                or "автоматически по e-mail выгрузки"
            ),
            "auto_import": (
                "Включен"
                if settings.onec_auto_import_enabled
                else "Отключен"
            ),
            "auto_import_time": settings.onec_auto_import_time,
            "auto_import_timezone": settings.app_timezone,
            "startup_catchup": _yes_no(
                settings.onec_auto_import_startup_catchup
            ),
        },
    }


@router.get("/settings")
def settings_overview(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    current = require_admin(request)
    onec_service = OneCImportService(settings, db)
    registry = HRRegistryService(settings, db)
    registry_summary = registry.summary()
    integrations = _integration_overview(settings)
    itinvent_control = ITInventControlService(settings, db).summary()
    integrations["itinvent"]["control_locations"] = itinvent_control["locations"]
    integrations["itinvent"]["control_types"] = itinvent_control["types"]
    integrations["itinvent"]["control_locations_count"] = itinvent_control["locations_count"]
    integrations["itinvent"]["control_types_count"] = itinvent_control["types_count"]
    integrations["itinvent"]["control_persisted"] = itinvent_control["persisted"]
    if registry_summary.get("source_id") not in {"", "org_com"}:
        integrations["onec"]["source_domain"] = registry_summary["source_id"]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
            "integrations": integrations,
            "onec_last_report": onec_service.load_last_report(),
            "onec_registry_summary": registry_summary,
            "onec_import_history": onec_service.history(limit=20),
            "onec_schedule": schedule_info(settings),
        },
    )


@router.post("/settings/test-integration/{integration}")
def test_integration(
    integration: str,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        if integration == "ad":
            message = ActiveDirectoryService(settings).test_connection()
        elif integration == "zimbra":
            message = ZimbraService(settings).test_connection()
        elif integration == "smtp":
            message = CredentialMailer(settings).test_connection()
        elif integration == "onec":
            message = OneCImportService(settings).test_connection()
        elif integration == "itinvent":
            message = ITInventService(settings).test_connection()
        else:
            return JSONResponse(
                {"ok": False, "error": "Неизвестная интеграция"},
                status_code=404,
            )

        return {"ok": True, "message": message}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )



@router.get("/settings/itinvent/catalog")
def itinvent_catalog(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    require_admin(request)
    try:
        payload = ITInventControlService(settings, db).catalog_payload()
        return {"ok": True, **payload}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/itinvent/control")
def save_itinvent_control(
    request: Request,
    csrf: str = Form(...),
    locations_json: str = Form("[]"),
    types_json: str = Form("[]"),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        locations = json.loads(locations_json or "[]")
        equipment_types = json.loads(types_json or "[]")
        if not isinstance(locations, list) or not isinstance(equipment_types, list):
            raise ValueError("Некорректный формат настроек IT Invent")
        selection = ITInventControlService(settings, db).save_from_keys(
            location_keys=[str(value) for value in locations],
            type_keys=[str(value) for value in equipment_types],
            operator=current.username,
        )
        return {
            "ok": True,
            "locations": [item.description for item in selection.locations],
            "types": [item.type_name for item in selection.equipment_types],
            "locations_count": len(selection.locations),
            "types_count": len(selection.equipment_types),
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/onec/find-latest")
def onec_find_latest(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        result = OneCImportService(settings).find_latest()
        return {"ok": True, "mail": result}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/onec/analyze-latest")
def onec_analyze_latest(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        report = OneCImportService(
            settings,
            db,
        ).analyze_latest(trigger="manual")
        return {"ok": True, "report": report}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/onec/reconcile")
def onec_reconcile(request: Request, csrf: str = Form(...), settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf); require_admin(request)
    try:
        return {"ok": True, "summary": HRRegistryService(settings, db).reconcile_current()}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

def _mapping_page_context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    result: dict | None = None,
    error: str = "",
):
    current = require_admin(request)
    service = EmailLoginMappingService(settings, db)
    source_domain = ""
    mappings: list[dict] = []
    source_error = ""
    try:
        source_domain = service.resolve_source_domain()
        mappings = service.list_mappings(source_domain)
    except Exception as exc:
        source_error = str(exc)

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "source_domain": source_domain,
        "source_error": source_error,
        "mappings": mappings,
        "prefill_email": request.query_params.get("email", ""),
        "prefill_ad_login": request.query_params.get("ad_login", ""),
        "result": result,
        "error": error,
    }


@router.get("/settings/email-login-mapping")
def email_login_mapping_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "email_login_mapping.html",
        _mapping_page_context(
            request,
            settings=settings,
            db=db,
        ),
    )


@router.post("/settings/email-login-mapping/add")
def email_login_mapping_add(
    request: Request,
    source_email: str = Form(...),
    ad_login: str = Form(...),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        result = EmailLoginMappingService(
            settings,
            db,
        ).add_manual(
            source_email,
            ad_login,
            current.username,
        )
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                result={"manual": result},
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/email-login-mapping/import")
async def email_login_mapping_import(
    request: Request,
    file: UploadFile = File(...),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        if not file.filename.lower().endswith(".xlsx"):
            raise ValueError("Загрузите файл XLSX")
        data = await file.read()
        result = EmailLoginMappingService(
            settings,
            db,
        ).import_xlsx(
            data,
            current.username,
        )
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                result={"import": result},
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/email-login-mapping/{mapping_id}/delete")
def email_login_mapping_delete(
    mapping_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        EmailLoginMappingService(
            settings,
            db,
        ).delete_mapping(
            mapping_id,
            current.username,
        )
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                result={"deleted": True},
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "email_login_mapping.html",
            _mapping_page_context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )

