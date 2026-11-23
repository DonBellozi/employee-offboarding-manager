from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.config import Settings
from app.db import Base
from app.models import AuditLog, HRSourceRecord, OneCImportRun
from app.models_dismissal_lifecycle import ADReactivationAlert, FinalDismissalBlockRun, FinalDismissalBlockTarget
from app.models_onec_sources import HREmploymentState
from app.routers import dashboard_dismissals as ui
from app.security import CurrentUser
from app.services import ad_reactivation_alerts as module
from app.services.ad import ADDirectoryUser
from app.services.final_dismissal_lifecycle import FinalDismissalLifecycleService


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        settings = Settings(_env_file=None, app_timezone="UTC", onec_source_domain="a.test",
                            zimbra_domains=["a.test", "b.test"])
        ad = Mock()
        factory = Mock(return_value=ad)
        monkeypatch.setattr(module, "ActiveDirectoryService", factory)
        yield SimpleNamespace(db=db, settings=settings, ad=ad, factory=factory, monkeypatch=monkeypatch)
    engine.dispose()


def employment(s, status="dismissed", domain="a.test", worker="worker"):
    row = HREmploymentState(worker_key=worker, source_id=domain, fio="Тестовый Работник",
                           status=status, is_present=status in {"active", "scheduled"})
    s.db.add_all([row, HRSourceRecord(worker_key=worker, source_id=domain,
                                     fio=row.fio, is_present=row.is_present)])
    s.db.commit()
    return row


def alert(s, **values):
    row = ADReactivationAlert(worker_key="worker", fio="Тестовый Работник", ad_login="worker",
                              last_error=module.INACTIVE_AD_MESSAGE, **values)
    s.db.add(row)
    s.db.commit()
    return row


def service(s):
    return module.ADReactivationAlertService(s.settings, s.db)


def audits(s):
    return list(s.db.scalars(select(AuditLog).where(AuditLog.action == "ad_reactivation_cancelled")))


@pytest.mark.parametrize("hr_state", ["dismissed", "absent", "no_records"])
def test_legacy_alert_closes_without_directory_calls_and_keeps_history(state, hr_state):
    s = state
    if hr_state != "no_records":
        employment(s, hr_state)
    row = alert(s, block_run_id=999999)
    assert service(s).close_inactive_alerts() == 1
    assert row.status == "resolved" and row.resolution == "inactive_again"
    assert row.resolved_by == "system" and row.resolved_at and not row.last_error
    assert s.db.get(ADReactivationAlert, row.id) is row
    assert len(audits(s)) == 1
    assert service(s).close_inactive_alerts() == 0
    assert len(audits(s)) == 1
    s.factory.assert_not_called()


@pytest.mark.parametrize("active_status", ["active", "scheduled"])
def test_active_employment_in_another_organization_keeps_alert(state, active_status):
    s = state
    employment(s)
    employment(s, active_status, "b.test")
    row = alert(s)
    assert service(s).close_inactive_alerts() == 0
    assert row.status == "open" and row.resolved_at is None
    assert not audits(s)
    s.factory.assert_not_called()


def test_import_in_progress_does_not_cancel_on_intermediate_hr_state(state):
    s = state
    employment(s)
    row = alert(s)
    s.db.add(OneCImportRun(status="running"))
    s.db.commit()
    assert service(s).close_inactive_alerts() == 0
    with pytest.raises(ValueError, match="импорта"):
        service(s).restore(alert_id=row.id, actor="operator")
    assert row.status == "open" and not audits(s)
    s.factory.assert_not_called()


@pytest.mark.parametrize("method", ["refresh", "restore", "restore_candidate"])
def test_old_action_closes_obsolete_work_without_ad_access(state, method):
    s = state
    employment(s)
    row = alert(s)
    kwargs = {"alert_id": row.id, "actor": "operator"}
    if method == "restore_candidate":
        kwargs.update(ad_login="worker", ad_object_guid="guid")
    for _ in range(2):
        with pytest.raises(module.ADReactivationNoLongerRequired):
            getattr(service(s), method)(**kwargs)
    assert row.status == "resolved" and row.resolution == "inactive_again"
    assert not row.last_error and len(audits(s)) == 1
    s.factory.assert_not_called()


def request(path="/", method="GET"):
    path, _, query = path.partition("?")
    return Request({"type": "http", "method": method, "scheme": "http", "path": path,
                    "query_string": query.encode(), "root_path": "", "server": ("test", 80),
                    "client": ("test", 1), "session": {"csrf": "token"},
                    "headers": [(b"host", b"test")]})


@pytest.mark.parametrize("handler", ["refresh_reactivated_ad", "restore_reactivated_ad", "restore_reactivated_ad_candidate"])
@pytest.mark.parametrize("already_closed", [False, True])
def test_old_forms_redirect_quietly_instead_of_operator_attention(state, handler, already_closed):
    s = state
    employment(s)
    row = alert(s)
    if already_closed:
        service(s).close_inactive_alerts()
    s.monkeypatch.setattr(ui, "require_operator", lambda req: CurrentUser("operator", "operator", "local"))
    kwargs = {"alert_id": row.id, "request": request(method="POST"), "csrf": "token",
              "db": s.db, "settings": s.settings}
    if handler.endswith("_candidate"):
        kwargs.update(ad_login="worker", ad_object_guid="guid")
    response = getattr(ui, handler)(**kwargs)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert row.status == "resolved" and not row.last_error
    s.factory.assert_not_called()


def prepare_dashboard(s):
    s.monkeypatch.setattr(ui, "get_current_user", lambda req: CurrentUser("operator", "operator", "local"))
    s.monkeypatch.setattr(ui.UpcomingDismissalService, "list_upcoming", lambda *a, **k: [])
    s.monkeypatch.setattr(ui.EmployeeArrivalService, "list_pending", lambda *a, **k: [])
    s.monkeypatch.setattr(ui, "_journal_items", lambda *a, **k: [])


def test_dashboard_clears_old_rows_and_old_error_url_without_hiding_other_errors(state):
    s = state
    employment(s)
    row = alert(s)
    prepare_dashboard(s)
    response = ui.dashboard(request("/?attention_error=" + quote(module.INACTIVE_AD_MESSAGE)), s.db, s.settings)
    html = response.body.decode()
    assert response.status_code == 200
    assert "Требуют внимания оператора" not in html
    assert module.INACTIVE_AD_MESSAGE not in html
    assert row.status == "resolved" and len(audits(s)) == 1
    response = ui.dashboard(request("/?attention_error=" + quote("Ошибка подключения AD")), s.db, s.settings)
    assert "Ошибка подключения AD" in response.body.decode()
    s.factory.assert_not_called()


def test_dashboard_preserves_real_active_worker_attention(state):
    s = state
    employment(s, "active")
    row = alert(s)
    row.last_error = "AD недоступен"
    s.db.commit()
    prepare_dashboard(s)
    response = ui.dashboard(request(), s.db, s.settings)
    html = response.body.decode()
    assert "Требуют внимания оператора" in html and "AD недоступен" in html
    assert row.status == "open"
    s.factory.assert_not_called()


def completed_run(s, row, *, result="disabled", age=30):
    day = date.today() - timedelta(days=age)
    run = FinalDismissalBlockRun(worker_key=row.worker_key, fio=row.fio,
        dismissal_date=day, effective_block_date=day, status="success")
    s.db.add(run)
    s.db.flush()
    s.db.add(FinalDismissalBlockTarget(run_id=run.id, system="ad", target_key="ad:worker",
        target_identifier="worker", status="completed", last_result=result))
    row.block_run_id = run.id
    s.db.commit()
    return run


def test_background_closes_alert_even_without_completed_target(state):
    s = state
    employment(s)
    row = alert(s)
    FinalDismissalLifecycleService(s.settings, s.db)._create_reactivation_alerts()
    assert row.status == "resolved" and row.resolution == "inactive_again"
    assert len(audits(s)) == 1
    s.factory.assert_not_called()


@pytest.mark.parametrize("resolution,expected", [("inactive_again", "open"), ("restored", "resolved")])
def test_later_confirmed_return_is_distinct_from_a_previous_resolution(state, resolution, expected):
    s = state
    employment(s, "active")
    row = alert(s, status="resolved", resolution=resolution)
    completed_run(s, row)
    FinalDismissalLifecycleService(s.settings, s.db)._create_reactivation_alerts()
    assert row.status == expected
    if expected == "open":
        assert row.resolution == "" and row.resolved_at is None
    s.factory.assert_not_called()  # Older than 14 days: the operator decides.


def test_return_with_confirmed_absent_ad_does_not_create_restore_work(state):
    s = state
    employment(s, "active")
    row = alert(s, status="resolved", resolution="inactive_again")
    completed_run(s, row, result="confirmed_absent")
    FinalDismissalLifecycleService(s.settings, s.db)._create_reactivation_alerts()
    assert row.status == "resolved"
    s.factory.assert_not_called()


def test_hr_becomes_inactive_during_discovery_never_enables_ad(state):
    s = state
    hr = employment(s, "active")
    row = alert(s)
    user = ADDirectoryUser("worker", "Тестовый Работник", "worker@a.test", "CN=worker", False, "guid")

    def discover(*args):
        hr.status = "dismissed"
        s.db.commit()
        return [user]

    s.monkeypatch.setattr(module.ADReactivationAlertService, "_discover_candidates", discover)
    with pytest.raises(module.ADReactivationNoLongerRequired):
        service(s).restore(alert_id=row.id, actor="operator")
    assert row.status == "resolved" and row.resolution == "inactive_again"
    assert not row.last_error
    s.ad.reactivate_existing_user.assert_not_called()


def test_actual_directory_failure_remains_operator_attention(state):
    s = state
    employment(s, "active")
    row = alert(s)
    s.monkeypatch.setattr(module.ADReactivationAlertService, "_discover_candidates", Mock(side_effect=RuntimeError("AD недоступен")))
    with pytest.raises(RuntimeError, match="AD недоступен"):
        service(s).restore(alert_id=row.id, actor="operator")
    assert row.status == "open" and row.last_error == "AD недоступен"
    assert not audits(s)


def test_real_active_worker_still_restores_with_verification(state):
    s = state
    employment(s, "active")
    row = alert(s)
    user = ADDirectoryUser("worker", "Тестовый Работник", "worker@a.test", "CN=worker", False, "guid")
    enabled = ADDirectoryUser("worker", "Тестовый Работник", "worker@a.test", "CN=worker", True, "guid")
    s.monkeypatch.setattr(module.ADReactivationAlertService, "_discover_candidates", lambda *a: [user])
    s.monkeypatch.setattr(module, "wait_for_reactivated_user", Mock(return_value=enabled))
    assert service(s).restore(alert_id=row.id, actor="operator") is row
    assert row.status == "resolved" and row.resolution == "restored"
    s.ad.reactivate_existing_user.assert_called_once_with("CN=worker")
