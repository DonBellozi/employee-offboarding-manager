"""Regression checks use the real application, not a separately assembled router."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import main, security
from app.config import Settings
from app.db import Base
from app.models import HRSourceRecord, OneCImportRun
from app.models_account_requirement import AccountRequirementCase, AccountRequirementDecisionEvent
from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.routers import account_requirement, employees
from app.security import CurrentUser


def request(path, method="GET"):
    parts = urlsplit(path)
    return Request({
        "type": "http", "method": method, "scheme": "http", "path": parts.path,
        "query_string": parts.query.encode(), "root_path": "", "app": main.app,
        "router": main.app.router, "server": ("test", 80), "client": ("test", 1),
        "session": {"csrf": "token"}, "headers": [(b"host", b"test")],
    })


def test_main_registers_decision_form_and_history_routes():
    paths = main.app.openapi()["paths"]
    assert "post" in paths.get("/employees/account-requirements/{case_id}/decision", {})
    assert "get" in paths.get("/account-requirements/{case_id}", {})
    assert "get" in paths.get("/admin/account-requirements", {})
    assert "get" in paths.get("/admin/account-requirements-export", {})


@pytest.fixture
def scenario(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = CurrentUser("operator", "operator", "local")
        monkeypatch.setattr(security, "get_current_user", lambda req: user)
        monkeypatch.setattr(employees, "get_current_user", lambda req: user)
        inspect = Mock(return_value={"errors": [], "has_candidates": False})
        monkeypatch.setattr(employees.EmployeeArrivalAccountService, "inspect", inspect)
        provision = Mock(side_effect=AssertionError("A decision must not create accounts"))
        monkeypatch.setattr(employees.ProvisioningService, "provision", provision)
        settings = Settings(_env_file=None, zimbra_domains=["example.test"],
                            zimbra_primary_domain="example.test")
        db.add(HRSourceRecord(worker_key="worker", source_id="example.test", source_name="Организация",
            fio="Иванов Иван Иванович", login="ivanov", corporate_email="ivanov@example.test",
            personal_email="personal@example.com", is_present=True,
            placements_json=json.dumps([{"department": "ИТ", "position": "Инженер"}])))
        arrival = HREmploymentArrivalEvent(worker_key="worker", source_id="example.test",
            source_name="Организация", fio="Иванов Иван Иванович", sequence=1, status="pending")
        db.add(arrival)
        db.commit()
        response = employees.new_employee(request("/employees/new"), arrival_event_ids=str(arrival.id),
                                          db=db, settings=settings)
        assert response.status_code == 200
        case = db.scalar(select(AccountRequirementCase))
        assert case is not None
        yield SimpleNamespace(db=db, arrival=arrival, case=case, settings=settings,
                              inspect=inspect, provision=provision, initial=response.body.decode())
    engine.dispose()


def decide(s, **changes):
    data = {"case_id": s.case.id, "action": "required", "revision": s.case.revision,
            "comment": "", "confirmed": "", "csrf": "token", "db": s.db, "settings": s.settings,
            **changes}
    return account_requirement.decide_requirement(
        request(f"/employees/account-requirements/{s.case.id}/decision", "POST"), **data)


def test_required_decision_opens_prefilled_creation_and_records_it_once(scenario):
    s = scenario
    assert f'action="/employees/account-requirements/{s.case.id}/decision"' in s.initial
    assert 'id="provision-form"' not in s.initial
    response = decide(s)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/employees/new"
    ids = parse_qs(target.query)["arrival_event_ids"][0]
    assert ids == str(s.arrival.id)
    page = employees.new_employee(request(response.headers["location"]), arrival_event_ids=ids,
                                  db=s.db, settings=s.settings)
    html = page.body.decode()
    assert page.status_code == 200
    assert html.count('id="provision-form"') == 1
    assert "Проверка и создание учётных записей" in html
    assert "Необходимость корпоративной учетной записи" not in html
    for value in ("Иванов", "Иван", "Иванович", "ivanov", "personal@example.com"):
        assert f'value="{value}"' in html
    assert '<option value="example.test" selected>' in html
    assert s.case.decision == "required" and s.case.decided_by == "operator"
    assert s.case.decision_snapshot_json and s.case.decided_at
    assert not s.case.created_account and s.case.external_action_status == "not_started"
    decisions = list(s.db.scalars(select(AccountRequirementDecisionEvent)))
    assert sum(row.new_decision == "required" for row in decisions) == 1
    employees.new_employee(request(response.headers["location"]), arrival_event_ids=ids,
                           db=s.db, settings=s.settings)
    assert len(list(s.db.scalars(select(AccountRequirementDecisionEvent)))) == len(decisions)
    detail = account_requirement.requirement_detail(request(f"/account-requirements/{s.case.id}"),
                                                   s.case.id, db=s.db)
    assert detail.status_code == 200 and "Учетная запись нужна" in detail.body.decode()
    s.provision.assert_not_called()


@pytest.mark.parametrize("condition", ["directory_error", "existing_account", "stale_revision", "import"])
def test_rejected_decisions_do_not_open_creation(scenario, condition):
    s = scenario
    changes = {}
    if condition == "directory_error":
        s.inspect.return_value = {"errors": ["AD unavailable"], "has_candidates": False}
    elif condition == "existing_account":
        s.inspect.return_value = {"errors": [], "has_candidates": True}
    elif condition == "stale_revision":
        changes["revision"] = 0
    else:
        s.db.add(OneCImportRun(status="running"))
        s.db.commit()
    response = decide(s, **changes)
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == f"/account-requirements/{s.case.id}"
    assert s.case.decision is None
    s.provision.assert_not_called()


def test_csrf_and_operator_role_remain_required(scenario, monkeypatch):
    s = scenario
    with pytest.raises(security.CSRFMismatchError):
        decide(s, csrf="wrong")
    monkeypatch.setattr(security, "get_current_user", lambda req: CurrentUser("viewer", "viewer", "local"))
    with pytest.raises(HTTPException) as error:
        decide(s)
    assert error.value.status_code == 403
    assert s.case.decision is None
    s.provision.assert_not_called()


def complete_immediately(awaitable):
    """The mocked lifespan has no asynchronous I/O and needs no socket event loop."""
    iterator = awaitable.__await__()
    try:
        iterator.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        iterator.close()
        pytest.fail("The mocked lifespan unexpectedly requested asynchronous I/O")


@pytest.mark.parametrize("context_error", [False, True])
def test_requirement_worker_starts_and_stops_with_real_lifespan(monkeypatch, context_error):
    constructors = {}
    for name in list(vars(main)):
        if name.endswith(("Worker", "Scheduler")):
            constructor = Mock(name=name)
            constructors[name] = constructor
            monkeypatch.setattr(main, name, constructor)
    requirement = Mock(name="AccountRequirementWorker")
    constructors["AccountRequirementWorker"] = requirement
    monkeypatch.setattr(main, "AccountRequirementWorker", requirement, raising=False)
    monkeypatch.setattr(main, "settings", SimpleNamespace(app_secret_key="test-secret"))
    monkeypatch.setattr(main, "engine", Mock())
    monkeypatch.setattr(main.Base.metadata, "create_all", Mock())
    monkeypatch.setattr(main, "ensure_compatibility_schema", Mock())
    monkeypatch.setattr(main, "ensure_bootstrap_admin", Mock())
    monkeypatch.setattr(main, "SessionLocal", MagicMock())
    monkeypatch.setattr(main, "OneCSourceRegistryService", Mock())
    monkeypatch.setattr(main, "ZimbraMailCleanupService", Mock())
    monkeypatch.setattr(main, "ZimbraMailRecallService", Mock())
    context = main.lifespan(main.app)
    complete_immediately(context.__aenter__())
    requirement.assert_called_once_with(main.settings, main.SessionLocal)
    requirement.return_value.start.assert_called_once_with()
    if context_error:
        assert not complete_immediately(context.__aexit__(RuntimeError, RuntimeError("test"), None))
    else:
        complete_immediately(context.__aexit__(None, None, None))
    for constructor in constructors.values():
        constructor.return_value.stop.assert_called_once_with()
