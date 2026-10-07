from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.config import Settings, get_settings
from app.db import get_db
from app.models_account_requirement import AccountRequirementCase, AccountRequirementDecisionEvent
from app.security import get_or_create_csrf, require_admin, require_operator, validate_csrf
from app.services.account_requirement import (
    AccountRequirementService, STATE_LABELS, DECISION_LABELS, build_account_requirement_dataset,
)
from app.services.employee_arrival_accounts import EmployeeArrivalAccountService
from app.time_utils import register_datetime_filters

router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


@router.post("/employees/account-requirements/{case_id}/decision")
def decide_requirement(request: Request, case_id: int, action: str = Form(...),
                       revision: int = Form(...), comment: str = Form(""),
                       confirmed: str = Form(""), csrf: str = Form(...),
                       db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    user = require_operator(request)
    validate_csrf(request, csrf)
    service = AccountRequirementService(db)
    case = db.get(AccountRequirementCase, case_id)
    if case is None:
        raise HTTPException(404, "Решение не найдено")
    raw_ids = ",".join(map(str, service.current_event_ids(case)))
    try:
        if case.state in {"pending", "clarification"} and action != "cancelled":
            state = EmployeeArrivalAccountService(settings, db).inspect(raw_ids)
            if state.get("errors"):
                raise ValueError("Проверка AD/Zimbra не завершена; повторите после восстановления связи")
            if state.get("has_candidates"):
                service.cancel(case, "Найдены существующие учетки", actor=user.username)
                raise ValueError("Найдены существующие учетки. Используйте сопоставление или восстановление")
        case = service.decide(case_id, action=action, actor=user.username,
                              is_admin=user.role == "admin", comment=comment,
                              expected_revision=revision, confirmed=confirmed == "true")
        raw_ids = ",".join(map(str, service.current_event_ids(case)))
        if action == "required" and raw_ids and not case.created_account:
            return RedirectResponse("/employees/new?" + urlencode({"arrival_event_ids": raw_ids}), status_code=303)
        return RedirectResponse(f"/account-requirements/{case.id}", status_code=303)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, StaleDataError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, ValueError) else "Решение уже изменилось. Обновите страницу"
        return RedirectResponse(f"/account-requirements/{case_id}?" + urlencode({"error": message}), status_code=303)


@router.get("/account-requirements/{case_id}")
def requirement_detail(request: Request, case_id: int, error: str = "", db: Session = Depends(get_db)):
    user = require_operator(request)
    service = AccountRequirementService(db)
    case = db.get(AccountRequirementCase, case_id)
    if case is None:
        raise HTTPException(404, "Решение не найдено")
    history = db.scalars(select(AccountRequirementDecisionEvent).where(
        AccountRequirementDecisionEvent.case_id == case.id,
    ).order_by(AccountRequirementDecisionEvent.id)).all()
    snapshot = json.loads(case.decision_snapshot_json or case.initial_snapshot_json)
    if case.state in {"pending", "clarification"}:
        try:
            snapshot = service.snapshot(case.worker_key)
        except ValueError:
            error = error or "Активная занятость больше не подтверждена. Ожидается фоновая отмена случая"
    return templates.TemplateResponse(request, "account_requirement_detail.html", {
        "user": user, "csrf": get_or_create_csrf(request), "case": case,
        "snapshot": snapshot,
        "history": history, "state_labels": STATE_LABELS, "decision_labels": DECISION_LABELS,
        "arrival_event_ids": ",".join(map(str, service.current_event_ids(case))), "error": error,
    })


@router.get("/admin/account-requirements")
def requirement_list(request: Request, state: str = "", decision: str = "", organization: str = "",
                     department: str = "", position: str = "", date_from: str = "",
                     date_to: str = "", db: Session = Depends(get_db)):
    user = require_admin(request)
    try:
        from_date = date.fromisoformat(date_from) if date_from else None
        to_date = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise HTTPException(400, "Некорректный период") from exc
    query = select(AccountRequirementCase).order_by(AccountRequirementCase.id.desc())
    if state:
        query = query.where(AccountRequirementCase.state == state)
    if decision:
        query = query.where(AccountRequirementCase.decision == decision)
    rows = []
    for case in db.scalars(query).all():
        stamp = case.decided_at or case.created_at
        if (from_date and stamp.date() < from_date) or (to_date and stamp.date() > to_date):
            continue
        snapshot = json.loads(case.decision_snapshot_json or case.initial_snapshot_json)
        placements = snapshot.get("placements", [])
        filters = {"organization": organization, "department": department, "position": position}
        if not any(all(value.casefold() in str(row.get(key, "")).casefold()
                       for key, value in filters.items() if value) for row in placements):
            continue
        rows.append({"case": case, "placements": placements})
    return templates.TemplateResponse(request, "account_requirement_list.html", {
        "user": user, "csrf": get_or_create_csrf(request), "rows": rows,
        "state_labels": STATE_LABELS, "decision_labels": DECISION_LABELS,
        "filters": {"state": state, "decision": decision, "organization": organization,
                    "department": department, "position": position,
                    "date_from": date_from or "", "date_to": date_to or ""},
    })


@router.get("/admin/account-requirements-export")
def requirement_export(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    dataset = build_account_requirement_dataset(db)
    return JSONResponse(dataset, headers={
        "Content-Disposition": 'attachment; filename="account-requirement-dataset.json"',
        "Cache-Control": "no-store", "X-Dataset-SHA256": dataset["dataset_sha256"],
    })
