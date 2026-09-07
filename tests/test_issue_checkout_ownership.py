"""Regression tests for existing filesystem ownership and bounded script execution."""
from __future__ import annotations

import sys
import os
import signal
import subprocess
import time
import json
from types import SimpleNamespace

import pytest

from agent_cli.coordinator_config import RepositoryConfig, WorkerConfig
from agent_cli.store import StoreError

pytestmark = pytest.mark.no_pg


def test_existing_checkout_cannot_issue_itself_an_ownership_marker(tmp_path, monkeypatch):
    from agent_cli import coordinator_git as git_work
    task_id = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa'
    workspace = tmp_path / 'work'
    unrelated = workspace / task_id
    (unrelated / '.git').mkdir(parents=True)
    sentinel = unrelated / 'existing.txt'
    sentinel.write_text('Preserve this existing checkout.\n')
    worker = WorkerConfig(
        'worker', 'formal-review', workspace,
        {'example/project': RepositoryConfig('example/project', 'develop', 'example/project',
                                              ('/operator/checks',), ('/operator/readiness',))},
        ('human',), 30, 60, 30,
    )
    task = {'id': task_id, 'session_id': 'worker', 'payload': {'coordinator': {
        'source': {'repo': 'example/project', 'number': 7}, 'phase': 'checkout',
    }}}
    mutations = []
    def forbidden_git(argv):
        mutations.append(argv)
        pytest.fail('Unowned pre-existing checkout reached Git execution')
    monkeypatch.setattr(git_work, 'scoped', lambda *a, **k: forbidden_git)
    store = SimpleNamespace(write=lambda *a, **k: None)
    with pytest.raises(StoreError, match='ownership|existing|unowned'):
        git_work.phase_checkout(store, worker, task, forbidden_git)
    assert mutations == []
    assert sentinel.read_text() == 'Preserve this existing checkout.\n'
    assert not (workspace / '.coordinator-control' / task_id / 'checkout.json').exists()


def test_bounded_script_process_preserves_prompt_and_working_directory(tmp_path):
    from agent_cli.coordinator_exec import run_bounded
    result = run_bounded(
        [sys.executable, '-c', 'import os,sys; print(os.getcwd()); print(sys.stdin.read())'],
        timeout=5, cwd=str(tmp_path), stdin_text='The exact model prompt.\n',
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(tmp_path), 'The exact model prompt.', '']


def test_bounded_script_process_kills_descendants_holding_output_pipes(tmp_path):
    from agent_cli.coordinator_exec import run_bounded
    result = run_bounded(
        [sys.executable, '-c',
         'import subprocess,sys,time; '
         'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
         'print("child started",flush=True); time.sleep(30)'],
        timeout=1, cwd=str(tmp_path),
    )
    assert result.returncode == 124
    assert 'child started' in result.stdout


@pytest.mark.parametrize('returncode', [1, 124, -15])
def test_failed_model_process_cannot_certify_approval(returncode):
    from agent_cli.coordinator_common import parse_model_result, review_is_approved
    status, result = parse_model_result('STATUS: complete\nRESULT: approved\n', returncode)
    assert not review_is_approved(status, result)


@pytest.mark.parametrize('value', ['token: dummy-private-value',
                                  'Authorization: Bearer dummy-private-value',
                                  '{"password": "dummy-private-value"}',
                                  'https://example:dummy-private-value@example.test/path'])
def test_shared_redaction_removes_values_and_preserves_commit_evidence(value):
    from agent_cli.coordinator_common import redact
    head = 'a' * 40
    output = redact(value + '\nReviewed head ' + head)
    assert 'dummy-private-value' not in output
    assert head in output


def test_shared_redaction_catches_bare_token_and_key_material():
    from agent_cli.coordinator_common import redact
    fake_token = 'ghp_' + 'A' * 40
    fake_key = '-----BEGIN ' + 'PRIVATE KEY-----\ndummy-private-value\n-----END ' + 'PRIVATE KEY-----'
    output = redact(fake_token + '\n' + fake_key)
    assert fake_token not in output
    assert 'dummy-private-value' not in output


def test_worker_termination_reaps_the_separate_child_process_group(tmp_path):
    pid_file = tmp_path / 'child.pid'
    program = (
        'import sys; from agent_cli.coordinator_exec import process_scope,run_bounded\n'
        'with process_scope():\n'
        ' run_bounded([sys.executable,"-c",'
        '"import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",'
        'sys.argv[1]],timeout=40)\n'
    )
    owner = subprocess.Popen([sys.executable, '-c', program, str(pid_file)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline and owner.poll() is None:
            time.sleep(0.02)
        assert pid_file.exists(), owner.communicate(timeout=5)
        child_pid = int(pid_file.read_text())
        owner.send_signal(signal.SIGTERM)
        owner.communicate(timeout=5)
        assert owner.returncode == 128 + signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.communicate(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_potential_credential_material_blocks_commit(monkeypatch):
    from agent_cli import coordinator_git as git_work
    from agent_cli.runtime import Completed
    calls = []
    def git(_store, _worker, _runner, _cwd, *argv):
        calls.append(argv)
        if argv[0] == 'status':
            return Completed(0, ' M source.py\n', '')
        if argv == ('diff', '--cached', '--name-only'):
            return Completed(0, 'source.py\n', '')
        if argv == ('diff', '--cached'):
            return Completed(0, '+credential = "' + 'ghp_' + 'A' * 40 + '"', '')
        return Completed(0, '', '')
    monkeypatch.setattr(git_work, 'git', git)
    with pytest.raises(StoreError, match='potential credential'):
        git_work.stage_sign_commit_if_changes(None, None, None, '/unused', 'Change issue.')
    assert not any(argv[0] == 'commit' for argv in calls)


def test_failed_push_never_switches_to_another_ref(monkeypatch):
    from agent_cli import coordinator_git as git_work
    from agent_cli.runtime import Completed
    calls = []
    monkeypatch.setattr(git_work, 'verify_checkout_identity', lambda *a: {'branch': 'task-owned'})
    monkeypatch.setattr(git_work, 'verify_signed_clean_head', lambda *a: 'a' * 40)
    def git(*args):
        calls.append(args[4:])
        return Completed(1, '', 'remote refused push')
    monkeypatch.setattr(git_work, 'git', git)
    with pytest.raises(StoreError, match='remote refused'):
        git_work.push_branch(None, None, None, '/unused', 'task-owned')
    assert calls == [('push', '--', 'publication', 'HEAD:refs/heads/task-owned')]


def test_process_has_no_ambient_github_account(tmp_path, monkeypatch):
    from agent_cli.coordinator_exec import run_bounded
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        monkeypatch.setenv(key, 'dummy-private-value')
    monkeypatch.setenv('GH_CONFIG_DIR', str(tmp_path / 'ambient'))
    code = (
        'import json,os; from pathlib import Path; '
        'print(json.dumps({"tokens":[k for k in os.environ if k in '
        '["GH_TOKEN","GITHUB_TOKEN","GH_ENTERPRISE_TOKEN","GITHUB_ENTERPRISE_TOKEN"]],'
        '"profile":os.environ["GH_CONFIG_DIR"],'
        '"files":list(str(p) for p in Path(os.environ["GH_CONFIG_DIR"]).iterdir())}))'
    )
    result = run_bounded([sys.executable, '-c', code], timeout=5)
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed['tokens'] == []
    assert observed['profile'] != str(tmp_path / 'ambient')
    assert observed['files'] == []
