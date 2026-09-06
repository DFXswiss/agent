from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_cli.github_accounts import Account, AccountError, load_accounts
from agent_cli.github_act import scan_github
from agent_cli.runtime import Completed, run_argv
from agent_cli.store import Store
from agent_cli.watch import scan_assigned, scan_merged


def configure(home: Path, *, sessions=None) -> None:
    (home / 'github-accounts.json').write_text(json.dumps({
        'accounts': {
            'one': {'login': 'WorkerOne', 'gh_config_dir': '/accounts/one'},
            'two': {'login': 'WorkerTwo', 'gh_config_dir': '/accounts/two'},
        },
        'sessions': sessions if sessions is not None else {'s1': 'one', 's2': 'two'},
    }))


def pending(store: Store, sid: str, aid: str) -> None:
    store.write('session', 'insert', sid, {'id': sid, 'kind': 'runner', 'status': 'active'})
    store.write('activity', 'insert', aid, {
        'id': aid, 'session_id': sid, 'type': 'pr.open', 'execution_status': 'pending',
        'payload': {'repo': 'owner/repo', 'head': f'feature-{sid}', 'title': 'Fix', 'base': 'develop'},
    })


def unpack(argv: list[str]) -> tuple[str, list[str]]:
    profile = next(s.split('=', 1)[1] for s in argv if s.startswith('GH_CONFIG_DIR='))
    return profile, argv[argv.index('gh'):]


@pytest.mark.no_pg
@pytest.mark.parametrize('data', [None, {}, {'accounts': None, 'sessions': None}, {'accounts': {}, 'sessions': {}}])
def test_installation_has_no_account(tmp_path, data):
    if data is not None:
        (tmp_path / 'github-accounts.json').write_text(json.dumps(data))
    accounts = load_accounts(tmp_path)
    assert accounts.accounts == {}
    with pytest.raises(AccountError, match='No GitHub account configured'):
        accounts.for_session('s1')


def test_unconfigured_executor_does_not_call_github(tmp_path):
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    def forbidden(argv):
        pytest.fail('Unconfigured execution must not access GitHub')
    assert scan_github(store, forbidden) == ['pr.open a1 error']
    assert 'No GitHub account configured' in store.row('activity', 'a1')['execution_error']


def test_two_sessions_use_separate_accounts(tmp_path):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    pending(store, 's2', 'a2')
    calls = []
    def runner(argv):
        profile, command = unpack(argv)
        calls.append((profile, command))
        number = 1 if profile == '/accounts/one' else 2
        if command[:3] == ['gh', 'api', 'user']:
            return Completed(0, 'WorkerOne' if number == 1 else 'WorkerTwo', '')
        if command[:3] == ['gh', 'pr', 'view']:
            return Completed(1, '', 'no pull requests found')
        assert command[:3] == ['gh', 'pr', 'create']
        assert command[command.index('--head') + 1] == f'feature-s{number}'
        assert '--draft' in command
        return Completed(0, f'https://github.com/owner/repo/pull/{number}', '')
    assert len(scan_github(store, runner)) == 2
    assert store.row('activity', 'a1')['execution_account'] == {'account': 'one', 'login': 'workerone'}
    assert store.row('activity', 'a2')['execution_account'] == {'account': 'two', 'login': 'workertwo'}
    assert store.row('activity', 'a1')['result']['number'] == 1
    assert store.row('activity', 'a2')['result']['number'] == 2
    assert len(calls) == 6


@pytest.mark.parametrize('auth', [Completed(0, 'WrongAccount', ''), Completed(1, '', 'PRIVATE_AUTH_DETAIL')])
def test_wrong_or_failed_login_never_falls_back(tmp_path, auth):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    calls = []
    def runner(argv):
        profile, command = unpack(argv)
        calls.append(profile)
        assert command[:3] == ['gh', 'api', 'user']
        return auth
    assert scan_github(store, runner) == ['pr.open a1 error']
    assert calls == ['/accounts/one']
    error = store.row('activity', 'a1')['execution_error']
    assert 'no fallback' in error
    assert 'PRIVATE_AUTH_DETAIL' not in error


def test_retry_cannot_change_account(tmp_path):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    scan_github(store, lambda argv: Completed(1, '', 'offline'))
    row = store.row('activity', 'a1')
    row['execution_status'] = 'pending'
    store.write('activity', 'update', 'a1', row)
    configure(tmp_path, sessions={'s1': 'two'})
    def forbidden(argv):
        pytest.fail('Changed binding must fail before authentication')
    scan_github(store, forbidden)
    assert 'binding changed' in store.row('activity', 'a1')['execution_error']


def test_existing_pr_from_another_account_is_not_adopted(tmp_path):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    def runner(argv):
        _, command = unpack(argv)
        if command[:3] == ['gh', 'api', 'user']:
            return Completed(0, 'WorkerOne', '')
        assert command[:3] == ['gh', 'pr', 'view']
        return Completed(0, json.dumps({'number': 4, 'url': 'https://github.com/owner/repo/pull/4',
            'state': 'OPEN', 'isDraft': True, 'author': {'login': 'WorkerTwo'}}), '')
    scan_github(store, runner)
    assert 'author differs' in store.row('activity', 'a1')['execution_error']


@pytest.mark.no_pg
def test_child_environment_is_isolated_without_mutating_parent(tmp_path, monkeypatch):
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\n' + '''import os, sys
assert all(k not in os.environ for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))
assert os.environ['GH_HOST'] == 'github.com'
print(os.path.basename(os.environ['GH_CONFIG_DIR']))
''')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('GH_TOKEN', 'parent-only')
    monkeypatch.setenv('GITHUB_TOKEN', 'parent-only')
    monkeypatch.setenv('GH_CONFIG_DIR', '/parent/config')
    monkeypatch.setenv('GH_HOST', 'wrong.example')
    one = Account('one', 'WorkerOne', '/accounts/WorkerOne').runner(run_argv)
    two = Account('two', 'WorkerTwo', '/accounts/WorkerTwo').runner(run_argv)
    assert one(['gh', 'api', 'user']).stdout.strip() == 'WorkerOne'
    assert two(['gh', 'api', 'user']).stdout.strip() == 'WorkerTwo'
    assert one(['gh', 'api', 'user']).stdout.strip() == 'WorkerOne'
    assert os.environ['GH_TOKEN'] == 'parent-only'
    assert os.environ['GH_CONFIG_DIR'] == '/parent/config'


@pytest.mark.no_pg
def test_git_requires_explicit_identity_and_uses_scoped_helper():
    account = Account('one', 'WorkerOne', '/accounts/one')
    with pytest.raises(AccountError, match='Git identity is not configured'):
        account.runner(lambda argv: pytest.fail('No auth before missing identity is reported'), require_git=True)
    identity = {'name': 'Worker One', 'email': 'one@example.com', 'signing_key': '/keys/one', 'signing_format': 'ssh'}
    calls = []
    def runner(argv):
        calls.append(argv)
        return Completed(0, 'WorkerOne', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', identity).runner(runner, require_git=True)
    scoped(['git', '-C', '/work', 'push', 'origin', 'feature'])
    command = calls[-1]
    assert 'GIT_AUTHOR_EMAIL=one@example.com' in command
    assert 'GIT_COMMITTER_EMAIL=one@example.com' in command
    assert 'user.signingkey=/keys/one' in command
    assert 'credential.helper=' in command
    assert 'credential.helper=!gh auth git-credential' in command
    assert 'GIT_TERMINAL_PROMPT=0' in command
    assert 'GIT_SSH_COMMAND=false' in command
    assert 'GIT_CONFIG_COUNT=0' in command
    assert 'http.https://github.com/.extraHeader=' in command


@pytest.mark.no_pg
@pytest.mark.parametrize('change', ['unknown-session-account', 'relative-directory', 'token-field', 'partial-git', 'invalid-json'])
def test_invalid_configuration_does_not_choose_defaults(tmp_path, change):
    configure(tmp_path)
    path = tmp_path / 'github-accounts.json'
    data = json.loads(path.read_text())
    if change == 'unknown-session-account': data['sessions']['s1'] = 'missing'
    elif change == 'relative-directory': data['accounts']['one']['gh_config_dir'] = './relative'
    elif change == 'token-field': data['accounts']['one']['token'] = 'not-allowed'
    elif change == 'partial-git': data['accounts']['one']['git'] = {'name': 'Incomplete'}
    path.write_text('{' if change == 'invalid-json' else json.dumps(data))
    with pytest.raises(AccountError): load_accounts(tmp_path)


def test_assignment_uses_execution_account_not_hub_pairing(tmp_path):
    configure(tmp_path, sessions={'assigned': 'two'})
    (tmp_path / 'watch.json').write_text(json.dumps({'assigned_repos': ['owner/repo']}))
    store = Store(tmp_path)
    store.set_meta('github_login', 'HubOwner')
    store.sync_set('assigned_watch_since', '2026-01-01T00:00:00Z')
    def runner(argv):
        profile, command = unpack(argv)
        assert profile == '/accounts/two'
        if command[:3] == ['gh', 'api', 'user']: return Completed(0, 'WorkerTwo', '')
        assert command[:3] == ['gh', 'issue', 'list']
        assert command[command.index('--assignee') + 1] == 'workertwo'
        return Completed(0, '[]', '')
    assert scan_assigned(store, runner, now='2026-09-06T00:00:00Z') == ([], 0)
    assert store.meta('github_login') == 'HubOwner'


@pytest.mark.no_pg
def test_container_account_keeps_credentials_and_signing_in_its_executor(tmp_path):
    data = {
        'accounts': {'container': {
            'login': 'ContainerWorker', 'gh_config_dir': '/home/worker/.config/gh',
            'git': {'name': 'Worker', 'email': 'worker@example.com', 'signing_format': 'ssh', 'signing_key': '/home/worker/.ssh/key'},
            'command_prefix': ['docker', 'exec', '-i', 'worker-container'],
            'worktree_paths': {'/srv/worker/data': '/data'},
        }},
        'sessions': {'s1': 'container'},
    }
    (tmp_path / 'github-accounts.json').write_text(json.dumps(data))
    calls = []
    def runner(argv):
        calls.append(argv)
        assert argv[:5] == ['docker', 'exec', '-i', 'worker-container', 'env']
        return Completed(0, 'ContainerWorker', '')
    scoped = load_accounts(tmp_path).for_session('s1').runner(runner, require_git=True)
    scoped(['git', '-C', '/srv/worker/data/repo', 'push', 'origin', 'feature'])
    command = calls[-1]
    assert command[command.index('-C') + 1] == '/data/repo'
    assert 'GH_CONFIG_DIR=/home/worker/.config/gh' in command
    assert 'user.signingkey=/home/worker/.ssh/key' in command
    scoped(['git', '-C', '/srv/worker/database/repo', 'status'])
    assert calls[-1][calls[-1].index('-C') + 1] == '/srv/worker/database/repo'
    with pytest.raises(AccountError, match='parent traversal'):
        scoped(['git', '-C', '/srv/worker/data/../other', 'status'])


@pytest.mark.no_pg
@pytest.mark.parametrize('key,value', [('command_prefix', False), ('worktree_paths', []), ('command_prefix', ['']), ('worktree_paths', {'/a': '/b/../c'})])
def test_invalid_executor_configuration_is_rejected(tmp_path, key, value):
    configure(tmp_path)
    path = tmp_path / 'github-accounts.json'
    data = json.loads(path.read_text())
    data['accounts']['one'][key] = value
    path.write_text(json.dumps(data))
    with pytest.raises(AccountError): load_accounts(tmp_path)


def test_failed_account_does_not_stop_other_accounts(tmp_path):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    pending(store, 's2', 'a2')
    def runner(argv):
        profile, command = unpack(argv)
        if profile == '/accounts/one':
            assert command[:3] == ['gh', 'api', 'user']
            return Completed(1, '', 'offline')
        if command[:3] == ['gh', 'api', 'user']: return Completed(0, 'WorkerTwo', '')
        if command[:3] == ['gh', 'pr', 'view']: return Completed(1, '', 'no pull requests found')
        assert command[:3] == ['gh', 'pr', 'create']
        return Completed(0, 'https://github.com/owner/repo/pull/2', '')
    lines = scan_github(store, runner)
    assert set(lines) == {'pr.open a1 error', 'pr.open a2 done number=2'}
    assert store.row('activity', 'a1')['execution_account']['account'] == 'one'
    assert store.row('activity', 'a2')['execution_account']['account'] == 'two'


def test_merge_watch_refuses_an_account_change(tmp_path):
    configure(tmp_path)
    store = Store(tmp_path)
    pending(store, 's1', 'a1')
    row = store.row('activity', 'a1')
    row.update(execution_status='done', execution_account={'account': 'two', 'login': 'workertwo'},
        result={'repo': 'owner/repo', 'number': 1, 'url': 'https://github.com/owner/repo/pull/1'})
    store.write('activity', 'update', 'a1', row)
    def forbidden(argv): pytest.fail('Changed binding must not reach GitHub')
    assert scan_merged(store, forbidden) == ([], 1)
