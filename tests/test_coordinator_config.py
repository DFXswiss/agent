from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.coordinator_config import load_coordinator_config
from agent_cli.store import StoreError

pytestmark = pytest.mark.no_pg


def config(root: Path) -> dict:
    return {'workers': {'selected-session': {
        'review_session': 'selected-review-session', 'workspace_root': str(root / 'work'),
        'repositories': {'example/project': {
            'base': 'develop', 'publication_repo': 'example/project',
            'check_argv': ['/operator/checks', '--full'],
            'readiness_argv': ['/operator/readiness'],
        }},
        'reply_logins': ['ExampleUser'], 'poll_seconds': 30,
        'lane_timeout': 1800, 'check_timeout': 600,
    }}}


@pytest.mark.parametrize('payload', [None, {}, {'workers': None}, {'workers': {}}])
def test_empty_installation_enables_no_coordinator(tmp_path, payload):
    if payload is not None:
        (tmp_path / 'coordinator.json').write_text(json.dumps(payload))
    assert load_coordinator_config(tmp_path) == {}


def test_explicit_worker_preserves_selected_commands_and_identities(tmp_path):
    (tmp_path / 'coordinator.json').write_text(json.dumps(config(tmp_path)))
    worker = load_coordinator_config(tmp_path)['selected-session']
    assert worker.session_id == 'selected-session'
    assert worker.review_session == 'selected-review-session'
    assert worker.reply_logins == ('exampleuser',)
    assert worker.repositories['example/project'].check_argv == ('/operator/checks', '--full')
    assert worker.repositories['example/project'].publication_repo == 'example/project'


@pytest.mark.parametrize('field', ['review_session', 'workspace_root', 'repositories',
                                  'reply_logins', 'poll_seconds', 'lane_timeout', 'check_timeout'])
def test_missing_selection_never_uses_a_default(tmp_path, field):
    data = config(tmp_path)
    del data['workers']['selected-session'][field]
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize('field,value', [('workspace_root', '../work'), ('lane_timeout', True),
                                        ('check_timeout', 0), ('reply_logins', []),
                                        ('review_session', 'selected-session')])
def test_unsafe_worker_selection_is_refused(tmp_path, field, value):
    data = config(tmp_path)
    data['workers']['selected-session'][field] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError):
        load_coordinator_config(tmp_path)


def test_worker_roots_cannot_share_an_execution_tree(tmp_path):
    data = config(tmp_path)
    other = dict(data['workers']['selected-session'])
    other['workspace_root'] = str(tmp_path / 'work' / 'nested')
    data['workers']['another-session'] = other
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='overlap'):
        load_coordinator_config(tmp_path)
