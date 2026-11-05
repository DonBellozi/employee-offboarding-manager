from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import json
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord, OneCImportRun, ProvisioningOperation
from app.models_account_requirement import AccountRequirementPreRegistration
from app.models_onec_sources import HREmploymentState
from app.services.login_owner import LoginOwnerService


class ASGIClient:
    def __init__(self, app):
        self.app = app

    def get(self, url):
        async def run():
            messages = []
            delivered = False
            parts = urlsplit(url)
            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await asyncio.Event().wait()
            async def send(message):
                messages.append(message)
            await self.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': 'GET', 'scheme': 'http', 'path': parts.path, 'raw_path': parts.path.encode(),
                'query_string': parts.query.encode(), 'root_path': '', 'client': ('test', 1),
                'server': ('test', 80), 'session': {'csrf': 'test'}, 'headers': [(b'host', b'test')]}, receive, send)
            start = next(item for item in messages if item['type'] == 'http.response.start')
            content = b''.join(item.get('body', b'') for item in messages).decode()
            return SimpleNamespace(status_code=start['status'], text=content,
                headers={key.decode(): value.decode() for key, value in start['headers']},
                json=lambda: json.loads(content))
        return asyncio.run(run())


@pytest.fixture
def db():
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def employee(db, key='one', domain='a.test', status='active', present=True):
    db.add(HRSourceRecord(worker_key=key, source_id=domain, fio=f'ФИО {key}',
                          corporate_email=f'{key}@{domain}', is_present=present))
    db.add(HREmploymentState(worker_key=key, source_id=domain, fio=f'ФИО {key}',
                             status=status, is_present=present))
    db.commit()


def mapping(db, key='one', guid='guid', zid='zid'):
    db.add(EmailLoginMapping(worker_key=key, source_domain='a.test', source_email=f'{key}@a.test',
                             ad_object_guid=guid, ad_login='login', zimbra_id=zid, zimbra_email=f'{key}@a.test'))
    db.commit()


def classify(db, **kwargs):
    return LoginOwnerService(db, SimpleNamespace()).classify(source='ad', stable_id='guid', **kwargs)


def test_active_by_stable_id(db):
    employee(db)
    mapping(db)
    assert classify(db)['state'] == 'active'


def test_active_in_other_organization_protected(db):
    employee(db, status='dismissed', present=False)
    employee(db, domain='b.test')
    mapping(db)
    assert classify(db)['state'] == 'active'


def test_inactive_is_not_permission_to_delete(db):
    employee(db, status='dismissed', present=False)
    mapping(db)
    row = classify(db)
    assert row['state'] == 'inactive'
    assert 'не разрешение' in row['message']
    assert 'can_delete' not in row


def test_unknown_no_fio_or_generated_login_guess(db):
    employee(db)
    assert classify(db)['state'] == 'unknown'


def test_exact_corporate_address_read_only_evidence(db):
    employee(db)
    assert classify(db, addresses=['ONE@A.TEST'])['state'] == 'active'


def test_id_email_conflict_is_ambiguous(db):
    employee(db)
    employee(db, key='two')
    mapping(db)
    assert classify(db, addresses=['two@a.test'])['state'] == 'ambiguous'


def test_multiple_id_owners_is_ambiguous(db):
    employee(db)
    employee(db, key='two')
    mapping(db)
    mapping(db, key='two')
    assert classify(db)['state'] == 'ambiguous'


def test_import_in_progress_not_trusted(db):
    employee(db)
    mapping(db)
    db.add(OneCImportRun(status='running'))
    db.commit()
    row = classify(db)
    assert row['state'] == 'unknown'
    assert 'обновляется' in row['message']


def test_no_stable_id_does_not_trust_email(db):
    employee(db)
    assert LoginOwnerService(db, SimpleNamespace()).classify(
        source='ad', stable_id='', addresses=['one@a.test'])['state'] == 'unknown'


def test_pre_hr_creation_not_dismissed(db):
    operation = ProvisioningOperation(operator_username='op', last_name='Новый', first_name='Человек',
        personal_email='', login='login', corporate_email='login@a.test', mail_domain='a.test')
    db.add(operation)
    db.flush()
    db.add(AccountRequirementPreRegistration(provisioning_operation_id=operation.id, fio='Новый человек',
        decided_by='op', initial_snapshot_json='{}', ad_object_guid='guid', status='waiting'))
    db.commit()
    assert classify(db)['state'] == 'waiting_hr'


def test_zimbra_stable_id(db):
    employee(db)
    mapping(db)
    assert LoginOwnerService(db, SimpleNamespace()).classify(source='zimbra', stable_id='zid')['state'] == 'active'


def test_sources_independent_and_errors_redacted(db, monkeypatch):
    from app.services import login_owner as module
    employee(db)
    settings = SimpleNamespace(ad_check_enabled=True, zimbra_check_enabled=True,
                               zimbra_domains=['a.test'], zimbra_primary_domain='a.test')
    ad = Mock()
    ad.get_user.side_effect = RuntimeError('SECRET')
    zimbra = Mock()
    zimbra.accounts_by_addresses.return_value = {'one@a.test': SimpleNamespace(zimbra_id='zid', addresses=['one@a.test'])}
    monkeypatch.setattr(module, 'ActiveDirectoryService', lambda settings: ad)
    monkeypatch.setattr(module, 'ZimbraService', lambda settings: zimbra)
    rows = LoginOwnerService(db, settings).inspect('one')
    assert [row['state'] for row in rows] == ['error', 'active']
    assert 'SECRET' not in str(rows)
    ad.delete_user.assert_not_called()
    zimbra.delete_account.assert_not_called()


def test_disabled_sources_never_contacted(db, monkeypatch):
    from app.services import login_owner as module
    ad = Mock(side_effect=AssertionError('must not connect'))
    monkeypatch.setattr(module, 'ActiveDirectoryService', ad)
    assert LoginOwnerService(db, SimpleNamespace(ad_check_enabled=False, zimbra_check_enabled=False)).inspect('one')[0]['state'] == 'unknown'
    ad.assert_not_called()


def test_http_validation_and_no_store(db, monkeypatch):
    from fastapi import FastAPI
    from app.db import get_db
    from app.routers import employees
    from app.services import login_owner as module
    api = FastAPI()
    api.include_router(employees.router)
    api.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(employees, 'get_current_user', lambda request: SimpleNamespace())
    inspect = Mock(return_value=[])
    monkeypatch.setattr(module.LoginOwnerService, 'inspect', inspect)
    client = ASGIClient(api)
    assert client.get('/employees/login-owner?login=bad%40login').status_code == 400
    inspect.assert_not_called()
    result = client.get('/employees/login-owner?login=One')
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    inspect.assert_called_once_with('one')


def test_http_requires_authentication(db):
    from fastapi import FastAPI
    from app.db import get_db
    from app.routers import employees
    api = FastAPI()
    api.include_router(employees.router)
    api.dependency_overrides[get_db] = lambda: db
    assert ASGIClient(api).get('/employees/login-owner?login=one').status_code != 200
