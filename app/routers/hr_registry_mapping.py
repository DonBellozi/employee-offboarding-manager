from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.hr_registry_manual_mapping import (
    HRRegistryManualMappingService,
)
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(
    Jinja2Templates(directory="app/templates")
)


@router.get('/dismissals/{run_id}/ad-mapping')
def dismissal_ad_mapping_page(run_id: int, request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    current = require_admin(request)
    from app.services.dismissal_ad_mapping import DismissalADMappingService
    try:
        context = DismissalADMappingService(settings, db).context(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return templates.TemplateResponse(request, 'dismissal_ad_mapping.html', {
        **context, 'user': current, 'csrf': get_or_create_csrf(request), 'login': '', 'candidate': None,
        'error': '', 'saved': False,
    })


@router.post('/dismissals/{run_id}/ad-mapping')
def dismissal_ad_mapping_save(run_id: int, request: Request, login: str = Form(...), action: str = Form('preview'),
    expected_guid: str = Form(''), confirmed: bool = Form(False), csrf: str = Form(...),
    settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf)
    current = require_admin(request)
    from app.services.dismissal_ad_mapping import DismissalADMappingService
    service = DismissalADMappingService(settings, db)
    try:
        context = service.context(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    candidate, saved, error = None, False, ''
    try:
        if action == 'save':
            service.save(run_id, login, expected_guid, current.username, confirmed)
            saved = True
        elif action == 'preview':
            candidate = service.inspect(run_id, login)
        else:
            raise ValueError('Неизвестное действие')
    except ValueError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(request, 'dismissal_ad_mapping.html', {
        **context, 'user': current, 'csrf': get_or_create_csrf(request), 'login': login,
        'candidate': candidate, 'error': error, 'saved': saved,
    }, status_code=400 if error else 200)


def _context(
    request: Request,
    *,
    record_id: int,
    settings: Settings,
    db: Session,
    error: str = "",
    embedded: bool = False,
    saved: bool = False,
):
    current = require_admin(request)
    service = HRRegistryManualMappingService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "item": service.page_data(record_id),
        "error": error,
        "embedded": embedded,
        "saved": saved,
    }


@router.get("/employees/registry/{record_id}/map")
def mapping_page(
    record_id: int,
    request: Request,
    modal: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "hr_registry_mapping.html",
        _context(
            request,
            record_id=record_id,
            settings=settings,
            db=db,
            embedded=bool(modal),
        ),
    )


@router.post("/employees/registry/{record_id}/map")
def mapping_save(
    record_id: int,
    request: Request,
    identifier: str = Form(...),
    csrf: str = Form(...),
    modal: str = Form("0"),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        service = HRRegistryManualMappingService(settings, db)
        result = service.save_identifier(
            record_id=record_id,
            identifier=identifier,
            actor=current.username,
        )
        MultiSourceHRRegistryViewService(
            settings,
            db,
        ).reconcile_all()

        if modal.strip().lower() in {"1", "true", "yes", "on"}:
            return templates.TemplateResponse(
                request,
                "hr_registry_mapping.html",
                _context(
                    request,
                    record_id=record_id,
                    settings=settings,
                    db=db,
                    embedded=True,
                    saved=True,
                ),
            )

        return RedirectResponse(
            "/employees/registry?"
            + urlencode(
                {
                    "q": result["fio"],
                    "source": result["source_id"],
                }
            ),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "hr_registry_mapping.html",
            _context(
                request,
                record_id=record_id,
                settings=settings,
                db=db,
                error=str(exc),
                embedded=modal.strip().lower() in {"1", "true", "yes", "on"},
            ),
            status_code=400,
        )


@router.post("/employees/registry/{record_id}/map/delete")
def mapping_delete(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    modal: str = Form("0"),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    service = HRRegistryManualMappingService(settings, db)
    record = service.get_record(record_id)
    fio = record.fio
    source_id = record.source_id
    service.delete_for_record(
        record_id=record_id,
        actor=current.username,
    )
    MultiSourceHRRegistryViewService(
        settings,
        db,
    ).reconcile_all()

    if modal.strip().lower() in {"1", "true", "yes", "on"}:
        return templates.TemplateResponse(
            request,
            "hr_registry_mapping.html",
            _context(
                request,
                record_id=record_id,
                settings=settings,
                db=db,
                embedded=True,
                saved=True,
            ),
        )

    return RedirectResponse(
        "/employees/registry?"
        + urlencode({"q": fio, "source": source_id}),
        status_code=303,
    )
