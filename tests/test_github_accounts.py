from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_cli.git_act import measure_mergeable
from agent_cli.github_accounts import (
    Account,
    AccountError,
    GitHubHttpsRemoteError,
    ensure_github_https_remote,
    load_accounts,
    resolve_effective_github_https_url,
    validate_repo_remote,
)
from agent_cli.github_act import scan_github
from agent_cli.runtime import Completed, run_argv
from agent_cli.store import Store
from agent_cli.watch import scan_assigned, scan_merged

IDENTITY = {
    'name': 'Worker One',
    'email': 'one@example.com',
    'signing_key': '/keys/one',
    'signing_format': 'ssh',
}
SAFE_ORIGIN = 'https://github.com/owner/repo.git'


def _git_payload(argv: list[str]) -> list[str]:
    """Strip account runner env/prefix wrappers down to the git argv."""
    if 'git' not in argv:
        return argv
    return argv[argv.index('git'):]


def _apply_url_rules(url: str, rules: list[tuple[str, str]]) -> str:
    best: tuple[str, str] | None = None
    for base, old in rules:
        if url.startswith(old) and (best is None or len(old) > len(best[1])):
            best = (base, old)
    if best is None:
        return url
    return best[0] + url[len(best[1]):]


def _remote_script(
    *,
    fetch_url: str = SAFE_ORIGIN,
    push_url: str | None = None,
    instead_of: list[tuple[str, str]] | None = None,
    push_instead_of: list[tuple[str, str]] | None = None,
):
    """Fake git remote get-url the way real Git does: rewrites already applied."""
    push_url = fetch_url if push_url is None else push_url
    instead_of = instead_of or []
    push_instead_of = push_instead_of or []

    def handle(argv: list[str]) -> Completed | None:
        git = _git_payload(argv)
        if not git or git[0] != 'git':
            return None
        if 'remote' not in git or 'get-url' not in git:
            return None
        explicit_url = None
        explicit_name = None
        index = 1
        while index < len(git):
            arg = git[index]
            if arg == '-c' and index + 1 < len(git):
                cfg = git[index + 1]
                if cfg.startswith('remote.') and '.url=' in cfg:
                    key, _, value = cfg.partition('=')
                    parts = key.split('.')
                    if len(parts) >= 3 and parts[0] == 'remote' and parts[-1] == 'url':
                        explicit_name = '.'.join(parts[1:-1])
                        explicit_url = value
                index += 2
                continue
            if arg in {'-C'} and index + 1 < len(git):
                index += 2
                continue
            index += 1
        name = git[-1]
        if explicit_url is not None and name == explicit_name:
            url = explicit_url
        elif name == 'origin':
            url = push_url if '--push' in git else fetch_url
        else:
            return Completed(1, '', 'unknown remote')
        url = _apply_url_rules(url, instead_of)
        if '--push' in git:
            url = _apply_url_rules(url, push_instead_of)
        return Completed(0, url + '\n', '')

    return handle


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
    remotes = _remote_script()
    calls = []
    def runner(argv):
        calls.append(argv)
        handled = remotes(argv)
        if handled is not None:
            return handled
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner, require_git=True)
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
    remotes = _remote_script()
    calls = []
    def runner(argv):
        calls.append(argv)
        assert argv[:5] == ['docker', 'exec', '-i', 'worker-container', 'env']
        handled = remotes(argv)
        if handled is not None:
            return handled
        if 'gh' in argv:
            return Completed(0, 'ContainerWorker', '')
        return Completed(0, '', '')
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
def test_ensure_github_https_remote_accepts_safe_urls():
    assert ensure_github_https_remote('https://github.com/Owner/Repo.git') == 'Owner/Repo'
    assert ensure_github_https_remote('https://github.com/Owner/Repo') == 'Owner/Repo'
    assert ensure_github_https_remote('https://github.com:443/Owner/Repo.git') == 'Owner/Repo'


@pytest.mark.no_pg
@pytest.mark.parametrize('url', [
    'https://x-access-token:ghs_secret@github.com/owner/repo.git',
    'https://user:pass@github.com/owner/repo',
    'git@github.com:owner/repo.git',
    'ssh://git@github.com/owner/repo.git',
    'https://gitlab.com/owner/repo.git',
    'https://github.com/owner/repo/extra',
    'http://github.com/owner/repo.git',
    '/absolute/local/path',
    'https://github.com:abc/owner/repo.git',
    'https://token:port-secret@github.com:notaport/owner/repo.git',
])
def test_ensure_github_https_remote_rejects_unsafe_without_leaking(url):
    with pytest.raises(GitHubHttpsRemoteError) as excinfo:
        ensure_github_https_remote(url)
    message = str(excinfo.value)
    assert 'ghs_secret' not in message
    assert 'pass' not in message
    assert 'x-access-token' not in message
    assert 'port-secret' not in message
    if '://' in url or url.startswith('git@'):
        assert url not in message


@pytest.mark.no_pg
def test_resolve_effective_url_applies_pushurl_and_rewrites():
    remotes = _remote_script(
        fetch_url='https://github.com/owner/repo.git',
        push_url='ssh://git@github.com/owner/repo.git',
        instead_of=[('https://github.com/', 'ssh://git@github.com/')],
    )
    def run(argv):
        handled = remotes(argv)
        assert handled is not None
        return handled
    # Fake get-url already applies insteadOf, matching real Git.
    assert resolve_effective_github_https_url(run, '/work', 'origin', push=False).startswith('https://')
    assert resolve_effective_github_https_url(run, '/work', 'origin', push=True) == 'https://github.com/owner/repo.git'
    assert validate_repo_remote(run, '/work', 'origin') == 'owner/repo'


@pytest.mark.no_pg
def test_explicit_url_uses_temporary_remote_get_url():
    remotes = _remote_script(
        instead_of=[('https://github.com/', 'ssh://git@github.com/')],
    )
    seen = []
    def run(argv):
        seen.append(list(argv))
        handled = remotes(argv)
        assert handled is not None
        return handled
    url = resolve_effective_github_https_url(
        run, '/work', 'ssh://git@github.com/owner/repo.git', push=False,
    )
    assert url == 'https://github.com/owner/repo.git'
    assert any(
        '-c' in cmd and any(
            isinstance(part, str) and part.startswith('remote.') and '.url=' in part
            for part in cmd
        )
        for cmd in seen
    )


@pytest.mark.no_pg
def test_pushurl_with_embedded_credentials_is_rejected_without_leaking():
    secret = 'leak-me-not'
    remotes = _remote_script(
        fetch_url=SAFE_ORIGIN,
        push_url=f'https://token:{secret}@github.com/owner/repo.git',
    )
    def run(argv):
        handled = remotes(argv)
        assert handled is not None
        return handled
    with pytest.raises(GitHubHttpsRemoteError, match='must not contain credentials') as excinfo:
        validate_repo_remote(run, '/work', 'origin')
    assert secret not in str(excinfo.value)


@pytest.mark.no_pg
def test_push_instead_of_rewrite_to_credential_url_is_rejected():
    secret = 'push-rewrite-secret'
    remotes = _remote_script(
        fetch_url=SAFE_ORIGIN,
        push_url=SAFE_ORIGIN,
        push_instead_of=[(f'https://bot:{secret}@github.com/', 'https://github.com/')],
    )
    def run(argv):
        handled = remotes(argv)
        assert handled is not None
        return handled
    with pytest.raises(GitHubHttpsRemoteError, match='must not contain credentials') as excinfo:
        resolve_effective_github_https_url(run, '/work', 'origin', push=True)
    assert secret not in str(excinfo.value)


@pytest.mark.no_pg
def test_account_runner_rejects_unsafe_remote_before_network_git():
    secret = 'before-network'
    remotes = _remote_script(fetch_url=f'https://user:{secret}@github.com/owner/repo.git')
    network = []
    def runner(argv):
        handled = remotes(argv)
        if handled is not None:
            return handled
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        git = _git_payload(argv)
        if git and git[0] == 'git' and any(v in git for v in ('fetch', 'push')):
            network.append(git)
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner, require_git=True)
    with pytest.raises(GitHubHttpsRemoteError) as excinfo:
        scoped(['git', '-C', '/work', 'fetch', '--', 'origin'])
    assert network == []
    assert secret not in str(excinfo.value)
    # Local metadata still works without remote validation.
    scoped(['git', '-C', '/work', 'rev-parse', 'HEAD'])
    scoped(['git', '-C', '/work', 'status', '--porcelain'])


@pytest.mark.no_pg
def test_account_runner_allows_safe_fetch_and_blocks_ssh_transport_url():
    remotes = _remote_script()
    seen = []
    def runner(argv):
        handled = remotes(argv)
        if handled is not None:
            return handled
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        seen.append(_git_payload(argv))
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner, require_git=True)
    scoped(['git', '-C', '/work', 'fetch', '--', 'origin'])
    assert any('fetch' in cmd for cmd in seen)
    with pytest.raises(GitHubHttpsRemoteError, match='HTTPS GitHub'):
        scoped(['git', '-C', '/work', 'fetch', '--', 'git@github.com:owner/repo.git'])
    scoped(['git', '-C', '/work', 'push', '--', 'origin', 'HEAD:refs/heads/feature'])
    scoped(['git', '-C', '/work', 'push', '--set-upstream', 'origin', 'feature'])


@pytest.mark.no_pg
@pytest.mark.parametrize('argv', [
    ['git', '-C', '/work', 'fetch'],
    ['git', '-C', '/work', 'fetch', '--all'],
    ['git', '-C', '/work', 'fetch', '--multiple', 'origin', 'other'],
    ['git', '-C', '/work', 'fetch', '--repo=https://github.com/other/repo.git', 'origin'],
    ['git', '-C', '/work', 'push'],
    ['git', '-C', '/work', 'push', 'HEAD:refs/heads/feature'],
    ['git', '-C', '/work', 'pull'],
])
def test_account_runner_refuses_implicit_or_multi_target_network_forms(argv):
    remotes = _remote_script()
    network = []
    def runner(cmd):
        handled = remotes(cmd)
        if handled is not None:
            return handled
        if 'gh' in cmd:
            return Completed(0, 'WorkerOne', '')
        git = _git_payload(cmd)
        if git and git[0] == 'git' and any(v in git for v in ('fetch', 'push', 'pull')):
            network.append(git)
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner, require_git=True)
    with pytest.raises(GitHubHttpsRemoteError):
        scoped(argv)
    assert network == []


@pytest.mark.no_pg
def test_container_mergeable_uses_explicit_fork_target_without_git_identity(tmp_path):
    data = {
        'accounts': {'container': {
            'login': 'ContainerWorker', 'gh_config_dir': '/home/worker/.config/gh',
            'command_prefix': ['docker', 'exec', '-i', 'worker-container'],
            'worktree_paths': {'/srv/worker/data': '/data'},
        }},
        'sessions': {'s1': 'container'},
    }
    (tmp_path / 'github-accounts.json').write_text(json.dumps(data))
    gh_calls = []
    def runner(argv):
        assert argv[:5] == ['docker', 'exec', '-i', 'worker-container', 'env']
        if 'git' in argv:
            raise AssertionError('explicit fork PR must not require git')
        idx = argv.index('gh')
        command = argv[idx:]
        if command[:3] == ['gh', 'api', 'user']:
            return Completed(0, 'ContainerWorker', '')
        gh_calls.append(command)
        if command[:3] == ['gh', 'pr', 'view']:
            assert command[:6] == ['gh', 'pr', 'view', '7', '--repo', 'upstream/product']
            return Completed(0, json.dumps({
                'mergeable': 'MERGEABLE', 'state': 'OPEN',
                'url': 'https://example.invalid/p/7', 'number': 7,
                'headRefOid': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            }), '')
        if command[:3] == ['gh', 'pr', 'checks']:
            assert command[command.index('--repo') + 1] == 'upstream/product'
            return Completed(0, '[]', '')
        raise AssertionError(f'unexpected argv: {argv}')
    scoped = load_accounts(tmp_path).for_session('s1').runner(runner, require_git=False)
    evidence = measure_mergeable(
        cwd='/srv/worker/data/repo',
        runner=scoped,
        repo='upstream/product',
        number=7,
    )
    assert 'mergeable' in evidence
    assert gh_calls
    for call in gh_calls:
        assert '--repo' in call
        assert call[call.index('--repo') + 1] == 'upstream/product'


@pytest.mark.no_pg
def test_container_mergeable_derives_branch_with_mapped_git_c(tmp_path):
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
    remotes = _remote_script()
    gh_calls = []
    def runner(argv):
        assert argv[:5] == ['docker', 'exec', '-i', 'worker-container', 'env']
        handled = remotes(argv)
        if handled is not None:
            return handled
        git = _git_payload(argv)
        if git and git[0] == 'git' and 'rev-parse' in git and '--abbrev-ref' in git:
            assert git[git.index('-C') + 1] == '/data/repo'
            return Completed(0, 'feature\n', '')
        idx = argv.index('gh')
        command = argv[idx:]
        if command[:3] == ['gh', 'api', 'user']:
            return Completed(0, 'ContainerWorker', '')
        gh_calls.append(command)
        if command[:3] == ['gh', 'pr', 'view']:
            assert command[:6] == ['gh', 'pr', 'view', 'feature', '--repo', 'owner/repo']
            return Completed(0, json.dumps({
                'mergeable': 'MERGEABLE', 'state': 'OPEN',
                'url': 'https://example.invalid/p/1', 'number': 1,
                'headRefOid': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            }), '')
        if command[:3] == ['gh', 'pr', 'checks']:
            assert command[command.index('--repo') + 1] == 'owner/repo'
            return Completed(0, '[]', '')
        return Completed(0, '', '')
    scoped = load_accounts(tmp_path).for_session('s1').runner(runner, require_git=True)
    evidence = measure_mergeable(cwd='/srv/worker/data/repo', runner=scoped)
    assert 'mergeable' in evidence
    assert gh_calls
    for call in gh_calls:
        assert '--repo' in call
        assert call[call.index('--repo') + 1] == 'owner/repo'


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


@pytest.mark.no_pg
@pytest.mark.parametrize('verb,option', [
    ('push', '--receive-pack'), ('push', '--receive-pack=decoy'),
    ('push', '--repo'), ('fetch', '--refmap'), ('pull', '--strategy'),
    ('pull', '--onto'), ('clone', '--upload-pack'), ('fetch', '--recurse-submodules'),
])
def test_unknown_transfer_option_never_reaches_git(verb, option):
    calls = []
    def runner(argv):
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        calls.append(argv)
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner)
    with pytest.raises(GitHubHttpsRemoteError, match='unsupported'):
        scoped(['git', '-C', '/work', verb, option, 'decoy', 'origin'])
    assert calls == []


@pytest.mark.no_pg
@pytest.mark.parametrize('prefix', [
    ['-C', '/work', '-c', 'remote.origin.url=https://probe:synthetic@github.com/owner/repo'],
    ['-C', '/work', '-C', '/different'],
    ['--git-dir', '/different/.git'],
])
def test_transfer_context_overrides_are_refused_before_validation(prefix):
    calls = []
    def runner(argv):
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        calls.append(argv)
        return Completed(0, '', '')
    scoped = Account('one', 'WorkerOne', '/accounts/one', IDENTITY).runner(runner)
    with pytest.raises(GitHubHttpsRemoteError, match='unsupported') as exc:
        scoped(['git', *prefix, 'push', 'origin', 'feature'])
    assert 'synthetic' not in str(exc.value)
    assert calls == []


@pytest.mark.no_pg
def test_malformed_unicode_netloc_does_not_disclose_credentials():
    secret = 'synthetic-redaction-probe'
    with pytest.raises(GitHubHttpsRemoteError, match='unsafe') as exc:
        ensure_github_https_remote(f'https://user:{secret}@github.com\uff1a443/owner/repo')
    assert secret not in str(exc.value)


@pytest.mark.no_pg
def test_transfer_cwd_is_mapped_once_and_submodule_transfers_are_disabled():
    remotes = _remote_script()
    calls = []
    raw_calls = []
    def runner(argv):
        raw_calls.append(argv)
        if 'gh' in argv:
            return Completed(0, 'WorkerOne', '')
        command = _git_payload(argv)
        calls.append(command)
        handled = remotes(argv)
        if handled is not None:
            return handled
        return Completed(0, '', '')
    account = Account('one', 'WorkerOne', '/accounts/one', IDENTITY,
                      worktree_paths=(('/srv', '/data'), ('/data', '/wrong')))
    scoped = account.runner(runner)
    scoped(['git', '-C', '/srv/repo', 'fetch', '--depth', '1', 'origin'])
    scoped(['git', '-C', '/srv/repo', 'push', '--', 'origin', 'HEAD:refs/heads/feature'])
    assert calls
    assert all(c[c.index('-C') + 1] == '/data/repo' for c in calls)
    pushed = raw_calls[-1]
    assert 'fetch.recurseSubmodules=false' in pushed
    assert 'push.recurseSubmodules=no' in pushed
    assert 'submodule.recurse=false' in pushed
