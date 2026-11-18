from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ldap3.core.exceptions import LDAPAttributeError

from app.config import Settings
from app.services.ad import ActiveDirectoryService
from app.services import final_dismissal_lifecycle as lifecycle
from test_ad_conflict_recovery import scenario
from test_missing_ad_dismissal import stuck


@pytest.mark.parametrize('original_check_names', [True, False])
def test_rootdse_operational_attribute_bypasses_only_local_schema_check(monkeypatch, original_check_names):
    service = ActiveDirectoryService(Settings(_env_file=None, ad_base_dn='OU=People,DC=example,DC=ru'))
    conn = SimpleNamespace(check_names=original_check_names, entries=[], result={}, response=[], calls=[])
    def search(base, query, **kwargs):
        conn.calls.append((base, conn.check_names))
        if not base:
            if conn.check_names:
                raise LDAPAttributeError('invalid attribute type defaultNamingContext')
            conn.entries = [{'defaultNamingContext': SimpleNamespace(value='DC=example,DC=ru')}]
            conn.response = [{'type': 'searchResEntry', 'attributes': {'defaultNamingContext': ['DC=example,DC=ru']}}]
        else:
            conn.entries = []
            conn.response = []
        conn.result = {'result': 0}
    conn.search = search
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service, '_service_connection', connection)
    assert service.confirm_login_absent(username='never.created') is True
    assert conn.calls == [('', False), ('DC=example,DC=ru', original_check_names)]
    assert conn.check_names == original_check_names


def test_schema_setting_is_restored_on_rootdse_failure(monkeypatch):
    service = ActiveDirectoryService(Settings(_env_file=None))
    conn = SimpleNamespace(check_names=True)
    def search(*args, **kwargs):
        raise RuntimeError('connection timed out')
    conn.search = search
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service, '_service_connection', connection)
    with pytest.raises(RuntimeError):
        service.confirm_login_absent(username='never.created')
    assert conn.check_names is True


def test_rootdse_never_constructs_schema_checked_ldap_entry(monkeypatch):
    service = ActiveDirectoryService(Settings(_env_file=None, ad_base_dn='DC=example,DC=ru'))
    class Connection:
        check_names = True
        response = []
        result = {}
        root = False

        def search(self, base, query, **kwargs):
            self.root = not base
            self.result = {'result': 0}
            self.response = ([{'type': 'searchResEntry', 'attributes':
                {'defaultNamingContext': [b'DC=example,DC=ru']}}] if self.root else [])

        @property
        def entries(self):
            if self.root:
                raise LDAPAttributeError('invalid attribute type defaultNamingContext')
            return []
    conn = Connection()
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(service, '_service_connection', connection)
    assert service.confirm_login_absent(username='missing') is True
    assert conn.check_names is True


def test_already_stuck_rootdse_error_is_retried_without_operator(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    target.last_result = lifecycle.VERIFY_MISSING_AD
    target.last_error = 'invalid attribute type defaultNamingContext'
    db.commit()
    worker.process()
    assert run.status == 'success' and target.last_result == 'confirmed_absent'
    ad.confirm_login_absent.assert_called_once_with(username='first')
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


def test_old_rootdse_failure_with_guid_keeps_guid_proof(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    target.stable_id = '11111111-1111-1111-1111-111111111111'
    target.last_error = 'invalid attribute type defaultNamingContext'
    ad.confirm_user_absent.return_value = True
    db.commit()
    worker.process()
    assert run.status == 'success'
    ad.confirm_user_absent.assert_called_once_with(object_guid=target.stable_id, username='first')
    ad.confirm_login_absent.assert_not_called()
    ad.disable_user.assert_not_called()


def test_rootdse_repair_uses_saved_target_after_hr_logins_disappear(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    target.last_error = 'invalid attribute type defaultNamingContext'
    for record in records:
        record.login = ''
    db.commit()
    worker.process()
    assert run.status == 'success' and target.last_result == 'confirmed_absent'
    ad.confirm_login_absent.assert_called_once_with(username='first')
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()


def test_rootdse_repair_does_not_block_a_newly_found_account(scenario):
    db, settings, records, worker, ad, run, target = stuck(scenario)
    target.last_error = 'invalid attribute type defaultNamingContext'
    ad.confirm_login_absent.return_value = False
    db.commit()
    worker.process()
    assert run.status == 'intervention' and run.completed_at is None
    ad.get_user.assert_not_called()
    ad.disable_user.assert_not_called()
