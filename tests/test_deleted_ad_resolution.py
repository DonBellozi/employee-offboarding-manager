from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from test_dismissal_ad_mapping import mapping, scenario
from app.config import Settings
from app.models_dismissal_lifecycle import FinalDismissalBlockTarget
from app.services.ad import ActiveDirectoryService

GUID='11111111-1111-1111-1111-111111111111'


def directory(monkeypatch, *, final_code=0, found=False, root_code=0, root_dn='DC=example,DC=test'):
    service=ActiveDirectoryService(Settings(_env_file=None,ad_check_enabled=True,ad_base_dn='OU=Old,DC=example,DC=test'))
    conn=SimpleNamespace(entries=[],result={},calls=[])
    def search(base,query,**kwargs):
        conn.calls.append((base,query,kwargs))
        if len(conn.calls)==1:
            conn.entries=[{'defaultNamingContext':SimpleNamespace(value=root_dn)}]
            conn.result={'result':root_code}
        else:
            conn.entries=[object()] if found else []
            conn.result={'result':final_code}
        return False  # Empty successful searches may return False in ldap3.
    conn.search=search
    @contextmanager
    def connection():yield conn
    monkeypatch.setattr(service,'_service_connection',connection)
    return service,conn


def test_empty_successful_domain_search_is_absence(monkeypatch):
    service,conn=directory(monkeypatch)
    assert service.confirm_user_absent(object_guid=GUID,username='old.user') is True
    assert conn.calls[1][0]=='DC=example,DC=test'
    assert '(sAMAccountName=old.user)' in conn.calls[1][1]
    assert 'objectGUID=' in conn.calls[1][1]


@pytest.mark.parametrize('code',[4,10,50,51,52])
def test_partial_referral_permission_or_server_error_is_not_absence(monkeypatch,code):
    service,conn=directory(monkeypatch,final_code=code)
    with pytest.raises(RuntimeError):service.confirm_user_absent(object_guid=GUID,username='old')


@pytest.mark.parametrize('root_code,root_dn',[(50,'DC=example,DC=test'),(0,''),(0,None)])
def test_root_domain_must_be_confirmed(monkeypatch,root_code,root_dn):
    service,conn=directory(monkeypatch,root_code=root_code,root_dn=root_dn)
    with pytest.raises(RuntimeError):service.confirm_user_absent(object_guid=GUID)


def test_existing_or_moved_object_or_reused_login_is_not_absence(monkeypatch):
    service,conn=directory(monkeypatch,found=True)
    assert service.confirm_user_absent(object_guid=GUID,username='reused') is False


def test_wrong_domain_cannot_prove_deletion(monkeypatch):
    service,conn=directory(monkeypatch,root_dn='DC=other,DC=test')
    with pytest.raises(RuntimeError,match='другой домен'):service.confirm_user_absent(object_guid=GUID)
    assert len(conn.calls)==1


def test_invalid_guid_or_disabled_checks_never_prove_absence(monkeypatch):
    service,conn=directory(monkeypatch)
    with pytest.raises(ValueError):service.confirm_user_absent(object_guid='invalid')
    assert conn.calls==[]
    service.settings.ad_check_enabled=False
    with pytest.raises(RuntimeError):service.confirm_user_absent(object_guid=GUID)


def prepared(data):
    db,settings,records,worker,ad,run,service,user=data
    service.save(run.id,'first',user.object_guid,'admin',True)
    return data


def test_selected_identity_deleted_finishes_as_absent_not_blocked(mapping):
    from app.routers.dashboard_dismissals import _completed_dismissal_journal_item
    db,settings,records,worker,ad,run,service,user=prepared(mapping)
    ad.get_user_by_object_guid.return_value=None
    ad.confirm_user_absent.return_value=True
    assert 'в журнал' in service.confirm_disabled(run.id,'admin')
    assert run.status=='success'
    targets=worker._targets(run.id)
    current=next(t for t in targets if t.target_key!='ad:conflict')
    assert current.last_result=='confirmed_absent'
    item=_completed_dismissal_journal_item(run,targets,notice=None,techexpert_notifications=[],deferral_items=[],snapshot=None)
    row=next(r for r in item['dismissal_system_rows'] if r['label']=='AD')
    assert row['value']=='Отсутствует' and 'Заблокирован' not in row['note']
    ad.disable_user.assert_not_called()
    ad.get_user.assert_called_once() # only explicit mapping save, not fallback after deletion


@pytest.mark.parametrize('proof',[False,None])
def test_missing_lookup_alone_does_not_finish(mapping,proof):
    db,settings,records,worker,ad,run,service,user=prepared(mapping)
    ad.get_user_by_object_guid.return_value=None
    ad.confirm_user_absent.return_value=proof
    with pytest.raises(ValueError):service.confirm_disabled(run.id,'admin')
    assert run.completed_at is None


def test_deleted_conflicting_target_no_longer_holds_verified_remaining_account(mapping):
    db,settings,records,worker,ad,run,service,user=prepared(mapping)
    old=FinalDismissalBlockTarget(run_id=run.id,system='ad',target_key='ad:old-guid',stable_id='old-guid',
        target_identifier='old',status='intervention',last_error='Учетная запись AD не найдена')
    db.add(old);db.commit()
    ad.confirm_user_absent.return_value=True
    assert 'в журнал' in service.confirm_disabled(run.id,'admin')
    assert old.status=='already_completed' and old.last_result=='confirmed_absent'
    assert run.status=='success'
    ad.confirm_user_absent.assert_called_once_with(object_guid='old-guid',username='old')
    from app.routers.dashboard_dismissals import _completed_dismissal_journal_item
    item=_completed_dismissal_journal_item(run,worker._targets(run.id),notice=None,techexpert_notifications=[],deferral_items=[],snapshot=None)
    row=next(r for r in item['dismissal_system_rows'] if r['label']=='AD')
    assert row['value']=='first' and row['note']=='Заблокирован'


@pytest.mark.parametrize('mode',['exists','unavailable','no_guid'])
def test_unproven_other_target_is_not_cleared(mapping,mode):
    db,settings,records,worker,ad,run,service,user=prepared(mapping)
    old=FinalDismissalBlockTarget(run_id=run.id,system='ad',target_key='ad:old',stable_id='' if mode=='no_guid' else 'old-guid',
        target_identifier='old',status='intervention',last_error='Unresolved')
    db.add(old);db.commit()
    ad.confirm_user_absent.return_value=False
    if mode=='unavailable':ad.confirm_user_absent.side_effect=RuntimeError('SECRET')
    if mode=='unavailable':
        with pytest.raises(ValueError) as error:service.confirm_disabled(run.id,'admin')
        assert 'SECRET' not in str(error.value)
    else:service.confirm_disabled(run.id,'admin')
    assert run.completed_at is None and old.status=='intervention'


def test_background_known_guid_absent_is_completed_without_login_fallback(mapping):
    db,settings,records,worker,ad,run,service,user=prepared(mapping)
    candidate=next(iter(worker._candidate_map().values()))
    worker._ensure_run(candidate)
    target=next(t for t in worker._targets(run.id) if t.target_key!='ad:conflict')
    ad.get_user.reset_mock()
    ad.get_user_by_object_guid.return_value=None
    ad.confirm_user_absent.return_value=True
    worker._process_ad(target)
    assert target.status=='already_completed' and target.last_result=='confirmed_absent'
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()
