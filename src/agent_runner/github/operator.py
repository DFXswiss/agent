"""The operator's GitHub account (KOR-27, ADR-053): a second way to connect beside the device flow of ADR-015.

On a platform device the operator may place the operator's own GitHub token in `OPERATOR_GITHUB_TOKEN_FILE` and name
the operator's user in `OPERATOR_GITHUB_USERS` or the organization that owns the account in `OPERATOR_GITHUB_ORG`
(`config._operator_github`; the file needs one of them, refused on org devices, at most one user: a personal
connection is never shared, so more than one is `config_invalid`, HOT-14). A `github.connect` of that user - the user
of the verified request header, so nobody can trigger it for another user - stores this token as the user's `github`
credential at once with `source:"operator"` (`approved`, `device_flow.connect`); every other user keeps the device
flow. Like every `github` credential, the stored token can be opened by the user (browser) and by the devices holding
the user's `CK`, and operations use it only for that user's own instructions (`credentials.acting`, ADR-053). A
company `github.connect` of an admin for `OPERATOR_GITHUB_ORG` answers with this token as the organization's
credential body (`for_org`, KOR-47, ADR-076 section 7); the organization's members use it as a company account.

`read_token(path, state)` reads the file anew at each connect (the operator replaces it atomically). Only a regular
file that is not a symlink, owned by the runner's account (effective uid) and without rights for group and others
(0600 or stricter) is opened; anything else is not read and the reason is logged once
(`operator_github file_unusable reason=<reason>`; again only after the reason changed or the file was usable). The
content must be one line: the token (printable ASCII without spaces, up to 4096 characters), optionally ended by a
line feed. The token is never logged, answered or counted.

Its directory must be owned by the runner's account and writable by nobody else (`directory`; 0700 recommended).

Reasons: `missing`, `directory`, `not_regular` (directory, symlink, device, ...), `owner`, `mode`, `unreadable`,
`format`.
The doctor line `operator_github` (`check`) names `OPERATOR_GITHUB_ORG` as its `value` when it is set.
The counters `connects` and `failures` (successful and failed connects through the operator's token) feed the
textfile metrics (`metrics.counters`).
"""

from __future__ import annotations

import hmac
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.github.client import TOKEN_RE

KEY = "operator_github"
MAX_BYTES = 4096 + 2
"""The longest file that may still hold one token (4096 characters and a line feed); one byte more is `format`."""


class Unusable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class OperatorGitHub:
    """Per daemon: counters, the last logged reason, the effective uid (tests replace it)."""

    euid: Callable[[], int] = os.geteuid
    connects: int = 0
    failures: int = 0
    logged: str | None = None


def state_for(context: Any) -> OperatorGitHub:
    found = vars(context).get(KEY)
    if isinstance(found, OperatorGitHub):
        return found
    created = OperatorGitHub()
    setattr(context, KEY, created)
    return created


def approved(context: Any, user: str) -> bool:
    """Whether `user` (of a verified request) connects through the operator's token: only on a platform device (the
    configuration refuses the settings on org devices; checked again here) and only for the operator's user."""
    config = context.config
    return (
        context.identity.device.kind == "platform"
        and config.operator_github_token_file is not None
        and user.lower() in config.operator_github_users
    )


def for_org(context: Any, org: str) -> bool:
    """Whether a company connect for `org` takes the operator's token: only on a platform device with the file and
    `OPERATOR_GITHUB_ORG` naming `org` (the admin check of the request is the caller's)."""
    config = context.config
    return (
        context.identity.device.kind == "platform"
        and config.operator_github_token_file is not None
        and getattr(config, "operator_github_org", None) == org
    )


def _check(info: os.stat_result, euid: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise Unusable("not_regular")
    if info.st_uid != euid:
        raise Unusable("owner")
    if info.st_mode & 0o077:
        raise Unusable("mode")


def _check_directory(path: Path, euid: int) -> None:
    """The directory of the file: a directory owned by the runner's account that nobody else may write (no other
    file can be put in place of the token file)."""
    try:
        info = path.lstat()
    except OSError:
        raise Unusable("missing") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != euid or info.st_mode & 0o022:
        raise Unusable("directory")


def _read(path: Path, euid: int) -> str:
    _check_directory(path.parent, euid)
    try:
        _check(path.lstat(), euid)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Unusable("missing") from None
    except OSError:
        raise Unusable("unreadable") from None
    try:
        # the file may have been replaced between `lstat` and `open`: the opened file is checked again
        _check(os.fstat(fd), euid)
        raw = os.read(fd, MAX_BYTES)
    except OSError:
        raise Unusable("unreadable") from None
    finally:
        os.close(fd)
    text = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        token = text.decode("ascii")
    except UnicodeDecodeError:
        raise Unusable("format") from None
    if not TOKEN_RE.fullmatch(token):
        raise Unusable("format")
    return token


def read_token(path: Path, state: OperatorGitHub, line: Callable[[str], None]) -> str:
    """The operator's token, or `Unusable` (its reason logged once, see the module comment)."""
    try:
        token = _read(path, state.euid())
    except Unusable as unusable:
        if state.logged != unusable.reason:
            state.logged = unusable.reason
            line(f"operator_github file_unusable reason={unusable.reason}")
        raise
    state.logged = None
    return token


def foreign_copy(context: Any, user: str, token: object) -> bool:
    """Whether `token` is the operator's token held as the `github` credential of a user who is not the operator's
    user: a copy stored while several users could be approved (before HOT-14). Such a copy is never used
    (`credentials.acting`); revoking the token at GitHub ends it everywhere. False without the setting or a usable
    file (nothing to compare with)."""
    config = context.config
    path = getattr(config, "operator_github_token_file", None)
    if path is None or user.lower() in getattr(config, "operator_github_users", frozenset()):
        return False
    try:
        operator_token = _read(path, state_for(context).euid())
    except Unusable:
        return False
    return isinstance(token, str) and hmac.compare_digest(operator_token.encode(), token.encode())


def check(config: Any, euid: Callable[[], int] = os.geteuid) -> tuple[str, str]:
    """`(result, detail)` of the doctor line `operator_github`: `ok off`, `ok usable` or `warn <reason>`."""
    path = config.operator_github_token_file
    if path is None:
        return "ok", "off"
    try:
        _read(path, euid())
    except Unusable as unusable:
        return "warn", unusable.reason
    return "ok", "usable"
