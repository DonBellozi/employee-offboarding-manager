from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.preliminary_dismissals import PreliminaryDismissalService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    result: str = "",
    error: str = "",
):
    current = require_admin(request)
    service = PreliminaryDismissalService(settings, db)
    summary = service.summary()
    sources = service.source_options()
    configured_source_ids = {
        str(rule["settings"]["source_id"])
        for rule in summary["rules"]
    }
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "summary": summary,
        "sources": sources,
        "new_sources": [
            source
            for source in sources
            if source.source_id not in configured_source_ids
        ],
        "saved": saved,
        "result": result,
        "error": error,
    }


@router.get("/settings/preliminary-dismissals")
def settings_page(
    request: Request,
    saved: int = 0,
    result: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "preliminary_dismissals.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
            result=result,
        ),
    )


@router.post("/settings/preliminary-dismissals/save")
def save_settings(
    request: Request,
    csrf: str = Form(...),
    rule_id: int = Form(0),
    source_id: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_ssl: str = Form(""),
    imap_username: str = Form(""),
    imap_password: str = Form(""),
    imap_folder: str = Form("INBOX"),
    imap_lookback_days: int = Form(7),
    sender_filter: str = Form(""),
    subject_filter: str = Form(""),
    subject_mode: str = Form("contains"),
    enabled: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        PreliminaryDismissalService(settings, db).save_settings(
            rule_id=rule_id or None,
            enabled=enabled.strip().casefold() in {"1", "true", "yes", "on"},
            source_id=source_id,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_ssl=imap_ssl.strip().casefold() in {"1", "true", "yes", "on"},
            imap_username=imap_username,
            imap_password=imap_password,
            imap_folder=imap_folder,
            imap_lookback_days=imap_lookback_days,
            sender_filter=sender_filter,
            subject_filter=subject_filter,
            operator=current.username,
            subject_mode=subject_mode,
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/preliminary-dismissals/check-now")
def check_now(
    request: Request,
    csrf: str = Form(...),
    rule_id: int = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        service = PreliminaryDismissalService(settings, db)
        result = service.process(
            force=True,
            rule_id=rule_id,
        )
        if result.get("status") in {"failed", "not_found"}:
            rule = service.get_settings(rule_id, create=False)
            message = (
                rule.last_error
                if rule is not None and rule.last_error
                else "Не удалось проверить выбранный почтовый ящик"
            )
            return templates.TemplateResponse(
                request,
                "preliminary_dismissals.html",
                _context(
                    request,
                    settings=settings,
                    db=db,
                    error=message,
                ),
                status_code=503,
            )
        message = (
            f"Ящиков: {int(result.get('rules', 0) or 0)}, "
            f"писем: {int(result.get('messages', 0) or 0)}, "
            f"работников: {int(result.get('items', 0) or 0)}, "
            f"сопоставлено: {int(result.get('matched', 0) or 0)}, "
            f"ошибок: {int(result.get('failed', 0) or 0)}."
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?result=" + quote_plus(message),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=503,
        )


@router.post("/settings/preliminary-dismissals/source-rules/save")
def save_source_rule(
    request: Request,
    csrf: str = Form(...),
    settings_id: int = Form(...),
    source_rule_id: int = Form(0),
    label: str = Form(""),
    sender_email: str = Form(...),
    subject_mode: str = Form("contains"),
    subject_value: str = Form(...),
    enabled: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        PreliminaryDismissalService(settings, db).save_source_rule(
            settings_id=settings_id,
            source_rule_id=source_rule_id or None,
            enabled=enabled.strip().casefold() in {"1", "true", "yes", "on"},
            label=label,
            sender_email=sender_email,
            subject_mode=subject_mode,
            subject_value=subject_value,
            operator=current.username,
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?result="
            + quote_plus("Правило отправителя сохранено."),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/preliminary-dismissals/source-rules/delete")
def delete_source_rule(
    request: Request,
    csrf: str = Form(...),
    settings_id: int = Form(...),
    source_rule_id: int = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        PreliminaryDismissalService(settings, db).delete_source_rule(
            settings_id=settings_id,
            source_rule_id=source_rule_id,
            operator=current.username,
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?result="
            + quote_plus("Правило отправителя удалено."),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )
