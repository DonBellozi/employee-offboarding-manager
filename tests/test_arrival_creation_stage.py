"""The positive decision switches screens without provisioning or losing its label."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import security
from app.config import Settings, get_settings
from app.db import Base, get_db
from app.models import HRSourceRecord
from app.models_account_requirement import AccountRequirementCase, AccountRequirementDecisionEvent
from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.routers import account_requirement, employees
from app.security import CurrentUser


class ASGIClient:
    def __init__(self, api):
        self.api = api

    def request(self, method, url, data=None):
        async def run():
            parts = urlsplit(url)
            body = urlencode(data or {}).encode()
            messages = []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await self.api({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": parts.path,
                "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
                "root_path": "", "client": ("test", 1), "server": ("test", 80),
                "session": {"csrf": "token"}, "headers": [
                    (b"host", b"test"), (b"content-type", b"application/x-www-form-urlencoded")],
            }, receive, send)
            start = next(item for item in messages if item["type"] == "http.response.start")
            content = b"".join(item.get("body", b"") for item in messages
                               if item["type"] == "http.response.body")
            return SimpleNamespace(status_code=start["status"], text=content.decode(),
                                   headers={k.decode(): v.decode() for k, v in start["headers"]})
        return asyncio.run(run())


@pytest.fixture
def scenario(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        current = CurrentUser("operator", "operator", "local")
        monkeypatch.setattr(security, "get_current_user", lambda request: current)
        monkeypatch.setattr(employees, "get_current_user", lambda request: current)
        inspection = Mock(return_value={"errors": [], "has_candidates": False})
        monkeypatch.setattr(employees.EmployeeArrivalAccountService, "inspect", inspection)
        provision = Mock(side_effect=AssertionError("A decision must not create accounts"))
        monkeypatch.setattr(employees.ProvisioningService, "provision", provision)
        api = FastAPI()
        api.include_router(employees.router)
        api.include_router(account_requirement.router)
        api.dependency_overrides[get_db] = lambda: db
        api.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None, zimbra_domains=["example.test"], zimbra_primary_domain="example.test")
        db.add(HRSourceRecord(worker_key="worker", source_id="example.test", source_name="Организация",
            fio="Иванов Иван Иванович", login="ivanov", corporate_email="ivanov@example.test",
            personal_email="personal@example.com", is_present=True,
            placements_json=json.dumps([{"department": "ИТ", "position": "Инженер"}])))
        event = HREmploymentArrivalEvent(worker_key="worker", source_id="example.test",
            source_name="Организация", fio="Иванов Иван Иванович", sequence=1, status="pending")
        db.add(event)
        db.commit()
        yield SimpleNamespace(api=api, client=ASGIClient(api), db=db, event=event,
                              inspection=inspection, provision=provision)
    engine.dispose()


def open_decision(s):
    response = s.client.request("GET", f"/employees/new?arrival_event_ids={s.event.id}&modal=1")
    assert response.status_code == 200
    return response, s.db.scalar(select(AccountRequirementCase))


def decide(s, case, action="required", **extra):
    return s.client.request("POST", f"/employees/account-requirements/{case.id}/decision", {
        "action": action, "revision": case.revision, "csrf": "token", **extra})


def test_required_replaces_decision_with_prefilled_creation_and_keeps_label(scenario):
    s = scenario
    before, case = open_decision(s)
    assert "Необходимость корпоративной учетной записи" in before.text
    assert '<summary>Если учётные записи уже выдали вручную</summary>' in before.text
    assert 'id="provision-form"' not in before.text
    response = decide(s, case)
    assert response.status_code == 303
    after = s.client.request("GET", response.headers["location"])
    assert after.status_code == 200
    assert '<h1 data-workspace-page-title>Проверка и создание учётных записей</h1>' in after.text
    assert after.text.count('id="provision-form"') == 1
    assert "Необходимость корпоративной учетной записи" not in after.text
    assert "Если учётные записи уже выдали вручную" not in after.text
    assert f'/employees/account-requirements/{case.id}/decision' not in after.text
    for field, value in (("last_name", "Иванов"), ("first_name", "Иван"),
                         ("middle_name", "Иванович"), ("login", "ivanov"),
                         ("personal_email", "personal@example.com")):
        assert f'name="{field}"' in after.text and f'value="{value}"' in after.text
    assert '<option value="example.test" selected>' in after.text
    assert f'href="/account-requirements/{case.id}" data-workspace-navigate' in after.text
    assert case.decision == "required" and case.decided_by == "operator"
    assert case.decided_at and case.decision_snapshot_json
    assert not case.created_account and case.external_action_status == "not_started"
    history = s.db.scalars(select(AccountRequirementDecisionEvent)).all()
    assert history[-1].new_decision == "required"
    s.client.request("GET", response.headers["location"])
    assert len(s.db.scalars(select(AccountRequirementDecisionEvent)).all()) == len(history)
    detail = s.client.request("GET", f"/account-requirements/{case.id}")
    assert "Учетная запись нужна" in detail.text
    assert f'href="/employees/new?arrival_event_ids={s.event.id}" data-workspace-navigate' in detail.text
    s.provision.assert_not_called()


@pytest.mark.parametrize("action,extra", [
    ("clarification", {}), ("not_required", {"confirmed": "true"}),
    ("required", {"revision": 0}),
])
def test_other_decisions_and_rejected_requests_do_not_open_creation(scenario, action, extra):
    s = scenario
    _, case = open_decision(s)
    response = decide(s, case, action, **extra)
    assert response.status_code in {303, 403}
    page = s.client.request("GET", f"/employees/new?arrival_event_ids={s.event.id}")
    assert 'id="provision-form"' not in page.text
    assert case.decision != "required"
    s.provision.assert_not_called()


def test_invalid_csrf_does_not_accept_decision(scenario):
    s = scenario
    _, case = open_decision(s)
    with pytest.raises(security.CSRFMismatchError):
        decide(s, case, csrf="wrong")
    assert case.decision is None
    s.provision.assert_not_called()


def test_directory_failure_during_decision_keeps_creation_closed(scenario):
    s = scenario
    _, case = open_decision(s)
    s.inspection.return_value = {"errors": ["AD unavailable"], "has_candidates": False}
    response = decide(s, case)
    assert response.status_code == 303
    page = s.client.request("GET", response.headers["location"])
    assert "Проверка AD/Zimbra не завершена" in page.text
    assert case.decision is None
    s.provision.assert_not_called()
