from __future__ import annotations

import logging
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.zimbra_mail_cleanup import format_duration_ms
from app.services.zimbra_mail_recall import ZimbraMailRecallService
from app.time_utils import register_datetime_filters


router = APIRouter()
logger = logging.getLogger(__name__)
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))
templates.env.filters["duration_ms"] = format_duration_ms

STATUS_LABELS = {
    "queued": "В очереди",
    "running": "Удаление выполняется",
    "needs_selection": "Нужно выбрать письмо",
    "success": "Завершено",
    "warning": "Требует проверки",
    "failed": "Ошибка",
}


def _redirect(
    *,
    run_id: int = 0,
    message: str = "",
    error: str = "",
):
    query: list[str] = []
    if run_id:
        query.append(f"run_id={int(run_id)}")
    if message:
        query.append(f"message={quote_plus(message)}")
    if error:
        query.append(f"error={quote_plus(error)}")
    suffix = "?" + "&".join(query) if query else ""
    return RedirectResponse(f"/zimbra-recall{suffix}", status_code=303)


def _execute_recall(settings: Settings, run_id: int) -> None:
    try:
        with SessionLocal() as db:
            ZimbraMailRecallService(settings, db).execute_run(run_id)
    except Exception:
        logger.exception("Background Zimbra recall failed for run %s", run_id)


def _phase(run) -> str:
    if run is None:
        return ""
    if run.status == "queued":
        return "Ожидает запуска"
    if run.status == "needs_selection":
        return "Найдено несколько писем"
    if run.status != "running":
        return STATUS_LABELS.get(run.status, run.status)
    if not run.message_id:
        return "Ищем исходное письмо в «Отправленных»"
    if not run.total_mailboxes:
        return "Получаем список почтовых ящиков"
    return (
        f"Проверено ящиков: {int(run.processed_mailboxes or 0)} "
        f"из {int(run.total_mailboxes or 0)}"
    )


@router.get("/zimbra-recall")
def recall_page(
    request: Request,
    run_id: int = 0,
    message: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    current = require_admin(request)
    service = ZimbraMailRecallService(settings, db)
    selected_run = service.get_run(run_id) if run_id else None
    active_run = service.active_run()
    progress_run = active_run or (
        selected_run
        if selected_run is not None
        and selected_run.status in {"queued", "running"}
        else None
    )
    return templates.TemplateResponse(
        request,
        "zimbra_mail_recall.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
            "message": message,
            "error": error,
            "selected_run": selected_run,
            "selected_candidates": service.candidates(selected_run),
            "selected_details": service.details(selected_run),
            "active_run": progress_run,
            "active_phase": _phase(progress_run),
            "runs": service.recent_runs(limit=30),
            "status_labels": STATUS_LABELS,
            "global_dry_run": settings.dry_run,
        },
    )


@router.post("/zimbra-recall/start")
def recall_start(
    request: Request,
    background_tasks: BackgroundTasks,
    csrf: str = Form(...),
    sender_email: str = Form(...),
    recipient_email: str = Form(...),
    sent_date: date = Form(...),
    approximate_time: str = Form(...),
    time_window_minutes: int = Form(30),
    subject_hint: str = Form(""),
    message_id: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        run = ZimbraMailRecallService(settings, db).prepare_run(
            sender_email=sender_email,
            recipient_email=recipient_email,
            sent_date=sent_date,
            approximate_time=approximate_time,
            time_window_minutes=time_window_minutes,
            subject_hint=subject_hint,
            message_id=message_id,
            actor=current.username,
        )
        background_tasks.add_task(_execute_recall, settings, run.id)
        return _redirect(
            run_id=run.id,
            message="Отзыв запущен",
        )
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/zimbra-recall/{run_id}/select/{candidate_index}")
def recall_select_candidate(
    run_id: int,
    candidate_index: int,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        run = ZimbraMailRecallService(settings, db).select_candidate(
            run_id,
            candidate_index,
            actor=current.username,
        )
        background_tasks.add_task(_execute_recall, settings, run.id)
        return _redirect(
            run_id=run.id,
            message="Письмо выбрано, удаление запущено",
        )
    except Exception as exc:
        db.rollback()
        return _redirect(run_id=run_id, error=str(exc))


@router.get("/zimbra-recall/progress")
def recall_progress(
    request: Request,
    run_id: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    require_admin(request)
    service = ZimbraMailRecallService(settings, db)
    run = service.get_run(run_id) if run_id else service.active_run()
    if run is None:
        return {"active": False, "run": None}
    return {
        "active": run.status in {"queued", "running"},
        "run": {
            "id": run.id,
            "status": run.status,
            "status_label": STATUS_LABELS.get(run.status, run.status),
            "phase": _phase(run),
            "processed_mailboxes": int(run.processed_mailboxes or 0),
            "total_mailboxes": int(run.total_mailboxes or 0),
            "matched_mailboxes": int(run.matched_mailboxes or 0),
            "found_messages": int(run.found_messages or 0),
            "deleted_messages": int(run.deleted_messages or 0),
            "remaining_messages": int(run.remaining_messages or 0),
            "error_count": int(run.error_count or 0),
            "duration": format_duration_ms(run.duration_ms),
            "result_url": f"/zimbra-recall?run_id={run.id}#result",
        },
    }
