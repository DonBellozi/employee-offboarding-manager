from collections import Counter
from html import unescape
from types import SimpleNamespace
from unittest.mock import Mock
import smtplib

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import AuditLog, DomainMailProfile, ProvisioningOperation
from app.services.mailer import (CredentialMailer, ensure_domain_mail_profiles,
    DEFAULT_COMBINED_BODY_HTML, COMBINED_TEMPLATE_VARIABLES, validate_mail_template)
from app.services.provisioning import ProvisioningInput, ProvisioningService


@pytest.fixture
def db():
    # Load the existing optional models referenced by metadata foreign keys.
    from app import models_account_requirement, models_employee_arrivals, models_mail_templates  # noqa: F401
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def service(db):
    settings = Settings(_env_file=None, zimbra_domains=['example.test'],
        zimbra_primary_domain='example.test', zimbra_domain_mode='separate',
        smtp_host='smtp.test', smtp_retry_attempts=3, smtp_retry_delay_seconds=0)
    instance = ProvisioningService(settings)
    instance.ad = Mock()
    instance.zimbra = Mock()
    instance.ad.create_disabled_user.return_value = SimpleNamespace(dn='DN', accepted_password='ACCEPTED-AD-SECRET')
    instance.zimbra.create_account.return_value = SimpleNamespace(primary_email='ivanov@example.test', aliases=())
    instance.check_login = Mock(return_value={'ad': False, 'zimbra': False})
    instance.mailer._send_once = Mock()
    return instance


def create(db, service, personal='personal@example.net', domain='example.test'):
    return service.provision(db, 'operator', ProvisioningInput('Иванов','Иван','Иванович',personal,'ivanov',domain))


def sends(service):
    return service.mailer._send_once.call_args_list


def test_combined_message_separately_to_each_recipient(db, service):
    result = create(db, service)
    assert result.status == 'success'
    assert result.credential_delivery_mode == 'combined'
    assert result.personal_mail_sent and result.corporate_mail_sent
    assert [call.kwargs['recipient'] for call in sends(service)] == ['personal@example.net','ivanov@example.test']
    for call in sends(service):
        message = call.args[0]
        assert message['Cc'] is None and message['Bcc'] is None
        assert message['To'] == call.kwargs['recipient']
        for body in message.iter_parts():
            content = unescape(body.get_content())
            assert result.mail_password in content
            assert 'ACCEPTED-AD-SECRET' in content
            assert 'ivanov@example.test' in content
    operation = db.get(ProvisioningOperation, result.operation_id)
    assert operation.credential_delivery_mode == 'combined'
    audit = db.scalar(select(AuditLog))
    assert result.mail_password not in audit.details
    assert 'ACCEPTED-AD-SECRET' not in audit.details


def test_no_personal_email_only_one_message(db, service):
    result = create(db, service, personal='')
    assert result.status == 'success'
    assert not result.personal_mail_sent and result.corporate_mail_sent
    assert len(sends(service)) == 1
    assert sends(service)[0].kwargs['recipient'] == result.corporate_email


def test_identical_addresses_deduplicated_case_insensitively(db, service):
    result = create(db, service, personal='IVANOV@example.test')
    assert result.personal_mail_sent and result.corporate_mail_sent
    assert len(sends(service)) == 1


@pytest.mark.parametrize('failed', ['personal@example.net','ivanov@example.test'])
def test_failure_retries_only_affected_destination(db, service, failed):
    def send(message, **kwargs):
        if kwargs['recipient'] == failed:
            raise smtplib.SMTPException('SMTP SECRET response')
    service.mailer._send_once.side_effect = send
    result = create(db, service)
    counts = Counter(call.kwargs['recipient'] for call in sends(service))
    assert counts[failed] == 3
    assert sorted(counts.values()) == [1, 3]
    assert result.status == 'partial'
    assert result.personal_mail_sent == (failed != 'personal@example.net')
    assert result.corporate_mail_sent == (failed != 'ivanov@example.test')
    assert 'SECRET' not in str(result.warnings)
    assert 'SECRET' not in db.get(ProvisioningOperation,result.operation_id).error_message


def test_temporary_corporate_error_does_not_resend_personal(db, service):
    attempts = Counter()
    def send(message, **kwargs):
        recipient = kwargs['recipient']
        attempts[recipient] += 1
        if recipient == 'ivanov@example.test' and attempts[recipient] == 1:
            raise OSError('temporary')
    service.mailer._send_once.side_effect = send
    result = create(db, service)
    assert result.status == 'success'
    assert attempts == {'personal@example.net':1, 'ivanov@example.test':2}


def test_ad_not_enabled_only_mail_credentials_are_sent(db, service):
    service.ad.enable_user.side_effect = RuntimeError('not enabled')
    result = create(db, service)
    assert result.credential_delivery_mode == 'mail_only' and result.status == 'partial'
    assert len(sends(service)) == 2
    for call in sends(service):
        assert 'ACCEPTED-AD-SECRET' not in call.args[0].as_string()
        assert result.mail_password in call.args[0].get_body(preferencelist=('plain',)).get_content()


@pytest.mark.parametrize('source', ['ad','zimbra'])
def test_failed_creation_does_not_send_credentials(db, service, source):
    if source == 'ad':
        service.ad.create_disabled_user.side_effect = RuntimeError('failed')
    else:
        service.zimbra.create_account.side_effect = RuntimeError('failed')
    result = create(db, service)
    assert result.status != 'success'
    service.mailer._send_once.assert_not_called()


def test_primary_alias_uses_primary_profile(db, service):
    service.settings.zimbra_domains = ['example.test','alias.test']
    service.settings.zimbra_domain_mode = 'primary_alias'
    result = create(db, service, domain='alias.test')
    assert result.corporate_email == 'ivanov@example.test'
    for call in sends(service):
        assert 'it@example.test' in call.args[0]['From']


def test_custom_templates_preserved_and_combined_initialized(db, service):
    profiles = ensure_domain_mail_profiles(db, service.settings)
    profile = profiles[0]
    profile.personal_body_html = 'CUSTOM MAIL'
    profile.corporate_body_html = 'CUSTOM AD'
    profile.combined_body_html = '<p>CUSTOM {{ ad_login }} {{ mail_password }}</p>'
    db.commit()
    ensure_domain_mail_profiles(db, service.settings)
    assert profile.personal_body_html == 'CUSTOM MAIL'
    assert profile.corporate_body_html == 'CUSTOM AD'
    assert profile.combined_body_html.startswith('<p>CUSTOM')


def test_combined_variables_and_html_escaping(db, service):
    profile = ensure_domain_mail_profiles(db, service.settings)[0]
    validate_mail_template(DEFAULT_COMBINED_BODY_HTML, allowed_variables=COMBINED_TEMPLATE_VARIABLES,
                           field_name='body', autoescape=True)
    service.mailer.send_combined_credentials(profile=profile, recipient='to@example.net', full_name='<script>',
        corporate_email='ivanov@example.test', mail_password='<mail&>', ad_login='ivanov', ad_password='<AD&>')
    body = sends(service)[0].args[0].get_body(preferencelist=('html',)).get_content()
    assert '<script>' not in body and '&lt;script&gt;' in body
    assert '&lt;AD&amp;&gt;' in body and '&lt;mail&amp;&gt;' in body
    with pytest.raises(ValueError):
        validate_mail_template('{{ unsupported }}', allowed_variables=COMBINED_TEMPLATE_VARIABLES,
                               field_name='body', autoescape=True)


def test_quit_failure_after_acceptance_is_not_retried(monkeypatch):
    from app.services import mailer as module
    settings = Settings(_env_file=None, smtp_host='smtp.test', smtp_retry_attempts=3, smtp_starttls=False)
    smtp = Mock()
    smtp.quit.side_effect = OSError('connection lost on QUIT')
    monkeypatch.setattr(module.smtplib, 'SMTP', Mock(return_value=smtp))
    CredentialMailer(settings)._send('to@example.net','Subject','<p>Body</p>', sender_email='it@example.test',sender_name='IT')
    smtp.send_message.assert_called_once()


def test_migration_preserves_legacy_rows_and_is_idempotent(monkeypatch):
    from app import db as module
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE domain_mail_profiles (id INTEGER PRIMARY KEY, personal_body_html TEXT)'))
        conn.execute(text("INSERT INTO domain_mail_profiles VALUES (1,'KEEP')"))
        conn.execute(text('CREATE TABLE provisioning_operations (id INTEGER PRIMARY KEY, personal_mail_sent BOOLEAN)'))
        conn.execute(text('INSERT INTO provisioning_operations VALUES (1,1)'))
    monkeypatch.setattr(module, 'engine', engine)
    module.ensure_compatibility_schema()
    module.ensure_compatibility_schema()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT personal_body_html FROM domain_mail_profiles')).scalar() == 'KEEP'
        assert conn.execute(text('SELECT credential_delivery_mode FROM provisioning_operations')).scalar() == 'legacy'
        assert conn.execute(text('SELECT personal_mail_sent FROM provisioning_operations')).scalar() == 1
    assert 'combined_body_html' in {col['name'] for col in inspect(engine).get_columns('domain_mail_profiles')}
    engine.dispose()


def request():
    from starlette.requests import Request
    return Request({'type':'http', 'method':'GET', 'scheme':'http', 'path':'/admin/mail-templates',
        'query_string':b'', 'headers':[], 'server':('test',80), 'session':{'csrf':'token'}})


def test_combined_editor_save_preview_and_synthetic_test(db, service, monkeypatch):
    from app.routers import mail_templates as module
    from app.security import CurrentUser
    monkeypatch.setattr(module, 'require_admin', lambda request: CurrentUser('admin','admin','local'))
    service.settings.zimbra_domains = ['example.org']
    service.settings.zimbra_primary_domain = 'example.org'
    profile = ensure_domain_mail_profiles(db, service.settings)[0]
    response = module.mail_templates(request(), db=db, settings=service.settings)
    page = response.body.decode()
    assert f'data-selected-template="{profile.id}:combined"' in page
    assert 'Все реквизиты' in page and 'combined' in page
    body = '<p>{{ full_name }} {{ ad_login }} {{ ad_password }} {{ mail_password }} {{ corporate_email }}</p>'
    result = module.update_mail_template(profile.id, request(), template_key='combined', sender_name='IT',
        sender_email='it@example.org', subject='All credentials', body_html=body, csrf='token', db=db, settings=service.settings)
    assert result.status_code == 303
    assert 'error=' not in result.headers['location'], result.headers['location']
    assert profile.combined_body_html == body
    assert profile.personal_body_html != body and profile.corporate_body_html != body
    preview = module.preview_mail_template_body(request(), body_html=body, csrf='token')
    assert '{{ ad_password }}' in preview.body.decode()
    send = Mock()
    monkeypatch.setattr(module.CredentialMailer, '_send', send)
    response = module.test_mail_template(profile.id, request(), template_key='combined', sender_name='IT',
        sender_email='it@example.org', subject='Draft', body_html=body, test_recipient='test@example.net',
        csrf='token', db=db, settings=service.settings)
    assert response['ok'] and response['sent']
    send.assert_called_once()
    rendered = send.call_args.args[2]
    assert 'TEST-AD-Password-123!' in rendered and 'TEST-Mail-Password-123!' in rendered
    assert profile.combined_subject == 'All credentials'
    assert profile.combined_body_html == body
    for row in db.scalars(select(AuditLog)):
        assert 'TEST-AD-Password' not in row.details and 'TEST-Mail-Password' not in row.details


def test_combined_save_requires_csrf(db, service, monkeypatch):
    from app.routers import mail_templates as module
    from app.security import CSRFMismatchError
    profile = ensure_domain_mail_profiles(db, service.settings)[0]
    with pytest.raises(CSRFMismatchError):
        module.update_mail_template(profile.id, request(), template_key='combined', sender_name='IT',
            sender_email='it@example.test', subject='Title', body_html='<p>Text</p>', csrf='bad', db=db, settings=service.settings)


def test_result_and_journal_show_recipient_status_without_passwords(db, service):
    from app.routers import employees
    result = create(db, service, personal='')
    operation = db.get(ProvisioningOperation,result.operation_id)
    item = employees._provisioning_journal_item(operation)
    assert 'Реквизиты на корпоративный адрес' in str(item['details'])
    assert 'Не указан' in str(item['details'])
    assert 'ACCEPTED-AD-SECRET' not in str(item)
    assert result.mail_password not in str(item)
    rendered = employees.templates.env.get_template('result.html').render(user=None, credentials=result)
    assert 'Реквизиты на личный адрес' in rendered
    assert 'Не указан' in rendered
    assert 'Письмо с реквизитами AD' not in rendered
    operation.credential_delivery_mode = 'legacy'
    legacy = employees._provisioning_journal_item(operation)
    assert 'Реквизиты почты отправлены' in str(legacy['details'])
