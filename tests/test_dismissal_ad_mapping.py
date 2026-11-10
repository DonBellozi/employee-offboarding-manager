from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from test_ad_conflict_recovery import scenario, start
from app.models import AuditLog, EmailLoginMapping, OneCImportRun
from app.models_onec_sources import HREmploymentState
from app.services import dismissal_ad_mapping as module
from app.services.upcoming_dismissals import UpcomingDismissalService


@pytest.fixture
def mapping(scenario,monkeypatch):
    db,settings,records,worker,ad=scenario
    run=start(scenario)
    for record in records: record.is_present=False
    for state in db.scalars(select(HREmploymentState)): state.is_present=False
    db.commit()
    user=SimpleNamespace(username='first',object_guid='guid-first',display_name='Иванов Иван Иванович',is_enabled=False)
    ad.get_user.return_value=user
    ad.get_user_by_object_guid.return_value=user
    monkeypatch.setattr(module,'ActiveDirectoryService',lambda settings:ad)
    return db,settings,records,worker,ad,run,module.DismissalADMappingService(settings,db),user


def test_historical_two_logins_confirmed_then_same_run_completes(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    before=[(r.login,r.is_present) for r in records]
    service.inspect(run.id,'first')
    assert db.scalars(select(EmailLoginMapping)).all()==[]
    service.save(run.id,'first',user.object_guid,'admin',True)
    assert [(r.login,r.is_present) for r in records]==before
    rows=db.scalars(select(EmailLoginMapping)).all()
    assert len(rows)==2 and all(r.ad_object_guid==user.object_guid for r in rows)
    assert all(not r.zimbra_id for r in rows)
    assert run.completed_at is None
    worker.process()
    assert run.status=='success' and run.completed_at
    assert UpcomingDismissalService(settings,db).list_upcoming()==[]
    ad.disable_user.assert_not_called()
    ad.enable_user.assert_not_called()
    ad.delete_user.assert_not_called()


def test_repeat_save_no_duplicate_audit_and_preserves_mail_mapping(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    old=EmailLoginMapping(worker_key=run.worker_key,source_domain=records[0].source_id,source_email='old@example.test',
        ad_login='wrong',ad_object_guid='old-guid',zimbra_id='keep-id',zimbra_email='keep@example.test')
    db.add(old); db.commit()
    service.save(run.id,'first',user.object_guid,'admin',True)
    service.save(run.id,'first',user.object_guid,'admin',True)
    assert old.zimbra_id=='keep-id' and old.zimbra_email=='keep@example.test'
    assert old.source_email=='old@example.test'
    assert len(db.scalars(select(EmailLoginMapping)).all())==2
    assert len(db.scalars(select(AuditLog).where(AuditLog.action=='dismissal_ad_identity_confirmed')).all())==1


@pytest.mark.parametrize('confirmed,guid',[(False,'guid-first'),(True,''),(True,'different-guid')])
def test_confirmation_and_exact_guid_required(mapping,confirmed,guid):
    db,settings,records,worker,ad,run,service,user=mapping
    with pytest.raises(ValueError):service.save(run.id,'first',guid,'admin',confirmed)
    assert db.scalars(select(EmailLoginMapping)).all()==[]


def test_other_worker_mapping_protected(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    db.add(EmailLoginMapping(worker_key='other',source_domain='other.test',source_email='',ad_object_guid=user.object_guid,
                            ad_login='first',zimbra_id='',zimbra_email=''))
    db.commit()
    with pytest.raises(ValueError,match='другому'):service.inspect(run.id,'first')


def test_ad_name_must_match_history(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    user.display_name='Другой Человек'
    with pytest.raises(ValueError,match='ФИО'):service.inspect(run.id,'first')


def test_active_in_other_org_rejected(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    state=db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='b.test'))
    state.status='active';db.commit()
    with pytest.raises(ValueError,match='активная'):service.save(run.id,'first',user.object_guid,'admin',True)


def test_import_started_during_ldap_check_prevents_save(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    def lookup(login):
        db.add(OneCImportRun(status='running'));db.commit()
        return user
    ad.get_user.side_effect=lookup
    with pytest.raises(ValueError,match='импорт'):service.save(run.id,'first',user.object_guid,'admin',True)
    assert db.scalars(select(EmailLoginMapping)).all()==[]


def test_missing_or_unavailable_ad_does_not_save(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    ad.get_user.return_value=None
    with pytest.raises(ValueError):service.inspect(run.id,'first')
    ad.get_user.side_effect=RuntimeError('SECRET')
    with pytest.raises(ValueError) as exc:service.inspect(run.id,'first')
    assert 'SECRET' not in str(exc.value)
    assert db.scalars(select(EmailLoginMapping)).all()==[]


def test_upcoming_exposes_mapping_action_only_for_conflict(mapping):
    db,settings,records,worker,ad,run,service,user=mapping
    items=UpcomingDismissalService(settings,db).list_upcoming()
    assert items[0]['ad_mapping_run_id']==run.id


def test_admin_route_preview_save_and_csrf(mapping,monkeypatch):
    from app.routers import hr_registry_mapping as route
    from app.security import CurrentUser,CSRFMismatchError
    from starlette.requests import Request
    db,settings,records,worker,ad,run,service,user=mapping
    monkeypatch.setattr(route,'require_admin',lambda request:CurrentUser('admin','admin','local'))
    request=Request({'type':'http','method':'POST','scheme':'http','path':'/dismissals/1/ad-mapping',
                     'query_string':b'','headers':[],'server':('test',80),'session':{'csrf':'token'}})
    page=route.dismissal_ad_mapping_page(run.id,request,settings,db)
    assert page.status_code==200 and 'Сопоставление AD' in page.body.decode()
    with pytest.raises(CSRFMismatchError):
        route.dismissal_ad_mapping_save(run.id,request,login='first',action='save',expected_guid=user.object_guid,
            confirmed=True,csrf='bad',settings=settings,db=db)
    preview=route.dismissal_ad_mapping_save(run.id,request,login='first',action='preview',expected_guid='',
        confirmed=False,csrf='token',settings=settings,db=db)
    assert preview.status_code==200 and user.object_guid in preview.body.decode()
    assert db.scalars(select(EmailLoginMapping)).all()==[]
    result=route.dismissal_ad_mapping_save(run.id,request,login='first',action='save',expected_guid=user.object_guid,
        confirmed=True,csrf='token',settings=settings,db=db)
    assert result.status_code==200 and 'сохранено' in result.body.decode()


def test_route_rejects_non_admin(mapping,monkeypatch):
    from app.routers.hr_registry_mapping import dismissal_ad_mapping_page
    from starlette.requests import Request
    from fastapi import HTTPException
    from app import security
    monkeypatch.setattr(security,'get_current_user',lambda request:security.CurrentUser('op','operator','domain'))
    db,settings,records,worker,ad,run,service,user=mapping
    request=Request({'type':'http','session':{'user':{'username':'op','role':'operator','source':'local'}}})
    with pytest.raises(HTTPException):dismissal_ad_mapping_page(run.id,request,settings,db)
