"""A planned login without an AD object is an empty action, not a blocked run."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from ldap3 import SUBTREE
from sqlalchemy import select

from test_ad_conflict_recovery import STAMP, scenario
from app.config import Settings
from app.models import OneCImportRun
from app.models_dismissal_lifecycle import ADReactivationAlert, FinalDismissalBlockRun
from app.models_onec_sources import HREmploymentState
from app.routers.dashboard_dismissals import _journal_items
from app.services import final_dismissal_lifecycle as lifecycle
from app.services.ad import ActiveDirectoryService
from app.services.upcoming_dismissals import UpcomingDismissalService


def directory(monkeypatch, *, code=0, found=False, root='DC=example,DC=test', referrals=False, root_code=0):
    service = ActiveDirectoryService(Settings(_env_file=None, ad_check_enabled=True,
        ad_base_dn='OU=Employees,DC=example,DC=test'))
    conn = SimpleNamespace(entries=[], result={}, response=[], calls=[])

    def search(base, query, **kwargs):
        conn.calls.append((base, query, kwargs))
        if len(conn.calls) == 1:
            conn.entries = [{'defaultNamingContext': SimpleNamespace(value=root)}]
            conn.result = {'result': root_code}
        else:
            conn.entries = [object()] if found else []
            conn.result = {'result': code}
            conn.response = [{'type': 'searchResRef'}] if referrals else []
        return False  # ldap3 may return False for a successful empty search.

    conn.search = search

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(service, '_service_connection', connection)
    return service, conn


def test_login_absence_requires_successful_search_over_whole_domain(monkeypatch):
    service, conn = directory(monkeypatch)
    assert service.confirm_login_absent(username=' planned.user ') is True
    base, query, kwargs = conn.calls[1]
    assert base == 'DC=example,DC=test'
    assert '(sAMAccountName=planned.user)' in query
    assert kwargs['search_scope'] == SUBTREE


def test_login_is_escaped_not_interpreted_as_filter(monkeypatch):
    service, conn = directory(monkeypatch)
    assert service.confirm_login_absent(username='x*)(name=*)') is True
    assert '(sAMAccountName=x\\2a\\29\\28name=\\2a\\29)' in conn.calls[1][1]


@pytest.mark.parametrize('code', [3, 4, 10, 50, 51, 52])
def test_ldap_timeout_limit_referral_denied_or_unavailable_is_not_absence(monkeypatch, code):
    service, _ = directory(monkeypatch, code=code)
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='planned')


def test_ldap_referral_in_response_is_not_absence(monkeypatch):
    service, _ = directory(monkeypatch, referrals=True)
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='planned')


@pytest.mark.parametrize('root_code', [3, 51, 52])
def test_unavailable_root_search_is_retryable(monkeypatch, root_code):
    service, _ = directory(monkeypatch, root_code=root_code)
    with pytest.raises(RuntimeError) as error:
        service.confirm_login_absent(username='planned')
    assert lifecycle.FinalDismissalLifecycleService._is_temporary_error(error.value)


def test_moved_or_existing_account_prevents_empty_action(monkeypatch):
    service, _ = directory(monkeypatch, found=True)
    assert service.confirm_login_absent(username='planned') is False


@pytest.mark.parametrize('root', ['', None, 'DC=other,DC=test'])
def test_unknown_or_different_domain_does_not_confirm_absence(monkeypatch, root):
    service, conn = directory(monkeypatch, root=root)
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='planned')
    assert len(conn.calls) == 1


def test_empty_login_and_disabled_ad_never_confirm_absence(monkeypatch):
    service, conn = directory(monkeypatch)
    with pytest.raises(ValueError):
        service.confirm_login_absent(username=' ')
    service.settings.ad_check_enabled = False
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='planned')
    assert not conn.calls


def single_login(data):
    db, settings, records, worker, ad = data
    records[1].login = records[0].login
    db.commit()
    ad.get_user.return_value = None
    ad.confirm_login_absent.return_value = True
    return db, settings, records, worker, ad


def stuck(data):
    db, settings, records, worker, ad = single_login(data)
    candidate = next(iter(worker._candidate_map().values()))
    run = worker._ensure_run(candidate)
    target = worker._targets(run.id)[0]
    target.status = 'intervention'
    target.last_error = lifecycle.MISSING_AD_ACCOUNT_ERROR
    target.last_result = 'error'
    target.attempts = 1
    target.next_attempt_at = None
    worker._refresh_run(run)
    db.commit()
    return db, settings, records, worker, ad, run, target


def test_new_missing_account_completes_and_moves_to_journal_immediately(scenario):
    db, settings, records, worker, ad = single_login(scenario)
    worker.process()
    run = db.scalar(select(FinalDismissalBlockRun))
    target = worker._targets(run.id)[0]
    assert run.status == 'success' and run.completed_at and not run.last_error
    assert target.status == 'already_completed' and target.last_result == 'confirmed_absent'
    assert UpcomingDismissalService(settings, db).list_upcoming() == []
    items = [item for item in _journal_items(db, today=STAMP.date(), timezone_name='Europe/Moscow')
             if item['kind'] == 'dismissal' and item['record_id'] == run.id]
    assert len(items) == 1
    row = next(row for row in items[0]['dismissal_system_rows'] if row['label'] == 'AD')
    assert row['value'] == 'Отсутствует'
    ad.confirm_login_absent.assert_called_once_with(username='first')
    ad.disable_user.assert_not_called()
    worker.process()
    assert len(db.scalars(select(FinalDismissalBlockRun)).all()) == 1
    assert ad.confirm_login_absent.call_count == 1


def test_existing_missing_error_is_rechecked_automatically_without_blocking(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    worker.process()
    assert run.status == 'success' and target.last_result == 'confirmed_absent'
    assert not run.last_error and not target.last_error
    assert UpcomingDismissalService(settings, db).list_upcoming() == []
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


@pytest.mark.parametrize('mode', ['unproven', 'denied'])
def test_missing_lookup_is_not_enough_to_archive(scenario, mode):
    db, settings, records, worker, ad = single_login(scenario)
    ad.confirm_login_absent.return_value = False
    if mode == 'denied':
        ad.confirm_login_absent.side_effect = RuntimeError('access denied')
    worker.process()
    run = db.scalar(select(FinalDismissalBlockRun))
    assert run.status == 'intervention' and run.completed_at is None
    assert UpcomingDismissalService(settings, db).list_upcoming()
    ad.disable_user.assert_not_called()


def test_repair_remains_read_only_after_network_retry_when_account_appears(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    ad.confirm_login_absent.side_effect = TimeoutError('connection timed out')
    worker.process()
    assert target.status == 'pending' and target.next_attempt_at is not None
    assert target.last_result == lifecycle.VERIFY_MISSING_AD and run.completed_at is None
    ad.confirm_login_absent.reset_mock(side_effect=True)
    ad.confirm_login_absent.return_value = False
    target.next_attempt_at = lifecycle.utcnow() - timedelta(seconds=1)
    db.commit()
    worker.process()
    assert run.status == 'intervention' and run.completed_at is None
    assert 'подтвердите сопоставление' in target.last_error
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


def test_temporary_failure_retries_and_eventually_completes(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    ad.confirm_login_absent.side_effect = RuntimeError('AD временно недоступна')
    worker.process()
    assert target.status == 'pending' and target.next_attempt_at and not run.completed_at
    ad.confirm_login_absent.reset_mock(side_effect=True)
    ad.confirm_login_absent.return_value = True
    target.next_attempt_at = lifecycle.utcnow() - timedelta(seconds=1)
    db.commit()
    worker.process()
    assert run.status == 'success' and target.last_result == 'confirmed_absent'
    ad.disable_user.assert_not_called()


@pytest.mark.parametrize('guard', ['active_elsewhere', 'deferred', 'import', 'stale', 'conflict'])
def test_hr_guards_prevent_repair_and_completion(scenario, guard):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    if guard == 'active_elsewhere':
        state = db.scalar(select(HREmploymentState).where(HREmploymentState.source_id == 'b.test'))
        state.status = 'active'
        state.dismissal_date = None
    elif guard == 'deferred':
        candidate = next(iter(worker._candidate_map().values()))
        candidate['effective_block_date'] = STAMP.date() + timedelta(days=7)
        worker._candidate_map = lambda: {('worker', STAMP.date()): candidate}
    elif guard == 'import':
        db.add(OneCImportRun(status='running'))
        worker._ready = lambda: not worker._import_running()
    elif guard == 'stale':
        worker._ready.return_value = False
    else:
        records[1].login = 'different'
    db.commit()
    worker.process()
    assert run.status != 'success' and run.completed_at is None
    ad.confirm_login_absent.assert_not_called()
    ad.disable_user.assert_not_called()


def test_other_interventions_are_not_requeued(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    target.last_error = 'access denied'
    db.commit()
    worker.process()
    assert target.status == 'intervention' and run.completed_at is None
    ad.confirm_login_absent.assert_not_called()


def test_repair_keeps_read_only_mode_after_intermediate_hr_conflict(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    ad.confirm_login_absent.side_effect = TimeoutError('connection timed out')
    worker.process()
    records[1].login = 'different'
    target.next_attempt_at = lifecycle.utcnow() - timedelta(seconds=1)
    db.commit()
    worker.process()
    assert target.status == 'intervention'
    assert target.last_result == lifecycle.VERIFY_MISSING_AD_CONFLICT
    records[1].login = 'first'
    ad.confirm_login_absent.reset_mock(side_effect=True)
    ad.confirm_login_absent.return_value = False
    db.commit()
    worker.process()
    assert target.status == 'intervention' and run.completed_at is None
    assert 'подтвердите сопоставление' in target.last_error
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


def test_absent_account_does_not_create_reactivation_alert_on_return(scenario):
    db, settings, records, worker, ad = single_login(scenario)
    worker.process()
    for state in db.scalars(select(HREmploymentState)).all():
        state.status = 'active'
        state.dismissal_date = None
    db.commit()
    lifecycle.FinalDismissalLifecycleService._create_reactivation_alerts(worker)
    assert not db.scalars(select(ADReactivationAlert)).all()
    ad.enable_user.assert_not_called()
