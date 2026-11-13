"""Explicit device-local configuration for the script-owned issue coordinator."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import StoreError


_GITHUB_LOGIN = re.compile(
    r'[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}'
)
DEFAULT_PARK_POLL_SECONDS = 120
DEFAULT_LANE_RETRIES = 2
DEFAULT_MAX_ROUNDS = 10


@dataclass(frozen=True)
class RepositoryConfig:
    repo: str
    base: str
    publication_repo: str
    check_argv: tuple[str, ...]
    readiness_argv: tuple[str, ...]


@dataclass(frozen=True)
class WorkerConfig:
    session_id: str
    review_session: str | None
    workspace_root: Path
    repositories: dict[str, RepositoryConfig]
    reply_logins: tuple[str, ...]
    poll_seconds: int
    lane_timeout: int
    check_timeout: int
    repository_owners: tuple[str, ...] = ()
    owner_check_argv: tuple[str, ...] = ()
    owner_readiness_argv: tuple[str, ...] | None = None
    require_hub_assignment: bool = False
    a38: bool = False
    second_reviewers: tuple[str, ...] = ()
    second_review_timeout_seconds: int = 86400
    park_poll_seconds: int = DEFAULT_PARK_POLL_SECONDS
    lane_retries: int = DEFAULT_LANE_RETRIES
    orders: bool = False
    max_rounds: int | None = None
    max_rounds_by_model: dict[str, int] = field(default_factory=dict)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\0\r\n'):
        raise StoreError(f'{label} requires a nonempty single-line string')
    return value


def _repo(value: object) -> str:
    name = _text(value, 'repository')
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+', name):
        raise StoreError('repository must be owner/name')
    if name.split('/')[1] in {'.', '..'}:
        raise StoreError('repository must be owner/name')
    return name


def _github_login(value: object, label: str, error: str) -> str:
    login = _text(value, label).casefold()
    if _GITHUB_LOGIN.fullmatch(login) is None:
        raise StoreError(error)
    return login


def _owner(value: object) -> str:
    return _github_login(value, 'repository owner', 'invalid repository owner')


def validate_base(value: object) -> str:
    base = _text(value, 'base')
    if (base.startswith('-') or base == '@' or base.endswith('.')
            or any(ord(c) < 32 or ord(c) == 127 or c in ' ~^:?*[\\' for c in base)
            or '..' in base or '@{' in base
            or any(not part or part.startswith('.') or part.endswith('.lock')
                   for part in base.split('/'))):
        raise StoreError('invalid base branch')
    return base


def _argv(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise StoreError(f'{label} requires an explicit nonempty argv array')
    return tuple(_text(part, label) for part in value)


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StoreError(f'{label} requires an explicit positive integer')
    return value


def _at_least(value: object, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StoreError(f'{label} requires an integer >= {minimum}')
    return value


def load_coordinator_config(home: Path) -> dict[str, WorkerConfig]:
    """Missing/empty configuration enables no worker, account or role."""
    path = home / 'coordinator.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise StoreError('Cannot read coordinator.json') from exc
    if not isinstance(data, dict) or set(data) - {'workers'}:
        raise StoreError('coordinator.json accepts only workers')
    raw = data.get('workers')
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise StoreError('workers must be an object or null')
    result = {}
    roots: set[Path] = set()
    for sid, item in raw.items():
        sid = _text(sid, 'session_id')
        required = {'workspace_root', 'reply_logins',
                    'poll_seconds', 'lane_timeout', 'check_timeout'}
        optional = {'review_session', 'repositories', 'repository_owners',
                    'owner_check_argv', 'owner_readiness_argv',
                    'require_hub_assignment', 'a38', 'second_reviewers',
                    'second_review_timeout_seconds', 'park_poll_seconds',
                    'lane_retries', 'orders', 'max_rounds',
                    'max_rounds_by_model'}
        if not isinstance(item, dict) or set(item) - required - optional or not required <= set(item):
            raise StoreError('each worker requires explicit workspace, replies and timing')
        a38 = item.get('a38', False)
        if not isinstance(a38, bool):
            raise StoreError('a38 must be a boolean')
        orders = item.get('orders', False)
        if not isinstance(orders, bool):
            raise StoreError('orders must be a boolean')
        review_raw = item.get('review_session')
        if not a38 and review_raw is None:
            raise StoreError('non-A38 workers require an explicit review_session')
        review = _text(review_raw, 'review_session') if review_raw is not None else None
        if not a38 and review == sid:
            raise StoreError('formal review requires a separately configured session')
        root = Path(_text(item['workspace_root'], 'workspace_root'))
        if not root.is_absolute() or '..' in root.parts:
            raise StoreError('workspace_root must be absolute without parent traversal')
        root = root.resolve()
        if any(root == other or root.is_relative_to(other) or other.is_relative_to(root) for other in roots):
            raise StoreError('worker workspace roots must not overlap')
        roots.add(root)
        replies = item['reply_logins']
        if not isinstance(replies, list) or not replies:
            raise StoreError('reply_logins requires explicit GitHub respondents')
        logins = tuple(_text(v, 'reply login').casefold() for v in replies)
        if any(not re.fullmatch(r'[a-z0-9-]+', v) for v in logins):
            raise StoreError('invalid reply login')
        reviewers_raw = item.get('second_reviewers')
        if a38 and (not isinstance(reviewers_raw, list) or not reviewers_raw):
            raise StoreError('A38 workers require nonempty second_reviewers')
        if reviewers_raw is None:
            reviewers_raw = []
        if not isinstance(reviewers_raw, list):
            raise StoreError('second_reviewers must be a list')
        reviewers = tuple(
            _github_login(v, 'second reviewer', 'invalid second reviewer login')
            for v in reviewers_raw
        )
        if len(set(reviewers)) != len(reviewers):
            raise StoreError('second_reviewers must be distinct')
        repos_raw = item.get('repositories', {})
        if not isinstance(repos_raw, dict):
            raise StoreError('repositories must be an object')
        repos = {}
        for repo, entry in repos_raw.items():
            repo = _repo(repo)
            if not isinstance(entry, dict) or set(entry) != {'base', 'publication_repo', 'check_argv', 'readiness_argv'}:
                raise StoreError('repository requires base, publication_repo, check_argv and readiness_argv')
            base = validate_base(entry['base'])
            repos[repo] = RepositoryConfig(repo, base, _repo(entry['publication_repo']),
                                          _argv(entry['check_argv'], 'check_argv', allow_empty=a38),
                                          _argv(entry['readiness_argv'], 'readiness_argv'))
        owners_raw = item.get('repository_owners', [])
        if not isinstance(owners_raw, list):
            raise StoreError('repository_owners must be a list')
        owners = tuple(_owner(value) for value in owners_raw)
        if len(set(owners)) != len(owners):
            raise StoreError('repository_owners must be distinct')
        if not repos and not owners:
            raise StoreError('worker requires repositories or repository_owners')
        owner_checks = _argv(
            item.get('owner_check_argv', []),
            'owner_check_argv',
            allow_empty=a38 if owners else True,
        )
        readiness_raw = item.get('owner_readiness_argv')
        owner_readiness = (
            _argv(readiness_raw, 'owner_readiness_argv')
            if readiness_raw is not None else None
        )
        if owners and not a38 and owner_readiness is None:
            raise StoreError('non-A38 owner-scoped workers require owner_readiness_argv')
        require_hub = item.get('require_hub_assignment', False)
        if not isinstance(require_hub, bool):
            raise StoreError('require_hub_assignment must be a boolean')
        max_rounds = (
            _at_least(item['max_rounds'], 'max_rounds', 1)
            if 'max_rounds' in item else None
        )
        raw = item.get('max_rounds_by_model', {})
        if not isinstance(raw, dict):
            raise StoreError('max_rounds_by_model must be an object')
        by_model = {
            _text(key, 'max_rounds_by_model'): _at_least(
                value, 'max_rounds_by_model', 1
            )
            for key, value in raw.items()
        }
        result[sid] = WorkerConfig(sid, review, root, repos, logins,
                                   _positive(item['poll_seconds'], 'poll_seconds'),
                                   _positive(item['lane_timeout'], 'lane_timeout'),
                                   _positive(item['check_timeout'], 'check_timeout'),
                                   owners,
                                   owner_checks,
                                   owner_readiness,
                                   require_hub,
                                   a38,
                                   reviewers,
                                   _at_least(item.get('second_review_timeout_seconds', 86400),
                                             'second_review_timeout_seconds', 600),
                                   park_poll_seconds=_at_least(
                                       item.get('park_poll_seconds', DEFAULT_PARK_POLL_SECONDS),
                                       'park_poll_seconds',
                                       0,
                                   ),
                                   lane_retries=_at_least(
                                       item.get('lane_retries', DEFAULT_LANE_RETRIES),
                                       'lane_retries',
                                       0,
                                   ),
                                   orders=orders,
                                   max_rounds=max_rounds,
                                   max_rounds_by_model=by_model)
    return result


def resolve_round_limit(
    worker: WorkerConfig, model: str, submitted: int | None = None
) -> dict[str, Any]:
    """The round limit of a task and where it came from."""
    from .lane import LANE_MODEL_LIMIT, redact_lane_text

    if submitted is not None:
        value, origin = submitted, "submit"
    elif model in worker.max_rounds_by_model:
        value, origin = worker.max_rounds_by_model[model], "model"
    elif worker.max_rounds is not None:
        value, origin = worker.max_rounds, "worker"
    else:
        value, origin = DEFAULT_MAX_ROUNDS, "default"
    return {
        "max_rounds": value,
        "origin": origin,
        "model": redact_lane_text(model, limit=LANE_MODEL_LIMIT),
        "limit": value,
    }
