"""The in-memory GitHub of the double: users and their tokens, App installations, repositories with git objects,
refs, pull requests and issue comments, and the seeding format.

Seeding (``POST /_double/seed`` or ``--seed FILE``) adds to what exists; names already present are refused, a
reset (``POST /_double/reset``) empties everything. Format (every key optional)::

    {"users": [{"login": "octo-user", "id": 101, "name": "Octo User", "email": null, "tokens": ["<token>"],
                "personal_tokens": ["<token>"], "collaborator": ["octo-org/hello"]}],
     "installations": [{"id": 31, "account": "octo-org", "account_type": "Organization",
                        "repository_selection": "selected", "repositories": ["octo-org/hello"],
                        "users": ["octo-user"], "permissions": {"contents": "write", ...}}],
     "repositories": [{"full_name": "octo-org/hello", "private": true, "default_branch": "main",
                       "description": null,
                       "branches": [{"name": "main", "commits": [{"message": "Initial commit.",
                                     "files": {"README.md": "hello\\n", "run.sh": {"content": "...", "mode": "100755"},
                                               "logo.bin": {"base64": "..."}, "link": {"target": "README.md"}}}]},
                                    {"name": "feature", "from": "main", "commits": [{"files": {"README.md": null}}]}],
                       "tags": {"v1.0": "main"},
                       "issues": [{"title": "...", "user": "octo-user"}],
                       "pulls": [{"title": "...", "head": "feature", "base": "main", "draft": true,
                                  "user": "octo-user"}],
                       "comments": [{"number": 1, "body": "...", "user": "octo-user"}]}],
     "device_flow": {"client_id": "...", "interval": 5, "expires_in": 900},
     "faults": [...], "expectations": [...]}

Issues (only a number, a title and an author: targets for comments) and pull requests share one number sequence per
repository, issues first. Commit ``files`` are changes against the parent (``null`` deletes a path); a branch
without ``from`` starts empty. An installation whose account is a user that is not listed in ``users`` is visible to
that user; repositories of an installation with ``repository_selection: "all"`` are all repositories of its account.
A repository is accessible with a token when one of the installations visible to the token's user covers it;
anything else is ``404``. ``personal_tokens`` are tokens without the GitHub App (classic personal access tokens,
KOR-27): they reach the repositories the user owns and those named in ``collaborator`` (with write access; a name
that is not seeded is simply not reachable), and GitHub refuses them the installation routes.

Time is logical: seeded and created objects take timestamps from `World.stamp`, which starts at ``base_time`` and
advances one second per object, so object ids are deterministic.
"""

from __future__ import annotations

import base64
import difflib
import gzip
import io
import re
import tarfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_doubles.github.objects import (
    BLOB_MODES,
    EXEC_MODE,
    FILE_MODE,
    LINK_MODE,
    Commit,
    ObjectError,
    ObjectStore,
    Signature,
)

BASE_TIME = 1767225600  # 2026-01-01T00:00:00Z
DEFAULT_PERMISSIONS = {"contents": "write", "issues": "write", "metadata": "read", "pull_requests": "write"}
PERMISSION_LEVELS = frozenset({"read", "write"})
SELECTIONS = frozenset({"all", "selected"})
ACCOUNT_TYPES = frozenset({"User", "Organization"})
PULL_STATES = frozenset({"open", "closed"})
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class SeedError(ValueError):
    """Seed data the double refuses (the control route answers 400)."""


def iso(seconds: int) -> str:
    """A timestamp as GitHub writes it (``2026-01-01T00:00:00Z``)."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> int:
    """Seconds since the epoch of an ISO 8601 timestamp (``Z`` or an offset)."""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SeedError(f"invalid timestamp {text!r}") from exc
    if moment.tzinfo is None:
        raise SeedError(f"timestamp without time zone {text!r}")
    return int(moment.timestamp())


# --- typed access to decoded JSON --------------------------------------------------------------------


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SeedError(f"{what} must be an object")
    return value


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise SeedError(f"{what} must be a list")
    return value


def _text(data: Mapping[str, Any], key: str, what: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise SeedError(f"{what}.{key} must be a string")
    return value


def _optional_text(data: Mapping[str, Any], key: str, what: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise SeedError(f"{what}.{key} must be a string or null")
    return value


def _int(data: Mapping[str, Any], key: str, what: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SeedError(f"{what}.{key} must be a non-negative integer")
    return value


def _bool(data: Mapping[str, Any], key: str, what: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SeedError(f"{what}.{key} must be a boolean")
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SeedError(f"{what}: unknown keys {unknown}")


# --- the model ---------------------------------------------------------------------------------------


@dataclass
class User:
    login: str
    id: int
    name: str | None
    email: str | None
    type: str = "User"
    collaborator: list[str] = field(default_factory=list)


@dataclass
class Installation:
    id: int
    account: str
    account_id: int
    account_type: str
    selection: str
    repositories: list[str]
    users: set[str]
    permissions: dict[str, str]
    created: int


@dataclass
class Pull:
    number: int
    id: int
    title: str
    body: str | None
    head: str
    base: str
    draft: bool
    state: str
    user: str
    created: int


@dataclass
class Issue:
    number: int
    title: str
    user: str


@dataclass
class Comment:
    id: int
    number: int
    body: str
    user: str
    created: int


@dataclass
class Repo:
    owner: str
    name: str
    id: int
    owner_id: int
    owner_type: str
    private: bool
    default_branch: str
    description: str | None
    created: int
    objects: ObjectStore = field(default_factory=ObjectStore)
    refs: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, Pull] = field(default_factory=dict)
    issues: dict[int, Issue] = field(default_factory=dict)
    comments: list[Comment] = field(default_factory=list)
    next_number: int = 1

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return self.full_name.lower()

    # --- refs and commits -----------------------------------------------------------------------

    def branch(self, name: str) -> str | None:
        """The head commit of branch `name`, None when it does not exist."""
        return self.refs.get(f"refs/heads/{name}")

    def resolve(self, ref: str) -> str | None:
        """The commit `ref` names: a branch, a tag, ``heads/…``/``tags/…``/``refs/…``, a full or abbreviated SHA."""
        for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/{ref}", ref):
            if candidate in self.refs:
                return self.refs[candidate]
        if SHORT_SHA_RE.match(ref):
            matches = [sha for sha in self.objects.commit_ids() if sha.startswith(ref)]
            if len(matches) == 1:
                return matches[0]
        return None

    def ancestors(self, sha: str) -> set[str]:
        """`sha` and every commit reachable from it."""
        seen: set[str] = set()
        todo = [sha]
        while todo:
            current = todo.pop()
            if current not in seen:
                seen.add(current)
                todo.extend(self.objects.commit(current).parents)
        return seen

    def history(self, sha: str, stop: set[str]) -> list[str]:
        """Commits reachable from `sha` but not in `stop`, oldest first (by commit time, then id)."""
        reachable = self.ancestors(sha) - stop
        return sorted(reachable, key=lambda c: (self.objects.commit(c).committer.time, c))

    def merge_base(self, a: str, b: str) -> str | None:
        """A best common ancestor of `a` and `b` (one that is no ancestor of another common ancestor; the newest by
        committer time, then id, when there are several), None for unrelated histories."""
        common = self.ancestors(a) & self.ancestors(b)
        below = set().union(*(self.ancestors(c) - {c} for c in common))
        best = common - below
        if not best:
            return None
        return max(best, key=lambda c: (self.objects.commit(c).committer.time, c))

    def tree_of(self, commit: str) -> str:
        return self.objects.commit(commit).tree


@dataclass(frozen=True)
class FileChange:
    """One changed file between two trees, in the form of the compare and pull-files routes."""

    path: str
    status: str
    sha: str | None
    additions: int
    deletions: int
    patch: str | None


NO_NEWLINE = "\\ No newline at end of file"


def _lines(data: bytes) -> list[str] | None:
    """The lines of a blob with their line ends (split at ``\\n`` only), None for binary content."""
    if b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parts = text.split("\n")
    return [f"{part}\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def unified_patch(old: list[str], new: list[str]) -> tuple[str | None, int, int]:
    """The hunks GitHub shows as ``patch`` (no file header, three context lines, the no-newline marker), with the
    numbers of added and deleted lines; None when nothing differs."""
    out: list[str] = []
    additions = deletions = 0
    for line in list(difflib.unified_diff(old, new, n=3))[2:]:
        additions += line.startswith("+")
        deletions += line.startswith("-")
        if line.endswith("\n"):
            out.append(line[:-1])
        else:
            out += [line, NO_NEWLINE]
    return ("\n".join(out) if out else None), additions, deletions


def diff_trees(objects: ObjectStore, old_tree: str, new_tree: str) -> list[FileChange]:
    """The changed files from `old_tree` to `new_tree`, by path (``added``, ``removed``, ``modified`` or ``changed``
    for a mode change); binary files have no patch and count no lines."""
    old = objects.files(old_tree)
    new = objects.files(new_tree)
    changes: list[FileChange] = []
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif before.sha == after.sha:
            status = "changed"
        else:
            status = "modified"
        current = after or before
        assert current is not None
        a = _lines(objects.blob(before.sha)) if before else []
        b = _lines(objects.blob(after.sha)) if after else []
        if a is None or b is None:
            changes.append(FileChange(path, status, current.sha, 0, 0, None))
            continue
        patch, additions, deletions = unified_patch(a, b)
        changes.append(FileChange(path, status, current.sha, additions, deletions, patch))
    return changes


def tarball(repo: Repo, commit: str) -> bytes:
    """The gzipped tar archive GitHub serves for `commit`: top directory ``<owner>-<repo>-<sha7>/``, a pax global
    header with the commit id as comment, directories 0775, files 0664 (executables 0775), links as symbolic links,
    owner root, every mtime the committer time."""
    objects = repo.objects
    stamp = objects.commit(commit).committer.time
    top = f"{repo.owner}-{repo.name}-{commit[:7]}"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as archive:

        def member(name: str, kind: bytes, mode: int, size: int = 0, link: str = "") -> tarfile.TarInfo:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.size, info.linkname, info.mtime = kind, mode, size, link, stamp
            info.uname = info.gname = "root"
            return info

        archive.addfile(member(top, tarfile.DIRTYPE, 0o775))
        for path, entry in objects.walk(objects.commit(commit).tree):
            name = f"{top}/{path}"
            if entry.is_tree:
                archive.addfile(member(name, tarfile.DIRTYPE, 0o775))
            elif entry.mode == LINK_MODE:
                target = objects.blob(entry.sha).decode("utf-8", "replace")
                archive.addfile(member(name, tarfile.SYMTYPE, 0o777, link=target))
            else:
                data = objects.blob(entry.sha)
                mode = 0o775 if entry.mode == EXEC_MODE else 0o664
                archive.addfile(member(name, tarfile.REGTYPE, mode, len(data)), io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)


class World:
    """Everything the double knows. Callers hold `lock` around reads and writes of a request."""

    def __init__(self, base_time: int = BASE_TIME) -> None:
        self.lock = threading.RLock()
        self.base_time = base_time
        self.clear()

    def clear(self) -> None:
        """Forget everything and restart the counters (the lock stays, so concurrent requests keep one lock)."""
        with self.lock:
            self.users: dict[str, User] = {}
            self.tokens: dict[str, str] = {}
            self.personal: set[str] = set()
            self.installations: dict[int, Installation] = {}
            self.repos: dict[str, Repo] = {}
            self._tick = 0
            self._ids = 1000

    # --- counters -----------------------------------------------------------------------------------

    def stamp(self) -> int:
        """The next logical timestamp (one second after the previous one)."""
        self._tick += 1
        return self.base_time + self._tick

    def new_id(self) -> int:
        """The next numeric id (users, repositories, installations, pull requests, comments)."""
        self._ids += 1
        return self._ids

    # --- lookups ------------------------------------------------------------------------------------

    def user_for_token(self, token: str) -> User | None:
        login = self.tokens.get(token)
        return self.users[login] if login is not None else None

    def add_token(self, login: str, token: str) -> None:
        self.tokens[token] = login

    def revoke_token(self, token: str) -> bool:
        """Forget a token (``POST /credentials/revoke``); True when it was known."""
        self.personal.discard(token)
        return self.tokens.pop(token, None) is not None

    def is_personal(self, token: str) -> bool:
        return token in self.personal

    def personal_repos(self, user: User) -> list[Repo]:
        """The repositories a personal token of `user` reaches (own and `collaborator`), by full name."""
        named = {name.lower() for name in user.collaborator}
        found = [repo for repo in self.repos.values() if repo.owner.lower() == user.login.lower() or repo.key in named]
        return sorted(found, key=lambda repo: repo.key)

    def account_id(self, login: str) -> int:
        """The id of user `login`, or a stable id for an organisation the seed only names."""
        user = self.users.get(login.lower())
        if user is not None:
            return user.id
        return 900000 + sum(login.lower().encode())

    def visible_installations(self, user: User) -> list[Installation]:
        """Installations `user` can see (listed as user, or installed on the user's own account), by id."""
        return sorted(
            (
                inst
                for inst in self.installations.values()
                if user.login in inst.users
                or (inst.account_type == "User" and inst.account.lower() == user.login.lower())
            ),
            key=lambda inst: inst.id,
        )

    def installation_repos(self, inst: Installation) -> list[Repo]:
        """The repositories an installation covers, by full name."""
        if inst.selection == "all":
            found = [repo for repo in self.repos.values() if repo.owner.lower() == inst.account.lower()]
        else:
            found = [self.repos[name] for name in inst.repositories if name in self.repos]
        return sorted(found, key=lambda repo: repo.key)

    def installation_for(self, user: User, repo: Repo) -> Installation | None:
        """The first installation visible to `user` that covers `repo`."""
        for inst in self.visible_installations(user):
            if repo in self.installation_repos(inst):
                return inst
        return None

    def repo(self, owner: str, name: str) -> Repo | None:
        return self.repos.get(f"{owner}/{name}".lower())

    # --- seeding ------------------------------------------------------------------------------------

    def seed(self, payload: Any) -> None:
        """Add seed data (see the module docstring); device flow, faults and expectations are read by the server."""
        data = _object(payload, "seed")
        _check_keys(data, {"users", "installations", "repositories", "device_flow", "faults", "expectations"}, "seed")
        saved = (dict(self.users), dict(self.tokens), dict(self.installations), dict(self.repos), self._tick, self._ids)
        personal = set(self.personal)
        try:
            for raw in _list(data.get("users", []), "users"):
                self._seed_user(_object(raw, "user"))
            for raw in _list(data.get("repositories", []), "repositories"):
                self._seed_repo(_object(raw, "repository"))
            for raw in _list(data.get("installations", []), "installations"):
                self._seed_installation(_object(raw, "installation"))
        except SeedError:
            # A refused seed leaves nothing behind (seeding only adds new objects, so restoring the tables suffices).
            self.users, self.tokens, self.installations, self.repos, self._tick, self._ids = saved
            self.personal = personal
            raise

    def _seed_user(self, data: dict[str, Any]) -> None:
        _check_keys(data, {"login", "id", "name", "email", "tokens", "personal_tokens", "collaborator"}, "user")
        login = _text(data, "login", "user")
        if not LOGIN_RE.match(login):
            raise SeedError(f"invalid login {login!r}")
        if login.lower() in self.users:
            raise SeedError(f"user {login} already exists")
        user = User(
            login,
            _int(data, "id", "user", self.new_id()),
            _optional_text(data, "name", "user"),
            _optional_text(data, "email", "user"),
        )
        for name in _list(data.get("collaborator", []), "user.collaborator"):
            if not isinstance(name, str) or "/" not in name:
                raise SeedError("user.collaborator must name repositories as owner/name")
            user.collaborator.append(name)
        self.users[login.lower()] = user
        for key in ("tokens", "personal_tokens"):
            for token in _list(data.get(key, []), f"user.{key}"):
                if not isinstance(token, str) or not token:
                    raise SeedError(f"user.{key} must be non-empty strings")
                if token in self.tokens:
                    raise SeedError("a token belongs to one user only")
                self.add_token(user.login.lower(), token)
                if key == "personal_tokens":
                    self.personal.add(token)

    def _seed_installation(self, data: dict[str, Any]) -> None:
        _check_keys(
            data,
            {"id", "account", "account_type", "repository_selection", "repositories", "users", "permissions"},
            "installation",
        )
        inst_id = _int(data, "id", "installation", self.new_id())
        if inst_id in self.installations:
            raise SeedError(f"installation {inst_id} already exists")
        account = _text(data, "account", "installation")
        account_type = _text(
            data, "account_type", "installation", "User" if account.lower() in self.users else "Organization"
        )
        if account_type not in ACCOUNT_TYPES:
            raise SeedError(f"installation.account_type must be one of {sorted(ACCOUNT_TYPES)}")
        selection = _text(data, "repository_selection", "installation", "selected")
        if selection not in SELECTIONS:
            raise SeedError(f"installation.repository_selection must be one of {sorted(SELECTIONS)}")
        repositories = []
        for name in _list(data.get("repositories", []), "installation.repositories"):
            if not isinstance(name, str) or name.lower() not in self.repos:
                raise SeedError(f"installation repository {name!r} is not seeded")
            repositories.append(name.lower())
        users = set()
        for login in _list(data.get("users", []), "installation.users"):
            if not isinstance(login, str) or login.lower() not in self.users:
                raise SeedError(f"installation user {login!r} is not seeded")
            users.add(self.users[login.lower()].login)
        permissions = dict(DEFAULT_PERMISSIONS)
        for key, level in _object(data.get("permissions", {}), "installation.permissions").items():
            if level not in PERMISSION_LEVELS:
                raise SeedError(f"installation.permissions.{key} must be read or write")
            permissions[key] = level
        self.installations[inst_id] = Installation(
            inst_id,
            account,
            self.account_id(account),
            account_type,
            selection,
            repositories,
            users,
            permissions,
            self.stamp(),
        )

    def _seed_repo(self, data: dict[str, Any]) -> None:
        _check_keys(
            data,
            {
                "full_name",
                "id",
                "private",
                "default_branch",
                "description",
                "branches",
                "tags",
                "pulls",
                "issues",
                "comments",
            },
            "repository",
        )
        full_name = _text(data, "full_name", "repository")
        owner, sep, name = full_name.partition("/")
        if not sep or not LOGIN_RE.match(owner) or not REPO_NAME_RE.match(name):
            raise SeedError(f"invalid repository name {full_name!r}")
        if full_name.lower() in self.repos:
            raise SeedError(f"repository {full_name} already exists")
        owner_type = "User" if owner.lower() in self.users else "Organization"
        repo = Repo(
            owner,
            name,
            _int(data, "id", "repository", self.new_id()),
            self.account_id(owner),
            owner_type,
            _bool(data, "private", "repository", True),
            _text(data, "default_branch", "repository", "main"),
            _optional_text(data, "description", "repository"),
            self.stamp(),
        )
        for raw in _list(data.get("branches", []), "repository.branches"):
            self._seed_branch(repo, _object(raw, "branch"))
        for tag, target in _object(data.get("tags", {}), "repository.tags").items():
            sha = repo.resolve(target) if isinstance(target, str) else None
            if sha is None:
                raise SeedError(f"tag {tag} points to an unknown ref")
            repo.refs[f"refs/tags/{tag}"] = sha
        self.repos[repo.key] = repo
        for raw in _list(data.get("issues", []), "repository.issues"):
            issue = _object(raw, "issue")
            _check_keys(issue, {"title", "user"}, "issue")
            self.add_issue(repo, _text(issue, "title", "issue"), self._seeded_login(_text(issue, "user", "issue")))
        for raw in _list(data.get("pulls", []), "repository.pulls"):
            self._seed_pull(repo, _object(raw, "pull"))
        for raw in _list(data.get("comments", []), "repository.comments"):
            self._seed_comment(repo, _object(raw, "comment"))

    def _seed_branch(self, repo: Repo, data: dict[str, Any]) -> None:
        _check_keys(data, {"name", "from", "commits"}, "branch")
        name = _text(data, "name", "branch")
        if not name or repo.branch(name) is not None:
            raise SeedError(f"branch {name!r} is empty or already exists")
        head: str | None = None
        if "from" in data:
            head = repo.resolve(_text(data, "from", "branch"))
            if head is None:
                raise SeedError(f"branch {name}: unknown start {data['from']!r}")
        for raw in _list(data.get("commits", []), "branch.commits"):
            head = self._seed_commit(repo, head, _object(raw, "commit"))
        if head is None:
            raise SeedError(f"branch {name} has no commit")
        repo.refs[f"refs/heads/{name}"] = head

    def _seed_commit(self, repo: Repo, parent: str | None, data: dict[str, Any]) -> str:
        _check_keys(data, {"message", "files", "author"}, "commit")
        objects = repo.objects
        files: dict[str, tuple[str, str]] = {}
        if parent is not None:
            files = {path: (f.mode, f.sha) for path, f in objects.files(repo.tree_of(parent)).items()}
        for path, spec in _object(data.get("files", {}), "commit.files").items():
            if spec is None:
                if path not in files:
                    raise SeedError(f"cannot delete missing path {path!r}")
                del files[path]
                continue
            files[path] = self._seed_file(objects, path, spec)
        try:
            tree = objects.build(files)
        except ObjectError as exc:
            raise SeedError(str(exc)) from exc
        when = self.stamp()
        author = self._signature(data.get("author"), when)
        commit = Commit(
            tree, (parent,) if parent else (), author, author, _text(data, "message", "commit", "Seed commit.")
        )
        return objects.put_commit(commit)

    @staticmethod
    def _seed_file(objects: ObjectStore, path: str, spec: Any) -> tuple[str, str]:
        if any(not part or part in {".", ".."} for part in path.split("/")):
            raise SeedError(f"invalid path {path!r}")
        if isinstance(spec, str):
            return FILE_MODE, objects.put_blob(spec.encode())
        spec = _object(spec, f"file {path}")
        _check_keys(spec, {"content", "base64", "target", "mode"}, f"file {path}")
        if "target" in spec:
            return LINK_MODE, objects.put_blob(_text(spec, "target", path).encode())
        mode = _text(spec, "mode", path, FILE_MODE)
        if mode not in BLOB_MODES - {LINK_MODE}:
            raise SeedError(f"file {path}: mode must be {FILE_MODE} or {EXEC_MODE}")
        if "base64" in spec:
            try:
                data = base64.b64decode(_text(spec, "base64", path), validate=True)
            except ValueError as exc:
                raise SeedError(f"file {path}: invalid base64") from exc
        else:
            data = _text(spec, "content", path).encode()
        return mode, objects.put_blob(data)

    def _signature(self, raw: Any, when: int) -> Signature:
        if raw is None:
            return Signature("Double Seed", "seed@example.invalid", when)
        data = _object(raw, "author")
        _check_keys(data, {"name", "email", "date"}, "author")
        stamp = parse_iso(_text(data, "date", "author")) if "date" in data else when
        return Signature(_text(data, "name", "author"), _text(data, "email", "author"), stamp)

    def _seed_pull(self, repo: Repo, data: dict[str, Any]) -> None:
        _check_keys(data, {"title", "body", "head", "base", "draft", "state", "user"}, "pull")
        head, base = _text(data, "head", "pull"), _text(data, "base", "pull", repo.default_branch)
        if repo.branch(head) is None or repo.branch(base) is None:
            raise SeedError(f"pull {head} → {base}: unknown branch")
        state = _text(data, "state", "pull", "open")
        if state not in PULL_STATES:
            raise SeedError("pull.state must be open or closed")
        user = self._seeded_login(_text(data, "user", "pull"))
        self.add_pull(
            repo,
            title=_text(data, "title", "pull"),
            body=_optional_text(data, "body", "pull"),
            head=head,
            base=base,
            draft=_bool(data, "draft", "pull", False),
            user=user,
            state=state,
        )

    def _seed_comment(self, repo: Repo, data: dict[str, Any]) -> None:
        _check_keys(data, {"number", "body", "user"}, "comment")
        number = _int(data, "number", "comment", 0)
        if number not in repo.pulls and number not in repo.issues:
            raise SeedError(f"comment on unknown issue or pull request #{number}")
        self.add_comment(
            repo, number, _text(data, "body", "comment"), self._seeded_login(_text(data, "user", "comment"))
        )

    def _seeded_login(self, login: str) -> str:
        if login.lower() not in self.users:
            raise SeedError(f"user {login!r} is not seeded")
        return self.users[login.lower()].login

    # --- mutations shared by seeding and the routes -------------------------------------------------

    def add_pull(
        self,
        repo: Repo,
        *,
        title: str,
        body: str | None,
        head: str,
        base: str,
        draft: bool,
        user: str,
        state: str = "open",
    ) -> Pull:
        pull = Pull(repo.next_number, self.new_id(), title, body, head, base, draft, state, user, self.stamp())
        repo.pulls[pull.number] = pull
        repo.next_number += 1
        return pull

    def add_issue(self, repo: Repo, title: str, user: str) -> Issue:
        issue = Issue(repo.next_number, title, user)
        repo.issues[issue.number] = issue
        repo.next_number += 1
        return issue

    def add_comment(self, repo: Repo, number: int, body: str, user: str) -> Comment:
        comment = Comment(self.new_id(), number, body, user, self.stamp())
        repo.comments.append(comment)
        return comment
