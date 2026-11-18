from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.config import Settings
from app.db import Base
from app.models import HRSourceRecord, ProvisioningOperation, ADProvisioningOperation, BlockingOperation, EmailLoginMapping
from app.models_blocking_directory import BlockingDirectoryAccount, BlockingDirectorySync
from app.routers import employees
from app.security import CurrentUser, CSRFMismatchError
from app.services import blocking, blocking_queue, blocking_directory
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService

GUID = '11111111-1111-1111-1111-111111111111'


@pytest.fixture
def data(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    # Match production's disabled autoflush to catch duplicate pending upserts.
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        settings = Settings(_env_file=None, ad_check_enabled=True, zimbra_check_enabled=True,
                            zimbra_domains=['example.ru'], itinvent_enabled=False)
        ad, mail = Mock(), Mock()
        user = ADDirectoryUser(username='ivanov', display_name='Иванов Иван Иванович',
            email='ivanov@example.ru', distinguished_name='CN=Иванов,DC=example,DC=ru',
            is_enabled=True, object_guid=GUID)
        identity = ZimbraAccountIdentity('mail-guid', 'ivanov@example.ru', 'ivanov',
                                         ('ivanov@example.ru',), 'active')
        ad.directory_users.return_value = [user]
        ad.get_user.return_value = user
        ad.get_user_by_object_guid.return_value = user
        mail.directory_accounts.return_value = [{'identity': identity, 'fio': user.display_name}]
        mail.account_by_address.return_value = identity
        mail.accounts_by_ids.return_value = {'mail-guid': identity}
        for module in (blocking, blocking_queue, blocking_directory):
            monkeypatch.setattr(module, 'ActiveDirectoryService', lambda settings: ad)
            monkeypatch.setattr(module, 'ZimbraService', lambda settings: mail)
        monkeypatch.setattr(employees, 'get_current_user', lambda request: CurrentUser('operator', 'operator', 'local'))
        yield SimpleNamespace(db=db, settings=settings, ad=ad, mail=mail, user=user, identity=identity,
            service=blocking.BlockingService(settings, db),
            directory=blocking_directory.BlockingDirectoryService(settings, db))
    engine.dispose()


def created(data, **extra):
    values = dict(operator_username='operator', last_name='Иванов', first_name='Иван',
                  middle_name='Иванович', personal_email='', login='ivanov',
                  corporate_email='ivanov@example.ru', mail_domain='example.ru',
                  ad_created=True, zimbra_created=True)
    values.update(extra)
    operation = ProvisioningOperation(**values)
    data.db.add(operation)
    data.db.commit()
    return operation


def request():
    return Request({'type': 'http', 'method': 'GET', 'path': '/blocking',
        'headers': [], 'session': {'csrf': 'token'}, 'query_string': b''})


def test_created_before_hr_is_searchable_and_blockable_immediately(data):
    created(data)
    rows = data.service.search('иванов')
    assert len(rows) == 1 and rows[0]['id'] < 0 and rows[0]['origin'] == 'Создана через систему'
    result = data.service.block(rows[0]['id'], 'operator')
    assert result.status == 'success'
    data.ad.disable_user.assert_called_once_with('ivanov')
    data.mail.close_account.assert_called_once_with('ivanov@example.ru')
    assert not data.db.scalars(select(HRSourceRecord)).all()
    operation = data.db.scalar(select(BlockingOperation))
    assert operation.source_record_id == rows[0]['id']
    assert data.service.latest_result_for_record(rows[0]['id']).operation_id == result.operation_id
    data.ad.directory_users.assert_not_called()
    data.mail.directory_accounts.assert_not_called()


def test_partial_and_repeated_creation_is_indexed_without_passwords_or_duplicates(data):
    created(data, zimbra_created=False)
    created(data, zimbra_created=False)
    rows = data.service.search('ivanov')
    assert len(rows) == 1
    assert rows[0]['id'] == data.service.search('ivanov')[0]['id']
    result = data.service.block(rows[0]['id'], 'operator')
    assert result.status == 'success' and result.zimbra.status_label == 'Отсутствует'
    data.mail.close_account.assert_not_called()
    assert all('password' not in column.name for column in BlockingDirectoryAccount.__table__.columns)


def test_ad_only_creation_history_is_included(data):
    data.db.add(ADProvisioningOperation(worker_key='outside', source_id='example.ru',
        operator_username='op', full_name='Иванов Иван', login='ivanov',
        corporate_email='ivanov@example.ru', ad_created=True))
    data.db.commit()
    rows = data.service.search('Иванов')
    assert len(rows) == 1 and rows[0]['id'] < 0


def test_separate_successful_ad_and_mail_creation_keep_both_targets(data):
    created(data, zimbra_created=False)
    created(data, ad_created=False)
    rows = data.service.search('ivanov')
    assert len(rows) == 1
    assert data.service.block(rows[0]['id'], 'operator').status == 'success'
    data.ad.disable_user.assert_called_once()
    data.mail.close_account.assert_called_once()


def test_draft_or_failed_creation_without_accounts_is_not_a_target(data):
    created(data, ad_created=False, zimbra_created=False)
    assert data.service.search('ivanov') == []


def test_hr_history_and_other_organizations_are_searchable_without_revival(data):
    record = HRSourceRecord(worker_key='former', source_id='other.ru', fio='Семёнов Иван',
        login='ivanov', corporate_email='ivanov@example.ru', is_present=False)
    data.db.add(record)
    data.db.commit()
    rows = data.service.search('  СЕМЕНОВ   Иван ')
    assert len(rows) == 1 and rows[0]['origin'] == 'История работников'
    assert data.service.card(record.id, allow_manual_history=True).record_id == record.id
    assert data.service.refresh_itinvent(record.id).state == 'not_configured'
    assert not record.is_present
    assert data.service.block(record.id, 'operator').status == 'success'
    assert not record.is_present


def test_six_hour_refresh_persists_and_survives_new_service_instance(data):
    data.directory.refresh_due()
    ids = {row.account_key: row.id for row in data.db.scalars(select(BlockingDirectoryAccount)).all()}
    new = blocking_directory.BlockingDirectoryService(data.settings, data.db)
    new.refresh_due()
    data.ad.directory_users.assert_called_once()
    data.mail.directory_accounts.assert_called_once()
    for state in data.directory.status():
        assert state.next_attempt_at - state.last_success_at >= timedelta(hours=5, minutes=59)
        state.next_attempt_at = blocking_directory.utcnow() - timedelta(seconds=1)
    data.db.commit()
    new.refresh_due()
    assert data.ad.directory_users.call_count == 2
    assert ids == {row.account_key: row.id for row in data.db.scalars(select(BlockingDirectoryAccount)).all()}


def test_failed_or_partial_refresh_preserves_last_successful_snapshot(data):
    data.directory.refresh_due()
    state = data.db.get(BlockingDirectorySync, 'ad')
    old_success = state.last_success_at
    state.next_attempt_at = blocking_directory.utcnow() - timedelta(seconds=1)
    data.db.commit()
    data.ad.directory_users.side_effect = RuntimeError('LDAP unavailable')
    data.directory.refresh_due()
    row = data.db.scalar(select(BlockingDirectoryAccount).where(BlockingDirectoryAccount.system == 'ad'))
    assert row.present and state.error and state.last_success_at == old_success
    assert state.next_attempt_at is not None
    assert data.service.search('Иванов')


def test_removed_accounts_are_hidden_but_identifiers_are_retained(data):
    data.directory.refresh_due()
    state = data.db.get(BlockingDirectorySync, 'ad')
    state.next_attempt_at = blocking_directory.utcnow() - timedelta(seconds=1)
    data.db.commit()
    data.ad.directory_users.return_value = []
    data.directory.refresh_due()
    row = data.db.scalar(select(BlockingDirectoryAccount).where(BlockingDirectoryAccount.system == 'ad'))
    assert row.ad_guid == GUID and not row.present
    assert all(item['origin'] != 'AD' for item in data.service.search('Иванов'))


def test_directory_sources_do_not_guess_counterparts_from_same_name_or_login(data):
    data.directory.refresh_due()
    rows = data.service.search('ivanov')
    ad_row = next(row for row in rows if row['origin'] == 'AD')
    mail_row = next(row for row in rows if row['origin'] == 'Zimbra')
    mail_card = data.service.card(mail_row['id'])
    assert not mail_card.effective_login and not mail_card.ad_target_guid
    data.ad.get_user.assert_not_called()
    result = data.service.block(mail_row['id'], 'operator')
    assert result.status == 'success' and result.ad.status_label == 'Отсутствует'
    data.ad.disable_user.assert_not_called()
    data.mail.close_account.assert_called_once()
    data.mail.close_account.reset_mock()
    assert data.service.block(ad_row['id'], 'operator').status == 'success'
    data.ad.disable_user.assert_called_once()
    data.mail.close_account.assert_not_called()


@pytest.mark.parametrize('system', ['ad', 'zimbra'])
def test_stale_stable_id_never_blocks_a_new_account_with_reused_login(data, system):
    data.directory.refresh_due()
    row = data.db.scalar(select(BlockingDirectoryAccount).where(BlockingDirectoryAccount.system == system))
    data.ad.get_user_by_object_guid.return_value = None
    data.mail.accounts_by_ids.return_value = {}
    result = data.service.block(-row.id, 'operator')
    assert result.status != 'success'
    assert (result.ad if system == 'ad' else result.zimbra).status == 'intervention'
    data.ad.get_user.assert_not_called()
    data.mail.account_by_address.assert_not_called()
    data.ad.disable_user.assert_not_called()
    data.mail.close_account.assert_not_called()


def test_routes_render_new_targets_and_keep_csrf(data):
    created(data)
    response = employees.blocking_search(request(), q='Иванов', db=data.db, settings=data.settings)
    assert response.status_code == 200
    assert 'Создана через систему' in response.body.decode()
    row = data.db.scalar(select(BlockingDirectoryAccount))
    card = employees.blocking_card(-row.id, request(), db=data.db, settings=data.settings)
    assert card.status_code == 200 and f'/blocking/{-row.id}' in card.body.decode()
    with pytest.raises(CSRFMismatchError):
        employees.block_employee_accounts(-row.id, request(), csrf='bad', db=data.db, settings=data.settings)
    response = employees.block_employee_accounts(-row.id, request(), csrf='token', db=data.db, settings=data.settings)
    assert response.status_code == 200
    assert 'Заблокирована системой' in response.body.decode()


def test_historical_mapping_never_switches_to_reused_login_or_email(data):
    record = HRSourceRecord(worker_key='former', source_id='example.ru', fio='Иванов Иван',
        login='ivanov', corporate_email='ivanov@example.ru', is_present=False)
    data.db.add_all([record, EmailLoginMapping(worker_key='former', source_domain='example.ru',
        source_email='ivanov@example.ru', ad_login='ivanov', ad_object_guid=GUID,
        zimbra_email='ivanov@example.ru', zimbra_id='mail-guid')])
    data.db.commit()
    data.ad.get_user_by_object_guid.return_value = None
    data.mail.accounts_by_ids.return_value = {}
    result = data.service.block(record.id, 'operator')
    assert result.status == 'failed'
    data.ad.get_user.assert_not_called()
    data.mail.account_by_address.assert_not_called()
    data.ad.disable_user.assert_not_called()
    data.mail.close_account.assert_not_called()


def test_worker_updates_index_without_executing_blocking(data):
    @contextmanager
    def session():
        yield data.db
    worker = blocking_directory.BlockingDirectoryWorker(data.settings, session)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    worker._stop = stop
    worker._run()
    assert len(data.directory.status()) == 2
    data.ad.directory_users.assert_called_once()
    data.mail.directory_accounts.assert_called_once()
    data.ad.disable_user.assert_not_called()
    data.mail.close_account.assert_not_called()


def test_paged_ad_read_collects_all_pages_and_rejects_partial_results(monkeypatch):
    service = ActiveDirectoryService(Settings(_env_file=None, ad_base_dn='DC=example,DC=ru'))
    conn = SimpleNamespace(entries=[], result={}, response=[], calls=[])
    def search(*args, **kwargs):
        conn.calls.append(kwargs)
        conn.entries = [SimpleNamespace()]
        cookie = b'next' if len(conn.calls) == 1 else b''
        conn.result = {'result': 0, 'controls': {'1.2.840.113556.1.4.319': {'value': {'cookie': cookie}}}}
    conn.search = search
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service, '_service_connection', connection)
    monkeypatch.setattr(service, '_entry_to_directory_user', lambda entry: SimpleNamespace(object_guid='guid'))
    assert len(service.directory_users()) == 2
    assert conn.calls[1]['paged_cookie'] == b'next'
    def partial(*args, **kwargs):
        conn.result = {'result': 4}
    conn.search = partial
    with pytest.raises(RuntimeError):
        service.directory_users()


def test_zimbra_directory_is_scoped_and_only_persists_names_and_identifiers(monkeypatch):
    service = ZimbraService(Settings(_env_file=None, zimbra_domains=['example.ru'],
                                   zimbra_backend='ssh_zmprov', zimbra_check_enabled=True))
    client = Mock()
    client.exec_command.return_value = (Mock(), Mock(), Mock())
    monkeypatch.setattr(service, '_client', lambda: client)
    monkeypatch.setattr(service, '_read_remote_command', lambda *a, **k: (0, '''# name ivanov@example.ru
zimbraId: id-1
mail: ivanov@example.ru
displayName: Иванов Иван
zimbraAccountStatus: active
userPassword: NEVER_STORE_THIS
zimbraMailAlias: old@example.ru
# name outside@elsewhere.ru
zimbraId: id-2
mail: outside@elsewhere.ru
''', ''))
    result = service.directory_accounts()
    assert len(result) == 1 and result[0]['fio'] == 'Иванов Иван'
    assert 'old@example.ru' in result[0]['identity'].addresses
    assert 'NEVER_STORE_THIS' not in str(result)
    client.close.assert_called_once()
