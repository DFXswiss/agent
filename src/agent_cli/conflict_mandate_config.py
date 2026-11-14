"""Explicit device-local configuration for comment-mandated conflict resolution."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .store import StoreError


@dataclass(frozen=True)
class ConflictRepository:
    repo: str
    check_argv: tuple[str, ...]


@dataclass(frozen=True)
class ConflictWorker:
    session_id: str
    workspace_root: Path
    repositories: dict[str, ConflictRepository]
    request_logins: tuple[str, ...]
    poll_seconds: int
    lane_timeout: int
    check_timeout: int
    max_rebase_steps: int


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\0\r\n'):
        raise StoreError(f'{label} requires a nonempty single-line string')
    return value


def validate_repository_name(value: object) -> str:
    name = _text(value, 'repository')
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+', name):
        raise StoreError('repository must be owner/name')
    if name.split('/')[1] in {'.', '..'}:
        raise StoreError('repository must be owner/name')
    return name


def _argv(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise StoreError(f'{label} requires an explicit nonempty argv array')
    return tuple(_text(part, label) for part in value)


def _at_least(value: object, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StoreError(f'{label} requires an integer >= {minimum}')
    return value


def _request_logins(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise StoreError('request_logins requires explicit GitHub logins')
    logins = tuple(_text(v, 'request login').casefold() for v in value)
    if any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?', login)
        or '--' in login
        for login in logins
    ):
        raise StoreError('invalid request login')
    if len(set(logins)) != len(logins):
        raise StoreError('request_logins must be distinct')
    return logins


def load_conflict_config(home: Path) -> dict[str, ConflictWorker]:
    """Missing/empty configuration enables no conflict worker."""
    path = home / 'conflicts.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise StoreError('Cannot read conflicts.json') from exc
    if not isinstance(data, dict) or set(data) - {'workers'}:
        raise StoreError('conflicts.json accepts only workers')
    raw = data.get('workers')
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise StoreError('workers must be an object or null')
    result: dict[str, ConflictWorker] = {}
    for sid, item in raw.items():
        sid = _text(sid, 'session_id')
        required = {
            'workspace_root',
            'repositories',
            'request_logins',
            'poll_seconds',
            'lane_timeout',
            'check_timeout',
            'max_rebase_steps',
        }
        if not isinstance(item, dict):
            raise StoreError('each worker must be an object')
        missing = sorted(required - set(item))
        unknown = sorted(set(item) - required)
        if missing:
            raise StoreError(f"worker requires {', '.join(missing)}")
        if unknown:
            raise StoreError(f"worker rejects unknown field {', '.join(unknown)}")
        root = Path(_text(item['workspace_root'], 'workspace_root'))
        if not root.is_absolute() or '..' in root.parts:
            raise StoreError('workspace_root must be absolute without parent traversal')
        repos_raw = item['repositories']
        if not isinstance(repos_raw, dict) or not repos_raw:
            raise StoreError('repositories requires at least one explicit repository')
        repositories: dict[str, ConflictRepository] = {}
        for repo, entry in repos_raw.items():
            repo = validate_repository_name(repo)
            if not isinstance(entry, dict):
                raise StoreError('repository entry must be an object')
            if 'check_argv' not in entry:
                raise StoreError('repository requires check_argv')
            unknown_repo = sorted(set(entry) - {'check_argv'})
            if unknown_repo:
                raise StoreError(f"repository rejects unknown field {', '.join(unknown_repo)}")
            repositories[repo] = ConflictRepository(
                repo=repo,
                check_argv=_argv(entry['check_argv'], 'check_argv'),
            )
        max_rebase_steps = _at_least(
            item['max_rebase_steps'], 'max_rebase_steps', 1
        )
        if max_rebase_steps > 200:
            raise StoreError('max_rebase_steps requires an integer >= 1 and <= 200')
        result[sid] = ConflictWorker(
            session_id=sid,
            workspace_root=root.resolve(),
            repositories=repositories,
            request_logins=_request_logins(item['request_logins']),
            poll_seconds=_at_least(item['poll_seconds'], 'poll_seconds', 30),
            lane_timeout=_at_least(item['lane_timeout'], 'lane_timeout', 60),
            check_timeout=_at_least(item['check_timeout'], 'check_timeout', 60),
            max_rebase_steps=max_rebase_steps,
        )
    return result


__all__ = [
    'ConflictRepository',
    'ConflictWorker',
    'load_conflict_config',
    'validate_repository_name',
]
