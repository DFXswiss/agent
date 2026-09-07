import json

import pytest

from agent_cli.ai_accounts import AccountError, load_ai_accounts
from agent_cli.store import Store
from agent_cli.usage import AuthStale, load_grok_bearer, scan_usage
from test_usage import SETTINGS, _auth_file, _credits, _owned_grok_session


def test_automatic_usage_does_not_read_ambient_account(tmp_path, monkeypatch):
    monkeypatch.setenv('GROK_HOME', str(tmp_path))
    _auth_file(tmp_path)
    store = Store(tmp_path)
    try:
        _owned_grok_session(store)
        def forbidden(token):
            pytest.fail('Unconfigured usage must not contact a provider')
        with pytest.raises(AuthStale, match='not configured'):
            scan_usage(store, fetch=forbidden)
        with pytest.raises(AuthStale, match='explicitly configured'):
            load_grok_bearer()
    finally:
        store.close()


def test_usage_uses_explicit_session_account_instead_of_newest_session(tmp_path, monkeypatch):
    chosen = tmp_path / 'chosen'
    chosen.mkdir()
    _auth_file(chosen, email='chosen@example.com')
    ambient = tmp_path / 'ambient'
    ambient.mkdir()
    _auth_file(ambient, email='ambient@example.com')
    monkeypatch.setenv('GROK_HOME', str(ambient))
    (tmp_path / 'ai-accounts.json').write_text(json.dumps({
        'accounts': {'chosen': {'provider': 'grok', 'config_dir': str(chosen)}},
        'roles': {'configured': {'account': 'chosen', 'model': 'explicit-model', 'access': 'read-only'}},
        'sessions': {'chosen-session': {'interactive': 'configured'}},
        'usage_session': 'chosen-session',
    }))
    store = Store(tmp_path)
    try:
        _owned_grok_session(store, 'chosen-session')
        _owned_grok_session(store, 'newer-unselected-session')
        aid = scan_usage(store, fetch=lambda token: (_credits(), SETTINGS))
        row = store.row('activity', aid)
        assert row['session_id'] == 'chosen-session'
        assert row['payload']['account_email'] == 'chosen@example.com'
    finally:
        store.close()


@pytest.mark.no_pg
@pytest.mark.parametrize('value', ['', 'missing', False, 42])
def test_invalid_usage_selection_is_not_replaced_with_another_account(tmp_path, value):
    (tmp_path / 'ai-accounts.json').write_text(json.dumps({'usage_session': value}))
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)
