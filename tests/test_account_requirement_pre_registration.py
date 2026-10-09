from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select, text, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.db import Base, get_db
from app.models import AuditLog, EmailLoginMapping, OneCImportRun, OperationStatus, ProvisioningOperation
from app.models_account_requirement import AccountRequirementCase, AccountRequirementPreRegistration
from app.security import CurrentUser
from app.services.account_requirement import AccountRequirementService, build_account_requirement_dataset
from app.services.account_requirement_pre_registration import PreRegistrationRequirementService, aware
from app.services.employee_arrivals import EmployeeArrivalService
from app.services.provisioning import ProvisioningInput, ProvisioningService
from test_account_requirement import ASGIClient, add_arrival, observe


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def pre_registration(db, **overrides):
    operation = ProvisioningOperation(operator_username="creator", last_name="Иванов", first_name="Иван",
        middle_name="Иванович", login="ivanov", personal_email="private@example.net",
        corporate_email="ivanov@example.test", mail_domain="example.test", status=OperationStatus.SUCCESS,
        ad_created=True, zimbra_created=True, created_at=datetime.now(timezone.utc) - timedelta(days=2))
    for key, value in overrides.items():
        setattr(operation, key, value)
    db.add(operation)
    db.flush()
    row = PreRegistrationRequirementService(db).begin(operation)
    row.ad_object_guid, row.zimbra_id = "guid", "zid"
    db.commit()
    return operation, row


def discovered(**overrides):
    return {"errors": [], "has_candidates": True,
            "ad_candidates": [{"object_guid": "guid", "username": "ivanov"}],
            "zimbra_candidates": [{"zimbra_id": "zid", "primary_email": "ivanov@example.test",
                                   "addresses": ["ivanov@example.test"]}], **overrides}


def bind(db, event, state=None):
    return AccountRequirementService(db).observe(str(event.id), state if state is not None else discovered())


def mapping(db, worker="hmac-worker", **overrides):
    row = EmailLoginMapping(worker_key=worker, source_domain="org-a", source_email="ivanov@example.test",
        ad_object_guid="guid", ad_login="ivanov", zimbra_id="zid", zimbra_email="ivanov@example.test",
        created_by="operator", last_verified_at=datetime.now(timezone.utc))
    for key, value in overrides.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def test_decision_waits_for_hr_and_begin_is_idempotent(db):
    operation, pre = pre_registration(db)
    assert PreRegistrationRequirementService(db).begin(operation).id == pre.id
    assert pre.status == "waiting" and pre.decided_by == "creator"
    assert json.loads(pre.initial_snapshot_json)["placements"] == []
    assert build_account_requirement_dataset(db)["records_count"] == 0
    assert len(db.scalars(select(AccountRequirementPreRegistration)).all()) == 1


def test_confirmed_hire_is_exported_with_original_decision_and_frozen_features(db):
    operation, pre = pre_registration(db)
    initial = pre.initial_snapshot_json
    record, event = add_arrival(db)
    case = bind(db, event)
    assert case.decision == "required" and case.decision_source == "pre_registration"
    assert case.decided_at == pre.decided_at
    assert case.hr_confirmed_at and aware(case.hr_confirmed_at) > aware(case.decided_at)
    assert case.provisioning_operation_id == operation.id
    assert pre.case_id == case.id and pre.status == "matched"
    assert case.initial_snapshot_json == initial == pre.initial_snapshot_json
    first = build_account_requirement_dataset(db)
    assert first["records_count"] == 1 and first["records"][0]["y"] == 1
    assert first["records"][0]["feature_source"] == "first_confirmed_hire"
    record.placements_json = '[{"department":"Будущий отдел","position":"Директор"}]'
    db.commit()
    assert bind(db, event).id == case.id
    assert build_account_requirement_dataset(db) == first
    assert first["records"][0]["X"]["position"] == "Инженер"
    for private in ("Иванов", "ivanov", "creator", "private@", "guid", "zid", "hmac-worker", "NEVER_COPY"):
        assert private not in json.dumps(first, ensure_ascii=False)


def test_operator_mapping_enriches_and_accepting_accounts_does_not_cancel_positive(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    record.corporate_email = ""  # Automatic guess cannot link on FIO alone.
    db.commit()
    assert bind(db, event) is None
    mapping(db)
    EmployeeArrivalService(db).mark_accounts_resolved(str(event.id), operator="operator", decision_details="Приняты")
    case = db.get(AccountRequirementCase, pre.case_id)
    assert event.status == "accounts_confirmed" and case.state == "decided"
    assert build_account_requirement_dataset(db)["records_count"] == 1


@pytest.mark.parametrize("problem", ["errors", "wrong-guid", "wrong-zid", "many-accounts", "other-owner", "homonym", "no-hr-mail", "no-role", "before-decision", "already-decided", "failed-creation"])
def test_unsafe_or_incomplete_match_does_not_become_training_example(db, problem):
    operation, pre = pre_registration(db)
    record, event = add_arrival(db)
    state = discovered()
    if problem == "errors":
        state["errors"] = ["AD unavailable"]
    elif problem == "wrong-guid":
        state["ad_candidates"][0]["object_guid"] = "recycled-guid"
    elif problem == "wrong-zid":
        state["zimbra_candidates"][0]["zimbra_id"] = "another-id"
    elif problem == "many-accounts":
        state["ad_candidates"].append({"object_guid": "another-guid"})
    elif problem == "other-owner":
        mapping(db, worker="another-worker")
    elif problem == "homonym":
        add_arrival(db, key="homonym")
    elif problem == "no-hr-mail":
        record.corporate_email = ""
    elif problem == "no-role":
        record.placements_json = '[{"department":"ИТ","position":""}]'
    elif problem == "before-decision":
        event.first_seen_at = pre.decided_at - timedelta(days=2)
    elif problem == "already-decided":
        case = observe(db, [event])
        AccountRequirementService(db).decide(case.id, action="required", actor="operator",
                                            expected_revision=case.revision, confirmed=True)
    elif problem == "failed-creation":
        operation.ad_created = operation.zimbra_created = False
    db.commit()
    bind(db, event, state)
    assert pre.status == "waiting" and pre.case_id is None
    assert not any(row["feature_source"] == "first_confirmed_hire" for row in build_account_requirement_dataset(db)["records"])


def test_missing_role_is_retried_then_frozen_once(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    record.placements_json = '[{"department":"ИТ","position":""}]'
    db.commit()
    assert bind(db, event) is None
    record.placements_json = '[{"department":"ИТ","position":"Инженер"}]'
    db.commit()
    assert bind(db, event).id == pre.case_id
    assert build_account_requirement_dataset(db)["records_count"] == 1


def test_missing_role_is_retried_in_background_even_after_operator_accepted_accounts(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    record.placements_json = '[{"department":"ИТ","position":""}]'
    db.commit()
    mapping(db)
    EmployeeArrivalService(db).mark_accounts_resolved(str(event.id), operator="operator", decision_details="Приняты")
    assert event.status == "accounts_confirmed" and pre.case_id is None
    record.placements_json = '[{"department":"ИТ","position":"Инженер"}]'
    db.commit()
    from app.services.account_requirement_worker import AccountRequirementWorker
    worker = AccountRequirementWorker(SimpleNamespace(), lambda: Session(db.bind, expire_on_commit=False))
    worker._run_once()
    db.expire_all()
    assert pre.status == "matched"
    assert build_account_requirement_dataset(db)["records_count"] == 1


def test_incomplete_pre_creation_can_merge_auto_cancelled_unlabelled_case(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    existing = observe(db, [event])
    record.placements_json = '[{"department":"ИТ","position":""}]'
    db.commit()
    mapping(db)
    EmployeeArrivalService(db).mark_accounts_resolved(str(event.id), operator="operator", decision_details="Приняты")
    assert existing.state == "cancelled" and existing.decision is None
    assert pre.merge_cancelled_automatically
    record.placements_json = '[{"department":"ИТ","position":"Инженер"}]'
    db.commit()
    PreRegistrationRequirementService(db).reconcile_confirmed()
    assert pre.case_id == existing.id and existing.decision == "required"
    assert len(db.scalars(select(AccountRequirementCase)).all()) == 1


def test_explicit_operator_cancellation_is_not_revived(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    existing = observe(db, [event])
    record.placements_json = '[{"department":"ИТ","position":""}]'
    db.commit()
    PreRegistrationRequirementService(db).attach(EmployeeArrivalService(db).registration_context(str(event.id)), discovered())
    AccountRequirementService(db).decide(existing.id, action="cancelled", actor="admin", is_admin=True,
        expected_revision=existing.revision, comment="Ошибочная регистрация")
    record.placements_json = '[{"department":"ИТ","position":"Инженер"}]'
    db.commit()
    assert pre.merge_case_id == existing.id and not pre.merge_cancelled_automatically
    assert existing.state == "cancelled" and pre.case_id is None


def test_verified_mapping_allows_changed_fio_and_account_names(db):
    _, pre = pre_registration(db)
    record, event = add_arrival(db)
    record.fio = event.fio = "Петров Иван Иванович"
    mapping(db, ad_login="petrov", zimbra_email="petrov@example.test")
    db.commit()
    EmployeeArrivalService(db).mark_accounts_resolved(str(event.id), operator="operator", decision_details="Сопоставлены")
    assert pre.status == "matched" and build_account_requirement_dataset(db)["records_count"] == 1


def test_explicit_mapping_with_conflicting_owner_cannot_enrich(db):
    _, pre = pre_registration(db)
    _, event = add_arrival(db)
    mapping(db)
    mapping(db, worker="another-worker")
    assert bind(db, event) is None
    assert pre.case_id is None


def test_identity_capture_error_can_be_resolved_by_existing_operator_mapping(db):
    _, pre = pre_registration(db)
    pre.ad_object_guid = pre.zimbra_id = ""
    record, event = add_arrival(db)
    record.corporate_email = ""
    db.commit()
    assert bind(db, event) is None
    mapping(db)
    EmployeeArrivalService(db).mark_accounts_resolved(str(event.id), operator="operator", decision_details="Сопоставлены")
    assert pre.status == "matched"


def test_correction_preserves_hr_snapshot_and_original_positive_date(db):
    _, pre = pre_registration(db)
    _, event = add_arrival(db)
    case = bind(db, event)
    snapshot = case.decision_snapshot_json
    AccountRequirementService(db).decide(case.id, action="not_required", actor="admin", is_admin=True,
                                        expected_revision=case.revision, confirmed=True)
    dataset = build_account_requirement_dataset(db)
    assert dataset["records"][0]["y"] == 0
    assert case.decision_snapshot_json == snapshot
    assert case.decision_source == "pre_registration"
    assert dataset["records"][0]["original_decision_at"] == aware(pre.decided_at).isoformat()


def test_pending_case_is_merged_not_duplicated(db):
    _, pre = pre_registration(db)
    _, event = add_arrival(db)
    existing = observe(db, [event])
    initial = existing.initial_snapshot_json
    assert bind(db, event).id == existing.id
    assert existing.initial_snapshot_json == initial
    assert pre.case_id == existing.id
    assert len(db.scalars(select(AccountRequirementCase)).all()) == 1


def test_simultaneous_orgs_and_later_rehire_have_no_duplicate_positive(db):
    _, pre = pre_registration(db)
    _, first = add_arrival(db)
    _, second = add_arrival(db, source="org-b")
    case = AccountRequirementService(db).observe(f"{first.id},{second.id}", discovered())
    assert AccountRequirementService(db).event_ids(case) == [first.id, second.id]
    first.status = second.status = "accounts_confirmed"
    first.ended_at = second.ended_at = datetime.now(timezone.utc)
    _, returned = add_arrival(db, sequence=2)
    assert bind(db, returned) is None
    assert pre.case_id == case.id
    assert build_account_requirement_dataset(db)["records_count"] == 1


def test_old_unmatched_decision_is_not_attached_to_later_rehire(db):
    _, pre = pre_registration(db)
    _, old = add_arrival(db)
    old.first_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
    old.status = "cancelled"
    old.ended_at = datetime.now(timezone.utc)
    db.commit()
    _, returned = add_arrival(db, sequence=2)
    assert bind(db, returned) is None and pre.case_id is None


def test_import_interlock_and_duplicate_registrations(db):
    _, pre = pre_registration(db)
    _, event = add_arrival(db)
    run = OneCImportRun(status="running")
    db.add(run)
    db.commit()
    with pytest.raises(ValueError, match="импорта"):
        bind(db, event)
    run.status = "success"
    db.commit()
    pre_registration(db)
    assert bind(db, event) is None
    assert pre.status == "waiting"


@pytest.fixture
def provisioning(db, monkeypatch):
    from app.services import provisioning as module
    settings = Settings(_env_file=None, app_secret_key="0123456789abcdef", dry_run=False,
                        zimbra_domains=["example.test"], zimbra_primary_domain="example.test")
    service = ProvisioningService(settings)
    service.ad = Mock()
    service.zimbra = Mock()
    service.mailer = Mock()
    service.ad.create_disabled_user.return_value = SimpleNamespace(dn="dn", accepted_password="TOP_SECRET_AD")
    service.ad.get_user.return_value = SimpleNamespace(object_guid="guid")
    service.zimbra.create_account.return_value = SimpleNamespace(primary_email="ivanov@example.test", aliases=())
    service.zimbra.account_by_address.return_value = SimpleNamespace(zimbra_id="zid")
    service.check_login = Mock(return_value={"ad": False, "zimbra": False})
    monkeypatch.setattr(module, "get_domain_mail_profile", lambda *args: SimpleNamespace())
    return service


DATA = ProvisioningInput("Иванов", "Иван", "Иванович", "private@example.net", "ivanov", "example.test")


@pytest.mark.parametrize("scenario", ["success", "failed", "partial", "dry_run", "occupied", "untracked", "pin-error"])
def test_real_provisioning_records_intent_before_external_action_and_excludes_dry_run(db, provisioning, scenario):
    service = provisioning
    if scenario == "dry_run":
        service.settings = SimpleNamespace(**service.settings.model_dump(), dry_run=True)
    if scenario == "occupied":
        service.check_login.return_value = {"ad": True, "zimbra": False}
        with pytest.raises(RuntimeError, match="занят"):
            service.provision(db, "creator", DATA, track_requirement=True)
    else:
        original = service.ad.create_disabled_user.return_value
        def create(**kwargs):
            if scenario not in {"dry_run", "untracked"}:
                pre = db.scalar(select(AccountRequirementPreRegistration))
                assert pre and pre.decided_by == "creator"
            if scenario == "failed":
                raise RuntimeError("AD unavailable")
            return original
        service.ad.create_disabled_user.side_effect = create
        if scenario == "partial":
            service.mailer.send_ad_credentials.side_effect = RuntimeError("SMTP failed")
        if scenario == "pin-error":
            service.ad.get_user.side_effect = RuntimeError("AD unavailable")
            service.zimbra.account_by_address.side_effect = RuntimeError("Zimbra unavailable")
        result = service.provision(db, "creator", DATA, track_requirement=scenario != "untracked")
        assert result.operation_id
    rows = db.scalars(select(AccountRequirementPreRegistration)).all()
    assert len(rows) == (0 if scenario in {"dry_run", "occupied", "untracked"} else 1)
    if rows:
        assert rows[0].status == "waiting"
        for private in ("TOP_SECRET_AD", "private@example.net", "guid", "zid"):
            assert private not in rows[0].initial_snapshot_json


def test_http_manual_creation_lists_positive_then_same_row_after_hr(db, provisioning, monkeypatch):
    from app import security
    from app.routers import employees, account_requirement
    user = CurrentUser("creator", "admin", "domain")
    monkeypatch.setattr(security, "get_current_user", lambda request: user)
    monkeypatch.setattr(employees, "get_current_user", lambda request: user)
    monkeypatch.setattr(employees, "ProvisioningService", lambda settings: provisioning)
    app = FastAPI()
    app.include_router(employees.router)
    app.include_router(account_requirement.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: provisioning.settings
    client = ASGIClient(app)
    response = client.post("/employees/provision", data={"last_name":"Иванов", "first_name":"Иван",
        "middle_name":"Иванович", "login":"ivanov", "mail_domain":"example.test", "personal_email":"",
        "confirm_no_personal_email":"true", "csrf":"token"})
    assert response.status_code == 200
    pre = db.scalar(select(AccountRequirementPreRegistration))
    assert pre
    listing = client.get("/admin/account-requirements")
    assert listing.status_code == 200 and "Создание до выгрузки" in listing.text
    assert "Ожидает кадрового подтверждения" in listing.text
    assert client.get(f"/account-requirements/pre-registration/{pre.id}").status_code == 200
    assert client.get("/admin/account-requirements?department=ИТ").text.count("Создание до выгрузки") == 0
    _, event = add_arrival(db)
    case = bind(db, event)
    listing = client.get("/admin/account-requirements")
    assert listing.text.count("Иванов Иван Иванович") == 1
    detail = client.get(f"/account-requirements/{case.id}")
    assert "Кадровые сведения этого приёма" in detail.text
    redirect = client.request("GET", f"/account-requirements/pre-registration/{pre.id}", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == f"/account-requirements/{case.id}"
    exported = client.get("/admin/account-requirements-export")
    assert exported.status_code == 200 and exported.json()["records_count"] == 1


def test_cancel_requires_admin_csrf_and_keeps_audit(db, monkeypatch):
    from app import security
    from app.routers import account_requirement
    _, pre = pre_registration(db)
    user = CurrentUser("operator", "operator", "domain")
    monkeypatch.setattr(security, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(account_requirement.router)
    app.dependency_overrides[get_db] = lambda: db
    client = ASGIClient(app)
    url = f"/account-requirements/pre-registration/{pre.id}/cancel"
    data = {"revision":pre.revision, "comment":"Ошибка оператора", "csrf":"token"}
    assert client.post(url, data).status_code == 403
    user = CurrentUser("admin", "admin", "domain")
    from app.security import CSRFMismatchError
    with pytest.raises(CSRFMismatchError):
        client.post(url, {**data, "csrf":"wrong"})
    assert client.post(url, data).status_code == 200
    assert pre.status == "cancelled"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "account_requirement_pre_registration_cancelled"))
    _, event = add_arrival(db)
    assert bind(db, event) is None


def test_compatibility_migration_is_additive_and_idempotent(monkeypatch):
    from app import db as module
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE account_requirement_cases (id INTEGER PRIMARY KEY, marker TEXT)"))
        conn.execute(text("INSERT INTO account_requirement_cases VALUES (1,'keep')"))
    monkeypatch.setattr(module, "engine", engine)
    module.ensure_compatibility_schema()
    module.ensure_compatibility_schema()
    assert "hr_confirmed_at" in {column["name"] for column in inspect(engine).get_columns("account_requirement_cases")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT marker FROM account_requirement_cases")).scalar() == "keep"
    engine.dispose()
