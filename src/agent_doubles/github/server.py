"""The HTTP server of the GitHub double and its control routes under ``/_double/``.

Routes (`ROUTES`, the REST subset the runner uses, 05-runner.md §11.4 and §12.1, 03-api.md §9):

- Device flow on the web host: ``POST /login/device/code``, ``POST /login/oauth/access_token`` (form or JSON
  parameters; JSON answers when ``Accept`` names ``application/json``, else form-encoded like GitHub) and
  ``GET /login/device`` (an informational page; the decision is made through ``POST /_double/device``).
- REST on the API host: user, installations and ``GET /user/repos``, repositories, commits, tarball (``302`` to the
  codeload-like route ``GET /codeload/{owner}/{repo}/legacy.tar.gz/{ref}``), refs, the Git Data API, compare, pull
  requests (``GET …/pulls`` filters by ``state``, ``head``, ``base`` and sorts by ``sort``/``direction``), issues
  (``GET …/issues``: issues and pull requests in issue form, filtered by ``state`` and ``labels``, sorted by
  ``sort``/``direction``; ``GET``/``PATCH …/issues/{number}``, ``POST …/issues``) and issue comments. A repository
  with ``has_issues: false`` answers the issue routes ``410 Issues are disabled for this repo`` (comments too, unless
  the number is a pull request).
- ``POST /credentials/revoke`` (``{"credentials": [token, ...]}``, KOR-40): unauthenticated like GitHub's (a request
  with ``Authorization`` is ``403`` and a failure); known tokens are forgotten, answered ``202 {}``. The journal
  keeps only the length of each token (`CREDENTIAL_FIELDS`, HOT-11), like the ``Authorization`` header.

API requests must carry ``Accept: application/vnd.github+json``, ``X-GitHub-Api-Version: 2022-11-28`` and
``Authorization: Bearer <token>`` (``token <token>`` is accepted too, as by GitHub). A missing or different header is
answered as GitHub would and recorded as a failure (an unsupported version is ``400``, a missing credential ``401``);
an unknown or revoked token is a plain ``401 Bad credentials`` (no failure: revoked tokens are a legitimate test
case). With a GitHub App token a repository the token's user cannot reach through an installation is ``404``, a write
without the installation permission ``403 Resource not accessible by integration``. A personal access token
(``classic``, ``fine_grained``) reaches its repositories without an installation; installation routes answer it
``403``, a classic token without the scope ``repo`` (``public_repo`` for a public repository) cannot write
(``403 Resource not accessible by personal access token``), and ``GET /user`` names its scopes in ``X-OAuth-Scopes``.
``personal_tokens`` of the seed
(KOR-27) are classic tokens with the scope ``repo``. A repository of an organisation in the token's ``sso`` list is
``403`` with ``X-GitHub-SSO`` (SAML enforcement; the authorisation URL is on the double's own web origin). An OAuth
token (``gho_``, the device flow of the OAuth App or a seeded one) reaches every repository of its user; an
organisation's OAuth App restriction and classic-token policy answer ``403`` with GitHub's messages (KOR-65).
Unknown routes answer ``404``, are journaled and are
failures; so are invalid JSON bodies, forced ref updates (the runner never forces) and a codeload request that
carries an ``Authorization`` header (a token forwarded across the redirect).

Every API answer carries the rate-limit headers (``x-ratelimit-limit``/``-remaining``/``-used``/``-reset``/
``-resource``) per user; faults (`agent_doubles.github.faults`) inject errors, rate limits, delays and disconnects.

Control routes (not journaled, not counted): ``GET /_double/state``, ``GET /_double/journal``,
``POST /_double/reset`` (empties the world, the plan, the device grants, the failures and the journal in memory),
``POST /_double/seed`` (`agent_doubles.github.repos`), ``GET|POST /_double/faults`` (``{"faults": [...]}``),
``POST /_double/expect`` (``{"expectations": [...]}``), ``GET|POST /_double/device`` (grants; user decision),
``POST /_double/clock`` (``{"advance": seconds}``), ``POST /_double/tokens`` (``{"revoke": "<token>"}``),
``POST /_double/organizations`` (an organisation's OAuth App and classic-token policies, KOR-65) and
``GET /_double/repos/{owner}/{repo}`` (refs, issues, pull requests and comments for assertions).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from agent_doubles.common.journal import Entry, Journal, decode_body, redact_fields, redact_headers
from agent_doubles.common.server import BodyError, DoubleHandler, DoubleServer
from agent_doubles.common.state import state_document
from agent_doubles.github import render
from agent_doubles.github.deviceflow import DeviceFlow, DeviceFlowError
from agent_doubles.github.faults import CONTINUING, Fault, FaultError, Plan, parse_expectation, parse_fault
from agent_doubles.github.objects import (
    BLOB_MODES,
    FILE_MODE,
    TREE_MODE,
    Commit,
    ObjectError,
    ObjectStore,
    Signature,
    git_mode,
)
from agent_doubles.github.repos import (
    SHA_RE,
    STATE_REASONS,
    Issue,
    Pull,
    Repo,
    SeedError,
    Token,
    User,
    World,
    parse_iso,
    tarball,
)

DOUBLE_NAME = "github"
# Body fields GitHub's API defines as credentials; the journal keeps only their length (HOT-11).
CREDENTIAL_FIELDS = frozenset({"credentials", "access_token", "refresh_token", "client_secret"})
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
RATE_LIMIT = 5000
RATE_WINDOW = 3600
PER_PAGE = 30
MAX_PER_PAGE = 100
SECONDARY_MESSAGE = (
    "You have exceeded a secondary rate limit. Please wait a few minutes before you try again. If you reach out to "
    "GitHub Support for help, please include the request ID."
)
MISMATCH_PATH = ".double-tree-mismatch"
ISSUES_DISABLED = render.error_body(410, "Issues are disabled for this repo", docs="https://docs.github.com/v3/issues/")
APP_TOKEN_NEEDED = render.error_body(
    403,
    "You must authenticate with an access token authorized to a GitHub App in order to list installations",
    docs="https://docs.github.com/rest/apps/installations#list-app-installations-accessible-to-the-user-access-token",
)
SAML_MESSAGE = (
    "Resource protected by organization SAML enforcement. You must grant your Personal Access token access to this "
    "organization."
)
SAML_DOCS = "https://docs.github.com/articles/authenticating-to-a-github-organization-with-saml-single-sign-on/"
OAUTH_RESTRICTED_MESSAGE = (
    "Although you appear to have the correct authorization credentials, the `{org}` organization has enabled OAuth App"
    " access restrictions, meaning that data access to third-parties is limited. For more information on these"
    " restrictions, including how to enable this app, visit"
    " https://docs.github.com/articles/restricting-access-to-your-organization-s-data/"
)
OAUTH_DOCS = "https://docs.github.com/articles/restricting-access-to-your-organization-s-data/"
CLASSIC_FORBIDDEN_MESSAGE = (
    "`{org}` forbids access via a personal access token (classic). Please use a GitHub App, OAuth App, or a personal"
    " access token with fine-grained permissions."
)
ISSUE_SORTS = frozenset({"created", "updated", "comments"})
PULL_SORTS = frozenset({"created", "updated", "popularity", "long-running"})
DEVICE_PAGE = (
    b"<!doctype html><title>Device activation</title><h1>Device activation (GitHub double)</h1>"
    b"<p>The test decides through POST /_double/device.</p>"
)
_PARAM_PATTERNS = {
    "owner": r"[^/]+",
    "repo": r"[^/]+",
    "installation_id": r"[0-9]+",
    "number": r"[0-9]+",
    "sha": r"[^/]+",
    "tree": r".+",
    "ref": r".+",
    "basehead": r".+",
}


@dataclass(frozen=True)
class Route:
    """One route: method, path template, area (``login``, ``api``, ``codeload``) and handler name."""

    method: str
    template: str
    area: str
    name: str
    pattern: re.Pattern[str] = field(init=False, compare=False)

    def __post_init__(self) -> None:
        regex = re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m.group(1)}>{_PARAM_PATTERNS[m.group(1)]})", self.template)
        object.__setattr__(self, "pattern", re.compile(f"^{regex}$"))

    @property
    def key(self) -> str:
        """``METHOD /template``: how faults and expectations name the route."""
        return f"{self.method} {self.template}"


ROUTES = (
    Route("POST", "/login/device/code", "login", "device_code"),
    Route("POST", "/login/oauth/access_token", "login", "access_token"),
    Route("GET", "/login/device", "login", "device_page"),
    Route("GET", "/user", "api", "user"),
    Route("GET", "/user/installations", "api", "installations"),
    Route("GET", "/user/installations/{installation_id}/repositories", "api", "installation_repositories"),
    Route("GET", "/user/repos", "api", "user_repos"),
    Route("GET", "/repos/{owner}/{repo}", "api", "repository"),
    Route("GET", "/repos/{owner}/{repo}/commits/{ref}", "api", "commit"),
    Route("GET", "/repos/{owner}/{repo}/tarball/{ref}", "api", "tarball"),
    Route("GET", "/codeload/{owner}/{repo}/legacy.tar.gz/{ref}", "codeload", "codeload"),
    Route("GET", "/repos/{owner}/{repo}/git/ref/{ref}", "api", "get_ref"),
    Route("POST", "/repos/{owner}/{repo}/git/refs", "api", "create_ref"),
    Route("PATCH", "/repos/{owner}/{repo}/git/refs/{ref}", "api", "update_ref"),
    Route("POST", "/repos/{owner}/{repo}/git/blobs", "api", "create_blob"),
    Route("GET", "/repos/{owner}/{repo}/git/blobs/{sha}", "api", "get_blob"),
    Route("POST", "/repos/{owner}/{repo}/git/trees", "api", "create_tree"),
    Route("GET", "/repos/{owner}/{repo}/git/trees/{tree}", "api", "get_tree"),
    Route("POST", "/repos/{owner}/{repo}/git/commits", "api", "create_commit"),
    Route("GET", "/repos/{owner}/{repo}/git/commits/{sha}", "api", "get_commit"),
    Route("GET", "/repos/{owner}/{repo}/compare/{basehead}", "api", "compare"),
    Route("GET", "/repos/{owner}/{repo}/pulls", "api", "list_pulls"),
    Route("POST", "/repos/{owner}/{repo}/pulls", "api", "create_pull"),
    Route("GET", "/repos/{owner}/{repo}/pulls/{number}", "api", "get_pull"),
    Route("GET", "/repos/{owner}/{repo}/pulls/{number}/files", "api", "pull_files"),
    Route("GET", "/repos/{owner}/{repo}/issues", "api", "list_issues"),
    Route("POST", "/repos/{owner}/{repo}/issues", "api", "create_issue"),
    Route("GET", "/repos/{owner}/{repo}/issues/{number}", "api", "get_issue"),
    Route("PATCH", "/repos/{owner}/{repo}/issues/{number}", "api", "update_issue"),
    Route("GET", "/repos/{owner}/{repo}/issues/{number}/comments", "api", "list_comments"),
    Route("POST", "/repos/{owner}/{repo}/issues/{number}/comments", "api", "create_comment"),
    Route("POST", "/credentials/revoke", "revoke", "revoke_credentials"),
)
ROUTE_KEYS = frozenset(route.key for route in ROUTES)
# What an API route handler returns: status, JSON payload (bytes for an empty redirect body) and extra headers.
Result = tuple[int, Any, list[tuple[str, str]]]
# Which installation permission a write route needs.
WRITE_PERMISSIONS = {
    "create_ref": "contents",
    "update_ref": "contents",
    "create_blob": "contents",
    "create_tree": "contents",
    "create_commit": "contents",
    "create_pull": "pull_requests",
    "create_comment": "issues",
    "create_issue": "issues",
    "update_issue": "issues",
}


def match_route(method: str, path: str) -> tuple[Route, dict[str, str]] | None:
    """The route for `method` and the (percent-decoded) `path`, with its parameters."""
    for route in ROUTES:
        if route.method == method:
            found = route.pattern.match(path)
            if found is not None:
                return route, found.groupdict()
    return None


class Reply(Exception):
    """An early answer from inside a route handler: status, body and extra headers."""

    def __init__(self, status: int, payload: Any, headers: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(status)
        self.status = status
        self.payload = payload
        self.headers = headers


def not_found() -> Reply:
    return Reply(404, render.error_body(404, "Not Found"))


def unprocessable(message: str) -> Reply:
    return Reply(422, render.error_body(422, message))


def invalid(resource: str, name: str) -> Reply:
    """``422 Validation Failed`` for an invalid parameter."""
    return Reply(422, render.validation_error("", resource, name, code="invalid"))


def not_a(name: str, value: Any, kind: str) -> Reply:
    """``422`` for a body property of the wrong JSON type, as GitHub words it."""
    return unprocessable(f"Invalid request.\n\nFor 'properties/{name}', {json.dumps(value)} is not {kind}.")


@dataclass
class Call:
    """One API request being answered."""

    entry: Entry
    route: Route
    params: dict[str, str]
    query: dict[str, str]
    raw: bytes
    headers: dict[str, str]
    user: User | None = None
    token: Token | None = None
    repo: Repo | None = None
    fault: Fault | None = None

    @property
    def the_user(self) -> User:
        assert self.user is not None
        return self.user

    @property
    def the_token(self) -> Token:
        assert self.token is not None
        return self.token

    @property
    def the_repo(self) -> Repo:
        assert self.repo is not None
        return self.repo

    def json_body(self) -> dict[str, Any]:
        """The JSON object of the body (refusal ``400 Problems parsing JSON`` otherwise)."""
        try:
            body = json.loads(self.raw or b"null")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise Reply(400, render.error_body(400, "Problems parsing JSON"))
        return body


@dataclass
class _Downloads:
    """Download tokens of private-repository tarball redirects: token → (repository key, commit)."""

    tokens: dict[str, tuple[str, str]] = field(default_factory=dict)
    count: int = 0


class GitHubDouble:
    """A running GitHub double.

    `clock` gives seconds since the epoch (device-flow timing, rate-limit resets); ``POST /_double/clock`` advances
    it. `public_url` replaces the bound URL in answers (device ``verification_uri``, ``url`` fields, ``Link`` and
    ``Location`` headers) when clients reach the double through another address. `html_url` replaces the base of the
    web links in answers only (``html_url``, avatars, blob and raw links): an ``https`` site the clients never call,
    as GitHub answers with ``https`` links; the codeload redirect and the device verification page stay on the served
    base, so a client that follows them still reaches the double.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        journal_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        public_url: str | None = None,
        html_url: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._offset = 0.0
        self._public_url = public_url.rstrip("/") if public_url else None
        self._html_url = html_url.rstrip("/") if html_url else None
        self.world = World()
        self.plan = Plan()
        self.device = DeviceFlow(self.clock, self._issue_token, lambda: self.served_url)
        self.journal = Journal(DOUBLE_NAME, journal_path)
        self._failures: list[str] = []
        self._requests = 0
        self._usage: dict[str, int] = {}
        self._reset_at: int | None = None
        self._downloads = _Downloads()
        self._token_count = 0
        self._server = DoubleServer(_handler_for(self), host, port)

    # --- lifecycle ----------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The bound base URL (put into ``GITHUB_API_URL`` and ``GITHUB_WEB_URL``)."""
        return self._server.url

    @property
    def served_url(self) -> str:
        """The base URL clients reach the double under (`public_url`, else the bound URL)."""
        return self._public_url or self.url

    @property
    def urls(self) -> render.Urls:
        base = self.served_url
        return render.Urls(base, self._html_url or base)

    def start(self) -> Self:
        self._server.start()
        return self

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- clock, failures, counters ------------------------------------------------------------------

    def clock(self) -> float:
        with self._lock:
            return self._clock() + self._offset

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._offset += seconds

    def fail(self, text: str) -> None:
        with self._lock:
            self._failures.append(text)

    def count_request(self) -> None:
        with self._lock:
            self._requests += 1

    @property
    def failures(self) -> list[str]:
        with self._lock:
            return list(self._failures)

    def rate_headers(self, user: User | None, exhausted_for: int | None = None) -> list[tuple[str, str]]:
        """Rate-limit headers after counting one request of `user` (anonymous requests share one bucket)."""
        key = user.login.lower() if user else ""
        now = int(self.clock())
        with self._lock:
            if self._reset_at is None or now >= self._reset_at:
                self._reset_at = now + RATE_WINDOW
                self._usage.clear()
            used = self._usage[key] = self._usage.get(key, 0) + 1
            reset = self._reset_at
        remaining = max(0, RATE_LIMIT - used)
        if exhausted_for is not None:
            remaining, reset = 0, now + exhausted_for
        return [
            ("x-ratelimit-limit", str(RATE_LIMIT)),
            ("x-ratelimit-remaining", str(remaining)),
            ("x-ratelimit-reset", str(reset)),
            ("x-ratelimit-used", str(min(used, RATE_LIMIT))),
            ("x-ratelimit-resource", "core"),
        ]

    def _issue_token(self, login: str, kind: str = "app", scope: str = "") -> str:
        """A new token of the device flow: a GitHub App user token (``ghu_``) or, of the OAuth App, an OAuth token
        (``gho_``) with the requested scopes (KOR-65)."""
        with self.world.lock:
            self._token_count += 1
            digest = render.node_id("token", f"{login}:{self._token_count}")
            prefix = "gho_" if kind == "oauth" else "ghu_"
            token = prefix + re.sub(r"[^A-Za-z0-9]", "", digest + "0" * 36)[:36]
            self.world.add_token(login.lower(), token, kind, scope.split())
            if kind == "oauth":
                self.world.tokens[token].client = self.device.oauth_client_id
            return token

    def download_token(self, repo: Repo, sha: str) -> str:
        with self._lock:
            self._downloads.count += 1
            token = f"DL{self._downloads.count:010d}"
            self._downloads.tokens[token] = (repo.key, sha)
            return token

    def download_allowed(self, token: str, repo: Repo, sha: str) -> bool:
        with self._lock:
            return self._downloads.tokens.get(token) == (repo.key, sha)

    # --- control ------------------------------------------------------------------------------------

    def reset(self) -> None:
        """Empty the world, the plan, the device grants, the failures and the journal in memory."""
        self.world.clear()
        self.plan.clear()
        self.device.reset()
        with self._lock:
            self._failures.clear()
            self._requests = 0
            self._usage.clear()
            self._reset_at = None
            self._downloads.tokens.clear()
            self._downloads.count = 0
        with self.world.lock:
            self._token_count = 0
        self.journal.clear()

    def seed(self, payload: Any) -> None:
        """Seed data, device-flow settings, faults and expectations; nothing is applied when any part is refused."""
        if not isinstance(payload, dict):
            raise SeedError("seed must be an object")
        faults = [parse_fault(raw, ROUTE_KEYS) for raw in _items(payload, "faults")]
        expectations = [parse_expectation(raw, ROUTE_KEYS) for raw in _items(payload, "expectations")]
        if "device_flow" in payload:
            DeviceFlow(self.clock, self._issue_token, lambda: "").configure(payload["device_flow"])
        with self.world.lock:
            self.world.seed(payload)
        if "device_flow" in payload:
            self.device.configure(payload["device_flow"])
        self.plan.add_faults(faults)
        self.plan.add_expectations(expectations)

    def state(self) -> dict[str, Any]:
        pending = self.plan.pending()
        with self._lock:
            failures, requests = list(self._failures), self._requests
        return state_document(DOUBLE_NAME, done=not pending, failures=failures, requests=requests, pending=pending)

    def inspect(self, owner: str, name: str) -> dict[str, Any] | None:
        """Refs, pull requests and comments of a repository (``GET /_double/repos/{owner}/{repo}``)."""
        with self.world.lock:
            repo = self.world.repo(owner, name)
            if repo is None:
                return None
            return {
                "full_name": repo.full_name,
                "refs": dict(sorted(repo.refs.items())),
                "issues": [
                    {
                        "number": i.number,
                        "title": i.title,
                        "body": i.body,
                        "state": i.state,
                        "state_reason": i.state_reason,
                        "labels": list(i.labels),
                        "assignees": list(i.assignees),
                        "user": i.user,
                    }
                    for i in repo.issues.values()
                ],
                "pulls": [
                    {
                        "number": p.number,
                        "title": p.title,
                        "body": p.body,
                        "head": p.head,
                        "base": p.base,
                        "draft": p.draft,
                        "state": p.state,
                        "labels": list(p.labels),
                        "user": p.user,
                    }
                    for p in repo.pulls.values()
                ],
                "comments": [{"id": c.id, "number": c.number, "body": c.body, "user": c.user} for c in repo.comments],
            }


def _items(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise FaultError(f"{key} must be a list")
    return value


def _handler_for(double: GitHubDouble) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        pass

    Handler.double = double
    return Handler


def request_id(entry: Entry) -> str:
    """The ``x-github-request-id`` of a journal entry."""
    return f"DBL0:{entry.seq:06d}"


def _page_args(query: dict[str, str]) -> tuple[int, int]:
    """``page`` (≥ 1) and ``per_page`` (1 to 100); invalid values fall back to the defaults, as GitHub does."""
    page = int(query["page"]) if query.get("page", "").isdigit() and int(query["page"]) > 0 else 1
    per_page = (
        int(query["per_page"]) if query.get("per_page", "").isdigit() and int(query["per_page"]) > 0 else PER_PAGE
    )
    return page, min(per_page, MAX_PER_PAGE)


def _signature(raw: Any, default: Signature, what: str) -> Signature:
    if raw is None:
        return default
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not isinstance(raw.get("email"), str):
        raise unprocessable(f"Invalid request.\n\nInvalid {what}: name and email are required.")
    stamp = default.time
    if "date" in raw:
        try:
            stamp = parse_iso(str(raw["date"]))
        except SeedError as exc:
            raise unprocessable(f"Invalid request.\n\nInvalid {what} date.") from exc
    return Signature(raw["name"], raw["email"], stamp)


def _tree_item_sha(objects: ObjectStore, item: dict[str, Any], path: str, kind: str) -> str | None:
    """The object id a tree entry names (a blob made from ``content``), None for a deletion (``sha: null``)."""
    if "content" in item:
        if kind != "blob" or not isinstance(item["content"], str) or item.get("sha") is not None:
            raise unprocessable(f"Invalid tree info: content of {path}")
        return objects.put_blob(item["content"].encode())
    if "sha" not in item:
        raise unprocessable(f"Invalid tree info: {path} needs sha or content")
    sha = item["sha"]
    if sha is not None and (not isinstance(sha, str) or objects.kind(sha) != kind):
        raise unprocessable(f"Invalid tree info: {kind} {sha} does not exist")
    return sha


def _valid_path(path: Any) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and "\0" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


class _Handler(DoubleHandler):
    server_version = "github-double/1"
    double: ClassVar[GitHubDouble]

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_PATCH(self) -> None:
        self._route()

    def do_PUT(self) -> None:
        self._route()

    def do_DELETE(self) -> None:
        self._route()

    # --- plumbing -------------------------------------------------------------------------------------

    def _route(self) -> None:
        parts = urlsplit(self.path)
        try:
            raw = self.read_body()
            problem: BodyError | None = None
        except BodyError as exc:
            raw, problem = b"", exc
        if parts.path.startswith("/_double/"):
            self._control(parts.path, raw, problem)
            return
        body = redact_fields(decode_body(raw), CREDENTIAL_FIELDS)
        entry = self.double.journal.open(
            self.command, parts.path, parts.query, redact_headers(self.headers.items()), body
        )
        self.double.count_request()
        try:
            if problem is not None:
                entry.note = problem.message
                self.double.fail(f"{self.command} {parts.path}: {problem.message}")
                self._send(entry, problem.status, render.error_body(problem.status, problem.message))
            else:
                self._dispatch(entry, unquote(parts.path), dict(parse_qsl(parts.query)), raw)
        finally:
            self.double.journal.close(entry)

    def _send(
        self, entry: Entry, status: int, payload: Any, headers: Iterable[tuple[str, str]] = (), *, raw: bool = False
    ) -> None:
        entry.status = status
        sent = [("x-github-request-id", request_id(entry)), *headers]
        if raw:
            data, content_type = payload
            self.send_body(status, data, content_type, sent)
        else:
            self.send_json(status, payload, sent)

    def _dispatch(self, entry: Entry, path: str, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        found = match_route(self.command, path)
        if found is None:
            text = f"unhandled route {self.command} {path}"
            entry.note = text
            double.fail(text)
            self._send(entry, 404, render.error_body(404, "Not Found"))
            return
        route, params = found
        entry.extra["route"] = route.key
        headers = {name.lower(): value for name, value in self.headers.items()}
        call = Call(entry, route, params, query, raw, headers)
        repo_key = f"{params['owner']}/{params['repo']}".lower() if "owner" in params else None
        for text in double.plan.observe(route.key, repo_key):
            double.fail(text)
        try:
            if route.area == "api":
                self._check_headers(call)
            call.fault = double.plan.take(route.key, repo_key)
            if call.fault is not None:
                entry.extra["fault"] = call.fault.kind
                if not self._apply_fault(call):
                    return
            if route.area == "api":
                self._api(call)
            else:
                getattr(self, f"_r_{route.name}")(call)
        except Reply as reply:
            self._answer(call, reply.status, reply.payload, reply.headers)

    def _apply_fault(self, call: Call) -> bool:
        """Answer an injected fault; True when the request continues to its (possibly altered) normal answer."""
        fault = call.fault
        assert fault is not None
        call.entry.note = f"fault {fault.kind}"
        if fault.kind == "slow":
            time.sleep(fault.delay_ms / 1000)
        if fault.kind in CONTINUING:
            return True
        if fault.kind == "disconnect":
            self.close_connection = True
            return False
        headers: list[tuple[str, str]] = []
        if fault.retry_after is not None:
            headers.append(("retry-after", str(fault.retry_after)))
        if fault.kind == "error":
            payload = render.error_body(fault.status, "Server Error")
        elif fault.kind == "retry_after":
            payload = render.error_body(429, "Too Many Requests")
        elif fault.kind == "secondary_rate_limit":
            payload = render.error_body(403, SECONDARY_MESSAGE)
        else:
            payload = render.error_body(fault.status, "API rate limit exceeded for user.")
        self._answer(call, fault.status, payload, headers)
        return False

    def _answer(self, call: Call, status: int, payload: Any, headers: Iterable[tuple[str, str]] = ()) -> None:
        """A JSON answer; API answers carry the version and rate-limit headers."""
        sent = list(headers)
        if isinstance(payload, bytes):
            self._send(
                call.entry, status, (payload, "text/html; charset=utf-8"), [*sent, *self._api_headers(call)], raw=True
            )
            return
        sent += self._api_headers(call)
        self._send(call.entry, status, payload, sent)

    def _api_headers(self, call: Call) -> list[tuple[str, str]]:
        sent: list[tuple[str, str]] = []
        if call.route.area == "api":
            limited = call.fault is not None and call.fault.kind in {"exhaust", "primary_rate_limit"}
            exhausted = call.fault.reset_in if call.fault is not None and limited else None
            sent += [
                ("x-github-api-version-selected", API_VERSION),
                ("x-github-media-type", "github.v3; format=json"),
                *self.double.rate_headers(call.user, exhausted),
            ]
        return sent

    # --- API checks -----------------------------------------------------------------------------------

    def _check_headers(self, call: Call) -> None:
        """The header rules of 05 §12.1 (violations are failures) and the token's user, if the token is known."""
        double = self.double
        route = call.route
        version = call.headers.get("x-github-api-version")
        if version is None:
            double.fail(f"{route.key}: missing X-GitHub-Api-Version")
        elif version != API_VERSION:
            double.fail(f"{route.key}: unsupported X-GitHub-Api-Version {version!r}")
            raise Reply(400, render.error_body(400, f"API version '{version}' is not supported."))
        if call.headers.get("accept") != ACCEPT:
            double.fail(f"{route.key}: Accept is not {ACCEPT}")
        scheme, _, token = call.headers.get("authorization", "").partition(" ")
        if scheme.lower() not in {"bearer", "token"} or not token:
            double.fail(f"{route.key}: request without credentials")
            raise Reply(401, render.error_body(401, "Requires authentication"))
        with double.world.lock:
            call.user = double.world.user_for_token(token.strip())
            call.token = double.world.tokens.get(token.strip()) if call.user is not None else None

    def _api(self, call: Call) -> None:
        route = call.route
        world = self.double.world
        with world.lock:
            if call.user is None:
                raise Reply(401, render.error_body(401, "Bad credentials"))
            if "owner" in call.params:
                self._check_repo_access(call)
            status, payload, headers = getattr(self, f"_r_{route.name}")(call)
        self._answer(call, status, payload, headers)

    def _check_repo_access(self, call: Call) -> None:
        """SAML enforcement, then the repository (``404``), then the write right of the token (``403``)."""
        world = self.double.world
        token = call.the_token
        owner = call.params["owner"]
        if owner.lower() in token.sso:
            sso = f"required; url={self.double.served_url}/orgs/{owner}/sso?authorization_request=x"
            raise Reply(403, render.error_body(403, SAML_MESSAGE, docs=SAML_DOCS), (("x-github-sso", sso),))
        call.repo = repo = world.repo(owner, call.params["repo"])
        needed = WRITE_PERMISSIONS.get(call.route.name)
        self._check_policies(token, owner, repo, needed is not None)
        if token.kind != "app":
            if repo is None or not token.reaches(repo):
                raise not_found()
            if needed is not None and not token.can_write(repo):
                raise Reply(403, render.error_body(403, "Resource not accessible by personal access token"))
            return
        inst = world.installation_for(call.the_user, repo) if repo else None
        if inst is None:
            raise not_found()
        if needed is not None and inst.permissions.get(needed) != "write":
            raise Reply(403, render.error_body(403, "Resource not accessible by integration"))

    def _check_policies(self, token: Token, owner: str, repo: Repo | None, write: bool) -> None:
        """An organisation's restriction of OAuth Apps and of classic tokens (KOR-65), as GitHub answers it."""
        org = self.double.world.organization(owner)
        if org is None or repo is None:
            return
        if token.kind == "oauth" and org.refuses_oauth(token.client) and (repo.private or write):
            raise Reply(403, render.error_body(403, OAUTH_RESTRICTED_MESSAGE.format(org=org.login), docs=OAUTH_DOCS))
        if token.kind == "classic" and org.classic_forbidden:
            raise Reply(403, render.error_body(403, CLASSIC_FORBIDDEN_MESSAGE.format(org=org.login)))

    def _can_push(self, call: Call, repo: Repo) -> bool:
        """Whether the token may push to `repo` (``permissions.push`` of repository answers)."""
        token = call.the_token
        if token.kind != "app":
            return token.can_write(repo)
        inst = self.double.world.installation_for(call.the_user, repo)
        return inst is not None and inst.permissions.get("contents") == "write"

    def _paginate(self, call: Call, items: list[Any]) -> tuple[list[Any], list[tuple[str, str]]]:
        """The page of `items` the query asks for, with the ``Link`` header GitHub sends."""
        page, per_page = _page_args(call.query)
        last = max(1, -(-len(items) // per_page))
        links = []
        base = f"{self.double.urls.api}{call.entry.path}"

        def link(number: int, rel: str) -> str:
            query = {**call.query, "per_page": str(per_page), "page": str(number)}
            return f'<{base}?{urlencode(query)}>; rel="{rel}"'

        if page > 1:
            links += [link(min(page - 1, last), "prev"), link(1, "first")]
        if page < last:
            links += [link(page + 1, "next"), link(last, "last")]
        headers = [("link", ", ".join(links))] if links else []
        return items[(page - 1) * per_page : page * per_page], headers

    # --- credential revocation ----------------------------------------------------------------------

    def _r_revoke_credentials(self, call: Call) -> None:
        if call.headers.get("authorization"):
            self.double.fail(f"{call.route.key}: request with credentials")
            raise Reply(403, render.error_body(403, "Must not be authenticated"))
        credentials = call.json_body().get("credentials")
        if not isinstance(credentials, list) or not credentials or not all(isinstance(t, str) for t in credentials):
            raise unprocessable("Invalid request.")
        with self.double.world.lock:
            known = sum(self.double.world.revoke_token(token) for token in credentials)
        call.entry.extra["revoked"] = known
        self._answer(call, 202, {})

    # --- device flow --------------------------------------------------------------------------------

    def _login_params(self, call: Call) -> dict[str, str]:
        params = dict(call.query)
        if "json" in call.headers.get("content-type", ""):
            body = call.json_body()
            params.update({key: str(value) for key, value in body.items()})
        else:
            params.update(dict(parse_qsl(call.raw.decode("utf-8", "replace"))))
        return params

    def _login_answer(self, call: Call, status: int, payload: dict[str, Any]) -> None:
        if "json" in call.headers.get("accept", ""):
            self._send(call.entry, status, payload)
        else:
            data = urlencode({key: str(value) for key, value in payload.items()}).encode()
            self._send(call.entry, status, (data, "application/x-www-form-urlencoded; charset=utf-8"), raw=True)

    def _r_device_code(self, call: Call) -> None:
        status, payload = self.double.device.request_code(self._login_params(call))
        self._login_answer(call, status, payload)

    def _r_access_token(self, call: Call) -> None:
        self._login_answer(call, 200, self.double.device.poll(self._login_params(call)))

    def _r_device_page(self, call: Call) -> None:
        self._send(call.entry, 200, (DEVICE_PAGE, "text/html; charset=utf-8"), raw=True)

    # --- users and installations --------------------------------------------------------------------

    def _r_user(self, call: Call) -> Result:
        user = call.the_user
        token = call.the_token
        headers = []
        if token.kind in {"classic", "oauth"}:
            headers = [("x-oauth-scopes", ", ".join(token.scopes)), ("x-accepted-oauth-scopes", "")]
        return 200, render.authenticated_user(self.double.urls, user, self.double.world.base_time), headers

    @staticmethod
    def _need_app_token(call: Call) -> None:
        if call.the_token.kind != "app":
            raise Reply(403, APP_TOKEN_NEEDED)

    def _r_installations(self, call: Call) -> Result:
        self._need_app_token(call)
        world = self.double.world
        found = world.visible_installations(call.the_user)
        page, headers = self._paginate(call, found)
        listed = [render.installation(self.double.urls, world, inst) for inst in page]
        return 200, {"total_count": len(found), "installations": listed}, headers

    def _r_installation_repositories(self, call: Call) -> Result:
        self._need_app_token(call)
        world = self.double.world
        inst = world.installations.get(int(call.params["installation_id"]))
        if inst is None or inst not in world.visible_installations(call.the_user):
            raise not_found()
        repos = world.installation_repos(inst)
        page, headers = self._paginate(call, repos)
        push = inst.permissions.get("contents") == "write"
        listed = [render.repository(self.double.urls, repo, form="installation", push=push) for repo in page]
        payload = {"total_count": len(repos), "repository_selection": inst.selection, "repositories": listed}
        return 200, payload, headers

    def _r_user_repos(self, call: Call) -> Result:
        """``GET /user/repos`` by full name (``affiliation``, ``visibility``, ``sort`` and ``direction`` are
        accepted and ignored)."""
        repos = self.double.world.token_repos(call.the_user, call.the_token)
        page, headers = self._paginate(call, repos)
        return 200, [render.repository(self.double.urls, r, push=self._can_push(call, r)) for r in page], headers

    # --- repositories, commits, tarball -------------------------------------------------------------

    def _r_repository(self, call: Call) -> Result:
        push = self._can_push(call, call.the_repo)
        return 200, render.repository(self.double.urls, call.the_repo, push=push), []

    def _resolve(self, call: Call, ref: str) -> str:
        sha = call.the_repo.resolve(ref)
        if sha is None:
            raise unprocessable(f"No commit found for SHA: {ref}")
        return sha

    def _r_commit(self, call: Call) -> Result:
        sha = self._resolve(call, call.params["ref"])
        return 200, render.commit(self.double.urls, self.double.world, call.the_repo, sha), []

    def _r_tarball(self, call: Call) -> Result:
        repo = call.the_repo
        sha = repo.resolve(call.params["ref"])
        if sha is None:
            raise not_found()
        location = f"{self.double.served_url}/codeload/{repo.full_name}/legacy.tar.gz/{sha}"
        if repo.private:
            location += f"?token={self.double.download_token(repo, sha)}"
        return 302, b"", [("location", location)]

    def _r_codeload(self, call: Call) -> None:
        double = self.double
        if "authorization" in call.headers:
            double.fail("codeload request carries an Authorization header (credential forwarded on redirect)")
        with double.world.lock:
            repo = double.world.repo(call.params["owner"], call.params["repo"])
            sha = repo.resolve(call.params["ref"]) if repo else None
            if repo is None or sha is None:
                raise not_found()
            if repo.private and not double.download_allowed(call.query.get("token", ""), repo, sha):
                raise not_found()
            data = tarball(repo, sha)
        disposition = f"attachment; filename={repo.owner}-{repo.name}-{sha[:7]}.tar.gz"
        self._send(call.entry, 200, (data, "application/x-gzip"), [("content-disposition", disposition)], raw=True)

    # --- refs -----------------------------------------------------------------------------------------

    def _r_get_ref(self, call: Call) -> Result:
        name = f"refs/{call.params['ref']}"
        sha = call.the_repo.refs.get(name)
        if sha is None:
            raise not_found()
        return 200, render.ref(self.double.urls, call.the_repo, name, sha), []

    def _commit_sha(self, call: Call, value: Any) -> str:
        if not isinstance(value, str) or call.the_repo.objects.kind(value) != "commit":
            raise unprocessable("Object does not exist")
        return value

    def _r_create_ref(self, call: Call) -> Result:
        body = call.json_body()
        repo = call.the_repo
        name = body.get("ref")
        if not isinstance(name, str) or not re.match(r"^refs/(heads|tags)/[^\s~^:?*\[\\]+$", name) or ".." in name:
            raise unprocessable("Reference name is not well formed")
        sha = self._commit_sha(call, body.get("sha"))
        if name in repo.refs:
            raise unprocessable("Reference already exists")
        repo.refs[name] = sha
        payload = render.ref(self.double.urls, repo, name, sha)
        return 201, payload, [("location", payload["url"])]

    def _r_update_ref(self, call: Call) -> Result:
        body = call.json_body()
        repo = call.the_repo
        name = f"refs/{call.params['ref']}"
        if body.get("force", False) is not False:
            self.double.fail(f"{call.route.key}: forced ref update of {name}")
            raise unprocessable("Forced updates are refused by the double")
        if name not in repo.refs:
            raise unprocessable("Reference does not exist")
        sha = self._commit_sha(call, body.get("sha"))
        if repo.refs[name] not in repo.ancestors(sha):
            raise unprocessable("Update is not a fast forward")
        repo.refs[name] = sha
        return 200, render.ref(self.double.urls, repo, name, sha), []

    # --- git data -------------------------------------------------------------------------------------

    def _r_create_blob(self, call: Call) -> Result:
        body = call.json_body()
        content, encoding = body.get("content"), body.get("encoding", "utf-8")
        if not isinstance(content, str):
            raise unprocessable('Invalid request.\n\n"content" wasn\'t supplied.')
        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=False)
            except binascii.Error as exc:
                raise unprocessable("Invalid Base64") from exc
        elif encoding in {"utf-8", "utf8"}:
            data = content.encode()
        else:
            raise unprocessable(f"encoding must be utf-8 or base64, not {encoding!r}")
        sha = call.the_repo.objects.put_blob(data)
        payload = render.blob_created(self.double.urls, call.the_repo, sha)
        return 201, payload, [("location", payload["url"])]

    def _r_get_blob(self, call: Call) -> Result:
        sha = call.params["sha"]
        if call.the_repo.objects.kind(sha) != "blob":
            raise not_found()
        return 200, render.blob(self.double.urls, call.the_repo, sha), []

    def _r_create_tree(self, call: Call) -> Result:
        body = call.json_body()
        objects = call.the_repo.objects
        entries = body.get("tree")
        if not isinstance(entries, list):
            raise unprocessable('Invalid request.\n\n"tree" wasn\'t supplied.')
        files: dict[str, tuple[str, str]] = {}
        base = body.get("base_tree")
        if base is not None:
            if not isinstance(base, str) or objects.kind(base) != "tree":
                raise unprocessable("Invalid tree info: base_tree is not a tree")
            files = {path: (f.mode, f.sha) for path, f in objects.files(base).items()}
        for item in entries:
            self._apply_tree_item(objects, files, item)
        if call.fault is not None and call.fault.kind == "tree_mismatch":
            files[MISMATCH_PATH] = (FILE_MODE, objects.put_blob(b"mismatch\n"))
        try:
            sha = objects.build(files)
        except ObjectError as exc:
            raise unprocessable(f"Invalid tree info: {exc}") from exc
        payload = render.tree(self.double.urls, call.the_repo, sha, recursive=False)
        return 201, payload, [("location", payload["url"])]

    @staticmethod
    def _apply_tree_item(objects: ObjectStore, files: dict[str, tuple[str, str]], item: Any) -> None:
        """Apply one entry of ``POST …/git/trees`` to the flattened `files` (a tree entry replaces the directory)."""
        if not isinstance(item, dict) or not _valid_path(item.get("path")):
            raise unprocessable("Invalid tree info: every entry needs a relative path without empty, . or .. parts")
        path = item["path"]
        mode, kind = git_mode(str(item.get("mode", ""))), item.get("type")
        if mode not in BLOB_MODES | {TREE_MODE} or kind != ("tree" if mode == TREE_MODE else "blob"):
            raise unprocessable(f"Invalid tree info: mode {item.get('mode')!r} and type {kind!r} of {path}")
        below = [p for p in files if p == path or p.startswith(f"{path}/")]
        sha = _tree_item_sha(objects, item, path, kind)
        if sha is None and not below:
            raise unprocessable(f"Invalid tree info: cannot delete missing path {path}")
        for p in below:
            del files[p]
        if sha is None:
            return
        if mode == TREE_MODE:
            for sub, entry in objects.files(sha).items():
                files[f"{path}/{sub}"] = (entry.mode, entry.sha)
        else:
            files[path] = (mode, sha)

    def _r_get_tree(self, call: Call) -> Result:
        repo = call.the_repo
        value = call.params["tree"]
        kind = repo.objects.kind(value)
        if kind == "tree":
            sha = value
        else:
            commit = value if kind == "commit" else repo.resolve(value) if not SHA_RE.match(value) else None
            if commit is None:
                raise not_found()
            sha = repo.tree_of(commit)
        payload = render.tree(self.double.urls, repo, sha, recursive="recursive" in call.query)
        if call.fault is not None and call.fault.kind == "truncated":
            payload["tree"] = payload["tree"][: call.fault.limit]
            payload["truncated"] = True
        return 200, payload, []

    def _r_create_commit(self, call: Call) -> Result:
        body = call.json_body()
        repo = call.the_repo
        objects = repo.objects
        message, tree, parents = body.get("message"), body.get("tree"), body.get("parents", [])
        if not isinstance(message, str):
            raise unprocessable('Invalid request.\n\n"message" wasn\'t supplied.')
        if not isinstance(tree, str) or objects.kind(tree) != "tree":
            raise unprocessable("Tree SHA does not exist")
        if not isinstance(parents, list) or any(not isinstance(p, str) or objects.kind(p) != "commit" for p in parents):
            raise unprocessable("Parent SHA does not exist or is not a commit object")
        user = call.the_user
        email = user.email or render.noreply_email(user)
        default = Signature(user.name or user.login, email, self.double.world.stamp())
        author = _signature(body.get("author"), default, "author")
        committer = _signature(body.get("committer"), author, "committer")
        sha = objects.put_commit(Commit(tree, tuple(parents), author, committer, message))
        payload = render.git_commit(self.double.urls, repo, sha)
        return 201, payload, [("location", payload["url"])]

    def _r_get_commit(self, call: Call) -> Result:
        sha = call.params["sha"]
        if call.the_repo.objects.kind(sha) != "commit":
            raise not_found()
        return 200, render.git_commit(self.double.urls, call.the_repo, sha), []

    def _r_compare(self, call: Call) -> Result:
        basehead = call.params["basehead"]
        base_ref, sep, head_ref = basehead.partition("...")
        if not sep:
            base_ref, sep, head_ref = basehead.partition("..")
        repo = call.the_repo
        base, head = repo.resolve(base_ref), repo.resolve(head_ref)
        if not sep or base is None or head is None:
            raise not_found()
        if repo.merge_base(base, head) is None:
            raise Reply(404, render.error_body(404, f"No common ancestor between {base_ref} and {head_ref}."))
        return (
            200,
            render.compare(self.double.urls, self.double.world, repo, base=base, head=head, basehead=basehead),
            [],
        )

    # --- pull requests and comments -----------------------------------------------------------------

    @staticmethod
    def _state_filter(call: Call) -> str:
        state = call.query.get("state", "open")
        if state not in {"open", "closed", "all"}:
            raise invalid("Issue", "state")
        return state

    @staticmethod
    def _sorted(call: Call, items: list[Any], sorts: frozenset[str], default_desc: bool) -> list[Any]:
        """`items` in the order of ``sort`` (``created``, ``updated``, ``comments``/``popularity`` by comment count,
        ``long-running`` like ``created``; ties by number) and ``direction`` (default ``desc`` for issues; for pull
        requests ``desc`` with ``sort=created`` only, ``asc`` otherwise, as GitHub)."""
        repo = call.the_repo
        sort = call.query.get("sort", "created")
        if sort not in sorts:
            raise invalid("Issue", "sort")
        direction = call.query.get("direction", "desc" if default_desc or sort == "created" else "asc")
        if direction not in {"asc", "desc"}:
            raise invalid("Issue", "direction")
        keys: dict[str, Callable[[Any], tuple[int, int]]] = {
            "created": lambda i: (i.created, i.number),
            "long-running": lambda i: (i.created, i.number),
            "updated": lambda i: (i.updated, i.number),
            "comments": lambda i: (repo.comment_count(i.number), i.number),
            "popularity": lambda i: (repo.comment_count(i.number), i.number),
        }
        return sorted(items, key=keys[sort], reverse=direction == "desc")

    def _r_list_pulls(self, call: Call) -> Result:
        repo = call.the_repo
        state = self._state_filter(call)
        found = [p for p in repo.pulls.values() if state in {"all", p.state}]
        if "head" in call.query:
            owner, _, branch = call.query["head"].partition(":")
            found = [p for p in found if owner.lower() == repo.owner.lower() and p.head == branch]
        if "base" in call.query:
            found = [p for p in found if p.base == call.query["base"]]
        page, headers = self._paginate(call, self._sorted(call, found, PULL_SORTS, default_desc=False))
        world = self.double.world
        return 200, [render.pull(self.double.urls, world, repo, p, listed=True) for p in page], headers

    def _r_create_pull(self, call: Call) -> Result:
        body = call.json_body()
        repo = call.the_repo
        title, head, base = body.get("title"), body.get("head"), body.get("base")
        draft, text = body.get("draft", False), body.get("body")
        if not isinstance(title, str) or not title:
            raise Reply(422, render.validation_error("", "PullRequest", "title", code="missing_field"))
        if not isinstance(draft, bool) or (text is not None and not isinstance(text, str)):
            raise unprocessable("Invalid request.\n\nInvalid draft or body.")
        if isinstance(head, str) and ":" in head:
            owner, _, head = head.partition(":")
            head = head if owner.lower() == repo.owner.lower() else None
        if not isinstance(head, str) or repo.branch(head) is None:
            raise Reply(422, render.validation_error("", "PullRequest", "head", code="invalid"))
        if not isinstance(base, str) or repo.branch(base) is None:
            raise Reply(422, render.validation_error("", "PullRequest", "base", code="invalid"))
        if any(p.state == "open" and p.head == head and p.base == base for p in repo.pulls.values()):
            message = f"A pull request already exists for {repo.owner}:{head}."
            raise Reply(422, render.validation_error(message, "PullRequest"))
        head_sha, base_sha = repo.branch(head), repo.branch(base)
        assert head_sha is not None
        assert base_sha is not None
        if not repo.history(head_sha, repo.ancestors(base_sha)):
            raise Reply(422, render.validation_error(f"No commits between {base} and {head}", "PullRequest"))
        world = self.double.world
        pull = world.add_pull(repo, title=title, body=text, head=head, base=base, draft=draft, user=call.the_user.login)
        payload = render.pull(self.double.urls, world, repo, pull)
        return 201, payload, [("location", payload["url"])]

    def _pull(self, call: Call) -> Any:
        found = call.the_repo.pulls.get(int(call.params["number"]))
        if found is None:
            raise not_found()
        return found

    def _r_get_pull(self, call: Call) -> Result:
        pull = self._pull(call)
        return 200, render.pull(self.double.urls, self.double.world, call.the_repo, pull), []

    def _r_pull_files(self, call: Call) -> Result:
        repo = call.the_repo
        head, _, changes, _ = render.pull_changes(repo, self._pull(call))
        page, headers = self._paginate(call, changes)
        return 200, [render.file_change(self.double.urls, repo, change, head) for change in page], headers

    def _issue_number(self, call: Call) -> int:
        """The number of an existing issue or pull request; issue comments of a repository without issues are
        ``410``."""
        number = int(call.params["number"])
        if number not in call.the_repo.pulls:
            self._issues_enabled(call)
            if number not in call.the_repo.issues:
                raise not_found()
        return number

    @staticmethod
    def _issues_enabled(call: Call) -> None:
        if not call.the_repo.has_issues:
            raise Reply(410, ISSUES_DISABLED)

    def _issue(self, call: Call) -> Issue | Pull:
        self._issues_enabled(call)
        found = call.the_repo.item(int(call.params["number"]))
        if found is None:
            raise not_found()
        return found

    def _r_list_issues(self, call: Call) -> Result:
        self._issues_enabled(call)
        repo = call.the_repo
        state = self._state_filter(call)
        items: list[Issue | Pull] = [*repo.issues.values(), *repo.pulls.values()]
        found = [i for i in items if state in {"all", i.state}]
        wanted = {name.strip().lower() for name in call.query.get("labels", "").split(",") if name.strip()}
        found = [i for i in found if wanted <= {name.lower() for name in i.labels}]
        page, headers = self._paginate(call, self._sorted(call, found, ISSUE_SORTS, default_desc=True))
        return 200, [render.issue(self.double.urls, self.double.world, repo, i) for i in page], headers

    def _r_get_issue(self, call: Call) -> Result:
        return 200, render.issue(self.double.urls, self.double.world, call.the_repo, self._issue(call)), []

    @staticmethod
    def _issue_text(body: dict[str, Any], name: str) -> str | None:
        value = body.get(name)
        if value is not None and not isinstance(value, str):
            raise not_a(name, value, "a string or null")
        return value

    def _label_names(self, call: Call, body: dict[str, Any]) -> list[str] | None:
        value = body.get("labels")
        if value is None:
            return None
        if not isinstance(value, list) or any(not isinstance(name, str) or not name for name in value):
            raise not_a("labels", value, "an array of strings")
        return self.double.world.labels(call.the_repo, value)

    def _r_create_issue(self, call: Call) -> Result:
        self._issues_enabled(call)
        body = call.json_body()
        repo = call.the_repo
        world = self.double.world
        title = body.get("title")
        if not isinstance(title, str) or not title:
            raise Reply(422, render.validation_error("", "Issue", "title", code="missing_field"))
        text = self._issue_text(body, "body")
        assignees = body.get("assignees", [])
        if not isinstance(assignees, list) or any(not isinstance(login, str) for login in assignees):
            raise not_a("assignees", assignees, "an array of strings")
        if any(login.lower() not in world.users for login in assignees):
            raise invalid("Issue", "assignees")
        labels = self._label_names(call, body) or []
        logins = [world.users[login.lower()].login for login in assignees]
        created = world.add_issue(repo, title, call.the_user.login, body=text, labels=labels, assignees=logins)
        payload = render.issue(self.double.urls, world, repo, created)
        return 201, payload, [("location", payload["url"])]

    def _r_update_issue(self, call: Call) -> Result:
        item = self._issue(call)
        body = call.json_body()
        title = body.get("title", item.title)
        if not isinstance(title, str) or not title:
            raise Reply(422, render.validation_error("", "Issue", "title", code="missing_field"))
        text = self._issue_text(body, "body") if "body" in body else item.body
        state = body.get("state", item.state)
        if state not in {"open", "closed"}:
            raise invalid("Issue", "state")
        reason = body.get("state_reason")
        if reason is not None and reason not in STATE_REASONS:
            raise invalid("Issue", "state_reason")
        labels = self._label_names(call, body)
        world = self.double.world
        now = world.stamp()
        if state != item.state:
            item.closed = now if state == "closed" else None
            reason = reason or ("completed" if state == "closed" else "reopened")
        if isinstance(item, Issue) and reason is not None:
            item.state_reason = reason
        item.title, item.body, item.state, item.updated = title, text, state, now
        if labels is not None:
            item.labels = labels
        return 200, render.issue(self.double.urls, world, call.the_repo, item), []

    def _r_list_comments(self, call: Call) -> Result:
        repo = call.the_repo
        number = self._issue_number(call)
        found = [c for c in repo.comments if c.number == number]
        page, headers = self._paginate(call, found)
        return 200, [render.comment(self.double.urls, self.double.world, repo, c) for c in page], headers

    def _r_create_comment(self, call: Call) -> Result:
        body = call.json_body()
        number = self._issue_number(call)
        text = body.get("body")
        if not isinstance(text, str) or not text:
            raise Reply(422, render.validation_error("", "IssueComment", "body", code="missing_field"))
        world = self.double.world
        created = world.add_comment(call.the_repo, number, text, call.the_user.login)
        payload = render.comment(self.double.urls, world, call.the_repo, created)
        return 201, payload, [("location", payload["url"])]

    # --- control routes -------------------------------------------------------------------------------

    def _control(self, path: str, raw: bytes, problem: BodyError | None) -> None:
        routes = {
            "/_double/state": ("GET",),
            "/_double/journal": ("GET",),
            "/_double/reset": ("POST",),
            "/_double/seed": ("POST",),
            "/_double/faults": ("GET", "POST"),
            "/_double/expect": ("POST",),
            "/_double/device": ("GET", "POST"),
            "/_double/clock": ("POST",),
            "/_double/tokens": ("POST",),
            "/_double/organizations": ("POST",),
        }
        inspect = re.match(r"^/_double/repos/([^/]+)/([^/]+)$", path)
        allowed = ("GET",) if inspect else routes.get(path)
        if allowed is None:
            self.send_json(404, {"error": "unknown control route"})
            return
        if self.command not in allowed:
            self.send_json(405, {"error": "method not allowed"}, (("allow", ", ".join(allowed)),))
            return
        if problem is not None:
            self.send_json(problem.status, {"error": problem.message})
            return
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            self.send_json(400, {"error": "body is not JSON"})
            return
        try:
            status, payload = self._control_action(path, inspect, body)
        except (SeedError, FaultError, DeviceFlowError, ObjectError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.send_json(status, payload)

    def _control_action(self, path: str, inspect: re.Match[str] | None, body: Any) -> tuple[int, Any]:
        double = self.double
        if inspect is not None:
            found = double.inspect(inspect.group(1), inspect.group(2))
            return (200, found) if found is not None else (404, {"error": "unknown repository"})
        actions: dict[tuple[str, str], Callable[[], Any]] = {
            ("GET", "/_double/state"): double.state,
            ("GET", "/_double/journal"): double.journal.records,
            ("POST", "/_double/reset"): self._reset,
            ("POST", "/_double/seed"): lambda: self._seed(body),
            ("GET", "/_double/faults"): double.plan.describe,
            ("POST", "/_double/faults"): lambda: self._add_faults(body),
            ("POST", "/_double/expect"): lambda: self._add_expectations(body),
            ("GET", "/_double/device"): double.device.grants,
            ("POST", "/_double/device"): lambda: double.device.control(body, self._known_login),
            ("POST", "/_double/clock"): lambda: self._advance(body),
            ("POST", "/_double/tokens"): lambda: self._tokens(body),
            ("POST", "/_double/organizations"): lambda: self._organizations(body),
        }
        return 200, actions[(self.command, path)]()

    def _reset(self) -> dict[str, Any]:
        self.double.reset()
        return self.double.state()

    def _seed(self, body: Any) -> dict[str, Any]:
        self.double.seed(body)
        return self.double.state()

    def _add_faults(self, body: Any) -> dict[str, Any]:
        plan = self.double.plan
        plan.add_faults([parse_fault(raw, ROUTE_KEYS) for raw in _items(_body_object(body), "faults")])
        return plan.describe()

    def _add_expectations(self, body: Any) -> dict[str, Any]:
        plan = self.double.plan
        plan.add_expectations(
            [parse_expectation(raw, ROUTE_KEYS) for raw in _items(_body_object(body), "expectations")]
        )
        return plan.describe()

    def _advance(self, body: Any) -> dict[str, Any]:
        advance = _body_object(body).get("advance")
        if isinstance(advance, bool) or not isinstance(advance, int | float) or advance < 0:
            raise FaultError("advance must be a non-negative number of seconds")
        self.double.advance(float(advance))
        return {"now": self.double.clock()}

    def _tokens(self, body: Any) -> dict[str, Any]:
        token = _body_object(body).get("revoke")
        if not isinstance(token, str):
            raise SeedError("revoke must name a token")
        with self.double.world.lock:
            self.double.world.revoke(token)
        return self.double.state()

    def _organizations(self, body: Any) -> dict[str, Any]:
        """``POST /_double/organizations``: add or change an organisation's policies (KOR-65), e.g. an owner's
        approval of the OAuth App: ``{"login": "third-org", "oauth_approved": ["<client id>"]}``."""
        with self.double.world.lock:
            found = self.double.world.set_organization(body)
        return {
            "login": found.login,
            "oauth_restricted": found.oauth_restricted,
            "oauth_approved": list(found.oauth_approved),
            "classic_forbidden": found.classic_forbidden,
        }

    def _known_login(self, login: str) -> str | None:
        world = self.double.world
        with world.lock:
            user = world.users.get(login.lower())
            return user.login if user else None


def _body_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise FaultError("body must be an object")
    return body
