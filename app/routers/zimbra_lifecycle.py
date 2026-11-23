from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, require_operator, validate_csrf
from app.services.zimbra_lifecycle import ZimbraLifecycleService
from app.services.zimbra_employment_lifecycle import (
    ZimbraEmploymentLifecycleService,
)
from app.time_utils import register_datetime_filters
from app.models_zimbra_lifecycle import ZimbraEmploymentAction
from app.services.zimbra_primary_transition import (
    ZimbraPrimaryTransitionService, STATUS_LABELS as TRANSITION_STATUSES, PHASE_LABELS,
)


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))

ACTION_LABELS = {
    "close": "Закрытие",
    "backup_delete": "Backup + удаление",
    "backup": "Backup",
    "delete": "Удаление",
    "skip": "Пропуск",
}
STATUS_LABELS = {
    "planned": "Запланировано",
    "success": "Успешно",
    "blocked": "Не разрешено",
    "failed": "Ошибка",
}


def _transition_context(request, settings, db, action_id, error='', *, manual=None, manual_email=None, manual_login=''):
    user = require_operator(request)
    action = db.get(ZimbraEmploymentAction, action_id)
    if action is None or action.action != 'transition':
        raise HTTPException(404, 'Переход не найден')
    service = ZimbraPrimaryTransitionService(settings, db)
    job = service.job(action_id)
    spec = None
    if job is None or job.status == 'cancelled':
        # Paint the review immediately. Live HR/Zimbra validation remains
        # mandatory in enqueue and before every background phase.
        spec = action
    return dict(user=user, csrf=get_or_create_csrf(request), action=action, job=job, spec=spec,
        error=error, backup_allowed=service.employment._settings().allow_backup,
        transition_statuses=TRANSITION_STATUSES, phase_labels=PHASE_LABELS,
        manual=manual, manual_email=manual_email if manual_email is not None else (job.new_address if job else action.replacement_address),
        manual_login=manual_login)


@router.get('/zimbra-transitions/{action_id}')
def transition_page(action_id: int, request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
        _transition_context(request, settings, db, action_id))


@router.post('/zimbra-transitions/{action_id}')
def transition_enqueue(action_id: int, request: Request, csrf: str = Form(...),
        expected_old: str = Form(...), expected_new: str = Form(...), confirmed: str = Form(''),
        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    user = require_operator(request)
    validate_csrf(request, csrf)
    try:
        ZimbraPrimaryTransitionService(settings, db).enqueue(action_id, user.username,
            expected_old=expected_old, expected_new=expected_new, confirmed=confirmed == 'true')
        return RedirectResponse(f'/zimbra-transitions/{action_id}', status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
            _transition_context(request, settings, db, action_id, str(exc)), status_code=400)


@router.post('/zimbra-transitions/{action_id}/retry')
def transition_retry(action_id: int, request: Request, csrf: str = Form(...),
        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    user = require_operator(request)
    validate_csrf(request, csrf)
    try:
        ZimbraPrimaryTransitionService(settings, db).retry(action_id, user.username)
        return RedirectResponse(f'/zimbra-transitions/{action_id}', status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
            _transition_context(request, settings, db, action_id, str(exc)), status_code=400)


@router.post('/zimbra-transitions/{action_id}/manual')
def transition_manual(action_id: int, request: Request, csrf: str = Form(...), email: str = Form(...),
        ad_login: str = Form(''), operation: str = Form('inspect'), expected_id: str = Form(''),
        expected_guid: str = Form(''), confirmed: str = Form(''),
        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    user = require_operator(request)
    validate_csrf(request, csrf)
    service = ZimbraPrimaryTransitionService(settings, db)
    try:
        if operation == 'inspect':
            manual = service.inspect_manual(action_id, email, ad_login)
            return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
                _transition_context(request, settings, db, action_id,
                    manual=manual, manual_email=email, manual_login=ad_login))
        if operation != 'confirm':
            raise ValueError('Неизвестное действие')
        service.confirm_manual(action_id, user.username, email=email, ad_login=ad_login,
            expected_id=expected_id, expected_guid=expected_guid, confirmed=confirmed == 'true')
        return RedirectResponse(f'/zimbra-transitions/{action_id}', status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
            _transition_context(request, settings, db, action_id, str(exc), manual_email=email, manual_login=ad_login),
            status_code=400)


@router.get('/zimbra-transitions/{action_id}/status')
def transition_status(action_id: int, request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    require_operator(request)
    job = ZimbraPrimaryTransitionService(settings, db).job(action_id)
    if job is None:
        raise HTTPException(404, 'Задание не найдено')
    return dict(status=job.status, label=TRANSITION_STATUSES.get(job.status, job.status), error=job.last_error)


@router.post('/zimbra-transitions/{action_id}/cancel')
def transition_cancel(action_id: int, request: Request, csrf: str = Form(...),
        settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    user = require_operator(request)
    validate_csrf(request, csrf)
    try:
        ZimbraPrimaryTransitionService(settings, db).cancel(action_id, user.username)
        return RedirectResponse(f'/zimbra-transitions/{action_id}', status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(request, 'zimbra_primary_transition.html',
            _transition_context(request, settings, db, action_id, str(exc)), status_code=400)


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    run_id: int | None = None,
    saved: bool = False,
    error: str = "",
):
    current = require_admin(request)
    service = ZimbraLifecycleService(settings, db)
    run = service.get_run(run_id) if run_id else None
    recent = service.recent_runs(limit=20)
    if run is None:
        run = recent[0] if recent else None

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "lifecycle": service.settings_view(),
        "run": run,
        "actions": service.run_actions(run.id) if run is not None else [],
        "runs": recent,
        "employment_actions": ZimbraEmploymentLifecycleService(
            settings, db
        ).recent_actions(),
        "action_labels": ACTION_LABELS,
        "status_labels": STATUS_LABELS,
        "saved": saved,
        "error": error,
    }


@router.get("/settings/zimbra-lifecycle")
def lifecycle_page(
    request: Request,
    run_id: int | None = None,
    saved: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "zimbra_lifecycle.html",
        _context(
            request,
            settings=settings,
            db=db,
            run_id=run_id,
            saved=bool(saved),
        ),
    )


@router.post("/settings/zimbra-lifecycle")
def lifecycle_save(
    request: Request,
    csrf: str = Form(...),
    allow_close: str = Form(""),
    allow_employment_close: str = Form(""),
    allow_alias_remove: str = Form(""),
    allow_backup: str = Form(""),
    allow_delete: str = Form(""),
    backup_dir: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    truthy = {"1", "true", "yes", "on"}
    try:
        ZimbraLifecycleService(settings, db).save_settings(
            allow_close=allow_close.strip().lower() in truthy,
            allow_employment_close=(
                allow_employment_close.strip().lower() in truthy
            ),
            allow_alias_remove=(
                allow_alias_remove.strip().lower() in truthy
            ),
            allow_backup=allow_backup.strip().lower() in truthy,
            allow_delete=allow_delete.strip().lower() in truthy,
            backup_dir=backup_dir,
            operator=current.username,
        )
        return RedirectResponse(
            "/settings/zimbra-lifecycle?saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_lifecycle.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/zimbra-lifecycle/plan")
def lifecycle_plan(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    run = ZimbraLifecycleService(settings, db).build_plan(current.username)
    return RedirectResponse(
        f"/settings/zimbra-lifecycle?run_id={run.id}",
        status_code=303,
    )


@router.post("/settings/zimbra-lifecycle/execute")
def lifecycle_execute(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        run = ZimbraLifecycleService(settings, db).execute(current.username)
        return RedirectResponse(
            f"/settings/zimbra-lifecycle?run_id={run.id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_lifecycle.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )
