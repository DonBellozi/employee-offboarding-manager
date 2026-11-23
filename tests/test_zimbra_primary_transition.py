from datetime import date, datetime, timezone, timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import security
from app.config import Settings
from app.db import Base
from app.models import HRSourceRecord, AuditLog
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import ZimbraLifecycleSettings, ZimbraEmploymentAction, ZimbraPrimaryTransition
from app.routers import zimbra_lifecycle as routes
from app.routers.dashboard_dismissals import _journal_items
from app.services import zimbra_primary_transition as module
from app.services import zimbra_employment_lifecycle as employment
from app.services.zimbra import ZimbraAccountIdentity
from app.services.zimbra_lifecycle import BackupResult

OLD, NEW, OTHER = 'ivanov@old.test', 'ivanov@new.test', 'project@extra.test'
NOW = datetime(2030, 1, 10, 20, 0, tzinfo=timezone.utc)


class Mailbox:
    def __init__(self):
        self.primary = OLD
        self.addresses = {OLD, NEW, OTHER}
        self.calls = []
        self.fail_rename = False
        self.lost_reply = False
        self.fail_cleanup = False

    def identity(self):
        return ZimbraAccountIdentity('z-1', self.primary, 'ivanov', tuple(self.addresses), 'active')

    def accounts_by_ids(self, ids):
        return {'z-1': self.identity()} if 'z-1' in ids else {}

    def accounts_by_addresses(self, addresses):
        return {address: self.identity() for address in addresses if address in self.addresses}

    def account_by_address(self, address):
        return self.identity() if address in self.addresses else None

    def remove_alias_by_id(self, zid, address):
        assert zid == 'z-1' and address != self.primary
        self.calls.append(('remove', address))
        if address == OLD and self.fail_cleanup:
            raise RuntimeError('cleanup failed')
        self.addresses.discard(address)

    def add_alias_by_id(self, zid, address):
        assert zid == 'z-1'
        self.calls.append(('restore', address))
        self.addresses.add(address)

    def rename_account(self, zid, address):
        assert zid == 'z-1' and address not in self.addresses
        self.calls.append(('rename', address))
        if self.fail_rename:
            raise RuntimeError('rename failed')
        self.primary = address
        self.addresses.add(address)
        if self.lost_reply:
            raise RuntimeError('connection timed out after rename')


@pytest.fixture
def case(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        settings = Settings(_env_file=None, app_timezone='UTC', zimbra_domains=['old.test','new.test'],
                            zimbra_backend='ssh_zmprov', zimbra_check_enabled=True)
        mail = Mailbox()
        ready = {'value': True}
        monkeypatch.setattr(employment.ZimbraEmploymentLifecycleService, 'local_now', property(lambda self: NOW))
        monkeypatch.setattr(employment.ZimbraEmploymentLifecycleService, '_ready', lambda self: ready['value'])
        monkeypatch.setattr(employment, 'ZimbraService', lambda settings: mail)
        monkeypatch.setattr(module, 'ZimbraService', lambda settings: mail)
        for source, address, status in [('old.test', OLD, 'dismissed'), ('new.test', NEW, 'active')]:
            db.add(HREmploymentState(worker_key='worker', source_id=source, fio='Иванов Иван',
                status=status, is_present=True, dismissal_date=NOW.date() if status == 'dismissed' else None))
            db.add(HRSourceRecord(worker_key='worker', source_id=source, fio='Иванов Иван',
                corporate_email=address, login='ivanov', is_present=status == 'active'))
        db.add(ZimbraLifecycleSettings(id=1, allow_backup=True, backup_dir='/opt/tmp'))
        db.commit()
        service = module.ZimbraPrimaryTransitionService(settings, db)
        spec = service.employment._current_specs()[0]
        assert spec.action == 'transition'
        action = service.employment._ensure_action(spec)
        db.commit()
        def export(*args, **kwargs):
            mail.calls.append(('backup', args[0]))
            return BackupResult('/opt/tmp/transition.tgz', 123)
        backup = Mock(side_effect=export)
        monkeypatch.setattr(module.ZimbraLifecycleService, '_backup_account', backup)
        monkeypatch.setattr(security, 'get_current_user', lambda request: security.CurrentUser('operator','operator','local'))
        yield SimpleNamespace(db=db, settings=settings, service=service, mail=mail, ready=ready,
                              backup=backup, action=action, export=export)
    engine.dispose()


def enqueue(case):
    return case.service.enqueue(case.action.id, 'operator', expected_old=OLD, expected_new=NEW, confirmed=True)


def request():
    return Request({'type':'http', 'method':'GET', 'path':'/', 'headers':[], 'query_string':b'',
                    'session':{'csrf':'token'}})


def test_review_get_renders_saved_addresses_without_any_directory_scan(case, monkeypatch):
    monkeypatch.setattr(module.ZimbraPrimaryTransitionService, 'inspect',
        Mock(side_effect=AssertionError('GET must not wait for live Zimbra/HR scan')))
    page = routes.transition_page(case.action.id, request(), settings=case.settings, db=case.db)
    assert page.status_code == 200
    assert OLD in page.body.decode() and NEW in page.body.decode()


def organization_event(case):
    from app.models_notifications import HREmploymentDismissalEvent
    event = HREmploymentDismissalEvent(worker_key='worker', source_id='old.test', fio='Иванов Иван',
        sequence=1, status='open', first_dismissal_date=NOW.date(), current_dismissal_date=NOW.date())
    case.db.add(event)
    case.db.commit()
    return event


@pytest.mark.parametrize('alias_present', [False, True])
def test_partial_dismissal_completes_today_without_closing_active_mailbox(case, alias_present):
    from app.services.upcoming_dismissals import UpcomingDismissalService
    from app.services.organization_dismissal_completion import organization_dismissal_complete
    organization_event(case)
    case.mail.primary = NEW
    case.mail.addresses = {NEW, OLD} if alias_present else {NEW}
    config = case.db.get(ZimbraLifecycleSettings, 1)
    config.allow_alias_remove = True
    # The production alias operation leaves the physical mailbox/primary intact.
    case.mail.remove_alias = lambda primary, alias: case.mail.remove_alias_by_id('z-1', alias)
    case.db.commit()
    assert not organization_dismissal_complete(case.db, 'worker', NOW.date(), {'old.test'})
    assert UpcomingDismissalService(case.settings, case.db)._all_candidates(include_expired=True)
    result = case.service.employment.process()
    assert result['completed'] == 1
    assert case.mail.primary == NEW and case.mail.addresses == {NEW}
    assert organization_dismissal_complete(case.db, 'worker', NOW.date(), {'old.test'})
    assert UpcomingDismissalService(case.settings, case.db)._all_candidates(include_expired=True) == []
    items = [item for item in _journal_items(case.db, today=NOW.date(), settings=case.settings)
             if item['kind'] == 'dismissal']
    assert len(items) == 1 and items[0]['status_key'] == 'success'
    assert any(step['action'] == 'Почта организации' for step in items[0]['dismissal_steps'])
    case.service.employment.process()
    assert len([item for item in _journal_items(case.db, today=NOW.date(), settings=case.settings)
                if item['kind'] == 'dismissal']) == 1


def test_transition_is_not_archived_until_backup_rename_and_cleanup_finish(case):
    from app.services.organization_dismissal_completion import organization_dismissal_complete
    organization_event(case)
    assert not organization_dismissal_complete(case.db, 'worker', NOW.date(), {'old.test'})
    assert not [i for i in _journal_items(case.db, today=NOW.date(), settings=case.settings) if i['kind']=='dismissal']
    enqueue(case)
    case.service.process_due()
    assert organization_dismissal_complete(case.db, 'worker', NOW.date(), {'old.test'})
    assert len([i for i in _journal_items(case.db, today=NOW.date(), settings=case.settings) if i['kind']=='dismissal']) == 1


def test_partial_dismissal_does_not_disappear_at_midnight_before_actions_finish(case,monkeypatch):
    from app.models_dismissal_lifecycle import FinalDismissalAutomationState
    from app.services.upcoming_dismissals import UpcomingDismissalService
    organization_event(case)
    tomorrow=NOW.date()+timedelta(days=1)
    case.db.add(FinalDismissalAutomationState(id=1,activated_on=NOW.date()-timedelta(days=1)))
    case.db.commit()
    monkeypatch.setattr(UpcomingDismissalService,'today',property(lambda self:tomorrow))
    assert UpcomingDismissalService(case.settings,case.db).list_upcoming()
    assert not [i for i in _journal_items(case.db,today=tomorrow,settings=case.settings) if i['kind']=='dismissal']
    enqueue(case)
    case.service.process_due()
    assert UpcomingDismissalService(case.settings,case.db).list_upcoming()==[]
    assert len([i for i in _journal_items(case.db,today=tomorrow,settings=case.settings) if i['kind']=='dismissal'])==1


@pytest.mark.parametrize('guard', ['hr', 'deferral', 'unavailable', 'unknown_address'])
def test_no_address_proof_keeps_hr_deferral_and_complete_mail_lookup_guards(case, guard):
    case.mail.primary = NEW
    case.mail.addresses = {NEW}
    if guard == 'hr':
        case.ready['value'] = False
    elif guard == 'deferral':
        case.db.add(DismissalDeferral(worker_key='worker', dismissal_date=NOW.date(),
            deferred_until=NOW.date()+timedelta(days=7), operator_username='operator'))
        case.db.commit()
    elif guard == 'unavailable':
        case.mail.accounts_by_addresses = Mock(side_effect=ConnectionError('Zimbra unavailable'))
    else:
        case.mail.addresses.add(OTHER)
    if guard == 'unavailable':
        with pytest.raises(ConnectionError):
            case.service.employment.process()
    else:
        assert case.service.employment.process()['completed'] == 0
    assert not case.mail.calls


@pytest.mark.parametrize('guard',['techexpert_pending','dsm_active','import','wrong_date','employment_active'])
def test_partial_completion_waits_for_other_organization_work(case, guard):
    from app.models_techexpert import TechExpertNotification, TechExpertSettings
    from app.models_synology import SynologyAccountState
    from app.models import OneCImportRun
    from app.services.organization_dismissal_completion import organization_dismissal_complete
    case.mail.primary=NEW
    case.mail.addresses={NEW}
    case.service.employment.process()
    assert organization_dismissal_complete(case.db,'worker',NOW.date(),{'old.test'})
    if guard=='techexpert_pending':
        case.db.add(TechExpertSettings(id=1,enabled=True,source_domain='old.test',subject='test',body_html='test'))
        case.db.add(TechExpertNotification(worker_key='worker',source_id='old.test',employment_event_id=1,
            event_updated_at=NOW,dismissal_date=NOW.date(),scheduled_for=NOW,status='pending'))
    elif guard=='dsm_active':
        case.db.add(SynologyAccountState(worker_key='worker',email=OLD,login='ivanov',stable_id='dsm-1'))
    elif guard=='import':
        case.db.add(OneCImportRun(status='running'))
    else:
        state=case.db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='old.test'))
        if guard=='wrong_date':
            state.dismissal_date=NOW.date()+timedelta(days=1)
        else:
            state.status='active'
    case.db.commit()
    assert not organization_dismissal_complete(case.db,'worker',NOW.date(),{'old.test'})


def test_approval_queues_once_and_does_not_mutate_zimbra_in_request(case):
    job = enqueue(case)
    assert job.status == 'queued' and job.phase == 'backup'
    assert enqueue(case).id == job.id
    assert len(case.db.scalars(select(ZimbraPrimaryTransition)).all()) == 1
    assert case.mail.calls == []
    case.backup.assert_not_called()


def test_backup_then_rename_then_old_address_removal_preserves_mailbox(case):
    job = enqueue(case)
    case.service.process_due()
    assert job.status == 'completed' and job.backup_size_bytes == 123
    assert case.mail.calls == [('backup',OLD), ('remove',NEW), ('rename',NEW), ('remove',OLD)]
    assert case.mail.primary == NEW and case.mail.addresses == {NEW, OTHER}
    assert case.action.status == 'completed' and not case.action.last_error
    audit = case.db.scalar(select(AuditLog).where(AuditLog.action == 'zimbra_primary_transition'))
    assert audit.actor == 'operator' and audit.result == 'success'
    case.service.process_due()
    assert len(case.mail.calls) == 4
    items=[item for item in _journal_items(case.db,today=NOW.date()) if item['kind']=='zimbra-transition']
    assert len(items)==1 and items[0]['corporate_email']==NEW and items[0]['operator']=='operator'


@pytest.mark.parametrize('which', ['unconfirmed','stale'])
def test_stale_or_unconfirmed_approval_is_rejected(case, which):
    with pytest.raises(ValueError):
        case.service.enqueue(case.action.id,'operator',expected_old=OLD,
            expected_new='changed@new.test' if which == 'stale' else NEW, confirmed=which != 'unconfirmed')
    assert not case.db.scalars(select(ZimbraPrimaryTransition)).all()
    assert not case.mail.calls


def test_backup_failure_never_detaches_alias_and_can_be_retried(case):
    job = enqueue(case)
    case.backup.side_effect = RuntimeError('export failed')
    case.service.process_due()
    assert job.status == 'failed' and job.phase == 'backup'
    assert not case.mail.calls and case.mail.primary == OLD
    case.backup.side_effect = case.export
    case.service.retry(case.action.id, 'admin')
    case.service.process_due()
    assert job.status == 'completed'


def test_empty_backup_is_not_a_permission_to_rename(case):
    job=enqueue(case)
    case.backup.side_effect=None
    case.backup.return_value=BackupResult('/opt/tmp/empty.tgz',0)
    case.service.process_due()
    assert job.status=='failed' and case.mail.calls==[]


@pytest.mark.parametrize('guard', ['import_or_stale','old_active','new_inactive','deferral','backup_disabled'])
def test_each_hr_and_permission_guard_prevents_any_mutation(case, guard):
    job = enqueue(case)
    states = {row.source_id: row for row in case.db.scalars(select(HREmploymentState)).all()}
    if guard == 'import_or_stale':
        case.ready['value'] = False
    elif guard == 'old_active':
        states['old.test'].status = 'active'
        states['old.test'].dismissal_date = None
    elif guard == 'new_inactive':
        states['new.test'].status = 'dismissed'
    elif guard == 'backup_disabled':
        case.db.get(ZimbraLifecycleSettings,1).allow_backup = False
    else:
        case.db.add(DismissalDeferral(worker_key='worker', dismissal_date=NOW.date(),
            deferred_until=NOW.date()+timedelta(days=7), operator_username='operator'))
    case.db.commit()
    case.service.process_due()
    assert job.status == 'waiting_hr' and not case.mail.calls


def test_hr_change_during_long_backup_stops_rename_and_later_reuses_backup(case):
    job = enqueue(case)
    state = case.db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='new.test'))
    def export(*args, **kwargs):
        state.status='dismissed'
        case.db.commit()
        return case.export(*args, **kwargs)
    case.backup.side_effect=export
    case.service.process_due()
    assert job.status=='waiting_hr' and job.phase=='rename'
    assert case.mail.calls==[('backup',OLD)]
    state.status='active'
    case.db.commit()
    case.service.process_due()
    assert job.status=='completed' and case.backup.call_count==1


def test_failed_rename_restores_active_alias_and_retry_does_not_repeat_backup(case):
    job=enqueue(case)
    case.mail.fail_rename=True
    case.service.process_due()
    assert job.status=='failed' and NEW in case.mail.addresses and case.mail.primary==OLD
    assert case.mail.calls[-1]==('restore',NEW)
    before=list(case.mail.calls)
    case.service.process_due()
    assert case.mail.calls==before
    case.mail.fail_rename=False
    case.service.retry(case.action.id,'operator')
    case.service.process_due()
    assert job.status=='completed' and case.backup.call_count==1


def test_lost_ssh_reply_after_successful_rename_is_verified_by_same_id(case):
    job=enqueue(case)
    case.mail.lost_reply=True
    case.service.process_due()
    assert job.status=='completed' and case.mail.primary==NEW
    assert not any(name=='restore' for name,_ in case.mail.calls)


def test_restart_resumes_after_detached_alias_without_second_export(case):
    job=enqueue(case)
    job.phase='rename'
    job.status='running'
    job.backup_path='/opt/tmp/already.tgz'
    job.backup_size_bytes=123
    case.mail.addresses.remove(NEW)
    case.db.commit()
    module.ZimbraPrimaryTransitionService(case.settings,case.db).process_due()
    assert job.status=='completed'
    case.backup.assert_not_called()


def test_cleanup_failure_keeps_operator_action_and_retry_only_cleans_old_address(case):
    job=enqueue(case)
    case.mail.fail_cleanup=True
    case.service.process_due()
    assert job.status=='failed' and job.phase=='cleanup' and case.mail.primary==NEW
    case.service.employment.process()
    assert case.action.status=='intervention'
    case.mail.fail_cleanup=False
    case.service.retry(case.action.id,'operator')
    case.service.process_due()
    assert job.status=='completed' and case.backup.call_count==1
    assert sum(name=='rename' for name,_ in case.mail.calls)==1


def test_other_mailbox_owning_new_address_is_never_touched(case,monkeypatch):
    job=enqueue(case)
    monkeypatch.setattr(case.mail,'account_by_address',lambda address: ZimbraAccountIdentity(
        'other-id',address,'ivanov',(address,),'active'))
    case.service.process_due()
    assert job.status=='failed' and case.mail.calls==[('backup',OLD)]


def test_routes_show_review_and_queue_behind_csrf_and_role_checks(case,monkeypatch):
    page=routes.transition_page(case.action.id, request(), settings=case.settings, db=case.db)
    assert page.status_code==200 and 'Сменить основной адрес' in page.body.decode()
    with pytest.raises(security.CSRFMismatchError):
        routes.transition_enqueue(case.action.id,request(),csrf='bad',expected_old=OLD,
            expected_new=NEW,confirmed='true',settings=case.settings,db=case.db)
    monkeypatch.setattr(security,'get_current_user',lambda request: security.CurrentUser('viewer','viewer','local'))
    with pytest.raises(HTTPException) as error:
        routes.transition_page(case.action.id,request(),settings=case.settings,db=case.db)
    assert error.value.status_code==403


def test_current_transition_clears_obsolete_hr_cancellation_error(case):
    case.action.status='cancelled'
    case.action.last_error='Повторная HR-проверка отменила действие'
    case.db.commit()
    case.service.employment.process()
    assert case.action.status=='intervention' and not case.action.last_error
    assert not case.mail.calls


def test_automatic_lifecycle_cannot_close_during_detached_alias_phase(case):
    job=enqueue(case)
    job.phase='rename'
    job.status='running'
    case.mail.addresses={OLD}
    case.db.commit()
    case.service.employment.process()
    close=case.db.scalar(select(ZimbraEmploymentAction).where(ZimbraEmploymentAction.action=='close'))
    assert close.status=='pending' and 'смены основного адреса' in close.last_error
    assert not case.mail.calls
    assert case.action.status=='intervention'


def test_operator_can_cancel_untouched_job_and_reapprove_with_new_backup(case):
    job=enqueue(case)
    case.service.cancel(case.action.id,'operator')
    case.service.process_due()
    assert job.status=='cancelled' and case.mail.calls==[]
    assert enqueue(case).id==job.id and job.status=='queued'
    case.service.process_due()
    assert job.status=='completed'


def test_cancellation_never_abandons_a_detached_active_alias(case):
    job=enqueue(case)
    job.phase='rename'
    job.status='failed'
    case.mail.addresses.remove(NEW)
    case.db.commit()
    with pytest.raises(ValueError):
        case.service.cancel(case.action.id,'operator')
    assert job.status=='failed'


def test_status_endpoint_reads_only_local_progress(case):
    job=enqueue(case)
    result=routes.transition_status(case.action.id,request(),settings=case.settings,db=case.db)
    assert result['status']=='queued' and result['label']=='В очереди'
    assert not case.mail.calls


def test_two_hr_events_cannot_queue_two_renames_of_the_same_mailbox(case,monkeypatch):
    job=enqueue(case)
    action=ZimbraEmploymentAction(plan_key='another-event',worker_key='worker',zimbra_id='z-1',
        action='transition',source_id='old.test',target_address=OLD,replacement_address=NEW,
        dismissal_date=NOW.date(),effective_action_date=NOW.date())
    case.db.add(action)
    case.db.commit()
    _,spec=case.service.inspect(case.action.id)
    monkeypatch.setattr(case.service,'inspect',lambda action_id:(action,spec))
    with pytest.raises(ValueError,match='уже есть'):
        case.service.enqueue(action.id,'operator',expected_old=OLD,expected_new=NEW,confirmed=True)
    assert len(case.db.scalars(select(ZimbraPrimaryTransition)).all())==1
    assert job.active_mailbox_id=='z-1'


def test_commands_target_stable_mailbox_id(monkeypatch):
    from app.services.zimbra import ZimbraService
    service=ZimbraService(Settings(_env_file=None))
    execute=Mock()
    monkeypatch.setattr(service,'_run_zmprov_direct',execute)
    service.rename_account('id-1',NEW)
    service.add_alias_by_id('id-1',NEW)
    service.remove_alias_by_id('id-1',OLD)
    assert [call.args[0] for call in execute.call_args_list]==[
        ['ra','id-1',NEW],['aaa','id-1',NEW],['raa','id-1',OLD]]


def test_background_worker_finishes_durable_queue(case):
    job=enqueue(case)
    @contextmanager
    def session():
        yield case.db
    worker=module.ZimbraPrimaryTransitionWorker(case.settings,session)
    stop=Mock()
    stop.is_set.return_value=False
    stop.wait.return_value=True
    worker._stop=stop
    worker._run()
    assert job.status=='completed' and case.mail.primary==NEW


def test_disabled_backup_cannot_be_bypassed_by_post(case):
    case.db.get(ZimbraLifecycleSettings,1).allow_backup=False
    case.db.commit()
    response=routes.transition_enqueue(case.action.id,request(),csrf='token',expected_old=OLD,
        expected_new=NEW,confirmed='true',settings=case.settings,db=case.db)
    assert response.status_code==400
    assert not case.db.scalars(select(ZimbraPrimaryTransition)).all()
    assert not case.mail.calls
