"""Domain-only absence proofs and recovery of stopped dismissal targets."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from ldap3.protocol.controls import build_control
from sqlalchemy import select

from test_ad_conflict_recovery import STAMP, scenario
from test_missing_ad_dismissal import stuck
from app.config import Settings
from app.models_dismissal_lifecycle import FinalDismissalBlockRun
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import ZimbraEmploymentAction, ZimbraLifecycleSettings
from app.routers.dashboard_dismissals import _journal_items
from app.services.ad import ActiveDirectoryService, DOMAIN_SCOPE_CONTROL
from app.services import final_dismissal_lifecycle as lifecycle
from app.services import zimbra_employment_lifecycle as mail_lifecycle
from app.services.upcoming_dismissals import UpcomingDismissalService
from app.services.zimbra import ZimbraAccountIdentity

REFERRAL_ERROR = 'AD вернула перенаправление; отсутствие учётки не подтверждено'
GUID = '11111111-1111-1111-1111-111111111111'


def directory(monkeypatch, *, found=False, code=0, stray_referral=False, root='DC=example,DC=test'):
    service = ActiveDirectoryService(Settings(_env_file=None, ad_check_enabled=True,
        ad_base_dn='OU=Employees,DC=example,DC=test'))
    conn = SimpleNamespace(entries=[], result={}, response=[], calls=[], auto_referrals=True)

    def search(base, query, **kwargs):
        conn.calls.append((base, query, kwargs, conn.auto_referrals))
        if not base:
            conn.result = {'result':0}
            conn.response = [{'type':'searchResEntry','attributes':{'defaultNamingContext':[root]}}]
            conn.entries = []
            return True
        conn.result = {'result':code}
        conn.entries = [object()] if found else []
        # Simulate AD's subordinate continuation references without domain scope.
        scoped = DOMAIN_SCOPE_CONTROL in kwargs.get('controls', [])
        conn.response = ([{'type':'searchResRef', 'uri':[
            'ldap://example.test/DC=DomainDnsZones,DC=example,DC=test',
            'ldap://example.test/DC=ForestDnsZones,DC=example,DC=test',
            'ldap://example.test/CN=Configuration,DC=example,DC=test']}]
            if not scoped or stray_referral else [])
        return False  # A successful empty ldap3 result can be False.

    conn.search = search
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service, '_service_connection', connection)
    return service, conn


def test_domain_control_is_critical_and_has_no_value_on_the_wire():
    oid, critical, value = DOMAIN_SCOPE_CONTROL
    assert oid == '1.2.840.113556.1.4.1339'
    encoded = build_control(oid, critical, value)
    assert bool(encoded['criticality']) is True
    assert not encoded['controlValue'].hasValue()


def test_normal_ad_partition_referrals_are_prevented_without_weakening_proof(monkeypatch):
    service, conn = directory(monkeypatch)
    assert service.confirm_login_absent(username='never.created') is True
    base, query, options, chasing = conn.calls[1]
    assert base == 'DC=example,DC=test'
    assert options['controls'] == [DOMAIN_SCOPE_CONTROL]
    assert chasing is False and conn.auto_referrals is True
    assert '(sAMAccountName=never.created)' in query


def test_account_in_another_ou_is_not_declared_absent(monkeypatch):
    service, conn = directory(monkeypatch, found=True)
    assert service.confirm_login_absent(username='moved.user') is False
    assert conn.calls[1][0] == 'DC=example,DC=test'


def test_guid_and_old_login_remain_in_the_same_domain_search(monkeypatch):
    service, conn = directory(monkeypatch)
    assert service.confirm_user_absent(object_guid=GUID, username='old.user') is True
    assert '(objectGUID=' in conn.calls[1][1]
    assert '(sAMAccountName=old.user)' in conn.calls[1][1]


@pytest.mark.parametrize('code', [3,4,10,12,49,50,51,52])
def test_errors_partial_results_and_unsupported_control_never_prove_absence(monkeypatch, code):
    service, conn = directory(monkeypatch, code=code)
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='missing')
    assert conn.auto_referrals is True


def test_unexpected_referral_despite_critical_control_still_blocks_completion(monkeypatch):
    service, conn = directory(monkeypatch, stray_referral=True)
    with pytest.raises(RuntimeError, match='перенаправление'):
        service.confirm_login_absent(username='missing')


def test_wrong_domain_is_rejected_before_domain_search(monkeypatch):
    service, conn = directory(monkeypatch, root='DC=other,DC=test')
    with pytest.raises(RuntimeError, match='другой домен'):
        service.confirm_login_absent(username='missing')
    assert len(conn.calls) == 1


def test_referral_chasing_is_restored_on_connection_error(monkeypatch):
    service, conn = directory(monkeypatch)
    original = conn.search
    def search(base, query, **options):
        if base:
            assert conn.auto_referrals is False
            raise RuntimeError('connection timed out')
        return original(base, query, **options)
    conn.search = search
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='missing')
    assert conn.auto_referrals is True


def referral_target(data):
    db, settings, records, worker, ad, run, target = stuck(data)
    target.last_error = REFERRAL_ERROR
    target.last_result = lifecycle.VERIFY_MISSING_AD
    db.commit()
    return db, settings, records, worker, ad, run, target


@pytest.mark.parametrize('mode', ['login','guid','no_current_login'])
def test_existing_referral_failure_is_repaired_read_only_and_moves_to_journal(scenario, mode):
    db, settings, records, worker, ad, run, target = referral_target(scenario)
    if mode == 'guid':
        target.stable_id = GUID
        ad.confirm_user_absent.return_value = True
    elif mode == 'no_current_login':
        for record in records:
            record.login = ''
            record.corporate_email = ''
    db.commit()
    worker.process()
    assert run.status == 'success' and run.completed_at and not run.last_error
    assert target.last_result == 'confirmed_absent' and not target.last_error
    assert UpcomingDismissalService(settings, db).list_upcoming() == []
    items = [item for item in _journal_items(db,today=STAMP.date(),timezone_name='Europe/Moscow')
             if item['kind']=='dismissal' and item['record_id']==run.id]
    assert len(items)==1
    assert next(row for row in items[0]['dismissal_system_rows'] if row['label']=='AD')['value']=='Отсутствует'
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()
    if mode == 'guid':
        ad.confirm_user_absent.assert_called_once_with(object_guid=GUID,username='first')
        ad.confirm_login_absent.assert_not_called()
    worker.process()
    assert len(db.scalars(select(FinalDismissalBlockRun)).all())==1


def test_account_found_during_backlog_repair_is_never_disabled(scenario):
    db,settings,records,worker,ad,run,target=referral_target(scenario)
    ad.confirm_login_absent.return_value=False
    worker.process()
    assert run.status=='intervention' and run.completed_at is None
    assert 'подтвердите сопоставление' in target.last_error
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


@pytest.mark.parametrize('guard', ['active_elsewhere','deferred','not_ready','conflict'])
def test_backlog_repair_preserves_hr_guards(scenario, guard):
    db,settings,records,worker,ad,run,target=referral_target(scenario)
    if guard=='active_elsewhere':
        state=db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='b.test'))
        state.status='active'
        state.dismissal_date=None
    elif guard=='deferred':
        candidate=next(iter(worker._candidate_map().values()))
        candidate['effective_block_date']=STAMP.date()+timedelta(days=7)
        worker._candidate_map=lambda:{('worker',STAMP.date()):candidate}
    elif guard=='not_ready':
        worker._ready.return_value=False
    else:
        records[1].login='other-login'
    db.commit()
    worker.process()
    assert run.status!='success' and run.completed_at is None
    ad.confirm_login_absent.assert_not_called()
    ad.disable_user.assert_not_called()


def test_temporary_ad_outage_stays_pending_and_retry_remains_read_only(scenario):
    db,settings,records,worker,ad,run,target=referral_target(scenario)
    ad.confirm_login_absent.side_effect=RuntimeError('AD временно недоступна')
    worker.process()
    assert target.status=='pending' and target.next_attempt_at and run.completed_at is None
    ad.confirm_login_absent.reset_mock(side_effect=True)
    ad.confirm_login_absent.return_value=True
    target.next_attempt_at=lifecycle.utcnow()-timedelta(seconds=1)
    db.commit()
    worker.process()
    assert run.status=='success'
    ad.disable_user.assert_not_called()


def test_missing_ad_does_not_prevent_independent_organization_mail_close(scenario,monkeypatch):
    db,settings,records,worker,ad,run,target=referral_target(scenario)
    records[1].corporate_email=''
    for record in records:
        record.login=''
    db.add(ZimbraLifecycleSettings(id=1,allow_employment_close=True))
    db.commit()
    current={'status':'active'}
    class Mail:
        def identity(self):
            return ZimbraAccountIdentity('mail-id','first@a.test','first',('first@a.test',),current['status'])
        def accounts_by_addresses(self, addresses):
            return {address:self.identity() for address in addresses if address=='first@a.test'}
        def accounts_by_ids(self, ids):
            return {'mail-id':self.identity()} if 'mail-id' in ids else {}
        def close_account(self,address):
            assert address=='first@a.test'
            current['status']='closed'
    monkeypatch.setattr(mail_lifecycle,'ZimbraService',lambda settings:Mail())
    monkeypatch.setattr(mail_lifecycle.ZimbraEmploymentLifecycleService,'_ready',lambda self:True)
    monkeypatch.setattr(mail_lifecycle.ZimbraEmploymentLifecycleService,'local_now',property(lambda self:STAMP))
    worker.process()
    assert run.status=='success' and target.last_result=='confirmed_absent'
    result=mail_lifecycle.ZimbraEmploymentLifecycleService(settings,db).process()
    assert result['completed']==1 and current['status']=='closed'
    mail_action=db.scalar(select(ZimbraEmploymentAction))
    assert mail_action.action=='close' and mail_action.status=='completed'
    ad.disable_user.assert_not_called()
