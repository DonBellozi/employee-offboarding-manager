from types import SimpleNamespace

import pytest
from ldap3.operation.search import build_attribute_selection
from ldap3.core.exceptions import LDAPAttributeError

from test_missing_ad_dismissal import directory, stuck
from test_ad_conflict_recovery import scenario


def test_real_ldap3_attribute_list_validation_is_bypassed_only_for_rootdse(monkeypatch):
    schema=SimpleNamespace(attribute_types={'objectClass':object(),'objectGUID':object(),'sAMAccountName':object()})
    with pytest.raises(LDAPAttributeError,match='in attribute list'):
        build_attribute_selection(['defaultNamingContext'],schema)
    service,conn=directory(monkeypatch,root='DC=example,DC=test')
    original=conn.search
    def search(base,query,**kwargs):
        build_attribute_selection(kwargs['attributes'],schema)
        return original(base,query,**kwargs)
    conn.search=search
    assert service.confirm_login_absent(username='missing') is True
    assert conn.calls[0][2]['attributes']==['*','+']
    assert conn.calls[1][2]['attributes']==['objectGUID','sAMAccountName']


@pytest.mark.parametrize('error', ['invalid attribute type defaultNamingContext',
    'invalid attribute type in attribute list: defaultNamingContext'])
def test_both_existing_rootdse_errors_are_retried(scenario,error):
    db,settings,records,worker,ad,run,target=stuck(scenario)
    target.last_error=error
    db.commit()
    worker.process()
    assert run.status=='success' and target.last_result=='confirmed_absent'
    ad.disable_user.assert_not_called()


def test_schema_errors_for_other_attributes_are_not_silently_retried(scenario):
    db,settings,records,worker,ad,run,target=stuck(scenario)
    target.last_error='invalid attribute type in attribute list: anotherAttribute'
    db.commit()
    worker.process()
    assert run.status=='intervention'
    ad.confirm_login_absent.assert_not_called()
