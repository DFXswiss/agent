"""The in-memory GitHub of the double: users and their tokens, App installations, repositories with git objects,
refs, issues, pull requests, labels and issue comments, and the seeding format.

Seeding (``POST /_double/seed`` or ``--seed FILE``) adds to what exists; names already present are refused, a
reset (``POST /_double/reset``) empties everything. Format (every key optional)::

    {"users": [{"login": "octo-user", "id": 101, "name": "Octo User", "email": null,
                "tokens": ["<token>", {"token": "<token>", "kind": "classic", "scopes": ["repo", "read:org"],
                                       "repositories": ["octo-org/hello"], "revoked": false, "sso": ["octo-org"]}],
                "personal_tokens": ["<token>"], "collaborator": ["octo-org/hello"],
                "ssh_signing_keys": ["ssh-ed25519 AAAA…", {"key": "ssh-ed25519 AAAA…", "title": "laptop"}]}],
     "installations": [{"id": 31, "account": "octo-org", "account_type": "Organization",
                        "repository_selection": "selected", "repositories": ["octo-org/hello"],
                        "users": ["octo-user"], "permissions": {"contents": "write", ...}}],
     "repositories": [{"full_name": "octo-org/hello", "private": true, "default_branch": "main",
                       "description": null, "has_issues": true,
                       "branches": [{"name": "main", "commits": [{"message": "Initial commit.",
                                     "files": {"README.md": "hello\\n", "run.sh": {"content": "...", "mode": "100755"},
                                               "logo.bin": {"base64": "..."}, "link": {"target": "README.md"}}}]},
                                    {"name": "feature", "from": "main", "commits": [{"files": {"README.md": null}}]}],
                       "tags": {"v1.0": "main"},
                       "issues": [{"title": "...", "user": "octo-user", "body": null, "state": "closed",
                                   "state_reason": "not_planned", "labels": ["bug"], "assignees": ["octo-user"]}],
                       "pulls": [{"title": "...", "head": "feature", "base": "main", "draft": true,
                                  "user": "octo-user", "labels": ["bug"]}],
                       "comments": [{"number": 1, "body": "...", "user": "octo-user"}]}],
     "organizations": [{"login": "third-org", "oauth_restricted": true, "oauth_approved": ["<client id>"],
                        "classic_forbidden": false}],
     "device_flow": {"client_id": "...", "oauth_client_id": "...", "interval": 5, "expires_in": 900},
     "faults": [...], "expectations": [...]}

Issues and pull requests share one number sequence per repository, issues first. An issue is ``open`` by default; a
closed one has the ``state_reason`` ``completed`` unless the seed names one (``completed``, ``not_planned``,
``reopened``, ``duplicate`` or ``null``). Label names of issues and pull requests create the repository's labels on
first use (as ``POST …/issues`` does); assignees are seeded logins; the comment count of an issue is the number of its
``comments``. ``has_issues: false`` disables the issue routes (``410``). Commit ``files`` are changes against the
parent (``null`` deletes a path); a branch without ``from`` starts empty. An installation whose account is a user that
is not listed in ``users`` is visible to that user; repositories of an installation with
``repository_selection: "all"`` are all repositories of its account.

A token is a string (a GitHub App user token, ``ghu_…``) or an object; only ``token`` is required. ``kind`` is ``app``
(default), ``oauth`` (an OAuth App token such as the GitHub CLI's, ``gho_…``, of the OAuth App ``client``; it reaches
the user's own repositories and those named in ``collaborator``, like GitHub's ``repo`` scope reaches everything the
account can reach), ``classic`` or ``fine_grained`` (personal access tokens); ``scopes`` are the OAuth scopes of a
classic or OAuth token; ``repositories`` limits a personal access token to these full names (default: every repository);
``revoked: true`` makes every request ``401 Bad credentials``; ``sso`` names organisations that enforce SAML single
sign-on for which the token is not authorized. With an App token a repository is accessible when one of the
installations visible to the token's user covers it; a personal access token reaches its repositories without an
installation; anything else is ``404``. An organisation (``organizations``, KOR-65) may restrict OAuth Apps
(``oauth_restricted``; GitHub's default for new organisations): an OAuth token of a client not in ``oauth_approved``
gets ``403`` with GitHub's restriction message for a private repository of it and for every write; ``classic_forbidden``
refuses classic tokens there with ``403`` (the organisation's personal access token policy). ``POST
/_double/organizations`` changes an organisation later (an owner's approval). An organisation the seed does not name
restricts nothing.
``personal_tokens`` (KOR-27) are short for classic tokens with the scope ``repo`` that reach the repositories the
user owns and those named in ``collaborator`` (a name that is not seeded is simply not reachable).
``ssh_signing_keys`` registers SSH signing keys of the user (a key line or ``{"key", "title"}``), as
``POST /user/ssh_signing_keys`` would (`agent_doubles.github.sshsig`: Ed25519 only; a key belongs to one account).
Which token may manage keys follows its ``kind`` and ``scopes`` (`agent_doubles.github.server`).

Commit verification (``verification`` of the commit answers) is decided once, when a signed commit is created
(`World.verify`), and kept, like GitHub's persistent verification records: a later removal of the key does not change
it. An unsigned commit is ``unsigned``. For an SSH signature (`agent_doubles.github.sshsig.classify`) the reasons are,
in this order: ``malformed_signature`` (does not parse), ``unknown_key`` (the signing key is no registered signing key
of any account), ``invalid`` (the signature does not verify over the commit without its ``gpgsig`` header, or its
namespace is not ``git``), ``bad_email`` (the key's account does not own the committer email: neither the account's
``email`` nor its ``<id>+<login>@users.noreply.github.com`` address) and ``valid`` (``verified: true`` with
``verified_at``); a PGP signature is ``gpgverify_unavailable``, anything else ``unknown_signature_type``.
``signature`` and ``payload`` (the signed bytes as text) are given for every signed commit.

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

from agent_doubles.github import sshsig
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
ISSUE_STATES = PULL_STATES
STATE_REASONS = frozenset({"completed", "not_planned", "reopened", "duplicate"})
TOKEN_KINDS = frozenset({"app", "oauth", "classic", "fine_grained"})
LABEL_COLOR = "ededed"
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class SeedError(ValueError):
    """Seed data the double refuses (the control route answers 400)."""


NOREPLY_DOMAIN = "users.noreply.github.com"


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


def _names(data: Mapping[str, Any], key: str, what: str) -> list[str]:
    value = _list(data.get(key, []), f"{what}.{key}")
    if any(not isinstance(item, str) or not item for item in value):
        raise SeedError(f"{what}.{key} must be non-empty strings")
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
class Token:
    """A credential of a user: a GitHub App user token (`kind` ``app``) or a personal access token (``classic`` with
    OAuth `scopes`, ``fine_grained``); `repositories` (lower-case full names, None for all) limits a personal access
    token, `sso` names (lower-case) organisations whose SAML enforcement the token is not authorized for."""

    token: str
    login: str
    kind: str = "app"
    scopes: list[str] = field(default_factory=list)
    repositories: list[str] | None = None
    revoked: bool = False
    sso: list[str] = field(default_factory=list)
    collaborator: list[str] | None = None
    """Set for ``personal_tokens`` and OAuth tokens: the token reaches only the user's own repositories and these
    (lower case)."""
    client: str | None = None
    """The OAuth App of an ``oauth`` token (its client ID)."""

    def reaches(self, repo: Repo) -> bool:
        """Whether this personal access token names `repo`."""
        if self.collaborator is not None:
            return repo.owner.lower() == self.login or repo.key in self.collaborator
        return self.repositories is None or repo.key in self.repositories

    def can_write(self, repo: Repo) -> bool:
        """Whether this personal access token may write to `repo` (classic tokens: scope ``repo``, or
        ``public_repo`` for a public repository)."""
        if self.kind == "fine_grained":
            return True
        return "repo" in self.scopes or ("public_repo" in self.scopes and not repo.private)


@dataclass(frozen=True)
class SigningKey:
    """An SSH signing key of an account: `blob` is the public key blob, `line` the normalised key line."""

    id: int
    login: str
    title: str
    line: str
    blob: bytes
    created: int


@dataclass(frozen=True)
class Verification:
    """The ``verification`` of a commit (`verified_at` in seconds since the epoch)."""

    verified: bool
    reason: str
    signature: str | None = None
    payload: str | None = None
    verified_at: int | None = None


UNSIGNED = Verification(False, "unsigned")


@dataclass
class Organization:
    """An organisation's access policies (KOR-65): OAuth App restrictions with the approved client IDs, and whether
    classic personal access tokens are refused."""

    login: str
    oauth_restricted: bool = False
    oauth_approved: list[str] = field(default_factory=list)
    classic_forbidden: bool = False

    def refuses_oauth(self, client: str | None) -> bool:
        return self.oauth_restricted and client not in self.oauth_approved


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
    updated: int = 0
    closed: int | None = None
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    state_reason: str | None = None


@dataclass
class Issue:
    number: int
    id: int
    title: str
    body: str | None
    user: str
    state: str
    state_reason: str | None
    labels: list[str]
    assignees: list[str]
    created: int
    updated: int
    closed: int | None


@dataclass
class Label:
    id: int
    name: str
    color: str = LABEL_COLOR
    description: str | None = None


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
    has_issues: bool = True
    objects: ObjectStore = field(default_factory=ObjectStore)
    refs: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, Pull] = field(default_factory=dict)
    issues: dict[int, Issue] = field(default_factory=dict)
    comments: list[Comment] = field(default_factory=list)
    labels: dict[str, Label] = field(default_factory=dict)
    next_number: int = 1
    verifications: dict[str, Verification] = field(default_factory=dict)
    """The verification decided when a signed commit was created, by commit id (unsigned commits have none)."""

    def verification(self, sha: str) -> Verification:
        return self.verifications.get(sha, UNSIGNED)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return self.full_name.lower()

    def item(self, number: int) -> Issue | Pull | None:
        """The issue or pull request `number`."""
        return self.issues.get(number) or self.pulls.get(number)

    def comment_count(self, number: int) -> int:
        return sum(1 for comment in self.comments if comment.number == number)

    @property
    def open_items(self) -> int:
        """Open issues and pull requests (``open_issues_count``)."""
        items: list[Issue | Pull] = [*self.issues.values(), *self.pulls.values()]
        return sum(1 for item in items if item.state == "open")

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
            self.tokens: dict[str, Token] = {}
            self.installations: dict[int, Installation] = {}
            self.repos: dict[str, Repo] = {}
            self.organizations: dict[str, Organization] = {}
            self.signing_keys: dict[int, SigningKey] = {}
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
        """The user of a known token that is not revoked."""
        found = self.tokens.get(token)
        return self.users[found.login] if found is not None and not found.revoked else None

    def add_token(self, login: str, token: str, kind: str = "app", scopes: list[str] | None = None) -> None:
        """Give user `login` (lower case) a GitHub App user token, or an OAuth token (`kind` ``oauth``) of the
        double's OAuth App with `scopes` that reaches the user's own and collaborator repositories."""
        if kind == "oauth":
            reach = [name.lower() for name in self.users[login].collaborator]
            self.tokens[token] = Token(token, login, kind, list(scopes or []), collaborator=reach)
            return
        self.tokens[token] = Token(token, login)

    def organization(self, login: str) -> Organization | None:
        return self.organizations.get(login.lower())

    def set_organization(self, raw: Any) -> Organization:
        """Add or change an organisation's policies (seed item or ``POST /_double/organizations``)."""
        data = _object(raw, "organization")
        _check_keys(data, {"login", "oauth_restricted", "oauth_approved", "classic_forbidden"}, "organization")
        login = _text(data, "login", "organization")
        if not LOGIN_RE.match(login):
            raise SeedError(f"invalid organization login {login!r}")
        known = self.organizations.get(login.lower()) or Organization(login)
        found = Organization(
            login,
            _bool(data, "oauth_restricted", "organization", known.oauth_restricted),
            _names(data, "oauth_approved", "organization") if "oauth_approved" in data else known.oauth_approved,
            _bool(data, "classic_forbidden", "organization", known.classic_forbidden),
        )
        self.organizations[login.lower()] = found
        return found

    def revoke_token(self, token: str) -> bool:
        """Forget a token (``POST /credentials/revoke``); True when it was known."""
        return self.tokens.pop(token, None) is not None

    def revoke(self, token: str) -> None:
        """Revoke a known token (later requests with it are ``401 Bad credentials``)."""
        found = self.tokens.get(token)
        if found is None:
            raise SeedError("unknown token")
        found.revoked = True

    def token_repos(self, user: User, token: Token) -> list[Repo]:
        """The repositories `token` reaches (an App token: through the installations visible to `user`), by full name,
        without those of organisations whose SAML enforcement the token is not authorized for."""
        if token.kind == "app":
            found = {
                repo.key: repo for inst in self.visible_installations(user) for repo in self.installation_repos(inst)
            }
            repos = list(found.values())
        else:
            repos = [repo for repo in self.repos.values() if token.reaches(repo)]
        return sorted((repo for repo in repos if repo.owner.lower() not in token.sso), key=lambda repo: repo.key)

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

    def user_for_email(self, email: str) -> User | None:
        """The user a commit email belongs to: its ``email`` or its noreply address (case-insensitive)."""
        for user in self.users.values():
            if email.lower() in {noreply_email(user).lower(), (user.email or "").lower()}:
                return user
        return None

    # --- SSH signing keys and commit verification ---------------------------------------------------

    def add_signing_key(self, login: str, title: str, line: str) -> SigningKey:
        """Register an SSH signing key for user `login` (lower case); `SeedError` for an invalid key or one that is
        already registered (with any account)."""
        try:
            blob = sshsig.parse_public_key(line)
        except sshsig.Malformed as exc:
            raise SeedError("key is invalid. You must supply a key in OpenSSH public key format") from exc
        if any(key.blob == blob for key in self.signing_keys.values()):
            raise SeedError("key is already in use")
        key = SigningKey(self.new_id(), login, title, sshsig.key_line(blob), blob, self.stamp())
        self.signing_keys[key.id] = key
        return key

    def user_signing_keys(self, login: str) -> list[SigningKey]:
        """The signing keys of user `login` (lower case), by id."""
        return sorted((key for key in self.signing_keys.values() if key.login == login), key=lambda key: key.id)

    def verify(self, commit: Commit) -> Verification:
        """The verification of a newly created commit (module docstring)."""
        if commit.signature is None:
            return UNSIGNED
        signature, payload = commit.signature, commit.unsigned_payload()
        reason = self._reason(signature, payload, commit.committer.email)
        verified_at = self.stamp() if reason == "valid" else None
        return Verification(reason == "valid", reason, signature, payload.decode(), verified_at)

    def _reason(self, signature: str, payload: bytes, email: str) -> str:
        found = sshsig.classify(signature)
        if isinstance(found, str):
            return found
        key = next((key for key in self.signing_keys.values() if key.blob == found.public_key), None)
        if key is None:
            return "unknown_key"
        if not sshsig.verifies(found, payload):
            return "invalid"
        owner = self.user_for_email(email)
        if owner is None or owner.login.lower() != key.login:
            return "bad_email"
        return "valid"

    # --- seeding ------------------------------------------------------------------------------------

    def seed(self, payload: Any) -> None:
        """Add seed data (see the module docstring); device flow, faults and expectations are read by the server."""
        data = _object(payload, "seed")
        _check_keys(
            data,
            {"users", "installations", "repositories", "organizations", "device_flow", "faults", "expectations"},
            "seed",
        )
        saved = (
            dict(self.users),
            dict(self.tokens),
            dict(self.installations),
            dict(self.repos),
            dict(self.signing_keys),
            self._tick,
            self._ids,
        )
        organizations = dict(self.organizations)
        try:
            for raw in _list(data.get("users", []), "users"):
                self._seed_user(_object(raw, "user"))
            for raw in _list(data.get("repositories", []), "repositories"):
                self._seed_repo(_object(raw, "repository"))
            for raw in _list(data.get("installations", []), "installations"):
                self._seed_installation(_object(raw, "installation"))
            for raw in _list(data.get("organizations", []), "organizations"):
                self.set_organization(raw)
        except SeedError:
            # A refused seed leaves nothing behind (seeding only adds new objects, so restoring the tables suffices).
            self.users, self.tokens, self.installations, self.repos, self.signing_keys, self._tick, self._ids = saved
            self.organizations = organizations
            raise

    def _seed_user(self, data: dict[str, Any]) -> None:
        _check_keys(
            data,
            {"login", "id", "name", "email", "tokens", "personal_tokens", "collaborator", "ssh_signing_keys"},
            "user",
        )
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
        for raw in _list(data.get("personal_tokens", []), "user.personal_tokens"):
            if not isinstance(raw, str) or not raw:
                raise SeedError("user.personal_tokens must be non-empty strings")
            self._add_seeded(
                Token(raw, user.login.lower(), "classic", ["repo"], collaborator=[n.lower() for n in user.collaborator])
            )
        for raw in _list(data.get("tokens", []), "user.tokens"):
            token = self._seed_token(user.login.lower(), raw)
            if token.kind == "oauth":
                token.collaborator = [name.lower() for name in user.collaborator]
            self._add_seeded(token)
        for raw in _list(data.get("ssh_signing_keys", []), "user.ssh_signing_keys"):
            spec = _object({"key": raw} if isinstance(raw, str) else raw, "ssh signing key")
            _check_keys(spec, {"key", "title"}, "ssh signing key")
            self.add_signing_key(
                user.login.lower(), _text(spec, "title", "ssh signing key", ""), _text(spec, "key", "ssh signing key")
            )

    def _add_seeded(self, token: Token) -> None:
        if token.token in self.tokens:
            raise SeedError("a token belongs to one user only")
        self.tokens[token.token] = token

    @staticmethod
    def _seed_token(login: str, raw: Any) -> Token:
        if isinstance(raw, str):
            raw = {"token": raw}
        data = _object(raw, "token")
        _check_keys(data, {"token", "kind", "scopes", "repositories", "revoked", "sso", "client"}, "token")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise SeedError("user.tokens must be non-empty strings")
        kind = _text(data, "kind", "token", "app")
        if kind not in TOKEN_KINDS:
            raise SeedError(f"token.kind must be one of {sorted(TOKEN_KINDS)}")
        repositories = None
        if "repositories" in data:
            repositories = [name.lower() for name in _names(data, "repositories", "token")]
            if any("/" not in name for name in repositories):
                raise SeedError("token.repositories must be full names (owner/name)")
        return Token(
            token,
            login,
            kind,
            _names(data, "scopes", "token"),
            repositories,
            _bool(data, "revoked", "token", False),
            [org.lower() for org in _names(data, "sso", "token")],
            client=_optional_text(data, "client", "token"),
        )

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
                "has_issues",
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
            _bool(data, "has_issues", "repository", True),
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
            self._seed_issue(repo, _object(raw, "issue"))
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

    def _seed_issue(self, repo: Repo, data: dict[str, Any]) -> None:
        _check_keys(data, {"title", "body", "user", "state", "state_reason", "labels", "assignees"}, "issue")
        state = _text(data, "state", "issue", "open")
        if state not in ISSUE_STATES:
            raise SeedError("issue.state must be open or closed")
        reason = _optional_text(data, "state_reason", "issue")
        if reason is not None and reason not in STATE_REASONS:
            raise SeedError(f"issue.state_reason must be null or one of {sorted(STATE_REASONS)}")
        if state == "closed" and "state_reason" not in data:
            reason = "completed"
        self.add_issue(
            repo,
            _text(data, "title", "issue"),
            self._seeded_login(_text(data, "user", "issue")),
            body=_optional_text(data, "body", "issue"),
            state=state,
            state_reason=reason,
            labels=_names(data, "labels", "issue"),
            assignees=[self._seeded_login(login) for login in _names(data, "assignees", "issue")],
        )

    def _seed_pull(self, repo: Repo, data: dict[str, Any]) -> None:
        _check_keys(data, {"title", "body", "head", "base", "draft", "state", "user", "labels"}, "pull")
        head, base = _text(data, "head", "pull"), _text(data, "base", "pull", repo.default_branch)
        if repo.branch(head) is None or repo.branch(base) is None:
            raise SeedError(f"pull {head} → {base}: unknown branch")
        state = _text(data, "state", "pull", "open")
        if state not in PULL_STATES:
            raise SeedError("pull.state must be open or closed")
        user = self._seeded_login(_text(data, "user", "pull"))
        pull = self.add_pull(
            repo,
            title=_text(data, "title", "pull"),
            body=_optional_text(data, "body", "pull"),
            head=head,
            base=base,
            draft=_bool(data, "draft", "pull", False),
            user=user,
            state=state,
        )
        pull.labels = self.labels(repo, _names(data, "labels", "pull"))

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
        when = self.stamp()
        closed = when if state == "closed" else None
        pull = Pull(repo.next_number, self.new_id(), title, body, head, base, draft, state, user, when, when, closed)
        repo.pulls[pull.number] = pull
        repo.next_number += 1
        return pull

    def add_issue(
        self,
        repo: Repo,
        title: str,
        user: str,
        *,
        body: str | None = None,
        state: str = "open",
        state_reason: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> Issue:
        when = self.stamp()
        issue = Issue(
            repo.next_number,
            self.new_id(),
            title,
            body,
            user,
            state,
            state_reason,
            self.labels(repo, labels or []),
            list(assignees or []),
            when,
            when,
            when if state == "closed" else None,
        )
        repo.issues[issue.number] = issue
        repo.next_number += 1
        return issue

    def labels(self, repo: Repo, names: list[str]) -> list[str]:
        """The repository's names of the labels `names` (matched without case), creating unknown ones, each once."""
        found: list[str] = []
        for name in names:
            label = repo.labels.get(name.lower())
            if label is None:
                label = repo.labels[name.lower()] = Label(self.new_id(), name)
            if label.name not in found:
                found.append(label.name)
        return found

    def add_comment(self, repo: Repo, number: int, body: str, user: str) -> Comment:
        """Add a comment to issue or pull request `number` (which must exist); its ``updated_at`` advances."""
        comment = Comment(self.new_id(), number, body, user, self.stamp())
        repo.comments.append(comment)
        item = repo.item(number)
        assert item is not None
        item.updated = comment.created
        return comment


def noreply_email(user: User) -> str:
    """The account's ``<id>+<login>@users.noreply.github.com`` address."""
    return f"{user.id}+{user.login}@{NOREPLY_DOMAIN}"
