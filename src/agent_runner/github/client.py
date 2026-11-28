"""GitHub HTTP client of the runner (05-runner.md section 12.1 "Rate limits" and headers, ADR-015).

REST only, never the `git` binary; the one exception is `graphql` (HOT-30: GitHub changes a pull request's draft
state only through GraphQL), which posts to GitHub's GraphQL endpoint with the same headers. Every API request
carries `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` and, with a token,
`Authorization: Bearer <token>`; the timeout is 30 s; redirects
are never followed, except by `download` (tarballs, RUN-10), which follows exactly one redirect to the codeload host of
the configured web URL and never forwards the token there.

Answers are classified into `GitHubError` with a closed `kind`:

| Answer | `kind` | retryable |
|---|---|---|
| connection error, timeout | `unreachable` (`cause`: the transport error's class, e.g. `ReadError`) | yes |
| `5xx` | `server_error` | yes |
| `429`; `403` with `Retry-After` or the secondary-limit message | `rate_limited` (`Retry-After`, else 60 s) | yes |
| `403`/`429` with `x-ratelimit-remaining: 0` (primary limit, see below) | `rate_limited` (seconds to the reset) | yes |
| `401` | `unauthorized` (token revoked or unknown) | no |
| other `403` | `forbidden` (`sso` when `x-github-sso: required...`: an organisation requires SSO authorisation) | no |
| `404`, `409`, `410`, `422` | `not_found`, `conflict`, `gone` (e.g. issues disabled), `unprocessable` | no |
| `3xx`, any other status, a body that is not JSON or larger than the limit | `invalid_response` | no |

A refusal for the primary limit whose reset is at most 60 s away is waited for once and the request repeated. A
successful answer with `x-ratelimit-remaining: 0` closes the rate gate of the token's owner until the reset: the next
request waits for it when it is at most 60 s away and fails `rate_limited` (retryable, `retry_after`) otherwise
(05 section 12.1: "wait until reset (<= 60 s else retryable failure)"). Retries are the pipeline's (RUN-07, 03-api.md
section 9: 3 attempts, 2 s, 8 s, 30 s, honouring `retry_after`); the client itself repeats a request only after a
primary-limit wait.

`GitHubSettings` (one per daemon, `settings_for(context)`) carries the configured URLs, the client IDs (GitHub App,
OAuth App), the GitHub App's URL name, the clocks and the rate gates per owner; tests may set `github_options`
(`GitHubOptions`) on the context.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
"""A GitHub token as stored and sent: printable ASCII without spaces, at most 4096 characters."""

ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
USER_AGENT = "agent-runner"
TIMEOUT_S = 30.0
MAX_BODY_BYTES = 16 * 1024 * 1024
"""Upper bound of a JSON answer (pages of at most 100 items; pull-request patches are bounded by GitHub)."""
RATE_WAIT_MAX_S = 60.0
DEFAULT_RETRY_AFTER_S = 60.0
RETRY_AFTER_MAX_S = 3600.0
PER_PAGE = 100
SECONDARY_TEXT = "secondary rate limit"
KINDS = frozenset(
    {
        "unreachable",
        "server_error",
        "rate_limited",
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "gone",
        "unprocessable",
        "invalid_response",
    }
)
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_STATUS_KINDS = {401: "unauthorized", 404: "not_found", 409: "conflict", 410: "gone", 422: "unprocessable"}
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_SSO_URL_RE = re.compile(r"url=(\S+)")
_SSO_PATH_RE = re.compile(r"^/orgs/[A-Za-z0-9-]{1,39}/sso$")
_SSO_QUERY_RE = re.compile(r"^authorization_request=[A-Za-z0-9_.~-]{1,512}$")
TOKEN_KINDS = (
    ("ghu_", "github_app"),
    ("gho_", "oauth_app"),
    ("ghp_", "classic"),
    ("github_pat_", "fine_grained"),
)
"""Prefixes of GitHub's token formats and the kind of token they mark (KOR-65); anything else is `unknown`."""


def token_kind(token: str | None) -> str:
    """`github_app` (a GitHub App user token), `oauth_app` (an OAuth App token, also the GitHub CLI's), `classic`,
    `fine_grained` (personal access tokens) or `unknown`, from the token's prefix."""
    for prefix, kind in TOKEN_KINDS:
        if token is not None and token.startswith(prefix):
            return kind
    return "unknown"


def sso_link(web_url: str, header: str) -> str | None:
    """The authorisation URL of an `x-github-sso: required; url=...` header when it is
    `<web origin>/orgs/<org>/sso?authorization_request=<id>` on the configured web host, else None."""
    match = _SSO_URL_RE.search(header)
    if match is None:
        return None
    web, target = urlsplit(web_url), urlsplit(match.group(1))
    if (target.scheme, target.netloc) != (web.scheme, web.netloc) or target.fragment:
        return None
    if not _SSO_PATH_RE.fullmatch(target.path) or not _SSO_QUERY_RE.fullmatch(target.query):
        return None
    return match.group(1)


class GitHubError(Exception):
    """A refused or failed GitHub request: closed `kind`, HTTP `status`, whether a retry may succeed, `retry_after`
    seconds, GitHub's `message` (kept for classification only, never passed on) and, of `unreachable`, the `cause`:
    the class name of the transport error (`ConnectError`, `ReadError`, `RemoteProtocolError`, ...; it names the
    phase - connecting, sending, reading - and carries no secret) and, of `forbidden`, `sso`: GitHub named an
    organisation that requires SSO authorisation of the token (header `x-github-sso: required; url=...`) and
    `sso_url`: that URL when it is exactly GitHub's authorisation page of an organisation on the configured web host
    (`sso_link`; KOR-65: the one next step shown to the person, never anything else of the header)."""

    def __init__(
        self,
        kind: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        message: str = "",
        cause: str = "",
        sso: bool = False,
        sso_url: str | None = None,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown GitHub error kind: {kind!r}")
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.message = message
        self.cause = cause
        self.sso = sso
        self.sso_url = sso_url


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    data: Any
    headers: httpx.Headers


@dataclass
class RateGate:
    """Wall-clock time until which the primary rate limit of one owner's token is exhausted."""

    blocked_until: float = 0.0


@dataclass
class GitHubOptions:
    """Test injection: clocks of the client (wall clock for rate-limit resets, sleep) and the HTTP transport."""

    wall: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] | None = None
    transport: httpx.AsyncBaseTransport | None = None


@dataclass
class GitHubSettings:
    api_url: str
    web_url: str
    client_id: str | None
    wall: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    transport: httpx.AsyncBaseTransport | None = None
    gates: dict[str, RateGate] = field(default_factory=dict)
    oauth_client_id: str | None = None
    """The public client ID of the OAuth App whose device flow `github.connect` prefers (KOR-65); None: not set up."""
    app_slug: str | None = None
    """The URL name of the GitHub App (`<web>/apps/<slug>`), for the install link of a next step (KOR-65)."""

    def gate(self, owner: str) -> RateGate:
        return self.gates.setdefault(owner, RateGate())

    def client(self, token: str | None, owner: str | None = None) -> GitHubClient:
        """A client for one use (close it with `aclose` or `async with`)."""
        made = GitHubClient(
            api_url=self.api_url,
            web_url=self.web_url,
            token=token,
            wall=self.wall,
            sleep=self.sleep,
            gate=self.gate(owner) if owner is not None else RateGate(),
            transport=self.transport,
        )
        made.oauth_client_id, made.app_slug = self.oauth_client_id, self.app_slug
        return made


ATTRIBUTE = "github_settings"


def settings_for(context: Any) -> GitHubSettings:
    """The GitHub settings of this daemon (built on first use from `context.config`)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, GitHubSettings):
        return found
    options = getattr(context, "github_options", None)
    options = options if isinstance(options, GitHubOptions) else GitHubOptions()
    config = context.config
    built = GitHubSettings(
        api_url=str(config.github_api_url).rstrip("/"),
        web_url=str(config.github_web_url).rstrip("/"),
        client_id=config.github_client_id,
        wall=options.wall,
        sleep=options.sleep or context.host.sleep,
        transport=options.transport,
        oauth_client_id=getattr(config, "github_oauth_client_id", None),
        app_slug=getattr(config, "github_app_slug", None),
    )
    setattr(context, ATTRIBUTE, built)
    return built


def _header_number(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name, "")
    return int(value) if value.isascii() and value.isdigit() else None


def _message(data: object) -> str:
    """GitHub's `message` and, of a `422`, the `message` and `field:<name>` of each `errors` item, joined by `; `."""
    if not isinstance(data, dict):
        return ""
    parts = [data.get("message")]
    errors = data.get("errors")
    for item in errors if isinstance(errors, list) else []:
        if isinstance(item, dict):
            parts.append(item.get("message"))
            field = item.get("field")
            parts.append(f"field:{field}" if isinstance(field, str) else None)
    return "; ".join(part for part in parts if isinstance(part, str))


def _parse(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise GitHubError("invalid_response") from None


def graphql_url(api_url: str) -> str:
    """GitHub's GraphQL endpoint of an API URL: `<api>/graphql` (github.com, the double), and `<host>/api/graphql`
    for the API URL `<host>/api/v3` of a GitHub Enterprise Server."""
    base = api_url.rstrip("/")
    return base.removesuffix("/v3") + "/graphql" if base.endswith("/api/v3") else base + "/graphql"


def codeload_allowed(web_url: str, location: str) -> bool:
    """A tarball redirect target on the codeload host of the configured web URL: `https://codeload.<host>/...`, or
    the double's `<web origin>/codeload/...` (the double serves codeload under its own origin)."""
    web, target = urlsplit(web_url), urlsplit(location)
    if target.username is not None or target.password is not None or not target.netloc:
        return False
    if target.scheme == "https" and target.netloc == f"codeload.{web.netloc}":
        return True
    return (target.scheme, target.netloc) == (web.scheme, web.netloc) and target.path.startswith("/codeload/")


class GitHubClient:
    """One token's requests to the GitHub API (and the web host for the device flow)."""

    def __init__(
        self,
        *,
        api_url: str,
        web_url: str,
        token: str | None,
        wall: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        gate: RateGate,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self._token = token
        self.token_kind = token_kind(token)
        """The kind of the token (`token_kind`), kept after `aclose` for the next step of a refusal (KOR-65)."""
        self.source: str | None = None
        """How the connection was made (`device_flow`, `token`; credential body), set by `ops.actor_client`."""
        self.oauth_client_id: str | None = None
        self.app_slug: str | None = None
        """The OAuth App's client ID and the GitHub App's URL name of the settings (`GitHubSettings.client`), for the
        next step of an access refusal (`reach`)."""
        self.wall = wall
        self.sleep = sleep
        self.gate = gate
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=timeout, trust_env=False)

    async def aclose(self) -> None:
        self._token = None
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION, "User-Agent": USER_AGENT}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _gate_wait(self) -> None:
        wait = self.gate.blocked_until - self.wall()
        if wait <= 0:
            return
        if wait > RATE_WAIT_MAX_S:
            raise GitHubError("rate_limited", retryable=True, retry_after=wait)
        await self.sleep(wait)

    def _note_limit(self, headers: httpx.Headers) -> float | None:
        """Close the gate on `x-ratelimit-remaining: 0`; the seconds until the reset (None when not exhausted)."""
        if headers.get("x-ratelimit-remaining") != "0":
            return None
        reset = _header_number(headers, "x-ratelimit-reset")
        until = float(reset) if reset is not None else self.wall() + DEFAULT_RETRY_AFTER_S
        self.gate.blocked_until = max(self.gate.blocked_until, until)
        return max(until - self.wall(), 0.0)

    async def _send(self, request: httpx.Request, limit: int) -> tuple[int, httpx.Headers, bytes]:
        try:
            response = await self._client.send(request, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise GitHubError("invalid_response", status=response.status_code)
                    chunks.append(chunk)
            finally:
                await response.aclose()
        except httpx.HTTPError as error:
            raise GitHubError("unreachable", retryable=True, cause=type(error).__name__) from None
        return response.status_code, response.headers, b"".join(chunks)

    def _refusal(self, status: int, headers: httpx.Headers, data: object, exhausted: float | None) -> GitHubError:
        message = _message(data)
        retry_after = _header_number(headers, "retry-after")
        if status >= 500:
            return GitHubError("server_error", status=status, retryable=True, message=message)
        if status in (403, 429):
            if exhausted is not None:
                return GitHubError("rate_limited", status=status, retryable=True, retry_after=exhausted)
            if retry_after is not None or status == 429 or SECONDARY_TEXT in message.lower():
                after = min(float(retry_after), RETRY_AFTER_MAX_S) if retry_after is not None else None
                return GitHubError(
                    "rate_limited", status=status, retryable=True, retry_after=after or DEFAULT_RETRY_AFTER_S
                )
            header = headers.get("x-github-sso", "")
            sso = header.lower().startswith("required")
            link = sso_link(self.web_url, header) if sso else None
            return GitHubError("forbidden", status=status, message=message, sso=sso, sso_url=link)
        kind = _STATUS_KINDS.get(status, "invalid_response")
        return GitHubError(kind, status=status, message=message)

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
        url: str | None = None,
    ) -> GitHubResponse:
        """One API request (`path` below the API URL, or an absolute `url` of a `Link` header on the API host)."""
        target = url if url is not None else self.api_url + path
        waited = False
        while True:
            await self._gate_wait()
            content = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers = self._headers()
            if content is not None:
                headers["Content-Type"] = "application/json"
            request = self._client.build_request(method, target, headers=headers, params=params, content=content)
            status, answer_headers, raw = await self._send(request, MAX_BODY_BYTES)
            exhausted = self._note_limit(answer_headers)
            if 200 <= status < 300:
                return GitHubResponse(status, _parse(raw), answer_headers)
            if status in (403, 429) and exhausted is not None and exhausted <= RATE_WAIT_MAX_S and not waited:
                waited = True
                continue
            try:
                data = _parse(raw)
            except GitHubError:
                data = None
            raise self._refusal(status, answer_headers, data, exhausted)

    async def graphql(self, query: str, variables: Mapping[str, object]) -> dict[str, Any]:
        """One GraphQL request (`graphql_url`, the same headers and refusals as `request`) and its `data`. GitHub
        answers GraphQL failures with `200` and `errors`: an error of type `NOT_FOUND` is `not_found`, `FORBIDDEN`
        `forbidden`, any other `unprocessable` (GitHub's messages kept for classification only); an answer without a
        `data` object is `invalid_response`."""
        body = {"query": query, "variables": dict(variables)}
        answer = await self.request("POST", "", body=body, url=graphql_url(self.api_url))
        data = answer.data if isinstance(answer.data, dict) else {}
        errors = data.get("errors")
        if errors:
            items = [item for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
            kinds = {item.get("type") for item in items}
            kind = "not_found" if "NOT_FOUND" in kinds else "forbidden" if "FORBIDDEN" in kinds else "unprocessable"
            message = "; ".join(str(item["message"]) for item in items if isinstance(item.get("message"), str))
            raise GitHubError(kind, status=answer.status, message=message)
        found = data.get("data")
        if not isinstance(found, dict):
            raise GitHubError("invalid_response", status=answer.status)
        return found

    async def items(self, path: str, *, key: str | None = None, max_pages: int = 100) -> list[Any]:
        """Every item of a paginated list (`per_page=100`, `Link: rel="next"`); `key` names the array inside an object
        answer (e.g. `installations`). Only the path and query of a next link are used, below the configured API URL,
        so the token never leaves the configured host."""
        found: list[Any] = []
        url: str | None = None
        params: Mapping[str, str | int] | None = {"per_page": PER_PAGE}
        for _ in range(max_pages):
            answer = await self.request("GET", path, params=params, url=url)
            page = answer.data.get(key) if key is not None and isinstance(answer.data, dict) else answer.data
            if not isinstance(page, list):
                raise GitHubError("invalid_response", status=answer.status)
            found.extend(page)
            match = _LINK_NEXT_RE.search(answer.headers.get("link", ""))
            if match is None:
                return found
            url, params = self._next_url(match.group(1)), None
        raise GitHubError("invalid_response")

    def _next_url(self, link: str) -> str:
        """The configured API URL with the path and query of a next link below the API base path."""
        base, parts = urlsplit(self.api_url), urlsplit(link)
        if not parts.path.startswith(base.path.rstrip("/") + "/"):
            raise GitHubError("invalid_response")
        query = f"?{parts.query}" if parts.query else ""
        return f"{base.scheme}://{base.netloc}{parts.path}{query}"

    async def web_form(self, path: str, form: Mapping[str, str]) -> GitHubResponse:
        """`POST` of a form to the web host (device flow), JSON answer requested; no token is sent."""
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        request = self._client.build_request("POST", self.web_url + path, headers=headers, data=dict(form))
        status, answer_headers, raw = await self._send(request, MAX_BODY_BYTES)
        if not 200 <= status < 300:
            raise self._refusal(status, answer_headers, None, None)
        return GitHubResponse(status, _parse(raw), answer_headers)

    async def download(self, path: str, write: Callable[[bytes], None], *, max_bytes: int) -> int:
        """Stream an API download (`GET path`) into `write`, following one redirect to the codeload host of the web
        URL without the token; the number of bytes. More than `max_bytes` is `invalid_response`."""
        await self._gate_wait()
        request = self._client.build_request("GET", self.api_url + path, headers=self._headers())
        try:
            response = await self._client.send(request, stream=True)
            try:
                self._note_limit(response.headers)
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location", "")
                    if not codeload_allowed(self.web_url, location):
                        raise GitHubError("invalid_response", status=response.status_code)
                    await response.aclose()
                    plain = self._client.build_request("GET", location, headers={"User-Agent": USER_AGENT})
                    response = await self._client.send(plain, stream=True)
                if not 200 <= response.status_code < 300:
                    raw = await response.aread()
                    raise self._refusal(response.status_code, response.headers, _safe(raw[:MAX_BODY_BYTES]), None)
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise GitHubError("invalid_response", status=response.status_code)
                    write(chunk)
                return total
            finally:
                await response.aclose()
        except httpx.HTTPError as error:
            raise GitHubError("unreachable", retryable=True, cause=type(error).__name__) from None


def _safe(raw: bytes) -> object:
    try:
        return _parse(raw)
    except GitHubError:
        return None
