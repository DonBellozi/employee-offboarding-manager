from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from test_dismissal_ad_mapping import mapping, scenario
from test_ad_conflict_recovery import STAMP
from app.models import AuditLog, EmailLoginMapping, OneCImportRun
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.services.final_dismissal_lifecycle import FinalDismissalLifecycleService
from app.services.upcoming_dismissals import UpcomingDismissalService


def prepare(data):
    db,settings,records,worker,ad,run,service,user=data
    service.save(run.id,'first',user.object_guid,'admin',True)
    return db,settings,records,worker,ad,run,service,user


def test_confirms_disabled_without_evening_gate_or_any_ad_write(mapping,monkeypatch):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    ready=Mock(side_effect=AssertionError('must not enter external-action gate'))
    monkeypatch.setattr(FinalDismissalLifecycleService,'_ready',ready)
    monkeypatch.setattr(FinalDismissalLifecycleService,'_process_ad',Mock(side_effect=AssertionError('AD write path')))
    message=service.confirm_disabled(run.id,'admin')
    assert 'в журнал' in message
    assert run.status=='success' and run.completed_at
    assert UpcomingDismissalService(settings,db).list_upcoming()==[]
    ready.assert_not_called()
    for method in ('disable_user','enable_user','delete_user','create_disabled_user'):
        getattr(ad,method).assert_not_called()
    assert db.scalar(select(AuditLog).where(AuditLog.action=='dismissal_ad_completion_verified')) is not None


def test_active_ad_is_not_blocked_or_marked_success(mapping):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    user.is_enabled=True
    assert 'AD активна' in service.confirm_disabled(run.id,'admin')
    assert run.status=='intervention' and run.completed_at is None
    ad.disable_user.assert_not_called()


@pytest.mark.parametrize('case',['missing','different_guid','wrong_name','unavailable','unknown_state'])
def test_ad_not_verified_never_finalizes(mapping,case):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    if case=='missing': ad.get_user_by_object_guid.return_value=None
    if case=='different_guid': ad.get_user_by_object_guid.return_value=SimpleNamespace(object_guid='another')
    if case=='wrong_name': user.display_name='Другой человек'
    if case=='unavailable': ad.get_user_by_object_guid.side_effect=RuntimeError('SECRET')
    if case=='unknown_state': user.is_enabled=None
    with pytest.raises(ValueError) as error:service.confirm_disabled(run.id,'admin')
    assert 'SECRET' not in str(error.value)
    assert run.completed_at is None


def test_explicit_identity_confirmation_required(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    with pytest.raises(ValueError,match='подтверждённого'):service.confirm_disabled(run.id,'admin')
    ad.get_user_by_object_guid.assert_not_called()


def test_deferral_protected(mapping):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    db.add(DismissalDeferral(worker_key=run.worker_key,dismissal_date=run.dismissal_date,
        deferred_until=STAMP.date()+timedelta(days=7)))
    db.commit()
    with pytest.raises(ValueError,match='отсрочка'):service.confirm_disabled(run.id,'admin')
    assert run.completed_at is None


def test_same_day_before_window_is_protected(mapping,monkeypatch):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    monkeypatch.setattr(FinalDismissalLifecycleService,'local_now',property(lambda self: STAMP.replace(hour=10)))
    with pytest.raises(ValueError,match='Срок'):service.confirm_disabled(run.id,'admin')


def test_overdue_can_be_confirmed_during_day(mapping,monkeypatch):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    monkeypatch.setattr(FinalDismissalLifecycleService,'local_now',property(lambda self:(STAMP+timedelta(days=1)).replace(hour=10)))
    assert 'в журнал' in service.confirm_disabled(run.id,'admin')


@pytest.mark.parametrize('change',['active','import','guid','deferral'])
def test_recheck_after_ldap_prevents_stale_completion(mapping,change):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    def lookup(guid):
        if change=='active':
            state=db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='b.test'))
            state.status='active'
        if change=='import':db.add(OneCImportRun(status='running'))
        if change=='guid':
            for row in db.scalars(select(EmailLoginMapping)):row.ad_object_guid='changed'
        if change=='deferral':db.add(DismissalDeferral(worker_key=run.worker_key,dismissal_date=run.dismissal_date,
            deferred_until=STAMP.date()+timedelta(days=7)))
        db.commit()
        return user
    ad.get_user_by_object_guid.side_effect=lookup
    with pytest.raises(ValueError):service.confirm_disabled(run.id,'admin')
    assert run.completed_at is None


def test_web_check_action_uses_existing_mapping_without_entering_login_again(mapping,monkeypatch):
    from app.routers import hr_registry_mapping as route
    from app.security import CurrentUser,CSRFMismatchError
    from starlette.requests import Request
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    monkeypatch.setattr(route,'require_admin',lambda request:CurrentUser('admin','admin','domain'))
    request=Request({'type':'http','method':'POST','path':'/dismissals/1/ad-mapping','scheme':'http',
                     'query_string':b'','headers':[],'server':('test',80),'session':{'csrf':'token'}})
    with pytest.raises(CSRFMismatchError):
        route.dismissal_ad_mapping_save(run.id,request,login='',action='check',csrf='bad',settings=settings,db=db)
    result=route.dismissal_ad_mapping_save(run.id,request,login='',action='check',csrf='token',settings=settings,db=db)
    assert result.status_code==200 and 'в журнал' in result.body.decode()
    assert run.status=='success'


def test_new_foreign_mapping_prevents_completion(mapping):
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    db.add(EmailLoginMapping(worker_key='other',source_domain='other.test',source_email='',
        ad_login='first',ad_object_guid=user.object_guid,zimbra_id='',zimbra_email=''))
    db.commit()
    with pytest.raises(ValueError,match='другим'):service.confirm_disabled(run.id,'admin')
    assert run.completed_at is None


def test_other_real_error_is_not_hidden(mapping):
    from app.models_dismissal_lifecycle import FinalDismissalBlockTarget
    db,settings,records,worker,ad,run,service,user=prepare(mapping)
    db.add(FinalDismissalBlockTarget(run_id=run.id,system='ad',target_key='ad:other',
        target_identifier='other',status='intervention',last_error='Нужна отдельная проверка'))
    db.commit()
    assert 'другие' in service.confirm_disabled(run.id,'admin')
    assert run.status=='partial' and run.completed_at is None
