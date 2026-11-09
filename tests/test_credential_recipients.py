from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from test_combined_credentials import db, service, sends, create  # shared fixtures delivered with patch
from app.models import CredentialDelivery, EmailLoginMapping, HRSourceRecord, OneCImportRun
from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.models_onec_sources import HREmploymentState
from app.services.provisioning import ProvisioningInput


def seed(db, extra='other@second.test', status='active', present=True):
    for domain,email,emp_status,emp_present in [('example.test','ivanov@example.test','active',True),
                                               ('second.test',extra,status,present)]:
        db.add(HRSourceRecord(worker_key='worker',source_id=domain,fio='Иванов Иван Иванович',
                             corporate_email=email,is_present=emp_present))
        db.add(HREmploymentState(worker_key='worker',source_id=domain,fio='Иванов Иван Иванович',
                                status=emp_status,is_present=emp_present))
    event=HREmploymentArrivalEvent(worker_key='worker',source_id='example.test',source_name='First',
                                   fio='Иванов Иван Иванович',sequence=1,status='pending')
    db.add(event)
    db.commit()
    return str(event.id)


def account(zid='second-box', addresses=('other@second.test',), status='active'):
    return SimpleNamespace(zimbra_id=zid,addresses=addresses,account_status=status)


def run(db,service,ids,personal='personal@example.net'):
    return service.provision(db,'operator',ProvisioningInput('Иванов','Иван','Иванович',personal,'ivanov','example.test'),arrival_event_ids=ids)


def test_all_current_organizations_sent_and_persisted_per_address(db,service):
    ids=seed(db)
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':account()}
    result=run(db,service,ids)
    assert result.status=='success'
    assert {call.kwargs['recipient'] for call in sends(service)} == {'personal@example.net','ivanov@example.test','other@second.test'}
    rows=db.scalars(select(CredentialDelivery)).all()
    assert len(rows)==3 and all(row.status=='sent' and row.sent_at for row in rows)
    assert all('SECRET' not in str(row.__dict__) for row in rows)
    db.expire_all()
    assert len(db.scalars(select(CredentialDelivery)).all())==3


def test_alias_of_created_mailbox_not_duplicated(db,service):
    ids=seed(db)
    shared=account('same',('ivanov@example.test','other@second.test'))
    service.zimbra.accounts_by_addresses.return_value={'ivanov@example.test':shared,'other@second.test':shared}
    result=run(db,service,ids)
    assert len(sends(service))==2
    row=next(row for row in result.delivery_results if row['email']=='other@second.test')
    assert row['recipient']=='ivanov@example.test' and row['status']=='sent'


@pytest.mark.parametrize('status,present',[('dismissed',True),('absent',False),('active',False)])
def test_historical_and_absent_employments_excluded(db,service,status,present):
    ids=seed(db,status=status,present=present)
    result=run(db,service,ids)
    assert result.status=='success' and len(sends(service))==2
    service.zimbra.accounts_by_addresses.assert_not_called()


def test_manual_creation_does_not_guess_worker_from_fio(db,service):
    seed(db)
    result=create(db,service)
    assert result.status=='success' and len(sends(service))==2
    service.zimbra.accounts_by_addresses.assert_not_called()


@pytest.mark.parametrize('accounts',[{}, {'other@second.test':account(status='closed')},
                                   {'other@second.test':account(addresses=('unrelated@second.test',))}])
def test_unverified_or_closed_mailbox_is_not_sent(db,service,accounts):
    ids=seed(db)
    service.zimbra.accounts_by_addresses.return_value=accounts
    result=run(db,service,ids)
    assert len(sends(service))==2
    row=next(row for row in result.delivery_results if row['email']=='other@second.test')
    assert row['status'] in {'unverified','skipped'}


def test_ambiguous_hr_address_not_sent(db,service):
    ids=seed(db)
    db.add(HRSourceRecord(worker_key='other-worker',source_id='third.test',fio='Другой',corporate_email='other@second.test'))
    db.commit()
    result=run(db,service,ids)
    assert result.status=='partial' and len(sends(service))==2


def test_mailbox_id_owned_by_other_worker_not_sent(db,service):
    ids=seed(db)
    db.add(EmailLoginMapping(worker_key='other-worker',source_domain='third.test',source_email='old@third.test',
        ad_object_guid='',ad_login='',zimbra_id='second-box',zimbra_email='old@third.test'))
    db.commit()
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':account()}
    result=run(db,service,ids)
    assert result.status=='partial' and len(sends(service))==2


def test_failed_additional_address_has_own_status_without_resend(db,service):
    import smtplib
    ids=seed(db)
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':account()}
    def send(message,**kwargs):
        if kwargs['recipient']=='other@second.test': raise smtplib.SMTPException('SECRET')
    service.mailer._send_once.side_effect=send
    result=run(db,service,ids)
    assert result.status=='partial'
    assert sum(call.kwargs['recipient']=='ivanov@example.test' for call in sends(service))==1
    assert sum(call.kwargs['recipient']=='other@second.test' for call in sends(service))==3
    assert {row['email']:row['status'] for row in result.delivery_results}=={
        'ivanov@example.test':'sent','personal@example.net':'sent','other@second.test':'failed'}
    assert 'SECRET' not in str(result.warnings)


def test_import_in_progress_skips_additional_addresses(db,service):
    ids=seed(db)
    db.add(OneCImportRun(status='running'))
    db.commit()
    result=run(db,service,ids)
    assert result.status=='partial' and len(sends(service))==2
    service.zimbra.accounts_by_addresses.assert_not_called()


def test_lookup_error_does_not_leak_or_rollback_created_accounts(db,service):
    ids=seed(db)
    service.zimbra.accounts_by_addresses.side_effect=RuntimeError('PASSWORD-SECRET')
    result=run(db,service,ids)
    assert result.ad_enabled and result.zimbra_created and result.status=='partial'
    assert 'PASSWORD-SECRET' not in str(result.warnings)


def test_personal_equal_to_additional_hr_address_and_aliases_deduped(db,service):
    ids=seed(db)
    db.add(HRSourceRecord(worker_key='worker',source_id='third.test',fio='Иванов Иван Иванович',corporate_email='alias@third.test'))
    db.add(HREmploymentState(worker_key='worker',source_id='third.test',fio='Иванов Иван Иванович',status='active',is_present=True))
    db.commit()
    shared=account('shared',('other@second.test','alias@third.test'))
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':shared,'alias@third.test':shared}
    result=run(db,service,ids,personal='other@second.test')
    assert result.status=='success' and len(sends(service))==2
    assert len(result.delivery_results)==3


def test_invalid_arrival_prevents_external_creation(db,service):
    with pytest.raises(ValueError): run(db,service,'999')
    service.ad.create_disabled_user.assert_not_called()


def test_name_changed_in_form_does_not_send_to_another_hr_identity(db,service):
    ids=seed(db)
    result=service.provision(db,'operator',ProvisioningInput('Другой','Человек','',
        'personal@example.net','ivanov','example.test'),arrival_event_ids=ids)
    assert result.status=='partial' and len(sends(service))==2
    service.zimbra.accounts_by_addresses.assert_not_called()


def test_scheduled_dismissal_is_still_active(db,service):
    ids=seed(db,status='scheduled')
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':account()}
    assert run(db,service,ids).status=='success'
    assert len(sends(service))==3


def test_http_registration_forwards_verified_event_context(db,service,monkeypatch):
    from app.routers import employees
    from app.security import CurrentUser
    from starlette.requests import Request
    ids=seed(db)
    service.zimbra.accounts_by_addresses.return_value={'other@second.test':account()}
    monkeypatch.setattr(employees,'get_current_user',lambda request:CurrentUser('op','operator','local'))
    monkeypatch.setattr(employees,'ProvisioningService',lambda settings:service)
    monkeypatch.setattr(employees.EmployeeArrivalAccountService,'inspect',lambda self,ids:{'errors':[],'has_candidates':True})
    monkeypatch.setattr(employees.EmployeeArrivalAccountService,'resolve',lambda self,**kwargs:{})
    request=Request({'type':'http','method':'POST','scheme':'http','path':'/employees/provision',
        'query_string':b'','headers':[],'server':('test',80),'session':{'csrf':'token'}})
    response=employees.provision_employee(request,last_name='Иванов',first_name='Иван',middle_name='Иванович',
        personal_email='personal@example.net',confirm_no_personal_email='false',login='ivanov',mail_domain='example.test',
        arrival_event_ids=ids,csrf='token',db=db,settings=service.settings)
    assert response.status_code==200
    assert 'other@second.test' in response.body.decode()
    assert len(sends(service))==3


def test_new_table_is_created_without_rewriting_existing_operations(db,service):
    from app.db import Base
    from app.models import ProvisioningOperation
    result=create(db,service)
    Base.metadata.create_all(db.bind)
    operation=db.get(ProvisioningOperation,result.operation_id)
    assert operation.login=='ivanov' and operation.corporate_mail_sent
    assert len(operation.deliveries)==2
