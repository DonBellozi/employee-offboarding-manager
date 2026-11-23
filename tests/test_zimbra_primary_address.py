from unittest.mock import Mock

import pytest

from app.config import Settings
from app.services.zimbra import ZimbraService

PRIMARY='new.login@new.test'
ALIAS='old.login@old.test'


@pytest.mark.parametrize('mails',[[ALIAS,PRIMARY],[PRIMARY,ALIAS]])
def test_primary_is_account_name_independent_of_multivalued_mail_order(mails):
    output='\n'.join(['# name '+PRIMARY,'zimbraId: id-1','zimbraAccountStatus: active',
        *['mail: '+address for address in mails],'zimbraMailAlias: '+ALIAS,
        'zimbraMailDeliveryAddress: '+PRIMARY])
    accounts=ZimbraService._parse_search_accounts(output)
    assert len(accounts)==1
    assert accounts[0].primary_email==PRIMARY and accounts[0].login=='new.login'
    assert set(accounts[0].addresses)=={PRIMARY,ALIAS}


def test_header_primary_is_preserved_even_when_missing_from_mail():
    account=ZimbraService._parse_search_accounts(f'# name {PRIMARY}\nzimbraId: id-1\nmail: {ALIAS}')[0]
    assert account.primary_email==PRIMARY and PRIMARY in account.addresses and ALIAS in account.addresses


def test_missing_header_uses_unique_delivery_address_before_mail():
    account=ZimbraService._parse_search_accounts(
        f'zimbraId: id-1\nmail: {ALIAS}\nmail: {PRIMARY}\nzimbraMailDeliveryAddress: {PRIMARY}')[0]
    assert account.primary_email==PRIMARY


def test_old_single_address_output_is_still_supported():
    account=ZimbraService._parse_search_accounts(f'zimbraId: id-1\nmail: {PRIMARY}\nmail: {PRIMARY}')[0]
    assert account.primary_email==PRIMARY


@pytest.mark.parametrize('attributes',[
    f'mail: {PRIMARY}\nmail: {ALIAS}',
    f'zimbraMailDeliveryAddress: {PRIMARY}\nzimbraMailDeliveryAddress: {ALIAS}',
])
def test_ambiguous_primary_without_header_is_error_not_missing_account(attributes):
    with pytest.raises(RuntimeError):
        ZimbraService._parse_search_accounts('zimbraId: id-1\n'+attributes)


def test_several_mailboxes_do_not_share_header_or_alias_attributes():
    output=f'# name {PRIMARY}\nzimbraId: id-1\nmail: {ALIAS}\n\n# name other@new.test\nzimbraId: id-2\nmail: other@new.test'
    accounts=ZimbraService._parse_search_accounts(output)
    assert [a.primary_email for a in accounts]==[PRIMARY,'other@new.test']
    assert accounts[1].addresses==('other@new.test',)


@pytest.mark.parametrize('output',['zimbraId: id-1', '# name bad address\nzimbraId: id-1',
                                 'zimbraId: id-1\nmail: @new.test'])
def test_account_without_usable_primary_is_error_not_proof_of_absence(output):
    with pytest.raises(RuntimeError):ZimbraService._parse_search_accounts(output)


def test_real_address_and_id_lookup_use_same_correct_primary(monkeypatch):
    settings=Settings(_env_file=None,zimbra_check_enabled=True,zimbra_backend='ssh_zmprov',zimbra_domains=['old.test','new.test'])
    service=ZimbraService(settings)
    client=Mock()
    monkeypatch.setattr(service,'_client',lambda:client)
    monkeypatch.setattr(service,'_execute_zmprov_lookup',Mock(return_value=
        f'# name {PRIMARY}\nzimbraId: id-1\nmail: {ALIAS}\nmail: {PRIMARY}\nzimbraMailAlias: {ALIAS}\nzimbraAccountStatus: active'))
    assert service.account_by_address(ALIAS).primary_email==PRIMARY
    assert service.accounts_by_ids(['id-1'])['id-1'].primary_email==PRIMARY
