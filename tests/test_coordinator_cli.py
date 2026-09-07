from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from agent_cli import main as cli
from test_coordinator_config import config

pytestmark = pytest.mark.no_pg


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HOME', str(tmp_path))
    (tmp_path / 'coordinator.json').write_text(json.dumps(config(tmp_path)))
    return tmp_path


def test_empty_coordinator_does_not_open_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('AGENT_HOME', str(tmp_path))
    monkeypatch.setattr(cli, 'open_store', lambda: pytest.fail('unconfigured worker opened store'))
    cli.cmd_coordinate([])
    assert capsys.readouterr().out == 'coordinator unconfigured\n'


@pytest.mark.parametrize('args', [[], ['--session', 'missing'], ['--unknown'], ['--follow']])
def test_coordinator_never_selects_first_worker(configured, monkeypatch, args):
    monkeypatch.setattr(cli, 'open_store', lambda: pytest.fail('invalid selection opened store'))
    with pytest.raises(SystemExit):
        cli.cmd_coordinate(args)


def test_selected_coordinator_tick_closes_store(configured, monkeypatch, capsys):
    calls = []
    store = SimpleNamespace(close=lambda: calls.append('close'))
    monkeypatch.setattr(cli, 'open_store', lambda: store)
    def tick(actual, worker):
        assert actual is store
        calls.append(worker.session_id)
        return ['coordinator observed']
    monkeypatch.setitem(sys.modules, 'agent_cli.coordinator', SimpleNamespace(tick=tick))
    cli.cmd_coordinate(['--session', 'selected-session'])
    assert calls == ['selected-session', 'close']
    assert capsys.readouterr().out == 'coordinator observed\n'


def test_script_follow_stops_when_worker_is_removed(configured, monkeypatch):
    calls = []
    store = SimpleNamespace(close=lambda: calls.append('close'))
    monkeypatch.setattr(cli, 'open_store', lambda: store)
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: calls.append(seconds))
    def tick(actual, worker):
        calls.append('tick')
        (configured / 'coordinator.json').write_text('{}')
        return []
    monkeypatch.setitem(sys.modules, 'agent_cli.coordinator', SimpleNamespace(tick=tick))
    cli.cmd_coordinate(['--session', 'selected-session', '--follow'])
    assert calls == ['tick', 'close', 30]


def test_legacy_supervise_cannot_start_coordinator_session(configured, monkeypatch):
    monkeypatch.setattr(cli, 'open_store', lambda: pytest.fail('legacy worker opened store'))
    with pytest.raises(SystemExit, match='static issue coordinator'):
        cli.cmd_supervise(['--session', 'selected-session', '--once'])


def test_configured_worker_is_supervised_as_static_child(configured, monkeypatch):
    from agent_cli.daemon import run_supervisor
    calls = []
    class Proc:
        def __init__(self, argv):
            self.argv = argv
            self.returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -15
        def wait(self, timeout=None):
            return self.returncode
    def popen(argv, **kwargs):
        proc = Proc(argv)
        calls.append(proc)
        return proc
    class End(Exception):
        pass
    def sleep(seconds):
        assert any(p.argv == ['agent', 'coordinate', '--session', 'selected-session', '--follow'] for p in calls)
        raise End
    with pytest.raises(End):
        run_supervisor(home=configured, argv_prefix=['agent'], popen=popen, sleep=sleep)
    assert all(p.returncode == -15 for p in calls)


def test_legacy_dispatch_cannot_start_coordinator_session(configured):
    from agent_cli.watch import dispatch_assigned
    from agent_cli.store import StoreError
    row = {'id': 'assignment', '_origin_device_id': 'device',
           'type': 'issue.assigned', 'session_id': 'selected-session'}
    store = SimpleNamespace(home=configured, row=lambda *_: row, device_id=lambda: 'device')
    def forbidden(*args):
        pytest.fail('legacy dispatch executed after coordinator selection')
    with pytest.raises(StoreError, match='static issue coordinator'):
        dispatch_assigned(store, 'assignment', sync=forbidden, start=forbidden,
                          knock=forbidden, workspace_root=configured / 'legacy')


def test_invalid_configuration_never_acquires_daemon_lock(configured, monkeypatch):
    from agent_cli import daemon
    from agent_cli.store import StoreError
    (configured / 'coordinator.json').write_text('{')
    monkeypatch.setattr(daemon, 'acquire_lock', lambda *_: pytest.fail('invalid config acquired lock'))
    with pytest.raises(StoreError, match='Cannot read coordinator.json'):
        daemon.run_supervisor(home=configured, argv_prefix=['agent'])
