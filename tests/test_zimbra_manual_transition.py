from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_zimbra_primary_transition import case, request, enqueue, organization_event, OLD, NEW, OTHER, NOW
from app import security
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import ZimbraLifecycleSettings, ZimbraPrimaryTransition
from app.routers import zimbra_lifecycle as routes
from app.services import zimbra_primary_transition as module
from app.services.zimbra import ZimbraAccountIdentity

GUID='11111111-1111-1111-1111-111111111111'


def manually_changed(case):
    case.mail.primary=NEW
    case.mail.addresses={NEW,OTHER}


def confirm(case, **kwargs):
    return case.service.confirm_manual(case.action.id,'operator',email=NEW,
        expected_id='z-1',confirmed=True,**kwargs)


def mapping(case, source='new.test', **kwargs):
    row=EmailLoginMapping(worker_key='worker',source_domain=source,source_email=NEW if source=='new.test' else OLD,
        ad_login='old-ad',ad_object_guid='old-guid',zimbra_id='z-1',zimbra_email=OLD,**kwargs)
    case.db.add(row);case.db.commit()
    return row


def test_inspect_is_read_only_and_reports_current_mailbox(case):
    manually_changed(case)
    checked=case.service.inspect_manual(case.action.id,NEW)
    assert checked['identity'].zimbra_id=='z-1' and checked['new_source']=='new.test'
    assert checked['ad_user'] is None and not case.db.new and not case.db.dirty
    assert case.service.job(case.action.id) is None
    assert case.mail.calls==[]


def test_confirmation_finishes_without_backup_mail_commands_or_passwords(case):
    manually_changed(case)
    existing=mapping(case)
    job=confirm(case)
    assert job.status=='completed' and job.phase=='manual_done' and job.active_mailbox_id is None
    assert case.action.status=='completed' and case.action.completed_at and not case.action.last_error
    assert existing.zimbra_id=='z-1' and existing.zimbra_email==NEW
    assert existing.ad_login=='old-ad' and existing.ad_object_guid=='old-guid'
    assert not job.backup_path  # An operator's attestation is not a fabricated backup file.
    case.backup.assert_not_called()
    assert case.mail.calls==[]
    audit=case.db.scalar(select(AuditLog).where(AuditLog.action=='zimbra_primary_transition_confirmed_manual'))
    assert audit.actor=='operator' and 'external_commands' in audit.details and 'password' not in audit.details.lower()
    case.service.process_due()
    assert case.mail.calls==[]
    assert confirm(case).id==job.id
    assert len(case.db.scalars(select(AuditLog).where(AuditLog.action=='zimbra_primary_transition_confirmed_manual')).all())==1


def test_new_physical_mailbox_can_be_selected_after_original_is_removed(case):
    identity=ZimbraAccountIdentity('z-new',NEW,'ivanov',(NEW,), 'active')
    case.mail.account_by_address=lambda address: identity if address==NEW else None
    case.mail.accounts_by_ids=lambda ids: {'z-new':identity} if 'z-new' in ids else {}
    case.mail.accounts_by_addresses=lambda addresses:{a:identity for a in addresses if a==NEW}
    old=mapping(case,source='old.test')
    job=case.service.confirm_manual(case.action.id,'operator',email=NEW,expected_id='z-new',confirmed=True)
    current=case.db.scalar(select(EmailLoginMapping).where(EmailLoginMapping.source_domain=='new.test'))
    assert current.zimbra_id=='z-new' and job.zimbra_id=='z-new'
    assert old.zimbra_id=='z-1' and old.source_email==OLD
    case.service.employment.process()
    assert case.mail.calls==[]


def test_explicit_new_ad_choice_updates_global_mapping_without_touching_ad(case,monkeypatch):
    manually_changed(case)
    old=mapping(case,source='old.test')
    active=mapping(case)
    ad=Mock()
    ad.get_user.return_value=SimpleNamespace(username='new-ad',object_guid=GUID,is_enabled=True,display_name='Иванов Иван')
    monkeypatch.setattr(module,'ActiveDirectoryService',lambda settings:ad)
    confirm(case,ad_login='new-ad',expected_guid=GUID)
    assert old.ad_login==active.ad_login=='new-ad'
    assert old.ad_object_guid==active.ad_object_guid==GUID
    assert old.zimbra_email==OLD and active.zimbra_email==NEW
    ad.disable_user.assert_not_called()
    ad.enable_user.assert_not_called()
    assert case.mail.calls==[]


@pytest.mark.parametrize('guard',['old_address','other_old_alias','inactive_mail','old_primary','hr_return',
    'new_inactive','new_not_present','date_changed','deferral','hr_not_ready','zimbra_disabled','outage','wrong_id'])
def test_incomplete_or_stale_manual_transfer_cannot_be_approved(case,guard):
    manually_changed(case)
    if guard=='old_address':case.mail.addresses.add(OLD)
    elif guard=='other_old_alias':case.mail.addresses.add('another@old.test')
    elif guard=='inactive_mail':
        original=case.mail.identity
        case.mail.identity=lambda:ZimbraAccountIdentity('z-1',NEW,'ivanov',original().addresses,'closed')
    elif guard=='old_primary':case.mail.primary=OLD
    elif guard in {'hr_return','date_changed','new_inactive','new_not_present'}:
        source='new.test' if guard.startswith('new_') else 'old.test'
        state=case.db.scalar(select(HREmploymentState).where(HREmploymentState.source_id==source))
        if guard=='hr_return':state.status='active'
        elif guard=='date_changed':state.dismissal_date=NOW.date()-timedelta(days=1)
        elif guard=='new_inactive':state.status='dismissed'
        else:state.is_present=False
        case.db.commit()
    elif guard=='deferral':
        case.db.add(DismissalDeferral(worker_key='worker',dismissal_date=NOW.date(),
            deferred_until=NOW.date()+timedelta(days=7)));case.db.commit()
    elif guard=='hr_not_ready':case.ready['value']=False
    elif guard=='zimbra_disabled':case.settings.zimbra_check_enabled=False
    elif guard=='outage':case.mail.accounts_by_ids=Mock(side_effect=ConnectionError('Zimbra unavailable'))
    else:
        case.mail.accounts_by_ids=lambda ids:{'z-new':ZimbraAccountIdentity('z-new',NEW,'ivanov',(NEW,), 'active')}
    with pytest.raises((ValueError,ConnectionError,module.TransitionDeferred)):confirm(case)
    assert case.service.job(case.action.id) is None
    assert not case.db.scalar(select(EmailLoginMapping))
    assert case.mail.calls==[]


@pytest.mark.parametrize('guard',['stale_id','no_preview','not_confirmed','other_owner','active_hr_owner','running','other_job','locked'])
def test_confirmation_requires_owner_expected_id_and_exclusive_job(case,guard):
    manually_changed(case)
    kwargs=dict(email=NEW,expected_id='z-1',confirmed=True)
    if guard=='stale_id':kwargs['expected_id']='old-preview-id'
    elif guard=='no_preview':kwargs['expected_id']=''
    elif guard=='not_confirmed':kwargs['confirmed']=False
    elif guard=='other_owner':
        case.db.add(EmailLoginMapping(worker_key='other',source_domain='new.test',source_email='',
            ad_login='',ad_object_guid='',zimbra_id='z-1',zimbra_email=NEW));case.db.commit()
    elif guard=='active_hr_owner':
        case.db.add(HRSourceRecord(worker_key='other',source_id='new.test',fio='Другой работник',corporate_email=NEW,is_present=True));case.db.commit()
    elif guard in {'running','other_job'}:
        case.db.add(ZimbraPrimaryTransition(action_id=case.action.id if guard=='running' else 999,worker_key='worker',
            zimbra_id='z-1',active_mailbox_id='z-1',old_address=OLD,new_address=NEW,old_source='old.test',new_source='new.test',
            operator='operator',status='running' if guard=='running' else 'queued'));case.db.commit()
    else:case.service._lock.acquire()
    try:
        with pytest.raises(ValueError):case.service.confirm_manual(case.action.id,'operator',**kwargs)
    finally:
        if guard=='locked':case.service._lock.release()
    assert case.action.status!='completed' and case.mail.calls==[]


def test_failed_automatic_job_can_be_settled_after_manual_completion(case):
    job=enqueue(case)
    job.status='failed';job.phase='cleanup';job.last_error='old failure'
    case.db.commit()
    manually_changed(case)
    assert confirm(case).id==job.id
    case.service.process_due()
    assert job.status=='completed' and not job.last_error and case.mail.calls==[]


def test_post_preview_and_confirmation_render_and_preserve_csrf_role_checks(case,monkeypatch):
    manually_changed(case)
    response=routes.transition_manual(case.action.id,request(),csrf='token',email=NEW,ad_login='',operation='inspect',
        settings=case.settings,db=case.db)
    assert response.status_code==200 and 'Утвердить в системе' in response.body.decode()
    assert case.service.job(case.action.id) is None
    with pytest.raises(security.CSRFMismatchError):
        routes.transition_manual(case.action.id,request(),csrf='bad',email=NEW,settings=case.settings,db=case.db)
    monkeypatch.setattr(security,'get_current_user',lambda request:security.CurrentUser('viewer','viewer','local'))
    with pytest.raises(HTTPException):
        routes.transition_manual(case.action.id,request(),csrf='token',email=NEW,settings=case.settings,db=case.db)


def test_mail_disabled_backup_permission_does_not_prevent_read_only_approval(case):
    manually_changed(case)
    case.db.get(ZimbraLifecycleSettings,1).allow_backup=False;case.db.commit()
    assert confirm(case).phase=='manual_done'
    case.backup.assert_not_called()


@pytest.mark.parametrize('guard',['different_guid','different_name','disabled','not_found','owned_elsewhere','ad_disabled'])
def test_optional_ad_mapping_is_checked_before_local_save(case,monkeypatch,guard):
    manually_changed(case)
    ad=Mock()
    ad.get_user.return_value=SimpleNamespace(username='new-ad',object_guid=GUID,is_enabled=True,display_name='Иванов Иван')
    monkeypatch.setattr(module,'ActiveDirectoryService',lambda settings:ad)
    if guard=='different_guid':ad.get_user.return_value.object_guid='other-guid'
    elif guard=='different_name':ad.get_user.return_value.display_name='Другой Работник'
    elif guard=='disabled':ad.get_user.return_value.is_enabled=False
    elif guard=='not_found':ad.get_user.return_value=None
    elif guard=='ad_disabled':case.settings.ad_check_enabled=False
    else:
        case.db.add(EmailLoginMapping(worker_key='other',source_domain='new.test',source_email='',
            ad_login='new-ad',ad_object_guid=GUID,zimbra_id='',zimbra_email=''));case.db.commit()
    with pytest.raises(ValueError):confirm(case,ad_login='new-ad',expected_guid=GUID)
    assert not case.db.scalar(select(EmailLoginMapping).where(EmailLoginMapping.worker_key=='worker'))
    assert case.action.status!='completed'
    ad.disable_user.assert_not_called()
    assert case.mail.calls==[]


def test_selected_organization_must_still_be_active_when_confirmation_is_saved(case,monkeypatch):
    manually_changed(case)
    case.db.add(HREmploymentState(worker_key='worker',source_id='third.test',fio='Иванов Иван',
        status='active',is_present=True));case.db.commit()
    original=case.service.inspect_manual
    def changed(*args):
        result=original(*args)
        state=case.db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='new.test'))
        state.status='dismissed';case.db.commit()
        return result
    monkeypatch.setattr(case.service,'inspect_manual',changed)
    with pytest.raises(ValueError):confirm(case)
    assert case.action.status!='completed'


def test_mail_status_change_between_address_and_id_reads_is_not_approved(case):
    manually_changed(case)
    case.mail.accounts_by_ids=lambda ids:{'z-1':ZimbraAccountIdentity('z-1',NEW,'ivanov',(NEW,OTHER),'closed')}
    with pytest.raises(ValueError):confirm(case)
    assert case.service.job(case.action.id) is None


def test_confirmation_settles_cancelled_action_and_moves_dismissal_to_journal(case):
    from app.services.upcoming_dismissals import UpcomingDismissalService
    from app.routers.dashboard_dismissals import _journal_items
    organization_event(case)
    case.action.status='cancelled';case.action.last_error='Повторная HR-проверка отменила действие'
    case.db.commit()
    manually_changed(case)
    confirm(case)
    assert UpcomingDismissalService(case.settings,case.db)._all_candidates(include_expired=True)==[]
    items=[i for i in _journal_items(case.db,today=NOW.date(),settings=case.settings) if i['kind']=='dismissal']
    assert len(items)==1
    assert any('Переход выполнен вручную' in str(step['details']) for step in items[0]['dismissal_steps'])


def test_confirmation_post_redirects_to_visible_completed_state(case):
    manually_changed(case)
    response=routes.transition_manual(case.action.id,request(),csrf='token',email=NEW,ad_login='',operation='confirm',
        expected_id='z-1',expected_guid='',confirmed='true',settings=case.settings,db=case.db)
    assert response.status_code==303
    response=routes.transition_page(case.action.id,request(),settings=case.settings,db=case.db)
    assert 'Ручное сопоставление сохранено' in response.body.decode()
    assert 'Утвердить в системе' not in response.body.decode()


def test_retry_cannot_race_with_manual_approval_or_requeue_approved_job(case):
    job=enqueue(case)
    job.status='failed';case.db.commit()
    case.service._lock.acquire()
    try:
        with pytest.raises(ValueError):case.service.retry(case.action.id,'operator')
    finally:case.service._lock.release()
    assert job.status=='failed'
    manually_changed(case)
    confirm(case)
    with pytest.raises(ValueError):case.service.retry(case.action.id,'operator')
    assert job.status=='completed'
