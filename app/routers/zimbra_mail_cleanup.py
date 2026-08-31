from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.zimbra_mail_cleanup import (
    WEEKDAY_LABELS,
    ZimbraMailCleanupService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))
TRUTHY = {"1", "true", "yes", "on"}

MODE_LABELS = {
    "dry_run": "Проверка",
    "manual_cleanup": "Ручная очистка",
    "automatic_cleanup": "Недельная очистка",
}
STATUS_LABELS = {
    "running": "Выполняется",
    "success": "Успешно",
    "warning": "Есть ограничение",
    "partial": "Частично",
    "failed": "Ошибка",
}
CONDITION_LABELS = {"from": "От кого", "to": "Кому"}
SCOPE_LABELS = {
    "all": "Все пользовательские ящики",
    "selected": "Только выбранные",
    "except": "Все, кроме выбранных",
}


def _redirect(*, message: str = "", error: str = "", run_id: int = 0):
    query: list[str] = []
    if message:
        query.append(f"message={quote_plus(message)}")
    if error:
        query.append(f"error={quote_plus(error)}")
    if run_id:
        query.append(f"run_id={int(run_id)}")
    suffix = "?" + "&".join(query) if query else ""
    return RedirectResponse(
        f"/settings/zimbra-mail-cleanup{suffix}",
        status_code=303,
    )


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    edit_id: int = 0,
    run_id: int = 0,
    message: str = "",
    error: str = "",
):
    current = require_admin(request)
    service = ZimbraMailCleanupService(settings, db)
    rules = service.rules()
    edit_rule = next((rule for rule in rules if rule.id == edit_id), None)
    selected_run = service.get_run(run_id) if run_id else None
    rule_views = []
    for rule in rules:
        preview = service.latest_preview(rule)
        rule_views.append(
            {
                "rule": rule,
                "mailboxes": service.rule_mailboxes(rule),
                "preview": preview,
                "preview_fresh": service.preview_is_fresh(preview),
            }
        )
    selected_rule = (
        next(
            (rule for rule in rules if rule.id == selected_run.rule_id),
            None,
        )
        if selected_run is not None
        else None
    )
    selected_run_can_cleanup = bool(
        selected_run is not None
        and selected_rule is not None
        and selected_run.mode == "dry_run"
        and selected_run.status in {"success", "warning"}
        and selected_run.found_messages > 0
        and selected_run.rule_snapshot_json
        == service._snapshot_json(selected_rule)
        and service.preview_is_fresh(selected_run)
    )
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "cleanup": service.settings_view(),
        "rule_views": rule_views,
        "edit_rule": edit_rule,
        "edit_mailboxes": (
            "\n".join(service.rule_mailboxes(edit_rule)) if edit_rule else ""
        ),
        "selected_run": selected_run,
        "selected_run_details": service.run_details(selected_run),
        "selected_run_can_cleanup": selected_run_can_cleanup,
        "runs": service.recent_runs(limit=30),
        "weekday_labels": WEEKDAY_LABELS,
        "mode_labels": MODE_LABELS,
        "status_labels": STATUS_LABELS,
        "condition_labels": CONDITION_LABELS,
        "scope_labels": SCOPE_LABELS,
        "message": message,
        "error": error,
        "global_dry_run": settings.dry_run,
    }


@router.get("/settings/zimbra-mail-cleanup")
def cleanup_page(
    request: Request,
    edit_id: int = 0,
    run_id: int = 0,
    message: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "zimbra_mail_cleanup.html",
        _context(
            request,
            settings=settings,
            db=db,
            edit_id=edit_id,
            run_id=run_id,
            message=message,
            error=error,
        ),
    )


@router.post("/settings/zimbra-mail-cleanup/schedule")
def cleanup_schedule_save(
    request: Request,
    csrf: str = Form(...),
    schedule_mode: str = Form(...),
    schedule_weekday: int = Form(...),
    schedule_time: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ZimbraMailCleanupService(settings, db).save_settings(
            schedule_mode=schedule_mode,
            schedule_weekday=schedule_weekday,
            schedule_time=schedule_time,
            actor=current.username,
        )
        return _redirect(message="Расписание очистки сохранено")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/settings/zimbra-mail-cleanup/rules/save")
def cleanup_rule_save(
    request: Request,
    csrf: str = Form(...),
    rule_id: int = Form(0),
    name: str = Form(...),
    condition_type: str = Form(...),
    condition_value: str = Form(...),
    retention_days: int = Form(...),
    scope_mode: str = Form(...),
    mailboxes: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    service = ZimbraMailCleanupService(settings, db)
    try:
        if rule_id:
            rule = service.update_rule(
                rule_id,
                name=name,
                condition_type=condition_type,
                condition_value=condition_value,
                retention_days=retention_days,
                scope_mode=scope_mode,
                mailboxes=mailboxes,
                actor=current.username,
            )
            message = (
                f"Правило «{rule.name}» сохранено и выключено до новой проверки"
            )
        else:
            rule = service.create_rule(
                name=name,
                condition_type=condition_type,
                condition_value=condition_value,
                retention_days=retention_days,
                scope_mode=scope_mode,
                mailboxes=mailboxes,
                actor=current.username,
            )
            message = f"Правило «{rule.name}» создано выключенным"
        return _redirect(message=message)
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/settings/zimbra-mail-cleanup/rules/{rule_id}/check")
def cleanup_rule_check(
    rule_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        run = ZimbraMailCleanupService(settings, db).dry_run(
            rule_id,
            actor=current.username,
        )
        return _redirect(
            message=(
                f"Проверка завершена: найдено {run.found_messages}, "
                f"ошибок {run.error_count}"
            ),
            run_id=run.id,
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/settings/zimbra-mail-cleanup/rules/{rule_id}/state")
def cleanup_rule_state(
    rule_id: int,
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    automatic_cleanup: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        row = ZimbraMailCleanupService(settings, db).set_rule_state(
            rule_id,
            enabled=enabled.strip().lower() in TRUTHY,
            automatic_cleanup=automatic_cleanup.strip().lower() in TRUTHY,
            actor=current.username,
        )
        return _redirect(
            message=(
                f"Правило «{row.name}»: "
                f"{'включено' if row.enabled else 'выключено'}"
            )
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/settings/zimbra-mail-cleanup/rules/{rule_id}/cleanup")
def cleanup_rule_execute(
    rule_id: int,
    request: Request,
    csrf: str = Form(...),
    preview_run_id: int = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        run = ZimbraMailCleanupService(settings, db).manual_cleanup(
            rule_id,
            preview_run_id=preview_run_id,
            actor=current.username,
        )
        return _redirect(
            message=(
                f"Очистка завершена: удалено {run.deleted_messages}, "
                f"ошибок {run.error_count}"
            ),
            run_id=run.id,
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/settings/zimbra-mail-cleanup/rules/{rule_id}/delete")
def cleanup_rule_delete(
    rule_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ZimbraMailCleanupService(settings, db).delete_rule(
            rule_id,
            actor=current.username,
        )
        return _redirect(message="Правило удалено")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))
