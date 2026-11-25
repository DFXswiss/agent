"""What the full login of a further subscription reads (05-runner.md section 11.6, ADR-091 section 1, KOR-71).

`SUBSCRIPTION_LOGIN_ARGV` (default `claude auth login --claudeai`) runs like the setup-token relay (`relay.py`), with
three differences, all checked against Claude Code `setup_token.CHECKED_VERSION` (the golden's pin) on the test
device up to the code input:

- **The link.** The URL the program hands to `BROWSER` redirects to `http://localhost:<port>/callback`, which a user
  who is not at the device cannot reach. The link for the code page is on the screen, on the rows after
  `VISIT_LABEL` (`screen_url`); the input field is the one of `claude setup-token` (`setup_token.PROMPT_LABEL`).
- **No keychain.** The program reaches the macOS keychain only through `security` in its `PATH`. The login's private
  `bin` directory comes first in `PATH` and holds `SECURITY_SCRIPT`, which refuses every call and writes nothing
  (`write_security`), so the program keeps the login in its plaintext store inside the temporary directory.
- **The login.** After the program ended successfully, `read_login` reads `$CLAUDE_CONFIG_DIR/.credentials.json`
  (`claudeAiOauth`: `accessToken`, `refreshToken`, `expiresAt` in ms, `scopes`, `subscriptionType`, `rateLimitTier`)
  and the address of `$CLAUDE_CONFIG_DIR/.claude.json` (`oauthAccount.emailAddress`, optional): regular files of
  this account, opened without following links, at most `MAX_FILE` bytes. A login without `user:profile` and
  `user:inference` is `scope_missing`; a missing or malformed store is `token_invalid`.

The tokens are kept in memory only; nothing here logs or returns text of the screen.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.login import capture, setup_token
from agent_runner.sessions.records import time_text

VISIT_LABEL = "If the browser didn't open, visit:"
"""The row above the link of the code page."""
STORE = ".credentials.json"
STORE_KEY = "claudeAiOauth"
ACCOUNT_FILE = ".claude.json"
SECURITY = "security"
SECURITY_SCRIPT = "#!/bin/sh\nexit 1\n"
REQUIRED_SCOPES = frozenset({"user:profile", "user:inference"})
MAX_FILE = 64 * 1024
TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
SCOPE_RE = re.compile(r"^[a-z_:]{1,64}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
EMAIL_RE = re.compile(r"^[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$")
MAX_SCOPES = 32
MAX_EXPIRY_MS = 253402300799000
"""9999-12-31T23:59:59Z: a larger `expiresAt` is out of form."""


class LoginUnusable(Exception):
    """The program's store holds no usable login (`code` of `contracts/requests/objects.json`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FullLogin:
    """A login read from the program's store (the tokens in memory only)."""

    token: str
    refresh_token: str
    expires_at: str
    scopes: tuple[str, ...]
    subscription_type: str | None
    rate_limit_tier: str | None
    email: str | None

    def __repr__(self) -> str:  # never the tokens
        return f"FullLogin(scopes={list(self.scopes)}, expires_at={self.expires_at})"

    def body(self) -> dict[str, object]:
        """The `claude_subscription` body fields of the login (`kind` `oauth_login`)."""
        found: dict[str, object] = {
            "token": self.token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "email": self.email,
            "kind": "oauth_login",
        }
        if self.subscription_type is not None:
            found["subscription_type"] = self.subscription_type
        if self.rate_limit_tier is not None:
            found["rate_limit_tier"] = self.rate_limit_tier
        return found


def write_security(bin_dir: Path) -> Path:
    """The refusing `security` (0700) in `bin_dir`; its path."""
    target = bin_dir / SECURITY
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(SECURITY_SCRIPT)
    return target


def screen_url(lines: Sequence[str]) -> str | None:
    """The link of the code page: the non-empty rows after the row with `VISIT_LABEL` up to the next empty row or the
    input field, joined (a narrow window wraps it); None while it is not shown. `capture.UrlInvalid` when the rows are
    not an authorize URL."""
    rows = [line.strip() for line in lines]
    marker = next((index for index, row in enumerate(rows) if VISIT_LABEL in row), None)
    if marker is None:
        return None
    pieces: list[str] = []
    for row in rows[marker + 1 :]:
        if not row or setup_token.PROMPT_LABEL in row:
            break
        pieces.append(row)
    if not pieces:
        return None
    url = "".join(pieces)
    if len(url) > capture.MAX_URL or not capture.URL_RE.fullmatch(url):
        raise capture.UrlInvalid
    return url


def _regular(descriptor: int) -> bool:
    info = os.fstat(descriptor)
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()


def _read(path: Path) -> Any:
    """The JSON of a regular file of this account (no link, at most `MAX_FILE` bytes); None when it is missing."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise LoginUnusable("token_invalid") from None
    if not _regular(descriptor):
        os.close(descriptor)
        raise LoginUnusable("token_invalid")
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise LoginUnusable("token_invalid")
    try:
        return json.loads(data)
    except ValueError:
        raise LoginUnusable("token_invalid") from None


def _label(value: object) -> str | None:
    return value if isinstance(value, str) and LABEL_RE.fullmatch(value) else None


def _email(config_dir: Path) -> str | None:
    try:
        found = _read(config_dir / ACCOUNT_FILE)
    except LoginUnusable:
        return None
    account = found.get("oauthAccount") if isinstance(found, Mapping) else None
    email = account.get("emailAddress") if isinstance(account, Mapping) else None
    return email if isinstance(email, str) and len(email) <= 254 and EMAIL_RE.fullmatch(email) else None


def expiry_text(millis: object) -> str:
    """`expiresAt` (Unix ms) as a contract time; `LoginUnusable("token_invalid")` when out of form."""
    if not isinstance(millis, int) or isinstance(millis, bool) or not 0 < millis <= MAX_EXPIRY_MS:
        raise LoginUnusable("token_invalid")
    return time_text(millis / 1000)


def scopes_of(value: object) -> tuple[str, ...]:
    """Scopes as a list (the store) or a space-separated text (the token endpoint), sorted and unique."""
    items = value.split() if isinstance(value, str) else value
    if (
        not isinstance(items, list)
        or len(items) > MAX_SCOPES
        or not all(isinstance(item, str) and SCOPE_RE.fullmatch(item) for item in items)
    ):
        raise LoginUnusable("token_invalid")
    return tuple(sorted(set(items)))


def read_login(config_dir: Path) -> FullLogin:
    """The login the program stored (module docstring)."""
    found = _read(config_dir / STORE)
    data = found.get(STORE_KEY) if isinstance(found, Mapping) else None
    if not isinstance(data, Mapping):
        raise LoginUnusable("token_invalid")
    token, refresh = data.get("accessToken"), data.get("refreshToken")
    if not (isinstance(token, str) and TOKEN_RE.fullmatch(token)) or not (
        isinstance(refresh, str) and TOKEN_RE.fullmatch(refresh)
    ):
        raise LoginUnusable("token_invalid")
    listed = data.get("scopes")
    scopes = scopes_of(listed if isinstance(listed, list) else None)
    if not set(scopes) >= REQUIRED_SCOPES:
        raise LoginUnusable("scope_missing")
    return FullLogin(
        token=token,
        refresh_token=refresh,
        expires_at=expiry_text(data.get("expiresAt")),
        scopes=scopes,
        subscription_type=_label(data.get("subscriptionType")),
        rate_limit_tier=_label(data.get("rateLimitTier")),
        email=_email(config_dir),
    )
