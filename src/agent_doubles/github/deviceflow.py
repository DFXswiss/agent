"""The OAuth device flow of the GitHub App and of an OAuth App (ADR-015, 05-runner.md §11.4, KOR-65) as GitHub
answers it.

Two clients: ``client_id`` (the GitHub App; its tokens are GitHub App user tokens ``ghu_…``) and ``oauth_client_id``
(an OAuth App; its tokens are OAuth tokens ``gho_…`` with the requested ``scope`` that reach every repository of the
user, subject to the organisations' OAuth App restrictions). An unknown client ID is ``404`` as on GitHub.

- ``POST /login/device/code`` (``client_id``, optional ``scope``) issues a device code and a user code
  (``XXXX-XXXX``) with ``verification_uri``, ``expires_in`` and ``interval``.
- ``POST /login/oauth/access_token`` (``client_id``, ``device_code``,
  ``grant_type=urn:ietf:params:oauth:grant-type:device_code``) answers ``200`` with either the token
  (``access_token``, ``token_type: "bearer"``, ``scope``) or an error: ``authorization_pending`` while the user has
  not decided, ``slow_down`` (with the new ``interval``, five seconds more) when polled before the interval passed,
  ``expired_token`` after ``expires_in``, ``access_denied`` when the user refused, ``incorrect_device_code``,
  ``incorrect_client_credentials``, ``unsupported_grant_type``. A device code yields its token once.

The user's decision is made through the control route ``POST /_double/device`` (`DeviceFlow.control`): ``authorize``
(with the ``login`` of a seeded user), ``deny``, ``expire``, or ``slow_down`` (the next ``count`` polls answer
``slow_down`` regardless of timing). The issued token is added to the user's tokens, so ``GET /user`` works with it.
Codes and tokens are deterministic (counters), the clock is injected.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_CLIENT_ID = "Iv1.0000double0000"
DEFAULT_OAUTH_CLIENT_ID = "Ov23double00000000"
ERROR_URI = "https://docs.github.com/developers/apps/authorizing-oauth-apps#error-codes-for-the-device-flow"
SLOW_DOWN_STEP = 5
DESCRIPTIONS = {
    "authorization_pending": "The authorization request is still pending.",
    "slow_down": "Too many requests have been made in the same timeframe.",
    "expired_token": "The device_code has expired.",
    "access_denied": "The authorization request was denied.",
    "incorrect_device_code": "The device_code provided is not valid.",
    "incorrect_client_credentials": "The client_id is not valid.",
    "unsupported_grant_type": "The grant type is not supported.",
    "device_flow_disabled": "Device flow must be explicitly enabled for this App",
}
ACTIONS = frozenset({"authorize", "deny", "expire", "slow_down"})


class DeviceFlowError(ValueError):
    """A control request or configuration the device flow refuses."""


@dataclass
class Grant:
    """One device authorization in progress."""

    device_code: str
    user_code: str
    scope: str
    client_id: str
    issued: float
    expires_in: int
    interval: int
    last_poll: float | None = None
    login: str | None = None
    denied: bool = False
    expired: bool = False
    used: bool = False
    forced_slow_down: int = 0


def error(code: str, **extra: Any) -> dict[str, Any]:
    """An error answer of the token endpoint."""
    return {"error": code, "error_description": DESCRIPTIONS[code], "error_uri": ERROR_URI, **extra}


class DeviceFlow:
    """Device codes and their state. `issue_token(login)` creates and registers a token for a user."""

    def __init__(
        self,
        clock: Callable[[], float],
        issue_token: Callable[[str, str, str], str],
        web_url: Callable[[], str],
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._issue_token = issue_token
        self._web_url = web_url
        self.client_id = DEFAULT_CLIENT_ID
        self.oauth_client_id = DEFAULT_OAUTH_CLIENT_ID
        self.interval = 5
        self.expires_in = 900
        self.enabled = True
        self._grants: dict[str, Grant] = {}
        self._count = 0

    def configure(self, raw: Any) -> None:
        """Apply ``device_flow`` seed settings: ``client_id``, ``oauth_client_id``, ``interval``, ``expires_in``,
        ``enabled``."""
        if not isinstance(raw, dict):
            raise DeviceFlowError("device_flow must be an object")
        unknown = sorted(set(raw) - {"client_id", "oauth_client_id", "interval", "expires_in", "enabled"})
        if unknown:
            raise DeviceFlowError(f"device_flow: unknown keys {unknown}")
        client_id = raw.get("client_id", self.client_id)
        oauth_client_id = raw.get("oauth_client_id", self.oauth_client_id)
        for name, value in (("client_id", client_id), ("oauth_client_id", oauth_client_id)):
            if not isinstance(value, str) or not value:
                raise DeviceFlowError(f"device_flow.{name} must be a non-empty string")
        numbers = {}
        for key in ("interval", "expires_in"):
            value = raw.get(key, getattr(self, key))
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DeviceFlowError(f"device_flow.{key} must be a non-negative integer")
            numbers[key] = value
        enabled = raw.get("enabled", self.enabled)
        if not isinstance(enabled, bool):
            raise DeviceFlowError("device_flow.enabled must be a boolean")
        with self._lock:
            self.client_id, self.oauth_client_id, self.enabled = client_id, oauth_client_id, enabled
            self.interval, self.expires_in = numbers["interval"], numbers["expires_in"]

    def reset(self) -> None:
        """Forget every grant, restore the default settings and restart the code counter."""
        with self._lock:
            self._grants.clear()
            self._count = 0
            self.client_id, self.interval, self.expires_in, self.enabled = DEFAULT_CLIENT_ID, 5, 900, True
            self.oauth_client_id = DEFAULT_OAUTH_CLIENT_ID

    # --- the two public endpoints --------------------------------------------------------------------

    def request_code(self, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """``POST /login/device/code``: status and answer."""
        with self._lock:
            client_id = params.get("client_id")
            if client_id not in (self.client_id, self.oauth_client_id):
                return 404, {"error": "Not Found"}
            if not self.enabled:
                return 400, error("device_flow_disabled")
            self._count += 1
            digest = hashlib.sha256(f"device-code-{self._count}".encode()).hexdigest()
            user_code = f"{digest[:4]}-{digest[4:8]}".upper()
            grant = Grant(
                digest[:40],
                user_code,
                params.get("scope", ""),
                str(client_id),
                self._clock(),
                self.expires_in,
                self.interval,
            )
            self._grants[grant.device_code] = grant
            return 200, {
                "device_code": grant.device_code,
                "user_code": grant.user_code,
                "verification_uri": f"{self._web_url()}/login/device",
                "expires_in": grant.expires_in,
                "interval": grant.interval,
            }

    def poll(self, params: dict[str, str]) -> dict[str, Any]:
        """``POST /login/oauth/access_token``: the answer (GitHub answers 200 also for errors)."""
        code: str | None
        extra: dict[str, Any] = {}
        with self._lock:
            grant = self._grants.get(params.get("device_code", ""))
            if params.get("client_id") not in (self.client_id, self.oauth_client_id) or (
                grant is not None and params.get("client_id") != grant.client_id
            ):
                code = "incorrect_client_credentials"
            elif params.get("grant_type") != GRANT_TYPE:
                code = "unsupported_grant_type"
            elif grant is None or grant.used:
                code = "incorrect_device_code"
            else:
                code, extra = self._decide(grant)
            if code is not None:
                return error(code, **extra)
            assert grant is not None
            grant.used = True
            login, scope, client = grant.login, grant.scope, grant.client_id
            kind = "oauth" if client == self.oauth_client_id else "app"
        assert login is not None
        # GitHub names the granted scopes of an OAuth App comma separated
        granted = ",".join(scope.split()) if kind == "oauth" else scope
        return {"access_token": self._issue_token(login, kind, scope), "token_type": "bearer", "scope": granted}

    def _decide(self, grant: Grant) -> tuple[str | None, dict[str, Any]]:
        """The error code of a poll for `grant` (None when the token is due) and extra answer fields."""
        now = self._clock()
        too_early = grant.last_poll is not None and now - grant.last_poll < grant.interval
        grant.last_poll = now
        code: str | None = None
        extra: dict[str, Any] = {}
        if grant.expired or now - grant.issued >= grant.expires_in:
            grant.expired = True
            code = "expired_token"
        elif grant.denied:
            code = "access_denied"
        elif grant.forced_slow_down or too_early:
            grant.forced_slow_down = max(0, grant.forced_slow_down - 1)
            grant.interval += SLOW_DOWN_STEP
            code, extra = "slow_down", {"interval": grant.interval}
        elif grant.login is None:
            code = "authorization_pending"
        return code, extra

    # --- control ------------------------------------------------------------------------------------

    def control(self, raw: Any, known_login: Callable[[str], str | None]) -> dict[str, Any]:
        """``POST /_double/device``: ``{"user_code", "action", "login"?, "count"?}``; the grant as JSON."""
        if not isinstance(raw, dict):
            raise DeviceFlowError("body must be an object")
        action = raw.get("action")
        if action not in ACTIONS:
            raise DeviceFlowError(f"action must be one of {sorted(ACTIONS)}")
        with self._lock:
            grant = next((g for g in self._grants.values() if g.user_code == raw.get("user_code")), None)
            if grant is None:
                raise DeviceFlowError(f"unknown user code {raw.get('user_code')!r}")
            if action == "authorize":
                login = known_login(str(raw.get("login", "")))
                if login is None:
                    raise DeviceFlowError(f"unknown login {raw.get('login')!r}")
                grant.login = login
            elif action == "deny":
                grant.denied = True
            elif action == "expire":
                grant.expired = True
            else:
                count = raw.get("count", 1)
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise DeviceFlowError("count must be a positive integer")
                grant.forced_slow_down = count
            return self._describe(grant)

    def grants(self) -> list[dict[str, Any]]:
        """Every grant (without the device code), oldest first."""
        with self._lock:
            return [self._describe(grant) for grant in self._grants.values()]

    @staticmethod
    def _describe(grant: Grant) -> dict[str, Any]:
        return {
            "user_code": grant.user_code,
            "login": grant.login,
            "denied": grant.denied,
            "expired": grant.expired,
            "used": grant.used,
            "interval": grant.interval,
        }
