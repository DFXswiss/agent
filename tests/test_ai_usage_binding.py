import json

import pytest

from agent_cli.ai_accounts import AccountError, load_ai_accounts
from agent_cli.store import Store
from agent_cli.usage import scan_session_usage
from driver_fixtures import write_drivers
from test_usage import USAGE_SESSION, _configure, _credential, _owned_session, _period_fetch


def test_automatic_usage_does_not_read_ambient_account(tmp_path, monkeypatch):
    monkeypatch.setenv('TOOL_A_HOME', str(tmp_path))
    _credential(tmp_path)
    store = Store(tmp_path)
    try:
        _owned_session(store)

        def forbidden(*_args):
            pytest.fail('Unconfigured usage must not contact a driver')

        with pytest.raises(AccountError, match='models is not configured'):
            scan_session_usage(store, fetch=forbidden)
    finally:
        store.close()


def test_usage_uses_explicit_session_account_instead_of_newest_session(tmp_path, monkeypatch):
    directories = _configure(tmp_path)
    _credential(directories['driver-a'], email='chosen@example.com')
    ambient = tmp_path / 'ambient'
    ambient.mkdir()
    _credential(ambient, email='ambient@example.com')
    monkeypatch.setenv('TOOL_A_HOME', str(ambient))
    store = Store(tmp_path)
    try:
        _owned_session(store, USAGE_SESSION)
        _owned_session(store, 'newer-unselected-session')
        aid = scan_session_usage(store, fetch=_period_fetch())
        row = store.row('activity', aid)
        assert row['session_id'] == USAGE_SESSION
        assert row['payload']['account_email'] == 'chosen@example.com'
    finally:
        store.close()


@pytest.mark.no_pg
@pytest.mark.parametrize('value', ['', 'missing', False, 42])
def test_invalid_usage_selection_is_not_replaced_with_another_account(tmp_path, value):
    write_drivers(tmp_path)
    (tmp_path / 'ai-accounts.json').write_text(json.dumps({
        'models': {'first': 'driver-a', 'second': 'driver-b'},
        'usage_session': value,
    }))
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)
