from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import security
from app.config import Settings
from app.db import Base
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord, OneCImportRun
from app.models_notifications import HREmploymentDismissalEvent
from app.models_onec_sources import HREmploymentState
from app.models_dismissal_lifecycle import FinalDismissalBlockRun, FinalDismissalBlockTarget
from app.models_techexpert import TechExpertNotification
from app.routers import techexpert as routes
from app.services import techexpert_identity_review as review
from app.services import techexpert_lifecycle as te
from app.services.ad import ADIdentityCandidatesError


@pytest.fixture
def case(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        settings = Settings(_env_file=None, app_timezone='UTC', zimbra_domains=['a.test','b.test'],
            onec_source_domain='a.test', ad_check_enabled=True)
        day = (datetime.now(timezone.utc)-timedelta(days=1)).date()
        state = HREmploymentState(worker_key='worker', source_id='a.test', fio='Иванов Иван Иванович',
            dismissal_date=day, status='dismissed', is_present=False)
        record = HRSourceRecord(worker_key='worker', source_id='a.test', fio=state.fio,
            corporate_email='ivanov@a.test', login='', is_present=False)
        event = HREmploymentDismissalEvent(worker_key='worker', source_id='a.test', fio=state.fio,
            sequence=1, first_dismissal_date=day, current_dismissal_date=day, status='absent')
        db.add_all([state, record, event])
        db.flush()
        row = TechExpertNotification(employment_event_id=event.id, worker_key='worker', source_id='a.test',
            fio=state.fio, dismissal_date=day, event_updated_at=event.updated_at,
            scheduled_for=datetime.now(timezone.utc)-timedelta(hours=1), status='intervention')
        db.add(row)
        db.commit()
        lifecycle = te.TechExpertLifecycleService(settings, db)
        lifecycle.config.source_domain = 'a.test'
        db.commit()
        user = SimpleNamespace(username='ivanov', object_guid='11111111-1111-1111-1111-111111111111',
            display_name=state.fio, is_enabled=True, distinguished_name='CN=Ivanov,DC=a,DC=test')
        ad = Mock()
        ad.lookup_group_user.side_effect = ADIdentityCandidatesError([user])
        ad.get_user.return_value = user
        monkeypatch.setattr(review, 'ActiveDirectoryService', lambda settings: ad)
        monkeypatch.setattr(te, 'ActiveDirectoryService', lambda settings: ad)
        monkeypatch.setattr(security, 'get_current_user', lambda request: security.CurrentUser('operator','operator','local'))
        yield SimpleNamespace(db=db, settings=settings, row=row, record=record, state=state, event=event,
            ad=ad, user=user, lifecycle=lifecycle, review=review.TechExpertIdentityReviewService(settings,db))
    engine.dispose()


def request():
    return Request({'type':'http','method':'GET','path':'/','headers':[], 'query_string':b'modal=1',
                    'session':{'csrf':'token'}})


def test_review_lists_candidates_and_negative_choice_without_touching_accounts(case):
    page = routes.dismissal_identity_page(case.row.id, request(), login='', settings=case.settings, db=case.db)
    body = page.body.decode()
    assert page.status_code == 200 and 'ivanov' in body and 'Подтвердить отсутствие AD' in body
    case.ad.disable_user.assert_not_called()
    case.ad.remove_user_from_group.assert_not_called()


def test_negative_decision_is_persisted_and_worker_completes_without_ad_writes(case,monkeypatch):
    fingerprint = case.review.inspect(case.row.id)['fingerprint']
    assert fingerprint
    case.review.save(case.row.id, 'operator', fingerprint=fingerprint, confirmed=True)
    monkeypatch.setattr(case.lifecycle,'_recheck_batch_candidate',lambda row: True)
    assert case.lifecycle._prepare_batch_candidate(case.row) is False
    assert case.row.status == 'skipped' and not case.row.last_error
    case.ad.remove_user_from_group.assert_not_called()
    case.ad.disable_user.assert_not_called()
    assert case.db.scalar(select(AuditLog).where(AuditLog.action==review.REJECTION_ACTION))


@pytest.mark.parametrize('change',['new_candidate','new_mapping','hr_return','import'])
def test_negative_choice_cannot_outlive_directory_identity_or_hr_changes(case,change):
    context = case.review.inspect(case.row.id)
    if change == 'new_candidate':
        case.ad.lookup_group_user.side_effect = ADIdentityCandidatesError([
            SimpleNamespace(**{**vars(case.user), 'object_guid':'22222222-2222-2222-2222-222222222222'})])
    elif change == 'new_mapping':
        case.record.login='ivanov'
    elif change == 'hr_return':
        case.state.status='active'
    else:
        case.db.add(OneCImportRun(status='running'))
    case.db.commit()
    with pytest.raises(ValueError):
        case.review.save(case.row.id,'operator',fingerprint=context['fingerprint'],confirmed=True)
    assert case.row.status=='intervention'


def test_new_candidate_invalidates_saved_negative_decision_in_background(case,monkeypatch):
    fingerprint=case.review.inspect(case.row.id)['fingerprint']
    case.review.save(case.row.id,'operator',fingerprint=fingerprint,confirmed=True)
    case.user.object_guid='22222222-2222-2222-2222-222222222222'
    monkeypatch.setattr(case.lifecycle,'_recheck_batch_candidate',lambda row: True)
    assert case.lifecycle._prepare_batch_candidate(case.row) is False
    assert case.row.status=='intervention' and 'кандидаты AD' in case.row.last_error


@pytest.mark.parametrize('active_elsewhere',[False,True])
def test_positive_mapping_preserves_mail_and_only_reopens_global_ad_when_everywhere_dismissed(case,active_elsewhere):
    mapping=EmailLoginMapping(worker_key='worker',source_domain='a.test',source_email='ivanov@a.test',
        ad_login='',ad_object_guid='',zimbra_id='saved-mail-id',zimbra_email='ivanov@a.test')
    case.db.add(mapping)
    if active_elsewhere:
        case.db.add(HREmploymentState(worker_key='worker',source_id='b.test',fio=case.state.fio,
            status='active',is_present=True))
    else:
        case.db.add(FinalDismissalBlockRun(worker_key='worker',dismissal_date=case.row.dismissal_date,
            effective_block_date=case.row.dismissal_date,status='success',completed_at=datetime.now(timezone.utc)))
    case.db.commit()
    case.review.save(case.row.id,'operator',login='ivanov',expected_guid=case.user.object_guid,confirmed=True)
    assert mapping.ad_login=='ivanov' and mapping.ad_object_guid==case.user.object_guid
    assert mapping.zimbra_id=='saved-mail-id' and mapping.zimbra_email=='ivanov@a.test'
    assert case.row.status=='pending'
    run=case.db.scalar(select(FinalDismissalBlockRun))
    if active_elsewhere:
        assert run is None
    else:
        assert run.status=='running' and run.completed_at is None
        assert case.db.scalar(select(FinalDismissalBlockTarget)).status=='pending'
    case.ad.remove_user_from_group.assert_not_called()
    case.ad.disable_user.assert_not_called()


@pytest.mark.parametrize('guard',['guid_changed','wrong_name','owned_elsewhere','not_confirmed','import'])
def test_positive_mapping_requires_fresh_identity_owner_and_confirmation(case,guard):
    guid=case.user.object_guid
    if guard=='guid_changed':
        guid='other-guid'
    elif guard=='wrong_name':
        case.user.display_name='Другой Работник'
    elif guard=='owned_elsewhere':
        case.db.add(EmailLoginMapping(worker_key='other',source_domain='a.test',source_email='',
            ad_login='ivanov',ad_object_guid=guid,zimbra_id='',zimbra_email=''))
    elif guard=='import':
        case.db.add(OneCImportRun(status='running'))
    case.db.commit()
    with pytest.raises(ValueError):
        case.review.save(case.row.id,'operator',login='ivanov',expected_guid=guid,confirmed=guard!='not_confirmed')
    assert not case.db.scalar(select(EmailLoginMapping).where(EmailLoginMapping.worker_key=='worker'))


def test_directory_outage_never_offers_negative_confirmation(case):
    case.ad.lookup_group_user.side_effect=ConnectionError('AD unavailable')
    context=case.review.inspect(case.row.id)
    assert context['check_error'] and not context['fingerprint']
    with pytest.raises(ValueError):
        case.review.save(case.row.id,'operator',fingerprint='forged',confirmed=True)


def test_routes_require_csrf_and_operator_role(case,monkeypatch):
    with pytest.raises(security.CSRFMismatchError):
        routes.dismissal_identity_save(case.row.id,request(),csrf='bad',settings=case.settings,db=case.db)
    monkeypatch.setattr(security,'get_current_user',lambda request:security.CurrentUser('viewer','viewer','local'))
    with pytest.raises(HTTPException):
        routes.dismissal_identity_page(case.row.id,request(),login='',settings=case.settings,db=case.db)


def test_confirmed_empty_ad_result_needs_no_operator_decision(case, monkeypatch):
    case.ad.lookup_group_user.side_effect=None
    case.ad.lookup_group_user.return_value=None
    monkeypatch.setattr(case.lifecycle,'_recheck_batch_candidate',lambda row: True)
    assert not case.lifecycle._prepare_batch_candidate(case.row)
    assert case.row.status=='skipped' and not case.row.last_error
    assert not case.db.scalar(select(AuditLog.id).where(AuditLog.action==review.REJECTION_ACTION))


def test_post_saves_decision_and_returns_visible_result(case):
    fingerprint=case.review.inspect(case.row.id)['fingerprint']
    response=routes.dismissal_identity_save(case.row.id,request(),csrf='token',login='',expected_guid='',
        fingerprint=fingerprint,confirmed='true',settings=case.settings,db=case.db)
    assert response.status_code==200 and 'Решение сохранено' in response.body.decode()


def test_name_only_search_exposes_all_candidates_and_never_silently_selects(monkeypatch):
    from contextlib import contextmanager
    from app.services.ad import ActiveDirectoryService
    service=ActiveDirectoryService(Settings(_env_file=None,ad_check_enabled=True,ad_base_dn='DC=a,DC=test'))
    conn=SimpleNamespace(entries=['one','two','three'],result={'result':0},response=[])
    conn.search=Mock()
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service,'_service_connection',connection)
    monkeypatch.setattr(service,'_entry_to_directory_user',lambda entry:SimpleNamespace(object_guid=entry,username=entry))
    with pytest.raises(ADIdentityCandidatesError) as error:
        service.lookup_group_user('',full_name='Иванов Иван Иванович')
    assert len(error.value.candidates)==3
    assert conn.search.call_args.kwargs['size_limit']==0
    conn.result={'result':4}
    with pytest.raises(RuntimeError):
        service.lookup_group_user('',full_name='Иванов Иван Иванович')


def test_historical_dismissal_details_read_saved_record_without_relaxing_manual_blocking(case, monkeypatch):
    from app.services.blocking import BlockingService
    from app.services.dismissal_details import DismissalDetailsService
    service=BlockingService(case.settings,case.db)
    with pytest.raises(LookupError):
        service.card(case.record.id)
    with pytest.raises(ValueError):
        service.card(case.record.id,allow_historical=True)
    calls=[]
    original=BlockingService.card
    def card(self,record_id,**kwargs):
        calls.append(kwargs)
        return original(self,record_id,**kwargs)
    monkeypatch.setattr(BlockingService,'card',card)
    monkeypatch.setattr(BlockingService,'_itinvent_lookup',lambda self,login:SimpleNamespace(
        state='disabled',itinvent=None,error='',checked_at=None))
    case.settings.ad_check_enabled=False
    case.settings.zimbra_check_enabled=False
    result,error=DismissalDetailsService(case.settings,case.db)._blocking_card('worker')
    assert result is not None and not error
    assert calls==[{'remember_itinvent':False,'allow_historical':True}]
    assert not case.db.dirty
