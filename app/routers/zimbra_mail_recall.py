from __future__ import annotations

import logging
import threading
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.models_zimbra_recall import ZimbraMailRecallBatch
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.zimbra_mail_cleanup import format_duration_ms
from app.services.zimbra_mail_recall import ZimbraMailRecallService
from app.time_utils import register_datetime_filters


router = APIRouter()
logger = logging.getLogger(__name__)
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))
templates.env.filters["duration_ms"] = format_duration_ms

STATUS_LABELS = {
    "draft": "В пакете",
    "queued": "В очереди",
    "running": "Удаление выполняется",
    "needs_selection": "Нужно выбрать письмо",
    "success": "Завершено",
    "warning": "Требует проверки",
    "failed": "Ошибка",
    "stopping": "Прерывается",
    "cancelled": "Остановлено",
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
            ZimbraMailRecallService(settings, db).execute_queue()
    except Exception:
        logger.exception("Background Zimbra recall failed for run %s", run_id)


def _wake_recall(settings: Settings, run_id: int = 0) -> None:
    # Do not occupy Starlette's shared thread pool for a long mailbox scan.
    threading.Thread(target=_execute_recall, args=(settings, run_id),
                     name="zimbra-recall-queue", daemon=True).start()


def _phase(run) -> str:
    if run is None:
        return ""
    if run.status == "queued":
        return "Ожидает запуска"
    if run.status == "needs_selection":
        return "Найдено несколько писем"
    if run.status == "stopping":
        return "Прерываем обработку. Ожидаем завершения текущих команд Zimbra"
    if run.status != "running":
        return STATUS_LABELS.get(run.status, run.status)
    if not run.total_mailboxes:
        return "Определяем письма и получаем список ящиков"
    return (
        f"Проверено ящиков: {int(run.processed_mailboxes or 0)} "
        f"из {int(run.total_mailboxes or 0)}"
    )


@router.get("/zimbra-recall")
def recall_page(
    request: Request,
    run_id: int = 0,
    batch_id: int = 0,
    message: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    current = require_admin(request)
    service = ZimbraMailRecallService(settings, db)
    selected_run = service.get_run(run_id) if run_id else None
    batches = service.batches()
    selected_batch = db.get(ZimbraMailRecallBatch, batch_id) if batch_id else None
    active_batches = list(reversed(service.batches(active_only=True)))
    progress_run = next((batch for batch in active_batches if batch.status in {"running", "stopping"}), None)
    progress_run = progress_run or (active_batches[0] if active_batches else None)
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
            "draft_runs": service.draft_runs(),
            "batches": batches,
            "selected_batch": selected_batch,
            "batch_runs": service.batch_runs(batch_id) if selected_batch else [],
        },
    )


@router.post("/zimbra-recall/start")
def recall_start(
    request: Request,
    background_tasks: BackgroundTasks,
    csrf: str = Form(...),
    sender_email: str = Form(...),
    recipient_email: str = Form(""),
    sent_date: str = Form(""),
    approximate_time: str = Form(""),
    time_window_minutes: int = Form(30),
    subject_hint: str = Form(""),
    message_id: str = Form(""),
    lookup_mailbox: str = Form(""),
    submit_action: str = Form("start"),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        if submit_action not in {"start", "draft"}:
            raise ValueError("Неизвестное действие")
        service = ZimbraMailRecallService(settings, db)
        run = service.prepare_run(
            sender_email=sender_email,
            recipient_email=recipient_email,
            sent_date=date.fromisoformat(sent_date) if not message_id.strip() and sent_date else None,
            approximate_time=approximate_time,
            time_window_minutes=time_window_minutes,
            subject_hint=subject_hint,
            message_id=message_id,
            actor=current.username,
            lookup_mailbox=lookup_mailbox,
            draft=True,
        )
        if submit_action == "start":
            service.queue_batch([run.id], actor=current.username)
            background_tasks.add_task(_wake_recall, settings, run.id)
        return _redirect(
            run_id=run.id,
            message="Запрос добавлен в пакет" if submit_action == "draft" else "Отзыв поставлен на выполнение. Если другой пакет уже работает, этот запустится следом",
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
        service = ZimbraMailRecallService(settings, db)
        service.queue_batch([run.id], actor=current.username, allowed_status="queued")
        background_tasks.add_task(_wake_recall, settings, run.id)
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
    batch_id: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    require_admin(request)
    service = ZimbraMailRecallService(settings, db)
    if run_id and not batch_id:
        job = service.get_run(run_id)
        batch_id = job.batch_id if job else 0
    run = db.get(ZimbraMailRecallBatch, batch_id) if batch_id else next(iter(service.batches(active_only=True)), None)
    if run is None:
        return {"active": False, "run": None}
    return {
        "active": run.status in {"queued", "running", "stopping"},
        "run": {
            "id": run.id,
            "status": run.status,
            "status_label": STATUS_LABELS.get(run.status, run.status),
            "phase": _phase(run),
            "processed_mailboxes": int(run.processed_mailboxes or 0),
            "total_mailboxes": int(run.total_mailboxes or 0),
            "found_messages": int(run.found_messages or 0),
            "deleted_messages": int(run.deleted_messages or 0),
            "remaining_messages": int(run.remaining_messages or 0),
            "error_count": int(run.error_count or 0),
            "duration": format_duration_ms(run.duration_ms),
            "result_url": f"/zimbra-recall?batch_id={run.id}#batch-result",
        },
    }


@router.post("/zimbra-recall/queue")
def recall_queue(request: Request, background_tasks: BackgroundTasks,
                 csrf: str = Form(...), run_ids: list[int] = Form([]),
                 settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        batch = ZimbraMailRecallService(settings, db).queue_batch(run_ids, actor=current.username)
        background_tasks.add_task(_wake_recall, settings, 0)
        return RedirectResponse(f"/zimbra-recall?batch_id={batch.id}", status_code=303)
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/zimbra-recall/batch/{batch_id}/cancel")
def recall_cancel_batch(batch_id: int, request: Request, csrf: str = Form(...),
                        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ZimbraMailRecallService(settings, db).cancel_batch(batch_id, actor=current.username)
        return _redirect(message="Прерывание запрошено. Уже отправленные команды Zimbra должны завершиться; удалённые письма не восстанавливаются")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/zimbra-recall/{run_id}/cancel")
def recall_cancel_draft(run_id: int, request: Request, csrf: str = Form(...),
                        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ZimbraMailRecallService(settings, db).cancel_draft(run_id, actor=current.username)
        return _redirect(message="Запрос отменён, письма по нему не удалялись")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))
