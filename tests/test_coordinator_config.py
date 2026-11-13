from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli.coordinator_config import load_coordinator_config, resolve_round_limit
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


def test_park_poll_seconds_defaults_to_120(tmp_path):
    (tmp_path / 'coordinator.json').write_text(json.dumps(config(tmp_path)))
    worker = load_coordinator_config(tmp_path)['selected-session']
    assert worker.park_poll_seconds == 120


@pytest.mark.parametrize('value', [-1, "5", True, 1.5])
def test_park_poll_seconds_accepts_zero_and_refuses_bad_values(tmp_path, value):
    data = config(tmp_path)
    data['workers']['selected-session']['park_poll_seconds'] = 0
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].park_poll_seconds == 0

    data['workers']['selected-session']['park_poll_seconds'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='park_poll_seconds requires an integer >= 0'):
        load_coordinator_config(tmp_path)


def test_lane_retries_defaults_to_two(tmp_path):
    (tmp_path / 'coordinator.json').write_text(json.dumps(config(tmp_path)))
    worker = load_coordinator_config(tmp_path)['selected-session']
    assert worker.lane_retries == 2


def test_orders_defaults_to_false(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].orders is False

    data['workers']['selected-session']['orders'] = True
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].orders is True


def test_orders_must_be_a_boolean(tmp_path):
    data = config(tmp_path)
    data['workers']['selected-session']['orders'] = 'yes'
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='orders must be a boolean'):
        load_coordinator_config(tmp_path)


def test_draft_on_accept_defaults_to_false(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].draft_on_accept is False

    data['workers']['selected-session']['draft_on_accept'] = True
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].draft_on_accept is True


@pytest.mark.parametrize('value', ['yes', 1, None])
def test_draft_on_accept_must_be_a_boolean(tmp_path, value):
    data = config(tmp_path)
    data['workers']['selected-session']['draft_on_accept'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='draft_on_accept must be a boolean'):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize('value', [-1, "1", True, 1.5])
def test_lane_retries_accepts_zero_and_refuses_bad_values(tmp_path, value):
    data = config(tmp_path)
    data['workers']['selected-session']['lane_retries'] = 0
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].lane_retries == 0

    data['workers']['selected-session']['lane_retries'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='lane_retries requires an integer >= 0'):
        load_coordinator_config(tmp_path)


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


@pytest.mark.parametrize('base', ['../main', '/main', 'feature/', 'feature//main',
                                 'feature/.hidden', 'main.lock', 'main.', 'ma\tin', '@'])
def test_invalid_branch_is_rejected_before_acceptance(tmp_path, base):
    data = config(tmp_path)
    data['workers']['selected-session']['repositories']['example/project']['base'] = base
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='invalid base branch'):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize('repo', ['../project', 'example/..', 'example/.', '-example/project'])
def test_repository_cannot_escape_selected_api_path(tmp_path, repo):
    data = config(tmp_path)
    entry = data['workers']['selected-session']['repositories'].pop('example/project')
    data['workers']['selected-session']['repositories'][repo] = entry
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='repository must be owner/name'):
        load_coordinator_config(tmp_path)


def test_a38_worker_requires_and_normalizes_external_reviewers(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker.pop('review_session')
    worker['a38'] = True
    worker['second_reviewers'] = ['Reviewer-One', 'reviewer-two']
    worker['repositories']['example/project']['check_argv'] = []
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    loaded = load_coordinator_config(tmp_path)['selected-session']
    assert loaded.review_session is None
    assert loaded.a38 is True
    assert loaded.second_reviewers == ('reviewer-one', 'reviewer-two')
    assert loaded.second_review_timeout_seconds == 86400
    assert loaded.repositories['example/project'].check_argv == ()


def test_a38_second_review_defaults_to_external(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].a38_second_review == 'external'

    worker = data['workers']['selected-session']
    worker.pop('review_session')
    worker['a38'] = True
    worker['second_reviewers'] = ['reviewer-one']
    worker['repositories']['example/project']['check_argv'] = []
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    assert load_coordinator_config(tmp_path)['selected-session'].a38_second_review == 'external'


@pytest.mark.parametrize(
    'a38,value,error',
    [
        (False, 'people', 'a38_second_review must be external or model'),
        (False, 'model', 'a38_second_review requires an A38 worker'),
    ],
)
def test_a38_second_review_refuses_bad_values(tmp_path, a38, value, error):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['a38'] = a38
    worker['a38_second_review'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match=error):
        load_coordinator_config(tmp_path)


def test_model_second_review_drops_the_reviewer_requirement(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker.pop('review_session')
    worker['a38'] = True
    worker['a38_second_review'] = 'model'
    worker['repositories']['example/project']['check_argv'] = []
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    loaded = load_coordinator_config(tmp_path)['selected-session']
    assert loaded.second_reviewers == ()
    assert loaded.a38_second_review == 'model'

    worker['second_reviewers'] = ['Same', 'same']
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='distinct'):
        load_coordinator_config(tmp_path)

    worker.pop('second_reviewers')
    worker['a38_second_review'] = 'external'
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='A38 workers require nonempty second_reviewers'):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize(
    'reviewers,error',
    [
        (None, 'nonempty second_reviewers'),
        ([], 'nonempty second_reviewers'),
        (['Same', 'same'], 'distinct'),
    ],
)
def test_a38_worker_refuses_missing_or_duplicate_reviewers(tmp_path, reviewers, error):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['a38'] = True
    worker['second_reviewers'] = reviewers
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match=error):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize(
    'fields,error',
    [
        ({'a38': 'yes'}, 'a38 must be a boolean'),
        ({'require_hub_assignment': 1}, 'require_hub_assignment must be a boolean'),
        ({'second_reviewers': 'reviewer'}, 'second_reviewers must be a list'),
        ({'a38': True, 'second_reviewers': ['-bad']}, 'invalid second reviewer login'),
        ({'a38': True, 'second_reviewers': ['a--b']}, 'invalid second reviewer login'),
        ({'a38': True, 'second_reviewers': ['a' * 40]}, 'invalid second reviewer login'),
    ],
)
def test_worker_refuses_malformed_a38_and_hub_fields(tmp_path, fields, error):
    data = config(tmp_path)
    data['workers']['selected-session'].update(fields)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match=error):
        load_coordinator_config(tmp_path)


def test_a38_worker_refuses_short_second_review_timeout(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['a38'] = True
    worker['second_reviewers'] = ['reviewer']
    worker['second_review_timeout_seconds'] = 599
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='integer >= 600'):
        load_coordinator_config(tmp_path)


def test_a38_worker_refuses_removed_a38_timeout_key(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['a38'] = True
    worker['second_reviewers'] = ['reviewer']
    worker['a38_timeout_seconds'] = 7200
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='explicit workspace, replies and timing'):
        load_coordinator_config(tmp_path)


def test_owner_scoped_worker_normalizes_distinct_owners_and_requires_checks(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['repositories'] = {}
    worker['repository_owners'] = ['Example-Org', 'AnotherUser']
    worker['owner_check_argv'] = ['/operator/owner-checks']
    worker['owner_readiness_argv'] = ['/operator/owner-readiness']
    worker['require_hub_assignment'] = True
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    loaded = load_coordinator_config(tmp_path)['selected-session']
    assert loaded.repository_owners == ('example-org', 'anotheruser')
    assert loaded.owner_check_argv == ('/operator/owner-checks',)
    assert loaded.owner_readiness_argv == ('/operator/owner-readiness',)
    assert loaded.require_hub_assignment is True


@pytest.mark.parametrize(
    'owners',
    [
        ['-bad'],
        ['bad-'],
        ['a--b'],
        ['a' * 40],
        ['same', 'Same'],
        ['with_underscore'],
    ],
)
def test_owner_scoped_worker_rejects_invalid_or_duplicate_owners(tmp_path, owners):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['repositories'] = {}
    worker['repository_owners'] = owners
    worker['owner_readiness_argv'] = ['/operator/owner-readiness']
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError):
        load_coordinator_config(tmp_path)


def test_worker_requires_repositories_or_repository_owners(tmp_path):
    data = config(tmp_path)
    data['workers']['selected-session']['repositories'] = {}
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='repositories or repository_owners'):
        load_coordinator_config(tmp_path)


def test_non_a38_owner_scope_requires_readiness_argv(tmp_path):
    data = config(tmp_path)
    data['workers']['selected-session']['repository_owners'] = ['example']
    data['workers']['selected-session']['owner_check_argv'] = ['/operator/checks']
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='owner_readiness_argv'):
        load_coordinator_config(tmp_path)


def test_max_rounds_is_optional(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    worker = load_coordinator_config(tmp_path)['selected-session']
    assert worker.max_rounds is None
    assert worker.max_rounds_by_model == {}

    selected = data['workers']['selected-session']
    selected['max_rounds'] = 7
    selected['max_rounds_by_model'] = {'grok-model': 3}
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    worker = load_coordinator_config(tmp_path)['selected-session']
    assert worker.max_rounds == 7
    assert worker.max_rounds_by_model == {'grok-model': 3}


@pytest.mark.parametrize('value', [0, -1, '8', True])
def test_max_rounds_refuses_bad_values(tmp_path, value):
    data = config(tmp_path)
    data['workers']['selected-session']['max_rounds'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='max_rounds requires an integer >= 1'):
        load_coordinator_config(tmp_path)


@pytest.mark.parametrize(
    'value,error',
    [
        ([], 'max_rounds_by_model must be an object'),
        ({'m': 0}, 'max_rounds_by_model requires an integer >= 1'),
        ({'m': '3'}, 'max_rounds_by_model requires an integer >= 1'),
        ({'': 3}, 'max_rounds_by_model requires a nonempty single-line string'),
    ],
)
def test_max_rounds_by_model_is_validated(tmp_path, value, error):
    data = config(tmp_path)
    data['workers']['selected-session']['max_rounds_by_model'] = value
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match=error):
        load_coordinator_config(tmp_path)


def test_resolve_round_limit_order(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    base = load_coordinator_config(tmp_path)['selected-session']
    worker = replace(
        base,
        max_rounds=7,
        max_rounds_by_model={'grok-model': 3},
    )
    cases = [
        (resolve_round_limit(worker, 'grok-model', 2), 2, 'submit'),
        (resolve_round_limit(worker, 'grok-model'), 3, 'model'),
        (resolve_round_limit(worker, 'other-model'), 7, 'worker'),
        (resolve_round_limit(base, 'other-model'), 10, 'default'),
    ]
    for result, limit, origin in cases:
        assert set(result) == {'max_rounds', 'origin', 'model', 'limit'}
        assert result['max_rounds'] == result['limit'] == limit
        assert result['origin'] == origin
        assert result['model'] in {'grok-model', 'other-model'}


def test_pinned_model_name_is_redacted_and_still_matches(tmp_path):
    data = config(tmp_path)
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    base = load_coordinator_config(tmp_path)['selected-session']
    model = '/home/someone/models/x'
    worker = replace(base, max_rounds_by_model={model: 3})
    result = resolve_round_limit(worker, model)
    assert result['origin'] == 'model'
    assert result['max_rounds'] == 3
    assert 'someone' not in result['model']


def test_a38_owner_scope_allows_absent_readiness_argv(tmp_path):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker.pop('review_session')
    worker['a38'] = True
    worker['second_reviewers'] = ['reviewer']
    worker['repositories'] = {}
    worker['repository_owners'] = ['example']
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    loaded = load_coordinator_config(tmp_path)['selected-session']
    assert loaded.owner_readiness_argv is None
    assert loaded.owner_check_argv == ()


@pytest.mark.parametrize('owner_check_argv', [None, []])
def test_non_a38_owner_scope_requires_nonempty_owner_check_argv(tmp_path, owner_check_argv):
    data = config(tmp_path)
    worker = data['workers']['selected-session']
    worker['repositories'] = {}
    worker['repository_owners'] = ['example']
    worker['owner_readiness_argv'] = ['/operator/owner-readiness']
    if owner_check_argv is not None:
        worker['owner_check_argv'] = owner_check_argv
    (tmp_path / 'coordinator.json').write_text(json.dumps(data))
    with pytest.raises(StoreError, match='owner_check_argv'):
        load_coordinator_config(tmp_path)
