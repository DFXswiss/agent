"""Device request `github.connect` (05-runner.md section 11.4, ADR-015, 04-crypto.md section 4.4, KOR-25).

The body is `{}` (the device flow this device prefers), `{app}` (the device flow of the named app: `oauth_app` or
`github_app`, KOR-65) or `{token}` (an existing GitHub token: a GitHub CLI login or a personal access token); any other
body is `upstream_error`, a token that is not printable ASCII up to 4096 characters `token_invalid`.

**Which device flow** (KOR-65, ADR-015 addendum): the **OAuth App** (`GITHUB_OAUTH_CLIENT_ID`, scopes `OAUTH_SCOPES`)
is preferred when it is set up - its token reaches every repository the account can reach, like the GitHub CLI's
(own account, forks, organisations; an organisation that restricts OAuth Apps asks its owners for approval, which
the next step of a held operation links). Otherwise the **GitHub App** (`GITHUB_CLIENT_ID`), whose token reaches only
repositories of owners where the App is installed. A flow that is not set up on this device is `flow_unavailable`
(the device does not announce `github.device_flow` then, `sync.ws.features`, so the web does not offer it).

**Token** (no `GITHUB_CLIENT_ID` needed): `GET <GITHUB_API_URL>/user` with the token: `401` -> `token_refused`
(revoked or unknown), `403` naming an SSO requirement -> `sso_required`, network errors and `5xx` ->
`upstream_unreachable`, anything else -> `upstream_error`. The scopes are the header `X-OAuth-Scopes` (a classic
token); without the header the token is fine-grained (or a GitHub App user token) and has `scopes: []`. A classic
token with neither `repo` nor `public_repo` is `scope_missing`. Then the credential is stored as in step 5 below with
`source: "token"` and the answer is `{step:"done", login, scopes, source:"token"}`.

**Device flow** (body `{}`):

1. `POST <GITHUB_WEB_URL>/login/device/code` with the public client ID of the chosen app (and, for the OAuth App, the
   scopes `OAUTH_SCOPES`); a
   network error or `5xx` is tried again after `DEVICE_CODE_DELAYS` (2 s, then 8 s: three attempts, the first two
   waits of the operation pipeline's backoff), then `upstream_unreachable` (KOR-16: the request has no effect a user
   sees - an unused device code expires - so one lost connection must not end the connect);
2. response 1 `{step:"device_code", user_code, verification_uri, expires_in}` (no secret: the device code stays here);
3. poll `POST /login/oauth/access_token` every `interval` seconds until the flow's end (`expires_in` and the request's
   15 minutes): `authorization_pending` -> keep polling, `slow_down` -> the interval GitHub names (at least 5 s
   more), `expired_token` -> `timeout`, `access_denied` and every other error -> `upstream_error`; network errors and
   `5xx` are polled again up to `UNREACHABLE_LIMIT` times in a row, then `upstream_unreachable`;
4. `GET <GITHUB_API_URL>/user` with the token (`login`, `id`);
5. credential `github {token, login, user_id, scopes, created_at, source}` (`source: "device_flow"`) under the
   owner's `CK`, written as this device while answering the request (`credentials.cache.put`); a read or write lost
   in transport (`hub_unreachable`) is sent again after `RESPOND_DELAYS` like a response (KOR-18: the user has
   already confirmed the code, so one lost request must not discard the connection), the write with the same record
   bytes, so it never stores a second version;
6. response 2 `{step:"done", login, scopes, source:"device_flow"}` (final).

A user approved for the operator's GitHub account (KOR-27, ADR-053, `github.operator`) skips steps 1-3: the
operator's token file is read, checked with step 4 (scopes from `X-OAuth-Scopes`, as far as GitHub names them) and
stored with step 5 (`source: "token"`); the only response is `{step:"done", login, scopes, source}` (final) - the
web client takes `done` as the first response as well. An unusable file is `credential_missing`; log lines of this
way carry `source=operator`.

Failures end with `{step:"failed", code}` (final): `flow_unavailable` (the asked device flow is not set up on this
device), `upstream_error`, `upstream_unreachable`, `timeout`, `token_invalid`
(an access token that is not printable ASCII up to 4096 characters), `token_refused`, `scope_missing`,
`sso_required`, `credential_missing` (this device holds no `CK` of the owner) - the codes of
`contracts/requests/objects.json`. The token is never logged or answered. A failure of a GitHub call is logged with
its step (`device_code`, `poll`, `user`), the error kind and, for a transport error, its class (`cause`), so the phase
of a lost connection is visible; a success is logged with its `source` and the scopes.
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.credentials.cache import CredentialUnavailable, put
from agent_runner.github import operator, ops, revoke
from agent_runner.github.client import (
    TOKEN_RE,
    GitHubClient,
    GitHubError,
    GitHubResponse,
    GitHubSettings,
    settings_for,
)
from agent_runner.requests.service import RESPOND_DELAYS, Flow
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
USER_CODE_RE = re.compile(r"^[A-Z0-9-]{4,16}$")
VERIFICATION_RE = re.compile(
    r"^(https://[!-~]{1,2040}|http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[!-~]{0,2000})?)$"
)
DEVICE_CODE_RE = re.compile(r"^[!-~]{1,256}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
SCOPE_RE = re.compile(r"^[a-z0-9:_-]{1,64}$")
MAX_EXPIRES = 3600
DEFAULT_INTERVAL = 5
SLOW_DOWN_STEP = 5
MAX_INTERVAL = 120
UNREACHABLE_LIMIT = 5
MAX_SCOPES = 64
DEVICE_CODE_DELAYS = (2, 8)
"""Seconds before the second and the third attempt of `POST /login/device/code`."""
TRANSIENT = frozenset({"unreachable", "server_error"})
APPS = ("oauth_app", "github_app")
OAUTH_SCOPES = "repo workflow"
"""`repo`: every repository the account can reach, public and private; `workflow`: commits that add or change files
under `.github/workflows` (GitHub refuses them without it)."""


def flow_app(settings: GitHubSettings, app: str | None = None) -> tuple[str, str, str | None] | None:
    """`(app, client_id, scope)` of the device flow `app` (None: the preferred one), or None when it is not set up."""
    chosen = app if app is not None else ("oauth_app" if settings.oauth_client_id is not None else "github_app")
    if chosen == "oauth_app":
        return None if settings.oauth_client_id is None else (chosen, settings.oauth_client_id, OAUTH_SCOPES)
    return None if settings.client_id is None else (chosen, settings.client_id, None)


class ConnectFailed(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _code(error: GitHubError) -> str:
    return "upstream_unreachable" if error.retryable else "upstream_error"


def _detail(step: str, error: GitHubError) -> str:
    """`step=<step> kind=<kind>[ status=<n>][ cause=<class>]` of a failed GitHub call (no secret)."""
    parts = [f"step={step}", f"kind={error.kind}"]
    if error.status is not None:
        parts.append(f"status={error.status}")
    if error.cause:
        parts.append(f"cause={error.cause}")
    return " ".join(parts)


def _failed(step: str, error: GitHubError) -> ConnectFailed:
    return ConnectFailed(_code(error), _detail(step, error))


async def start(flow: Flow, web: GitHubClient, client_id: str, scope: str | None = None) -> GitHubResponse:
    """`POST /login/device/code`, tried again after a transient failure (`DEVICE_CODE_DELAYS`)."""
    form = {"client_id": client_id} if scope is None else {"client_id": client_id, "scope": scope}
    attempt = 0
    while True:
        try:
            return await web.web_form("/login/device/code", form)
        except GitHubError as error:
            if error.kind not in TRANSIENT or attempt == len(DEVICE_CODE_DELAYS):
                raise _failed("device_code", error) from None
            flow.service.line(
                f"github_connect retry request_id={flow.id} attempt={attempt + 1} {_detail('device_code', error)}"
            )
            await flow.sleep(DEVICE_CODE_DELAYS[attempt])
            attempt += 1


def _seconds(value: object, default: int | None = None) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if default is not None and value is None:
        return default
    raise ConnectFailed("upstream_error")


def _device_code(data: object) -> tuple[str, str, str, int, int]:
    """`(device_code, user_code, verification_uri, expires_in, interval)` of GitHub's answer."""
    if not isinstance(data, dict):
        raise ConnectFailed("upstream_error")
    device_code, user_code, uri = data.get("device_code"), data.get("user_code"), data.get("verification_uri")
    if not (
        isinstance(device_code, str)
        and DEVICE_CODE_RE.fullmatch(device_code)
        and isinstance(user_code, str)
        and USER_CODE_RE.fullmatch(user_code)
        and isinstance(uri, str)
        and VERIFICATION_RE.fullmatch(uri)
    ):
        raise ConnectFailed("upstream_error")
    expires = min(_seconds(data.get("expires_in")), MAX_EXPIRES)
    interval = min(_seconds(data.get("interval"), DEFAULT_INTERVAL), MAX_INTERVAL)
    return device_code, user_code, uri, expires, interval


def scopes(value: object) -> list[str]:
    """The granted scopes (`scope` is comma separated; GitHub App user tokens have none)."""
    if not isinstance(value, str):
        return []
    found = [item.strip() for item in value.split(",") if item.strip()]
    return sorted({item for item in found if SCOPE_RE.fullmatch(item)})[:MAX_SCOPES]


async def poll(flow: Flow, web: GitHubClient, client_id: str, grant: tuple[str, int, int]) -> dict[str, Any]:
    """Poll until GitHub issues the token (its answer object) or the flow ends (`ConnectFailed`); `grant` is
    `(device_code, interval, expires_in)`."""
    device_code, interval, expires = grant
    end = flow.service.clock() + min(expires, flow.remaining())
    form = {"client_id": client_id, "device_code": device_code, "grant_type": GRANT_TYPE}
    unreachable = 0
    while True:
        await flow.sleep(interval)
        if flow.service.clock() > end:
            raise ConnectFailed("timeout")
        try:
            answer = await web.web_form("/login/oauth/access_token", form)
        except GitHubError as error:
            unreachable += 1
            if not error.retryable or unreachable > UNREACHABLE_LIMIT:
                raise _failed("poll", error) from None
            continue
        unreachable = 0
        data = answer.data if isinstance(answer.data, dict) else {}
        error_code = data.get("error")
        if error_code is None and "access_token" in data:
            return data
        if error_code == "authorization_pending":
            continue
        if error_code == "slow_down":
            named = data.get("interval")
            step = named if isinstance(named, int) and not isinstance(named, bool) else interval + SLOW_DOWN_STEP
            interval = min(max(step, interval + SLOW_DOWN_STEP), MAX_INTERVAL)
            continue
        if error_code == "expired_token":
            raise ConnectFailed("timeout")
        raise ConnectFailed("upstream_error")


SCOPES_HEADER = "x-oauth-scopes"
REPO_SCOPES = frozenset({"repo", "public_repo"})


async def _user(client: GitHubClient) -> tuple[str, int, GitHubResponse]:
    """`GET /user`: the login, the account ID and the answer (its headers carry the scopes of a classic token)."""
    answer = await client.request("GET", "/user")
    data = answer.data if isinstance(answer.data, dict) else {}
    login, user_id = data.get("login"), data.get("id")
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise ConnectFailed("upstream_error")
    try:
        return login, ops.integer(user_id), answer
    except GitHubError:
        raise ConnectFailed("upstream_error") from None


async def _store(
    flow: Flow, token: str, identity: tuple[str, int, list[str]], source: str, *, minted: bool = False
) -> None:
    """Credential `github {token, login, user_id, scopes, created_at, source}` under the owner's `CK`."""
    login, user_id, granted = identity
    body = {
        "token": token,
        "login": login,
        "user_id": user_id,
        "scopes": granted,
        "created_at": time_text(flow.service.sync.wall()),
        "source": source,
    }
    try:
        await put(flow.context, flow.user, "github", body, flow.id, delays=RESPOND_DELAYS, sleep=flow.sleep)
    except CredentialUnavailable as unavailable:
        flow.service.line(f"github_connect store_failed reason={unavailable.reason} code={unavailable.code}")
        raise ConnectFailed("credential_missing" if unavailable.reason == "key_missing" else "upstream_error") from None
    except HubRefusal as refusal:
        raise ConnectFailed(
            "upstream_error", f"step=store kind=hub status={refusal.status} error={refusal.code}"
        ) from None
    except HubTransportError as error:
        raise ConnectFailed("upstream_unreachable", f"step=store kind=hub {error.describe()}") from None
    finally:
        body.clear()
    if minted:  # this device minted the token through the device flow: it revokes it on disconnect (KOR-40)
        await revoke.remember(flow.context, flow.user, token)


async def _connect_operator(flow: Flow) -> tuple[str, int, list[str]]:
    """The operator's token (KOR-27, `github.operator`) checked with `GET /user` and stored; the GitHub login. An
    unusable file is `credential_missing` (its reason is logged by `read_token`). Stored with `source: "token"`: it
    is an existing token, not one of the device flow."""
    settings = settings_for(flow.context)
    state = operator.state_for(flow.context)
    try:
        token = operator.read_token(flow.context.config.operator_github_token_file, state, flow.service.line)
    except operator.Unusable as unusable:
        raise ConnectFailed("credential_missing", f"step=operator_file reason={unusable.reason}") from None
    async with settings.client(token, flow.user) as api:
        try:
            login, user_id, answer = await _user(api)
        except GitHubError as error:
            raise _failed("user", error) from None
    identity = (login, user_id, scopes(answer.headers.get(SCOPES_HEADER)))
    await _store(flow, token, identity, "token")
    return identity


async def _device_flow(flow: Flow, app: str | None) -> tuple[str, int, list[str]]:
    """The device flow of `app` (None: the preferred one) up to the stored credential; `(login, user_id, scopes)`."""
    settings = settings_for(flow.context)
    chosen = flow_app(settings, app)
    if chosen is None:
        flow.service.line(f"github_connect client_id_missing app={app or 'preferred'}")
        raise ConnectFailed("flow_unavailable", f"step=request kind=app app={app or 'preferred'}")
    name, client_id, scope = chosen
    flow.service.line(f"github_connect device_flow request_id={flow.id} app={name}")
    async with settings.client(None) as web:
        started = await start(flow, web, client_id, scope)
        device_code, user_code, uri, expires, interval = _device_code(started.data)
        await flow.respond(
            {"step": "device_code", "user_code": user_code, "verification_uri": uri, "expires_in": expires}
        )
        issued = await poll(flow, web, client_id, (device_code, interval, expires))
    token = issued.pop("access_token", None)
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ConnectFailed("token_invalid")
    async with settings.client(token, flow.user) as api:
        try:
            login, user_id, _answer = await _user(api)
        except GitHubError as error:
            raise _failed("user", error) from None
    identity = (login, user_id, scopes(issued.get("scope")))
    await _store(flow, token, identity, "device_flow", minted=True)
    return identity


async def _token(flow: Flow, token: str) -> tuple[str, int, list[str]]:
    """Check an existing token with `GET /user` and store it; `(login, user_id, scopes)`."""
    async with settings_for(flow.context).client(token, flow.user) as api:
        try:
            login, user_id, answer = await _user(api)
        except GitHubError as error:
            if error.kind == "unauthorized":
                raise ConnectFailed("token_refused", _detail("user", error)) from None
            if error.sso:
                raise ConnectFailed("sso_required", _detail("user", error)) from None
            raise _failed("user", error) from None
    header = answer.headers.get(SCOPES_HEADER)
    granted = scopes(header)
    if header is not None and not REPO_SCOPES & set(granted):
        raise ConnectFailed("scope_missing", "step=user kind=scopes")
    identity = (login, user_id, granted)
    await _store(flow, token, identity, "token")
    return identity


async def _connect(flow: Flow) -> tuple[str, int, list[str], str]:
    """The connection the body asks for: `(login, user_id, scopes, source)`."""
    body = flow.body
    app = body.get("app")
    if set(body) - {"token", "app"} or len(body) > 1 or (app is not None and app not in APPS):
        raise ConnectFailed("upstream_error", "step=request kind=body")
    if "token" not in body:
        return (*await _device_flow(flow, app), "device_flow")
    token = body["token"]
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ConnectFailed("token_invalid", "step=request kind=token")
    return (*await _token(flow, token), "token")


async def connect(flow: Flow) -> None:
    by_operator = operator.approved(flow.context, flow.user)
    source = " source=operator" if by_operator else ""
    try:
        if by_operator:
            login, _user_id, granted = await _connect_operator(flow)
            kind = "token"
        else:
            login, _user_id, granted, kind = await _connect(flow)
    except ConnectFailed as failed:
        detail = f" {failed.detail}" if failed.detail else ""
        flow.service.line(f"github_connect failed request_id={flow.id} code={failed.code}{source}{detail}")
        if by_operator:
            operator.state_for(flow.context).failures += 1
        await flow.respond({"step": "failed", "code": failed.code}, final=True)
        return
    shown = ",".join(granted) or "-"
    flow.service.line(f"github_connect done request_id={flow.id}{source or ' source=' + kind} scopes={shown}")
    if by_operator:
        operator.state_for(flow.context).connects += 1
    await flow.respond({"step": "done", "login": login, "scopes": granted, "source": kind}, final=True)
