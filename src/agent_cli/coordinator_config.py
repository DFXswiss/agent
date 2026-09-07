"""Explicit device-local configuration for the script-owned issue coordinator."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .store import StoreError


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
    review_session: str
    workspace_root: Path
    repositories: dict[str, RepositoryConfig]
    reply_logins: tuple[str, ...]
    poll_seconds: int
    lane_timeout: int
    check_timeout: int


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


def _argv(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise StoreError(f'{label} requires an explicit nonempty argv array')
    return tuple(_text(part, label) for part in value)


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StoreError(f'{label} requires an explicit positive integer')
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
        fields = {'review_session', 'workspace_root', 'repositories', 'reply_logins',
                  'poll_seconds', 'lane_timeout', 'check_timeout'}
        if not isinstance(item, dict) or set(item) != fields:
            raise StoreError('each worker requires explicit review session, workspace, repositories, replies and timing')
        review = _text(item['review_session'], 'review_session')
        if review == sid:
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
        repos_raw = item['repositories']
        if not isinstance(repos_raw, dict) or not repos_raw:
            raise StoreError('repositories must be an explicit nonempty object')
        repos = {}
        for repo, entry in repos_raw.items():
            repo = _repo(repo)
            if not isinstance(entry, dict) or set(entry) != {'base', 'publication_repo', 'check_argv', 'readiness_argv'}:
                raise StoreError('repository requires base, publication_repo, check_argv and readiness_argv')
            base = _text(entry['base'], 'base')
            if (base.startswith('-') or base == '@' or base.endswith('.')
                    or any(ord(c) < 32 or ord(c) == 127 or c in ' ~^:?*[\\' for c in base)
                    or '..' in base or '@{' in base
                    or any(not part or part.startswith('.') or part.endswith('.lock')
                           for part in base.split('/'))):
                raise StoreError('invalid base branch')
            repos[repo] = RepositoryConfig(repo, base, _repo(entry['publication_repo']),
                                          _argv(entry['check_argv'], 'check_argv'),
                                          _argv(entry['readiness_argv'], 'readiness_argv'))
        result[sid] = WorkerConfig(sid, review, root, repos, logins,
                                   _positive(item['poll_seconds'], 'poll_seconds'),
                                   _positive(item['lane_timeout'], 'lane_timeout'),
                                   _positive(item['check_timeout'], 'check_timeout'))
    return result
