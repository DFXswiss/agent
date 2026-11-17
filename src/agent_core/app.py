"""HTTP and WebSocket hub."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.middleware.sessions import SessionMiddleware

from .config import Config
from .db import Store, dumps, loads, row_dict, utcnow
from .github import (
    GITHUB_REQUEST_TIMEOUT,
    GitHub,
    GitHubError,
    RealGitHub,
    valid_repository_name,
)
from .teams import Roster, load_roster, visible_logins
from .tokens import TokenError, issue, parse

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_TABLES = frozenset(
    {
        "session",
        "activity",
        "task",
        "task_round",
        "agent",
        "checklist_item",
        "local_check",
        "review_gate",
        "open_work",
        "ping",
    }
)
ALLOWED_OPS = frozenset({"insert", "update", "delete"})
PING_KINDS = frozenset({"review-request", "ping", "question"})
CONTROL_ACTIONS = frozenset({"start", "stop", "input", "resize"})
CONTROL_KEYS = frozenset({"enter", "ctrl-c", "tab"})
PAIR_TTL_SECONDS = 600
TERMINAL_RING_SIZE = 64
TERMINAL_CHUNK_MAX = 8192
MATCH_PATHS = frozenset({"type", "payload.repo", "payload.issue_key", "payload.to_session"})
MAX_SUBSCRIPTIONS = 32
MAX_MATCH_PREDICATES = 4
MAX_IN_VALUES = 16
QUERY_ROW_CAP = 500
TIMELINE_ROW_CAP = 5000
ISSUE_CACHE_SECONDS = 60
ISSUE_CACHE_MAX_ENTRIES = 500
ASSIGNMENT_ENRICHMENT_LIMIT = 50
ASSIGNMENT_ENRICHMENT_SECONDS = 10.0
ASSIGNMENT_RECOVERY_SECONDS = 10.0
REQUEST_LEASE_SECONDS = 120
GITHUB_REAUTH_DETAIL = "sign in on the website again"
GITHUB_TOKEN_REQUIRED_DETAIL = "sign in on the website once so the hub holds your GitHub token"
REQUEST_FINGERPRINT_CONFLICT_DETAIL = (
    "request id was used for different content; reload the assignment list"
)
REQUEST_ALREADY_COMPLETED_DETAIL = "request already completed; reload the assignment list"
DEVIATION_REQUIRED_DETAIL = (
    'deviation must be {"declared": false}; a deviation needs an explicit grant on the issue'
)
RESERVED_REQUEST_MARKER_DETAIL = "body contains a reserved agent-core request marker line"
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HUB_AUTHORED_ACTIVITY_TYPES = frozenset({"assignment.created", "assignment.reply"})
ASSIGNMENT_WRITE_ROUTES = frozenset(
    {
        "/api/assignments",
        "/api/assignments/{owner}/{name}/{number}/comments",
    }
)
_MISSING = object()


class AssignmentCreateBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent: StrictStr | None = None
    repo: StrictStr | None = None
    title: StrictStr | None = None
    body: StrictStr | None = None
    request_id: StrictStr | None = None
    deviation: Any = None


class Hub:
    def __init__(self, cfg: Config, github: GitHub, store: Store, roster: Roster) -> None:
        self.cfg = cfg
        self.github = github
        self.store = store
        self.roster = roster
        self.teams = roster.teams
        self.queues: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []
        self.device_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.control_ready: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.terminal_rings: dict[str, list[dict[str, Any]]] = {}
        self.terminal_queues: list[tuple[str, str, asyncio.Queue[dict[str, Any]]]] = []
        self.github_tokens: dict[str, str] = {}
        self.issue_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
        # Bumped by every reply invalidation; an enrichment caches only if it is unchanged.
        self.issue_cache_epoch = 0
        self.issue_cache_lock = threading.Lock()
        for login, token in self.store.all_oauth_tokens():
            self.github_tokens[login] = token

    def visible(self, login: str) -> set[str]:
        return visible_logins(login, self.teams)

    def device_for_token(self, raw: str) -> dict[str, Any]:
        try:
            parsed = parse(self.cfg.session_secret, raw)
        except TokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        row = self.store.query_one(
            "SELECT * FROM device WHERE id = ? AND token_id = ?",
            (parsed.device_id, parsed.token_id),
        )
        if row is None or _reserved_device_id(row["id"]):
            raise HTTPException(status_code=401, detail="unknown device token")
        if row["revoked_at"] is not None:
            raise HTTPException(status_code=401, detail="device token is revoked")
        if row["github_login"] != parsed.login:
            raise HTTPException(status_code=401, detail="device token login does not match")
        return row_dict(row)

    def session_login(self, request: Request) -> str:
        login = request.session.get("login")
        if not isinstance(login, str) or login == "":
            raise HTTPException(status_code=401, detail="not signed in")
        return login

    def _allowed_for(self, login: str, event: dict[str, Any]) -> bool:
        visible = self.visible(login)
        if event.get("to") == login or event.get("from") == login:
            return True
        owner = event.get("login")
        if isinstance(owner, str):
            return owner in visible
        return False

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []
        for item in list(self.queues):
            login, queue = item
            if not self._allowed_for(login, event):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(item)
        for item in dead:
            if item in self.queues:
                self.queues.remove(item)

    def publish_terminal(self, event: dict[str, Any]) -> None:
        session_id = event.get("session_id")
        dead: list[tuple[str, str, asyncio.Queue[dict[str, Any]]]] = []
        for item in list(self.terminal_queues):
            _login, sid, queue = item
            if sid != session_id:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(item)
        for item in dead:
            if item in self.terminal_queues:
                self.terminal_queues.remove(item)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization")
    if header is None or not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    token = header[7:].strip()
    if token == "":
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    return token


def can_control(hub: Hub, login: str, origin_device_id: str) -> bool:
    if not isinstance(origin_device_id, str) or origin_device_id == "":
        return False
    row = hub.store.query_one(
        "SELECT github_login FROM device WHERE id = ? AND revoked_at IS NULL",
        (origin_device_id,),
    )
    if row is None:
        return False
    device_login = row["github_login"]
    if not isinstance(device_login, str):
        return False
    device_login = device_login.strip().lower()
    actor = login.strip().lower()
    return device_login == actor or hub.roster.owns_agent(actor, device_login)


def create_app(cfg: Config, github: GitHub | None = None, store: Store | None = None) -> FastAPI:
    roster = load_roster(cfg.teams_path)
    app_store = store if store is not None else Store(cfg.database)
    try:
        hub = Hub(cfg, github or RealGitHub(cfg), app_store, roster)
    except BaseException:
        if store is None:
            app_store.close()
        raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if store is None:
                app_store.close()

    app = FastAPI(title="agent-core", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        same_site="lax",
        https_only=cfg.public_url.startswith("https://"),
    )
    app.state.hub = hub

    @app.exception_handler(HTTPException)
    async def http_exception_response(request: Request, exc: HTTPException) -> JSONResponse:
        content: dict[str, Any] = {"detail": exc.detail}
        request_id_final = _request_id_error_is_final(
            request, exc.status_code, exc.detail
        )
        if request_id_final is not None:
            content["request_id_final"] = request_id_final
        return JSONResponse(content, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_response(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        response = await request_validation_exception_handler(request, exc)
        route = request.scope.get("route")
        if getattr(route, "path", None) not in ASSIGNMENT_WRITE_ROUTES:
            return response
        try:
            raw_body = await request.json()
        except Exception:
            return response
        if not isinstance(raw_body, dict) or "request_id" not in raw_body:
            return response
        content = loads(response.body)
        content["request_id_final"] = True
        return JSONResponse(content, status_code=response.status_code)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/github")
    def auth_start(request: Request, next: str | None = None) -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        if next is not None:
            if not next.startswith("/pair?"):
                raise HTTPException(status_code=400, detail="next must be a /pair query")
            request.session["after_login"] = next
        redirect_uri = f"{cfg.public_url}/auth/github/callback"
        return RedirectResponse(hub.github.authorize_url(state, redirect_uri), status_code=302)

    @app.get("/auth/github/callback")
    def auth_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
        expect = request.session.get("oauth_state")
        if not isinstance(expect, str) or expect == "" or state != expect:
            raise HTTPException(status_code=400, detail="oauth state mismatch")
        if not isinstance(code, str) or code == "":
            raise HTTPException(status_code=400, detail="oauth code is missing")
        try:
            user = hub.github.login_for_code(code)
        except GitHubError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        request.session.pop("oauth_state", None)
        request.session["login"] = user.login
        if user.token:
            hub.github_tokens[user.login] = user.token
            hub.store.put_oauth_token(user.login, user.token)
        dest = request.session.pop("after_login", "/")
        if not isinstance(dest, str) or not dest.startswith("/"):
            dest = "/"
        return RedirectResponse(dest, status_code=302)

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        login = request.session.get("login")
        if isinstance(login, str) and login:
            hub.store.delete_oauth_token(login)
            hub.github_tokens.pop(login, None)
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        return {
            "login": login,
            "visible": sorted(hub.visible(login)),
            "teams": _teams_for(login, hub.teams),
            "owned_agents": hub.roster.owned_agents(login),
        }

    @app.post("/pair/prepare")
    def pair_prepare(body: dict[str, Any]) -> dict[str, str]:
        device_id = _require_str(body, "device_id")
        challenge = _require_str(body, "challenge")
        name = _require_str(body, "device_name")
        _reject_reserved_device_id(device_id)
        if len(challenge) < 16 or len(challenge) > 128:
            raise HTTPException(status_code=400, detail="challenge length is invalid")
        if len(name) > 128 or len(device_id) > 80:
            raise HTTPException(status_code=400, detail="device fields are too long")
        existing = hub.store.query_one("SELECT id FROM device WHERE id = ?", (device_id,))
        if existing is not None:
            raise HTTPException(status_code=409, detail="device is already paired")
        hub.store.execute(
            "DELETE FROM pending_pair WHERE created_at < ?",
            (_cutoff(),),
        )
        pending = hub.store.query_one("SELECT COUNT(*) AS n FROM pending_pair")
        if pending is not None and int(pending["n"]) >= 200:
            raise HTTPException(status_code=429, detail="too many pending pairing challenges")
        hub.store.execute(
            "INSERT OR REPLACE INTO pending_pair (challenge, device_id, device_name, created_at, token, github_login) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            (challenge, device_id, name, utcnow()),
        )
        return {"challenge": challenge, "pair_url": f"{cfg.public_url}/pair?challenge={challenge}"}

    @app.get("/pair/wait")
    def pair_wait(device_id: str, challenge: str) -> dict[str, Any]:
        claimed = hub.store.claim_pair_token(challenge, device_id, _cutoff())
        if claimed is not None:
            return {"status": "paired", "token": claimed[0], "login": claimed[1]}
        row = hub.store.query_one(
            "SELECT * FROM pending_pair WHERE challenge = ? AND device_id = ?",
            (challenge, device_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="unknown pairing challenge")
        if _age_seconds(row["created_at"]) > PAIR_TTL_SECONDS:
            raise HTTPException(status_code=410, detail="pairing challenge expired")
        return {"status": "pending"}

    @app.post("/pair/confirm")
    def pair_confirm(request: Request, body: dict[str, Any]) -> dict[str, str]:
        login = hub.session_login(request)
        challenge = _require_str(body, "challenge")
        paired_login = login.strip().lower()
        if body.get("as_login") is not None:
            as_login = _require_str(body, "as_login").lower()
            if not hub.roster.is_agent(as_login):
                raise HTTPException(status_code=404, detail="unknown agent")
            if not hub.roster.owns_agent(login, as_login):
                raise HTTPException(status_code=403, detail="not an owner of that agent")
            paired_login = as_login
        pending = hub.store.query_one("SELECT * FROM pending_pair WHERE challenge = ?", (challenge,))
        if pending is None:
            raise HTTPException(status_code=404, detail="unknown pairing challenge")
        _reject_reserved_device_id(pending["device_id"])
        if pending["token"] is not None:
            raise HTTPException(status_code=409, detail="challenge is already paired")
        created = pending["created_at"]
        if _age_seconds(created) > PAIR_TTL_SECONDS:
            raise HTTPException(status_code=410, detail="pairing challenge expired")
        token_id = str(uuid.uuid4())
        token = issue(cfg.session_secret, paired_login, pending["device_id"], token_id)
        hub.store.execute(
            "INSERT INTO device (id, github_login, name, token_id, created_at, revoked_at, confirmed_by) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (pending["device_id"], paired_login, pending["device_name"], token_id, utcnow(), login),
        )
        hub.store.execute(
            "UPDATE pending_pair SET token = ?, github_login = ? WHERE challenge = ?",
            (token, paired_login, challenge),
        )
        return {"status": "paired", "login": paired_login, "device_id": pending["device_id"]}

    def _dashboard_html() -> HTMLResponse:
        page = STATIC / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="dashboard is missing")
        html = page.read_text(encoding="utf-8").replace("__PUBLIC_URL__", cfg.public_url)
        return HTMLResponse(html)

    def _brand_file(name: str, media_type: str) -> FileResponse:
        path = STATIC / name
        if not path.is_file():
            raise HTTPException(status_code=500, detail="brand file is missing")
        return FileResponse(path, media_type=media_type)

    @app.api_route("/pair", methods=["GET", "HEAD"])
    def pair_page() -> HTMLResponse:
        return _dashboard_html()

    @app.api_route("/", methods=["GET", "HEAD"])
    def index() -> HTMLResponse:
        return _dashboard_html()

    @app.api_route("/session", methods=["GET", "HEAD"])
    def session_page_query() -> HTMLResponse:
        page = STATIC / "session.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="session page is missing")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"])
    def favicon_ico() -> FileResponse:
        return _brand_file("favicon.ico", "image/x-icon")

    @app.api_route("/favicon.svg", methods=["GET", "HEAD"])
    def favicon_svg() -> FileResponse:
        return _brand_file("favicon.svg", "image/svg+xml")

    @app.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"])
    def apple_touch_icon() -> FileResponse:
        return _brand_file("apple-touch-icon.png", "image/png")

    @app.api_route("/og.png", methods=["GET", "HEAD"])
    def og_png() -> FileResponse:
        return _brand_file("og.png", "image/png")

    @app.post("/sync/push")
    async def sync_push(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        events = body.get("events")
        if not isinstance(events, list):
            raise HTTPException(status_code=400, detail="events must be a list")
        accepted = 0
        notify_to: set[str] = set()
        materialized: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise HTTPException(status_code=400, detail="event must be an object")
            notify, snap = _accept_event(hub, device, event)
            notify_to.update(notify)
            if snap is not None:
                materialized.append(snap)
            accepted += 1
        if accepted:
            await hub.publish({"type": "events", "login": device["github_login"], "count": accepted})
            for target in notify_to:
                await hub.publish(
                    {
                        "type": "events",
                        "login": device["github_login"],
                        "to": target,
                        "count": accepted,
                    }
                )
            for snap in materialized:
                _enqueue_subscription_fanout(hub, snap)
        return {"accepted": accepted}

    @app.get("/sync/pull")
    def sync_pull(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        cursors = _cursors(request)
        return {
            "events": _own_events(hub, device["id"], cursors),
            "inbox": _inbox_snapshots(hub, device["id"]),
            "pings": _ping_snapshots(hub, device["github_login"]),
            "subscriptions": _subscription_snapshots(hub, device),
        }

    @app.get("/sync/restore")
    def sync_restore(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        own = _own_events(hub, device["id"], {})
        return {
            "device_id": device["id"],
            "login": device["github_login"],
            "own_events": own,
            "inbox": _inbox_snapshots(hub, device["id"]),
            "pings": _ping_snapshots(hub, device["github_login"]),
        }

    @app.put("/sync/subscriptions")
    def sync_subscriptions_put(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        raw = body.get("subscriptions")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="subscriptions must be a list")
        if len(raw) > MAX_SUBSCRIPTIONS:
            raise HTTPException(status_code=400, detail="at most 32 subscriptions per device")
        match_jsons: list[str] = []
        for item in raw:
            if not isinstance(item, dict) or set(item.keys()) != {"match"}:
                raise HTTPException(status_code=400, detail="each subscription must be an object with exactly one key match")
            match_jsons.append(dumps(_validate_match(item["match"])))
        hub.store.replace_device_subscriptions(device["id"], match_jsons)
        return _subscriptions_response(hub, device["id"])

    @app.get("/sync/subscriptions")
    def sync_subscriptions_get(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        return _subscriptions_response(hub, device["id"])

    @app.post("/sync/query")
    def sync_query(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        if not isinstance(body, dict) or "match" not in body:
            raise HTTPException(status_code=400, detail="match is required")
        match = _validate_match(body["match"])
        return {"rows": _query_matching_rows(hub, device["github_login"], match)}

    @app.get("/api/prs")
    def api_prs(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        allowed = sorted(hub.visible(login))
        token = hub.github_tokens.get(login, "")
        if token == "":
            token = hub.store.get_oauth_token(login)
            if token:
                hub.github_tokens[login] = token
        if token == "":
            return {"generated_at": utcnow(), "prs": [], "source": "none"}
        try:
            prs = hub.github.search_open_prs(token, allowed)
        except GitHubError as exc:
            text = str(exc)
            if exc.status_code == 401:
                _forget_github_token(hub, login, token)
                return {"generated_at": utcnow(), "prs": [], "source": "none"}
            raise HTTPException(status_code=502, detail=text) from exc
        truncated = len(prs) >= 50
        return {"generated_at": utcnow(), "prs": prs, "source": "github", "truncated": truncated}

    @app.post("/api/assignments")
    async def api_assignment_create(
        request: Request, request_body: AssignmentCreateBody
    ):
        body = request_body.model_dump(exclude_unset=True)
        _prepare_request_id_error_state(request, body)
        actor, device_actor = _request_actor(hub, request)
        if device_actor and hub.roster.is_agent(actor):
            raise HTTPException(status_code=403, detail="an agent device cannot create assignments")
        deviation = _require_no_deviation(body)
        agent = _require_str(body, "agent").lower()
        if not hub.roster.is_agent(agent):
            raise HTTPException(status_code=404, detail="unknown agent")
        if not hub.roster.owns_agent(actor, agent):
            raise HTTPException(status_code=403, detail="not an owner of that agent")
        repo = _require_str(body, "repo")
        if not valid_repository_name(repo):
            raise HTTPException(status_code=400, detail="repo must be Owner/Name")
        title = _bounded_body_string(body, "title", 1, 256)
        issue_body = _bounded_body_string(body, "body", 1, 60000)
        _reject_reserved_request_marker(issue_body)
        request_id = _optional_request_id(body)
        request_fingerprint = _request_fingerprint(
            {
                "repo": repo.casefold(),
                "title": title,
                "body": issue_body,
                "agent": agent,
                "deviation": deviation,
            }
        )
        request_owner = uuid.uuid4().hex
        recovering_request = False
        recovery_since = ""
        if request_id is not None:
            now, lease_until = _request_lease_times()
            reservation = hub.store.reserve_request(
                actor,
                "assignment.created",
                request_id,
                request_fingerprint,
                request_owner,
                now,
                lease_until,
            )
            recovering_request = (
                reservation in ("reserved", "taken_over")
                and hub.store.request_has_prior_write(
                    actor, "assignment.created", request_id, request_owner
                )
            )
            if recovering_request:
                request.state.request_id_prior_write = True
                recovery_since = hub.store.request_created_at(
                    actor, "assignment.created", request_id, request_owner
                )
            if reservation == "conflict":
                raise HTTPException(
                    status_code=409,
                    detail=REQUEST_FINGERPRINT_CONFLICT_DETAIL,
                )
            if reservation == "done":
                replay = _assignment_request_replay(
                    hub,
                    actor,
                    "assignment.created",
                    request_id,
                    request_fingerprint,
                )
                response = _assignment_create_response(replay) if replay is not None else None
                if response is not None:
                    return JSONResponse(response, status_code=200)
                raise HTTPException(
                    status_code=409,
                    detail=REQUEST_ALREADY_COMPLETED_DETAIL,
                )
            if reservation == "busy":
                raise HTTPException(
                    status_code=409,
                    detail="request is still in progress; retry shortly",
                )
        trailer = f"Assigned through {cfg.public_url} by @{actor}."
        posted_body = issue_body.rstrip() + "\n\n" + trailer
        if request_id is not None:
            posted_body += "\n\n" + _request_marker(request_id)
        url = ""
        number: int | None = None
        issue_created = False
        issue_write_started = False
        token = ""
        try:
            token = _github_token(hub, actor)
            created = None
            if request_id is not None and recovering_request:
                recovery_deadline = time.monotonic() + ASSIGNMENT_RECOVERY_SECONDS

                def matches_recovery(row: dict[str, Any]) -> bool:
                    return _recovered_body_matches(
                        row, actor, posted_body, request_id
                    )

                recent = await asyncio.to_thread(
                    hub.github.recent_issues_by_creator,
                    token,
                    repo,
                    actor,
                    _github_read_timeout(recovery_deadline),
                    recovery_since,
                    matches_recovery,
                )
                created = next(
                    (
                        issue_row
                        for issue_row in recent
                        if matches_recovery(issue_row)
                    ),
                    None,
                )
            if created is None:
                _renew_request_or_conflict(
                    hub,
                    actor,
                    "assignment.created",
                    request_id,
                    request_owner,
                )
                _mark_request_prior_write_or_conflict(
                    hub,
                    actor,
                    "assignment.created",
                    request_id,
                    request_owner,
                )
                if request_id is not None:
                    request.state.request_id_prior_write = True
                issue_write_started = True
                created = await asyncio.to_thread(
                    hub.github.create_issue, token, repo, title, posted_body, [agent]
                )
            elif request_id is not None:
                _mark_request_prior_write_or_conflict(
                    hub,
                    actor,
                    "assignment.created",
                    request_id,
                    request_owner,
                )
                request.state.request_id_prior_write = True
            issue_created = True
            created_number = created.get("number")
            created_url = created.get("html_url")
            if isinstance(created_number, int) and not isinstance(created_number, bool):
                number = created_number
            if isinstance(created_url, str):
                url = created_url
            if number is None or not url:
                raise GitHubError("GitHub issue creation returned no issue number or URL")
            canonical_repo = _issue_repository_name(created)
            if canonical_repo is None:
                repository = await asyncio.to_thread(hub.github.repository, token, repo)
                full_name = repository.get("full_name")
                if not valid_repository_name(full_name):
                    raise GitHubError("GitHub repository lookup returned no canonical full_name")
                canonical_repo = full_name
            repo = canonical_repo
            current = await asyncio.to_thread(hub.github.issue, token, repo, number)
            if agent not in _issue_assignees(current):
                # The assignees endpoint returns the updated issue; reuse it instead of a
                # second GET, so a failed read never drops a mandate GitHub already holds.
                _renew_request_or_conflict(
                    hub,
                    actor,
                    "assignment.created",
                    request_id,
                    request_owner,
                )
                current = await asyncio.to_thread(hub.github.add_assignees, token, repo, number, [agent])
            if agent not in _issue_assignees(current):
                if request_id is not None:
                    # The issue exists; a retry may take over at once and recover it.
                    hub.store.expire_request(
                        actor, "assignment.created", request_id, request_owner
                    )
                return JSONResponse(
                    _request_id_error_content(request, 502, {
                        "detail": "GitHub did not retain the requested assignee after one retry",
                        "url": url,
                        "number": number,
                        "assignee_missing": True,
                    }),
                    status_code=502,
                )
        except HTTPException:
            if request_id is not None:
                if (
                    getattr(request.state, "request_id_prior_write", False)
                    or issue_write_started
                ):
                    hub.store.expire_request(
                        actor, "assignment.created", request_id, request_owner
                    )
                else:
                    hub.store.release_request(
                        actor, "assignment.created", request_id, request_owner
                    )
            raise
        except GitHubError as exc:
            if exc.status_code == 401:
                _forget_github_token(hub, actor, token)
                reauth_failure: dict[str, Any] = {"detail": GITHUB_REAUTH_DETAIL}
                if issue_created and url:
                    reauth_failure["url"] = url
                if issue_created and number is not None:
                    reauth_failure["number"] = number
                if request_id is not None:
                    if getattr(request.state, "request_id_prior_write", False):
                        hub.store.expire_request(
                            actor, "assignment.created", request_id, request_owner
                        )
                    else:
                        hub.store.release_request(
                            actor, "assignment.created", request_id, request_owner
                        )
                return JSONResponse(
                    _request_id_error_content(request, 409, reauth_failure),
                    status_code=409,
                )
            if request_id is not None and recovering_request and not issue_write_started:
                hub.store.expire_request(
                    actor, "assignment.created", request_id, request_owner
                )
                # Keep the reservation; only an unfinished read is "in progress".
                if exc.status_code is None:
                    raise HTTPException(
                        status_code=409,
                        detail="request is still in progress; retry shortly",
                    ) from exc
                recovery_failure: dict[str, Any] = {"detail": _bounded_github_error(exc)}
                if issue_created and url:
                    recovery_failure["url"] = url
                if issue_created and number is not None:
                    recovery_failure["number"] = number
                return JSONResponse(
                    _request_id_error_content(request, 502, recovery_failure),
                    status_code=502,
                )
            if request_id is not None and (
                issue_created or (issue_write_started and exc.status_code is not None)
            ):
                # A definite answer ends this attempt; only a write that may still be
                # running keeps its lease. Marker recovery stops a second issue.
                hub.store.expire_request(
                    actor, "assignment.created", request_id, request_owner
                )
            if issue_created:
                failure: dict[str, Any] = {"detail": _bounded_github_error(exc)}
                if url:
                    failure["url"] = url
                if number is not None:
                    failure["number"] = number
                return JSONResponse(
                    _request_id_error_content(request, 502, failure),
                    status_code=502,
                )
            if (
                request_id is not None
                and not getattr(request.state, "request_id_prior_write", False)
                and (not issue_write_started or exc.status_code is not None)
            ):
                hub.store.release_request(
                    actor, "assignment.created", request_id, request_owner
                )
            raise HTTPException(status_code=502, detail=_bounded_github_error(exc)) from exc
        if number is None:
            raise HTTPException(status_code=502, detail="GitHub issue creation returned no issue number")
        stored_url = _stored_github_issue_url(repo, number, url)
        activity_id = str(uuid.uuid4())
        event_payload = {
            "id": activity_id,
            "type": "assignment.created",
            "created_at": utcnow(),
            "payload": {
                "agent": agent,
                "repo": repo,
                "issue_key": f"{repo}#{number}",
                "number": number,
                "url": stored_url,
                "title": title,
                "actor": actor,
                "deviation": {"declared": False, "stated_by": actor},
            },
        }
        if request_id is not None:
            event_payload["payload"]["request_id"] = request_id
            event_payload["payload"]["request_fingerprint"] = request_fingerprint
            recorded = _record_web_event_for_request(
                hub,
                actor,
                "activity",
                "insert",
                activity_id,
                event_payload,
                activity_type="assignment.created",
                request_id=request_id,
                owner=request_owner,
            )
            if not recorded:
                raise HTTPException(
                    status_code=409,
                    detail="request is still in progress; retry shortly",
                )
        else:
            _record_web_event(
                hub, actor, "activity", "insert", activity_id, event_payload
            )
        await hub.publish({"type": "events", "login": actor, "count": 1})
        return JSONResponse(
            {"repo": repo, "number": number, "url": url, "agent": agent, "actor": actor},
            status_code=201,
        )

    @app.get("/api/assignments")
    def api_assignments(request: Request, agent: str) -> dict[str, Any]:
        login = hub.session_login(request)
        wanted = agent.strip().lower()
        if not hub.roster.is_agent(wanted) or wanted not in hub.visible(login):
            raise HTTPException(status_code=404, detail="unknown agent")
        _prune_issue_cache(hub)
        token = _optional_github_token(hub, login)
        rows = hub.store.query(
            "SELECT * FROM row_replica WHERE table_name = 'activity' "
            "AND github_login IN ({}) ORDER BY updated_at DESC, row_id DESC".format(
                _placeholders(hub.visible(login))
            ),
            tuple(sorted(hub.visible(login))),
        )
        assignments: list[dict[str, Any]] = []
        enrich = bool(token)
        enrichment_deadline = time.monotonic() + ASSIGNMENT_ENRICHMENT_SECONDS
        for row in rows:
            payload = loads(row["payload"])
            if (
                not isinstance(payload, dict)
                or not _trusted_hub_activity(row, payload)
                or payload.get("type") != "assignment.created"
            ):
                continue
            inner = payload.get("payload")
            if (
                not isinstance(inner, dict)
                or str(inner.get("agent", "")).lower() != wanted
            ):
                continue
            item = dict(inner)
            item["enriched"] = False
            repo = item.get("repo")
            number = item.get("number")
            if (
                enrich
                and time.monotonic() < enrichment_deadline
                and len(assignments) < ASSIGNMENT_ENRICHMENT_LIMIT
                and isinstance(repo, str)
                and isinstance(number, int)
                and not isinstance(number, bool)
            ):
                try:
                    item.update(
                        _assignment_enrichment(
                            hub,
                            login,
                            token,
                            repo,
                            number,
                            enrichment_deadline,
                        )
                    )
                except GitHubError as exc:
                    if exc.status_code == 401:
                        _forget_github_token(hub, login, token)
                        token = ""
                        enrich = False
                    elif exc.status_code is None:
                        enrich = False
            assignments.append(item)
        _prune_issue_cache(hub)
        return {"agent": wanted, "assignments": assignments}

    @app.post("/api/assignments/{owner}/{name}/{number}/comments")
    async def api_assignment_reply(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: dict[str, StrictStr | None],
    ):
        _prepare_request_id_error_state(request, body)
        actor, device_actor = _request_actor(hub, request)
        if device_actor and hub.roster.is_agent(actor):
            raise HTTPException(
                status_code=403,
                detail="not an owner of an agent assigned to this issue",
            )
        repo = f"{owner}/{name}"
        if not valid_repository_name(repo) or number < 1:
            raise HTTPException(status_code=404, detail="unknown issue")
        comment_body = _bounded_body_string(body, "body", 1, 60000)
        _reject_reserved_request_marker(comment_body)
        request_id = _optional_request_id(body)
        request_fingerprint = _request_fingerprint(
            {
                "owner": owner.casefold(),
                "name": name.casefold(),
                "number": number,
                "body": comment_body,
            }
        )
        request_owner = uuid.uuid4().hex
        recovering_request = False
        recovery_since = ""
        if request_id is not None:
            now, lease_until = _request_lease_times()
            reservation = hub.store.reserve_request(
                actor,
                "assignment.reply",
                request_id,
                request_fingerprint,
                request_owner,
                now,
                lease_until,
            )
            recovering_request = (
                reservation in ("reserved", "taken_over")
                and hub.store.request_has_prior_write(
                    actor, "assignment.reply", request_id, request_owner
                )
            )
            if recovering_request:
                request.state.request_id_prior_write = True
                recovery_since = hub.store.request_created_at(
                    actor, "assignment.reply", request_id, request_owner
                )
            if reservation == "conflict":
                raise HTTPException(
                    status_code=409,
                    detail=REQUEST_FINGERPRINT_CONFLICT_DETAIL,
                )
            if reservation == "done":
                replay = _assignment_request_replay(
                    hub,
                    actor,
                    "assignment.reply",
                    request_id,
                    request_fingerprint,
                )
                if replay is not None and isinstance(replay.get("url"), str):
                    return JSONResponse({"url": replay["url"]}, status_code=200)
                raise HTTPException(
                    status_code=409,
                    detail=REQUEST_ALREADY_COMPLETED_DETAIL,
                )
            if reservation == "busy":
                raise HTTPException(
                    status_code=409,
                    detail="request is still in progress; retry shortly",
                )
        posted_body = comment_body
        if request_id is not None:
            posted_body += "\n\n" + _request_marker(request_id)
        token = ""
        comment_write_started = False
        issue_row: dict[str, Any] | None = None
        try:
            token = _github_token(hub, actor)
            issue_row = await asyncio.to_thread(hub.github.issue, token, repo, number)
            # A transferred issue redirects to its new repository and number; comment
            # on exactly the issue whose assignees authorized the reply.
            path_repo, path_number = repo, number
            repo = _issue_repository_name(issue_row) or repo
            fetched_number = issue_row.get("number")
            if isinstance(fetched_number, int) and not isinstance(fetched_number, bool) and fetched_number > 0:
                number = fetched_number
            # Every assigned agent's timeline gets the reply; only the actor's own
            # agents grant the right to post it.
            assigned_agents = {
                agent for agent in _issue_assignees(issue_row) if hub.roster.is_agent(agent)
            }
            owned_assigned = {
                agent for agent in assigned_agents if hub.roster.owns_agent(actor, agent)
            }
            if not owned_assigned:
                raise HTTPException(status_code=403, detail="not an owner of an agent assigned to this issue")
            comment = None
            if request_id is not None and recovering_request:
                recovery_deadline = time.monotonic() + ASSIGNMENT_RECOVERY_SECONDS

                def matches_recovery(row: dict[str, Any]) -> bool:
                    return _recovered_body_matches(
                        row, actor, posted_body, request_id
                    )

                comments = await asyncio.to_thread(
                    hub.github.issue_comments,
                    token,
                    repo,
                    number,
                    _github_read_timeout(recovery_deadline),
                    recovery_since,
                    matches_recovery,
                )
                comment = next(
                    (
                        row for row in comments
                        if matches_recovery(row)
                    ),
                    None,
                )
            if comment is None:
                _renew_request_or_conflict(
                    hub,
                    actor,
                    "assignment.reply",
                    request_id,
                    request_owner,
                )
                _mark_request_prior_write_or_conflict(
                    hub,
                    actor,
                    "assignment.reply",
                    request_id,
                    request_owner,
                )
                if request_id is not None:
                    request.state.request_id_prior_write = True
                comment_write_started = True
                comment = await asyncio.to_thread(
                    hub.github.create_issue_comment, token, repo, number, posted_body
                )
            elif request_id is not None:
                _mark_request_prior_write_or_conflict(
                    hub,
                    actor,
                    "assignment.reply",
                    request_id,
                    request_owner,
                )
                request.state.request_id_prior_write = True
        except HTTPException:
            if request_id is not None:
                if (
                    getattr(request.state, "request_id_prior_write", False)
                    or comment_write_started
                ):
                    hub.store.expire_request(
                        actor, "assignment.reply", request_id, request_owner
                    )
                else:
                    hub.store.release_request(
                        actor, "assignment.reply", request_id, request_owner
                    )
            raise
        except GitHubError as exc:
            if exc.status_code == 401:
                _forget_github_token(hub, actor, token)
                if request_id is not None:
                    if getattr(request.state, "request_id_prior_write", False):
                        hub.store.expire_request(
                            actor, "assignment.reply", request_id, request_owner
                        )
                    else:
                        hub.store.release_request(
                            actor, "assignment.reply", request_id, request_owner
                        )
                raise HTTPException(status_code=409, detail=GITHUB_REAUTH_DETAIL) from exc
            if request_id is not None and recovering_request and not comment_write_started:
                hub.store.expire_request(
                    actor, "assignment.reply", request_id, request_owner
                )
                # Keep the reservation; only an unfinished read is "in progress".
                if exc.status_code is None:
                    raise HTTPException(
                        status_code=409,
                        detail="request is still in progress; retry shortly",
                    ) from exc
                reply_failure: dict[str, Any] = {"detail": _bounded_github_error(exc)}
                if issue_row is not None:
                    issue_url = issue_row.get("html_url")
                    reply_failure["url"] = _stored_github_issue_url(
                        repo, number, issue_url if isinstance(issue_url, str) else ""
                    )
                    reply_failure["number"] = number
                return JSONResponse(
                    _request_id_error_content(request, 502, reply_failure),
                    status_code=502,
                )
            if request_id is not None and comment_write_started and exc.status_code is not None:
                # A definite answer ends this attempt; marker recovery stops a second comment.
                hub.store.expire_request(
                    actor, "assignment.reply", request_id, request_owner
                )
            elif (
                request_id is not None
                and not getattr(request.state, "request_id_prior_write", False)
                and (not comment_write_started or exc.status_code is not None)
            ):
                hub.store.release_request(
                    actor, "assignment.reply", request_id, request_owner
                )
            raise HTTPException(status_code=502, detail=_bounded_github_error(exc)) from exc
        url = comment.get("html_url")
        if not isinstance(url, str) or not url:
            url = str(issue_row.get("html_url") or "")
        stored_url = _stored_github_issue_url(repo, number, url)
        activity_id = str(uuid.uuid4())
        event_payload = {
            "id": activity_id,
            "type": "assignment.reply",
            "created_at": utcnow(),
            "payload": {
                "agent": sorted(owned_assigned)[0],
                "agents": sorted(assigned_agents),
                "repo": repo,
                "issue_key": f"{repo}#{number}",
                "number": number,
                "url": stored_url,
                "actor": actor,
                "body": comment_body,
            },
        }
        if request_id is not None:
            event_payload["payload"]["request_id"] = request_id
            event_payload["payload"]["request_fingerprint"] = request_fingerprint
            recorded = _record_web_event_for_request(
                hub,
                actor,
                "activity",
                "insert",
                activity_id,
                event_payload,
                activity_type="assignment.reply",
                request_id=request_id,
                owner=request_owner,
            )
            if not recorded:
                raise HTTPException(
                    status_code=409,
                    detail="request is still in progress; retry shortly",
                )
        else:
            _record_web_event(
                hub, actor, "activity", "insert", activity_id, event_payload
            )
        _forget_cached_issue(hub, repo, number)
        _forget_cached_issue(hub, path_repo, path_number)
        await hub.publish({"type": "events", "login": actor, "count": 1})
        return JSONResponse({"url": url}, status_code=201)

    @app.get("/api/usage")
    def api_usage(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        return _usage_response(hub, login)

    @app.get("/api/state")
    def api_state(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        allowed = hub.visible(login)
        tables = {
            "session": "session",
            "activity": "activity",
            "task": "task",
            "task_round": "task_round",
            "agent": "agent",
            "checklist": "checklist_item",
            "checks": "local_check",
            "gates": "review_gate",
            "work": "open_work",
            "pings": "ping",
        }
        out: dict[str, Any] = {
            "generated_at": utcnow(),
            "login": login,
            "visible": sorted(allowed),
            "teams": _teams_for(login, hub.teams),
        }
        for key, table in tables.items():
            rows = hub.store.query(
                "SELECT payload, github_login, origin_device_id FROM row_replica "
                "WHERE table_name = ? AND github_login IN ({}) "
                "ORDER BY updated_at DESC, row_id DESC".format(_placeholders(allowed)),
                (table, *sorted(allowed)),
            )
            items = [_payload_with_origin(r) for r in rows]
            if key == "session":
                for item in items:
                    origin = item.get("_origin_device_id")
                    origin_s = origin if isinstance(origin, str) else ""
                    item["can_control"] = can_control(hub, login, origin_s)
                    item["control_connected"] = origin_s in hub.control_ready
            out[key] = items
        devices = hub.store.query(
            "SELECT id, github_login, name, created_at, revoked_at, confirmed_by FROM device "
            "WHERE github_login IN ({}) "
            "ORDER BY created_at DESC, id DESC".format(_placeholders(allowed)),
            tuple(sorted(allowed)),
        )
        out["devices"] = [row_dict(r) for r in devices]
        return out

    def _session_detail_response(request: Request, session_id: str) -> dict[str, Any]:
        login = hub.session_login(request)
        row = _visible_session_row(hub, login, session_id)
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="replica payload is not an object")
        origin = row["origin_device_id"]
        return {
            "id": session_id,
            "payload": payload,
            "_github_login": row["github_login"],
            "_origin_device_id": origin,
            "can_control": can_control(hub, login, origin),
            "control_connected": origin in hub.control_ready,
        }

    @app.get("/api/session")
    def api_session_detail_query(request: Request, id: str) -> dict[str, Any]:
        return _session_detail_response(request, id)

    @app.get("/api/sessions/{session_id}")
    def api_session_detail(request: Request, session_id: str) -> dict[str, Any]:
        return _session_detail_response(request, session_id)

    @app.get("/api/session-timeline")
    def api_session_timeline_query(request: Request, id: str) -> dict[str, Any]:
        login = hub.session_login(request)
        session_row = _visible_session_row(hub, login, id)
        rows = _session_timeline(hub, session_row, hub.visible(login))
        truncated = len(rows) > TIMELINE_ROW_CAP
        return {
            "session_id": id,
            "timeline": rows[-TIMELINE_ROW_CAP:],
            "truncated": truncated,
        }

    async def _session_control_response(
        request: Request, session_id: str, body: dict[str, Any]
    ) -> JSONResponse:
        login = _any_login(hub, request)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        action, payload = _parse_control_body(body)
        row = _visible_session_row(hub, login, session_id)
        origin = row["origin_device_id"]
        if not can_control(hub, login, origin):
            raise HTTPException(status_code=403, detail="not the owning device")
        queue = hub.control_ready.get(origin)
        if queue is None:
            raise HTTPException(status_code=409, detail="owning device is not control-connected")
        frame = {
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": payload,
        }
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise HTTPException(status_code=503, detail="control queue is full") from exc
        return JSONResponse({"queued": True}, status_code=202)

    @app.post("/api/session/control")
    async def api_session_control_query(
        request: Request, id: str, body: dict[str, Any]
    ) -> JSONResponse:
        return await _session_control_response(request, id, body)

    @app.post("/api/sessions/{session_id}/control")
    async def api_session_control(
        request: Request, session_id: str, body: dict[str, Any]
    ) -> JSONResponse:
        return await _session_control_response(request, session_id, body)

    async def _session_terminal_response(request: Request, session_id: str):
        login = hub.session_login(request)
        _visible_session_row(hub, login, session_id)
        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
            hub.terminal_queues.append((login, session_id, queue))

            async def gen():
                try:
                    yield f"data: {dumps({'type': 'hello', 'session_id': session_id})}\n\n"
                    for chunk in list(hub.terminal_rings.get(session_id, [])):
                        yield (
                            f"data: {dumps({'type': 'terminal', 'session_id': session_id, 'seq': chunk['seq'], 'data': chunk['data']})}\n\n"
                        )
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=20.0)
                        except asyncio.TimeoutError:
                            yield ":\n\n"
                            continue
                        yield f"data: {dumps(item)}\n\n"
                finally:
                    item = (login, session_id, queue)
                    if item in hub.terminal_queues:
                        hub.terminal_queues.remove(item)

            return StreamingResponse(gen(), media_type="text/event-stream")
        chunks = list(hub.terminal_rings.get(session_id, []))
        return {"session_id": session_id, "chunks": chunks}

    @app.get("/api/session/terminal")
    async def api_session_terminal_query(request: Request, id: str):
        return await _session_terminal_response(request, id)

    @app.get("/api/sessions/{session_id}/terminal")
    async def api_session_terminal(request: Request, session_id: str):
        return await _session_terminal_response(request, session_id)

    @app.post("/api/pings")
    async def api_ping(request: Request, body: dict[str, Any]) -> dict[str, str]:
        login = hub.session_login(request)
        target = _require_str(body, "to").lower()
        kind = _require_str(body, "kind")
        if kind not in PING_KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(sorted(PING_KINDS))}")
        if target not in hub.visible(login):
            raise HTTPException(status_code=403, detail="target is outside your teams")
        note = body.get("body")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="body must be a string")
        task_id = body.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise HTTPException(status_code=400, detail="task_id must be a string")
        ping_id = str(uuid.uuid4())
        payload = {
            "id": ping_id,
            "from_login": login,
            "to_login": target,
            "kind": kind,
            "task_id": task_id,
            "body": note,
            "created_at": utcnow(),
            "acked_at": None,
        }
        _record_web_event(hub, login, "ping", "insert", ping_id, payload)
        await hub.publish({"type": "ping", "from": login, "to": target, "id": ping_id, "login": login})
        return {"id": ping_id}

    @app.post("/api/pings/{ping_id}/ack")
    async def api_ping_ack(request: Request, ping_id: str) -> dict[str, Any]:
        login = _any_login(hub, request)
        row = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = 'ping' AND row_id = ?",
            (ping_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="unknown ping")
        payload = loads(row["payload"])
        if payload.get("to_login") != login:
            raise HTTPException(status_code=403, detail="only the recipient can ack")
        payload["acked_at"] = utcnow()
        _record_web_event(hub, login, "ping", "update", ping_id, payload)
        owner = row["github_login"]
        await hub.publish(
            {
                "type": "ping",
                "from": payload.get("from_login"),
                "to": login,
                "id": ping_id,
                "login": owner,
            }
        )
        return {"id": ping_id, "acked_at": payload["acked_at"], "payload": payload}

    @app.get("/api/stream")
    async def api_stream(request: Request):
        login = hub.session_login(request)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        hub.queues.append((login, queue))

        async def gen():
            try:
                yield f"data: {dumps({'type': 'hello', 'login': login})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ":\n\n"
                        continue
                    yield f"data: {dumps(item)}\n\n"
            finally:
                item = (login, queue)
                if item in hub.queues:
                    hub.queues.remove(item)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.websocket("/sync/ws")
    async def sync_ws(ws: WebSocket) -> None:
        token = ws.query_params.get("token")
        if not isinstance(token, str) or token == "":
            await ws.close(code=4401)
            return
        try:
            device = hub.device_for_token(token)
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        login = device["github_login"]
        device_id = device["id"]
        hub.queues.append((login, queue))
        hub.device_queues[device_id] = queue
        out_task: asyncio.Task[None] | None = None
        try:
            await ws.send_json({"type": "hello", "login": login, "device_id": device_id})

            async def pump_out() -> None:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        await ws.send_json({"type": "ping"})
                        continue
                    await ws.send_json(item)

            out_task = asyncio.create_task(pump_out())
            while True:
                try:
                    raw = await ws.receive_json()
                except WebSocketDisconnect:
                    raise
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                msg_type = raw.get("type")
                if msg_type == "control-ready":
                    hub.control_ready[device_id] = queue
                elif msg_type == "terminal":
                    _handle_terminal(hub, device, raw)
                elif msg_type == "control-ack":
                    continue
        except WebSocketDisconnect:
            pass
        finally:
            if out_task is not None:
                out_task.cancel()
                try:
                    await out_task
                except (asyncio.CancelledError, Exception):
                    pass
            if hub.control_ready.get(device_id) is queue:
                hub.control_ready.pop(device_id, None)
            if hub.device_queues.get(device_id) is queue:
                hub.device_queues.pop(device_id, None)
            item = (login, queue)
            if item in hub.queues:
                hub.queues.remove(item)

    return app


def _cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=PAIR_TTL_SECONDS)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cursors(request: Request) -> dict[str, int]:
    out: dict[str, int] = {}
    for raw in request.query_params.getlist("cursor"):
        if ":" not in raw:
            raise HTTPException(status_code=400, detail="cursor must be origin_id:seq")
        origin, seq = raw.rsplit(":", 1)
        if origin == "" or not seq.isdigit():
            raise HTTPException(status_code=400, detail="cursor must be origin_id:seq")
        out[origin] = int(seq)
    return out


def _own_events(hub: Hub, device_id: str, cursors: dict[str, int]) -> list[dict[str, Any]]:
    after = cursors.get(device_id, 0)
    rows = hub.store.query(
        "SELECT e.* FROM ledger_event e WHERE e.origin_device_id = ? AND e.origin_seq > ? ORDER BY e.origin_seq ASC",
        (device_id, after),
    )
    return [_event_out(r) for r in rows]


def _owned_session_ids(hub: Hub, device_id: str) -> set[str]:
    rows = hub.store.query(
        "SELECT row_id FROM row_replica WHERE table_name = 'session' AND origin_device_id = ?",
        (device_id,),
    )
    return {row["row_id"] for row in rows}


def _message_to_session(payload: dict[str, Any]) -> str | None:
    if payload.get("type") != "message":
        return None
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    target = inner.get("to_session")
    if not isinstance(target, str) or target == "":
        return None
    return target


def _inbox_snapshots(hub: Hub, device_id: str) -> list[dict[str, Any]]:
    owned = _owned_session_ids(hub, device_id)
    if not owned:
        return []
    out: list[dict[str, Any]] = []
    parent_ids: set[str] = set()
    for row in hub.store.query("SELECT * FROM row_replica WHERE table_name = 'activity'"):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        to_session = _message_to_session(payload)
        if to_session is None or to_session not in owned:
            continue
        out.append(_replica_out(row))
        parent_id = payload.get("session_id")
        if isinstance(parent_id, str) and parent_id != "":
            parent_ids.add((parent_id, row["origin_device_id"]))
    for parent_id, origin in sorted(parent_ids):
        parent = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = 'session' AND row_id = ?",
            (parent_id,),
        )
        if parent is not None and parent["origin_device_id"] == origin:
            out.append(_replica_out(parent))
    return out


def _ping_snapshots(hub: Hub, login: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in hub.store.query("SELECT * FROM row_replica WHERE table_name = 'ping'"):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if payload.get("from_login") == login or payload.get("to_login") == login:
            out.append(_replica_out(row))
    return out


def _record_web_event(
    hub: Hub,
    login: str,
    table: str,
    op: str,
    row_id: str,
    payload: dict[str, Any],
) -> None:
    origin = f"web:{login}"
    encoded = dumps(payload)
    occurred = utcnow()
    hub.store.record_web_event(origin, login, table, op, row_id, encoded, occurred)


def _record_web_event_for_request(
    hub: Hub,
    login: str,
    table: str,
    op: str,
    row_id: str,
    payload: dict[str, Any],
    *,
    activity_type: str,
    request_id: str,
    owner: str,
) -> bool:
    origin = f"web:{login}"
    encoded = dumps(payload)
    occurred = utcnow()
    return hub.store.record_web_event_for_request(
        origin,
        login,
        table,
        op,
        row_id,
        encoded,
        occurred,
        actor=login,
        activity_type=activity_type,
        request_id=request_id,
        owner=owner,
    )


def _request_actor(hub: Hub, request: Request) -> tuple[str, bool]:
    header = request.headers.get("authorization")
    if header is not None:
        if not header.lower().startswith("bearer ") or header[7:].strip() == "":
            raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
        device = hub.device_for_token(header[7:].strip())
        return device["github_login"], True
    return hub.session_login(request), False


def _github_token(hub: Hub, login: str) -> str:
    token = hub.github_tokens.get(login, "")
    if not token:
        token = hub.store.get_oauth_token(login)
        if token:
            hub.github_tokens[login] = token
    if not token:
        raise HTTPException(
            status_code=409,
            detail=GITHUB_TOKEN_REQUIRED_DETAIL,
        )
    return token


def _forget_github_token(hub: Hub, login: str, token: str) -> None:
    hub.store.delete_oauth_token(login, token)
    if hub.github_tokens.get(login) == token:
        hub.github_tokens.pop(login, None)


def _optional_github_token(hub: Hub, login: str) -> str:
    token = hub.github_tokens.get(login, "") or hub.store.get_oauth_token(login)
    if token:
        hub.github_tokens[login] = token
    return token


def _bounded_body_string(body: dict[str, Any], key: str, minimum: int, maximum: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or len(value) < minimum or len(value) > maximum:
        raise HTTPException(status_code=400, detail=f"{key} must be a string of length {minimum}..{maximum}")
    return value


def _reject_reserved_request_marker(body: str) -> None:
    if any(
        line.strip().startswith("<!-- agent-core-request:")
        for line in body.splitlines()
    ):
        raise HTTPException(status_code=400, detail=RESERVED_REQUEST_MARKER_DETAIL)


def _require_no_deviation(body: dict[str, Any]) -> dict[str, bool]:
    value = body.get("deviation")
    if (
        not isinstance(value, dict)
        or set(value) != {"declared"}
        or value.get("declared") is not False
    ):
        raise HTTPException(status_code=400, detail=DEVIATION_REQUIRED_DETAIL)
    return {"declared": False}


def _optional_request_id(body: dict[str, Any]) -> str | None:
    if "request_id" not in body:
        return None
    request_id = body["request_id"]
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        raise HTTPException(status_code=400, detail="request_id must be a UUID")
    return request_id


def _request_lease_times() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=REQUEST_LEASE_SECONDS)
    return (
        now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lease_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _request_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


def _prepare_request_id_error_state(request: Request, body: dict[str, Any]) -> None:
    request.state.request_id_sent = "request_id" in body
    request.state.request_id_prior_write = False


def _request_id_error_is_final(
    request: Request, status_code: int, detail: Any
) -> bool | None:
    if not getattr(request.state, "request_id_sent", False):
        return None
    if status_code in (400, 403, 404):
        return not getattr(request.state, "request_id_prior_write", False)
    if status_code != 409:
        return False
    if detail == REQUEST_FINGERPRINT_CONFLICT_DETAIL:
        return True
    if detail == REQUEST_ALREADY_COMPLETED_DETAIL:
        return True
    if detail in (GITHUB_REAUTH_DETAIL, GITHUB_TOKEN_REQUIRED_DETAIL):
        return not getattr(request.state, "request_id_prior_write", False)
    return False


def _request_id_error_content(
    request: Request, status_code: int, content: dict[str, Any]
) -> dict[str, Any]:
    request_id_final = _request_id_error_is_final(
        request, status_code, content.get("detail")
    )
    if request_id_final is not None:
        content["request_id_final"] = request_id_final
    return content


def _renew_request_or_conflict(
    hub: Hub,
    actor: str,
    activity_type: str,
    request_id: str | None,
    owner: str,
) -> None:
    if request_id is None:
        return
    now, lease_until = _request_lease_times()
    if hub.store.renew_request(
        actor, activity_type, request_id, owner, now, lease_until
    ):
        return
    raise HTTPException(
        status_code=409,
        detail="request is still in progress; retry shortly",
    )


def _mark_request_prior_write_or_conflict(
    hub: Hub,
    actor: str,
    activity_type: str,
    request_id: str | None,
    owner: str,
) -> None:
    if request_id is None:
        return
    if hub.store.mark_request_prior_write(actor, activity_type, request_id, owner):
        return
    raise HTTPException(
        status_code=409,
        detail="request is still in progress; retry shortly",
    )


def _request_marker(request_id: str) -> str:
    return f"<!-- agent-core-request:{request_id} -->"


def _body_without_request_marker(body: Any, request_id: str) -> str | None:
    if not isinstance(body, str):
        return None
    suffix = "\n\n" + _request_marker(request_id)
    if not body.endswith(suffix):
        return None
    return body[: -len(suffix)]


def _comment_author(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    if not isinstance(user, dict):
        return ""
    login = user.get("login")
    return login.strip().lower() if isinstance(login, str) else ""


def _recovered_body_matches(
    row: dict[str, Any], actor: str, expected_body: str, request_id: str
) -> bool:
    return (
        _comment_author(row) == actor
        and _body_without_request_marker(row.get("body"), request_id)
        == _body_without_request_marker(expected_body, request_id)
    )


def _github_read_timeout(deadline: float) -> float:
    return min(
        GITHUB_REQUEST_TIMEOUT,
        max(0.0, deadline - time.monotonic()),
    )


def _assignment_request_replay(
    hub: Hub,
    actor: str,
    activity_type: str,
    request_id: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    rows = hub.store.query(
        "SELECT * FROM row_replica WHERE table_name = 'activity' "
        "AND origin_device_id = ? AND github_login = ? ORDER BY updated_at DESC, row_id DESC",
        (f"web:{actor}", actor),
    )
    for row in rows:
        payload = loads(row["payload"])
        if (
            not isinstance(payload, dict)
            or not _trusted_hub_activity(row, payload)
            or payload.get("type") != activity_type
        ):
            continue
        inner = payload.get("payload")
        if (
            isinstance(inner, dict)
            and inner.get("request_id") == request_id
            and inner.get("request_fingerprint") == request_fingerprint
        ):
            return inner
    return None


def _assignment_create_response(inner: dict[str, Any]) -> dict[str, Any] | None:
    repo = inner.get("repo")
    number = inner.get("number")
    url = inner.get("url")
    agent = inner.get("agent")
    actor = inner.get("actor")
    if (
        not isinstance(repo, str)
        or not isinstance(number, int)
        or isinstance(number, bool)
        or not isinstance(url, str)
        or not isinstance(agent, str)
        or not isinstance(actor, str)
    ):
        return None
    return {"repo": repo, "number": number, "url": url, "agent": agent, "actor": actor}


def _bounded_github_error(exc: Exception) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    return text[:600] or "GitHub request failed"


def _issue_repository_name(issue_row: dict[str, Any]) -> str | None:
    repository_url = issue_row.get("repository_url")
    if not isinstance(repository_url, str):
        return None
    match = re.search(r"/repos/([^/]+)/([^/]+)/?$", repository_url)
    if match is None:
        return None
    repo = f"{match.group(1)}/{match.group(2)}"
    return repo if valid_repository_name(repo) else None


def _stored_github_issue_url(repo: str, number: int, candidate: str = "") -> str:
    """Return a canonical GitHub issue URL, preserving only a valid comment fragment."""
    base = f"https://github.com/{repo}/issues/{number}"
    if not isinstance(candidate, str) or candidate == "":
        return base
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return base
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != f"/{repo}/issues/{number}"
        or parsed.query
        or re.fullmatch(r"issuecomment-[0-9]+", parsed.fragment or "") is None
    ):
        return base
    return f"{base}#{parsed.fragment}"


def _issue_assignees(issue_row: dict[str, Any]) -> set[str]:
    raw = issue_row.get("assignees")
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("login"), str):
            out.add(item["login"].strip().lower())
        elif isinstance(item, str):
            out.add(item.strip().lower())
    return {login for login in out if login}


def _assignment_enrichment(
    hub: Hub,
    viewer: str,
    token: str,
    repo: str,
    number: int,
    deadline: float,
) -> dict[str, Any]:
    if not token:
        return {"enriched": False}
    key = (viewer, repo, number)
    cached = hub.issue_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return dict(cached[1])
    epoch = hub.issue_cache_epoch
    if time.monotonic() >= deadline:
        return {"enriched": False}
    try:
        issue_timeout = _github_read_timeout(deadline)
        if issue_timeout <= 0:
            return {"enriched": False}
        issue_row = hub.github.issue(token, repo, number, issue_timeout)
        comments_timeout = _github_read_timeout(deadline)
        if comments_timeout <= 0:
            return {"enriched": False}
        raw_comments = hub.github.issue_comments(
            token, repo, number, comments_timeout
        )
        if time.monotonic() >= deadline:
            return {"enriched": False}
    except GitHubError as exc:
        if exc.status_code in (None, 401):
            raise
        return {"enriched": False}
    comments: list[dict[str, Any]] = []
    for raw in raw_comments:
        if not isinstance(raw, dict):
            continue
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        author = user.get("login") if isinstance(user, dict) else ""
        body = raw.get("body") if isinstance(raw.get("body"), str) else ""
        created_at = raw.get("created_at") if isinstance(raw.get("created_at"), str) else ""
        updated_at = (
            raw.get("updated_at")
            if isinstance(raw.get("updated_at"), str)
            else created_at
        )
        url = raw.get("html_url") if isinstance(raw.get("html_url"), str) else ""
        comments.append(
            {
                "_id": _comment_id(raw),
                "author": author if isinstance(author, str) else "",
                "created_at": created_at,
                "updated_at": updated_at,
                "body": body[:4000],
                "url": url,
            }
        )
    comments.sort(
        key=lambda row: (row["updated_at"], row["_id"]), reverse=True
    )
    for comment in comments:
        comment.pop("_id", None)
    result = {
        "enriched": True,
        "state": issue_row.get("state") if isinstance(issue_row.get("state"), str) else "",
        "assignees": sorted(_issue_assignees(issue_row)),
        "comments": comments[:20],
    }
    # A reply during these reads bumps the epoch; do not cache what it may have made stale.
    with hub.issue_cache_lock:
        if hub.issue_cache_epoch == epoch:
            hub.issue_cache[key] = (time.monotonic() + ISSUE_CACHE_SECONDS, result)
    return dict(result)


def _comment_id(comment: dict[str, Any]) -> int:
    value = comment.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _forget_cached_issue(hub: Hub, repo: str, number: int) -> None:
    """Drop every viewer's cached copy of one issue after a reply changed it."""
    folded = repo.casefold()
    with hub.issue_cache_lock:
        hub.issue_cache_epoch += 1
        for key in list(hub.issue_cache):
            _viewer, cached_repo, cached_number = key
            if cached_number == number and cached_repo.casefold() == folded:
                hub.issue_cache.pop(key, None)


def _prune_issue_cache(hub: Hub) -> None:
    now = time.monotonic()
    for key, (expires_at, _result) in list(hub.issue_cache.items()):
        if expires_at <= now:
            hub.issue_cache.pop(key, None)
    overflow = len(hub.issue_cache) - ISSUE_CACHE_MAX_ENTRIES
    if overflow > 0:
        # Sort a snapshot: the enrichment threadpool may insert while this runs.
        snapshot = list(hub.issue_cache.items())
        oldest = [key for key, _entry in sorted(snapshot, key=lambda item: item[1][0])[:overflow]]
        for key in oldest:
            hub.issue_cache.pop(key, None)


def _activity_inner(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _int_usage(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _row_time(payload: dict[str, Any], fallback: str) -> str:
    for key in ("created_at", "started_at", "ran_at", "recorded_at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    inner = _activity_inner(payload)
    for key in ("created_at", "started_at", "ran_at", "recorded_at"):
        value = inner.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _usage_response(hub: Hub, login: str) -> dict[str, Any]:
    allowed = hub.visible(login)
    rows = hub.store.query(
        "SELECT * FROM row_replica WHERE table_name = 'activity' "
        "AND github_login IN ({}) ORDER BY updated_at DESC, row_id DESC".format(_placeholders(allowed)),
        tuple(sorted(allowed)),
    )
    lane_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    account_latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    grok: list[dict[str, Any]] = []
    for row in rows:
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        inner = _activity_inner(payload)
        activity_type = payload.get("type")
        if activity_type == "lane.run":
            provider = inner.get("vendor") or inner.get("provider") or payload.get("vendor") or payload.get("provider") or ""
            model = inner.get("model") or payload.get("model") or ""
            session_id = payload.get("session_id") or inner.get("session_id") or ""
            if not all(isinstance(value, str) for value in (provider, model, session_id)):
                continue
            usage = inner.get("usage") if isinstance(inner.get("usage"), dict) else payload.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            key = (row["github_login"], session_id, provider, model)
            item = lane_groups.get(key)
            at = _row_time(payload, row["updated_at"])
            if item is None:
                item = {
                    "login": row["github_login"],
                    "session_id": session_id,
                    "provider": provider,
                    "model": model,
                    "runs": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "last_at": at,
                    "_changed_key": (row["updated_at"], row["row_id"]),
                }
                lane_groups[key] = item
            item["runs"] += 1
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                item[field] += _int_usage(usage.get(field))
            if at > item["last_at"]:
                item["last_at"] = at
            changed_key = (row["updated_at"], row["row_id"])
            if changed_key > item["_changed_key"]:
                item["_changed_key"] = changed_key
        if activity_type != "usage.snapshot":
            continue
        vendor = inner.get("vendor") or inner.get("provider") or ""
        if vendor == "grok":
            grok.append(_payload_with_origin(row))
        if vendor != "openrouter":
            continue
        account = inner.get("account") or inner.get("account_email") or ""
        if not isinstance(account, str):
            account = ""
        account_key = (row["github_login"], vendor, account)
        candidate = {
            "login": row["github_login"],
            "vendor": vendor,
            "account": account,
            "usage_usd": inner.get("usage_usd"),
            "usage_daily_usd": inner.get("usage_daily_usd"),
            "usage_weekly_usd": inner.get("usage_weekly_usd"),
            "usage_monthly_usd": inner.get("usage_monthly_usd"),
            "limit_usd": inner.get("limit_usd"),
            "limit_remaining_usd": inner.get("limit_remaining_usd"),
            "fetched_at": inner.get("fetched_at"),
            "_changed_key": (row["updated_at"], row["row_id"]),
        }
        current = account_latest.get(account_key)
        if current is None or str(candidate.get("fetched_at") or "") > str(current.get("fetched_at") or ""):
            if current is not None and current["_changed_key"] > candidate["_changed_key"]:
                candidate["_changed_key"] = current["_changed_key"]
            account_latest[account_key] = candidate
        elif candidate["_changed_key"] > current["_changed_key"]:
            current["_changed_key"] = candidate["_changed_key"]
    # Newest first means the latest replica change (CONTRIBUTING); last_at and
    # fetched_at stay as displayed facts.
    lanes = sorted(
        lane_groups.values(),
        key=lambda row: (
            row["_changed_key"],
            row["login"],
            row["session_id"],
            row["provider"],
            row["model"],
        ),
        reverse=True,
    )
    accounts = sorted(
        account_latest.values(),
        key=lambda row: (row["_changed_key"], row["login"], row["account"]),
        reverse=True,
    )
    for item in (*lanes, *accounts):
        item.pop("_changed_key", None)
    return {"lanes": lanes, "accounts": accounts, "grok": grok}


def _payload_relation(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    inner = _activity_inner(payload)
    value = inner.get(key)
    return value if isinstance(value, str) else ""


def _tool_text(call: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = call.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _lane_tool_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a lane run's tool list for display.

    The producer writes inner ``tools``; legacy inner ``tool_calls`` is used only
    when ``tools`` is not a list, so an empty ``tools`` list wins.
    """
    inner = _activity_inner(payload)
    tools = inner.get("tools")
    modern = isinstance(tools, list)
    raw = tools if modern else inner.get("tool_calls")
    calls: list[dict[str, str]] = []
    for call in raw if isinstance(raw, list) else []:
        if not isinstance(call, dict):
            continue
        error = call.get("error")
        if error is True or (
            not modern and not error and call.get("status") == "error"
        ):
            error_text = "error"
        elif isinstance(error, str):
            error_text = error
        else:
            error_text = ""
        calls.append(
            {
                "tool": _tool_text(call, ("tool", "name")) or "tool",
                "target": _tool_text(call, ("target", "path", "command")),
                "error": error_text,
            }
        )
    return {"calls": calls, "truncated": inner.get("tools_truncated") is True}


def _timeline_item(row: Any, payload: dict[str, Any]) -> dict[str, Any]:
    item = {
        "table": row["table_name"],
        "row_id": row["row_id"],
        "at": _row_time(payload, row["updated_at"]),
        "payload": payload,
    }
    if row["table_name"] == "activity" and payload.get("type") == "lane.run":
        item["tools"] = _lane_tool_view(payload)
    return item


def _timeline_row_matches_session_login(row: Any, session_login: str) -> bool:
    login = row["github_login"]
    return isinstance(login, str) and login.lower() == session_login


def _session_timeline(hub: Hub, session_row: Any, allowed: set[str]) -> list[dict[str, Any]]:
    session_id = session_row["row_id"]
    session_login = str(session_row["github_login"]).lower()
    session_payload = loads(session_row["payload"])
    if not isinstance(session_payload, dict):
        session_payload = {}
    selected: list[tuple[Any, dict[str, Any]]] = [(session_row, session_payload)]
    task_ids: set[str] = set()
    rows = hub.store.query(
        "SELECT * FROM row_replica WHERE github_login IN ({}) AND table_name IN "
        "('activity','task','task_round','agent','checklist_item','local_check','review_gate')".format(
            _placeholders(allowed)
        ),
        tuple(sorted(allowed)),
    )
    for row in rows:
        if row["table_name"] != "task":
            continue
        payload = loads(row["payload"])
        if (
            not isinstance(payload, dict)
            or not _timeline_row_matches_session_login(row, session_login)
            or _payload_relation(payload, "session_id") != session_id
        ):
            continue
        selected.append((row, payload))
        row_id = row["row_id"]
        if isinstance(row_id, str) and row_id:
            task_ids.add(row_id)
        payload_id = payload.get("id")
        if isinstance(payload_id, str) and payload_id:
            task_ids.add(payload_id)
    for row in rows:
        if row["table_name"] != "activity":
            continue
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not _trusted_hub_activity(row, payload):
            continue
        if (
            payload.get("type") not in HUB_AUTHORED_ACTIVITY_TYPES
            and not _timeline_row_matches_session_login(row, session_login)
        ):
            continue
        task_id = _payload_relation(payload, "task_id")
        inner = payload.get("payload")
        assignment_for_session = False
        if payload.get("type") in HUB_AUTHORED_ACTIVITY_TYPES and isinstance(inner, dict):
            agent = inner.get("agent")
            agents = inner.get("agents")
            assignment_for_session = (
                isinstance(agent, str) and agent.lower() == session_login
            ) or (
                isinstance(agents, list)
                and any(
                    isinstance(item, str) and item.lower() == session_login for item in agents
                )
            )
        if (
            _payload_relation(payload, "session_id") == session_id
            or (task_id != "" and task_id in task_ids)
            or assignment_for_session
        ):
            selected.append((row, payload))
    for row in rows:
        if row["table_name"] in ("activity", "task"):
            continue
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not _timeline_row_matches_session_login(row, session_login):
            continue
        task_id = _payload_relation(payload, "task_id")
        if _payload_relation(payload, "session_id") == session_id or (task_id != "" and task_id in task_ids):
            selected.append((row, payload))
    timeline = [_timeline_item(row, payload) for row, payload in selected]
    timeline.sort(key=lambda item: (item["at"], item["row_id"], item["table"]))
    return timeline


def _any_login(hub: Hub, request: Request) -> str:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return hub.device_for_token(header[7:].strip())["github_login"]
    return hub.session_login(request)


def _reserved_device_id(device_id: Any) -> bool:
    return isinstance(device_id, str) and device_id.lower().startswith("web:")


def _reject_reserved_device_id(device_id: str) -> None:
    if _reserved_device_id(device_id):
        raise HTTPException(status_code=400, detail="web: device ids are reserved")


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value.strip()


def _require_int_string(name: str, value: str) -> int:
    if not value.isdigit() and not (value.startswith("-") and value[1:].isdigit()):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")
    return int(value)


def _placeholders(items: set[str]) -> str:
    if not items:
        raise HTTPException(status_code=500, detail="visibility set is empty")
    return ",".join("?" for _ in items)


def _teams_for(login: str, teams: dict[str, list[str]]) -> list[str]:
    self = login.lower()
    return sorted(name for name, members in teams.items() if self in members)


def _age_seconds(created_at: str) -> float:
    from datetime import datetime, timezone

    stamp = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _event_out(row: Any) -> dict[str, Any]:
    return {
        "hub_seq": row["hub_seq"],
        "origin_device_id": row["origin_device_id"],
        "origin_seq": row["origin_seq"],
        "table": row["table_name"],
        "op": row["op"],
        "row_id": row["row_id"],
        "payload": loads(row["payload"]),
        "occurred_at": row["occurred_at"],
    }


def _replica_out(row: Any) -> dict[str, Any]:
    return {
        "table": row["table_name"],
        "row_id": row["row_id"],
        "origin_device_id": row["origin_device_id"],
        "github_login": row["github_login"],
        "payload": loads(row["payload"]),
        "updated_at": row["updated_at"],
    }


def _payload_with_origin(row: Any) -> dict[str, Any]:
    payload = loads(row["payload"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="replica payload is not an object")
    payload["_github_login"] = row["github_login"]
    payload["_origin_device_id"] = row["origin_device_id"]
    return payload


def _visible_session_row(hub: Hub, login: str, session_id: str) -> Any:
    row = hub.store.query_one(
        "SELECT * FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (session_id,),
    )
    if row is None or row["github_login"] not in hub.visible(login):
        raise HTTPException(status_code=404, detail="unknown session")
    return row


def _require_dim(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 500:
        raise HTTPException(status_code=400, detail=f"{key} must be an integer from 1 to 500")
    return value


def _optional_dim(body: dict[str, Any], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _require_dim(body, key)


def _parse_control_body(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = body.get("action")
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        raise HTTPException(status_code=400, detail="action must be one of start, stop, input, resize")
    if action == "start":
        payload: dict[str, Any] = {}
        if "command" in body and body["command"] is not None:
            command = body["command"]
            if not isinstance(command, str) or len(command) < 1 or len(command) > 4000:
                raise HTTPException(status_code=400, detail="command must be a string of length 1..4000")
            payload["command"] = command
        if "provider" in body and body["provider"] is not None:
            provider = body["provider"]
            if provider != "grok":
                raise HTTPException(status_code=400, detail="provider must be grok")
            payload["provider"] = "grok"
        if "model" in body and body["model"] is not None:
            model = body["model"]
            if not isinstance(model, str) or len(model) < 1 or len(model) > 64:
                raise HTTPException(status_code=400, detail="model must be a string of length 1..64")
            payload["model"] = model
        if payload.get("provider") and payload.get("command"):
            raise HTTPException(status_code=400, detail="provider and command cannot both be set")
        if "model" in payload and payload.get("provider") != "grok":
            raise HTTPException(status_code=400, detail="model requires provider grok")
        cols = _optional_dim(body, "cols")
        rows = _optional_dim(body, "rows")
        if cols is not None:
            payload["cols"] = cols
        if rows is not None:
            payload["rows"] = rows
        return action, payload
    if action == "stop":
        return action, {}
    if action == "input":
        data = body.get("data")
        key = body.get("key")
        has_data = data is not None
        has_key = key is not None
        if has_data == has_key:
            raise HTTPException(status_code=400, detail="input requires exactly one of data or key")
        if has_data:
            if not isinstance(data, str):
                raise HTTPException(status_code=400, detail="data must be a string")
            byte_len = len(data.encode("utf-8"))
            if byte_len < 1 or byte_len > 4096:
                raise HTTPException(status_code=400, detail="data must be 1..4096 utf-8 bytes")
            return action, {"data": data}
        if not isinstance(key, str) or key not in CONTROL_KEYS:
            raise HTTPException(status_code=400, detail="key must be one of enter, ctrl-c, tab")
        return action, {"key": key}
    cols = body.get("cols")
    rows = body.get("rows")
    if cols is None or rows is None:
        raise HTTPException(status_code=400, detail="resize requires cols and rows")
    return action, {"cols": _require_dim(body, "cols"), "rows": _require_dim(body, "rows")}


def _handle_terminal(hub: Hub, device: dict[str, Any], raw: dict[str, Any]) -> None:
    session_id = raw.get("session_id")
    seq = raw.get("seq")
    data = raw.get("data")
    if not isinstance(session_id, str) or session_id == "":
        return
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return
    if not isinstance(data, str):
        return
    row = hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (session_id,),
    )
    if row is None:
        return
    if row["origin_device_id"] != device["id"]:
        return
    if len(data) > TERMINAL_CHUNK_MAX:
        data = data[:TERMINAL_CHUNK_MAX]
    ring = hub.terminal_rings.setdefault(session_id, [])
    ring.append({"seq": seq, "data": data})
    if len(ring) > TERMINAL_RING_SIZE:
        del ring[:-TERMINAL_RING_SIZE]
    event = {"type": "terminal", "session_id": session_id, "seq": seq, "data": data}
    hub.publish_terminal(event)


def _session_mail_notify(hub: Hub, device: dict[str, Any], table: str, op: str, payload: dict[str, Any]) -> list[str]:
    if table != "activity" or op not in ("insert", "update"):
        return []
    to_session = _message_to_session(payload)
    if to_session is None:
        if payload.get("type") == "message":
            raise HTTPException(status_code=400, detail="message activity requires payload.to_session")
        return []
    target = hub.store.query_one(
        "SELECT github_login FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (to_session,),
    )
    if target is None:
        raise HTTPException(status_code=404, detail="unknown session")
    owner = target["github_login"]
    if owner not in hub.visible(device["github_login"]):
        raise HTTPException(status_code=403, detail="target session is outside your teams")
    parent_id = payload.get("session_id")
    if not isinstance(parent_id, str) or parent_id == "":
        raise HTTPException(status_code=400, detail="message activity requires session_id")
    parent = hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (parent_id,),
    )
    if parent is None:
        raise HTTPException(status_code=400, detail="message activity session_id is unknown")
    if parent["origin_device_id"] != device["id"]:
        raise HTTPException(status_code=403, detail="message activity session_id is not owned by this device")
    return [owner]


def _validate_ping(hub: Hub, device: dict[str, Any], table: str, op: str, payload: dict[str, Any], replica: Any) -> None:
    if table != "ping":
        return
    login = device["github_login"]
    if op == "insert":
        if payload.get("from_login") != login:
            raise HTTPException(status_code=403, detail="from_login must be this device login")
        target = payload.get("to_login")
        if not isinstance(target, str) or target == "":
            raise HTTPException(status_code=400, detail="to_login is required")
        if target != target.lower():
            raise HTTPException(status_code=400, detail="to_login must be lowercase")
        if target not in hub.visible(login):
            raise HTTPException(status_code=403, detail="target is outside your teams")
        return
    if op != "update":
        return
    if replica is None:
        raise HTTPException(status_code=404, detail="unknown ping")
    previous = loads(replica["payload"])
    if not isinstance(previous, dict):
        return
    if payload.get("from_login") != previous.get("from_login") or payload.get("to_login") != previous.get("to_login"):
        raise HTTPException(status_code=403, detail="cannot retarget a ping")


def _reject_open_session_collision(hub: Hub, origin: str, table: str, op: str, row_id: str) -> None:
    if table != "session" or op != "insert":
        return
    seen = hub.store.query_one(
        "SELECT origin_seq FROM ledger_event WHERE table_name = 'session' AND row_id = ? LIMIT 1",
        (row_id,),
    )
    if seen is not None:
        raise HTTPException(status_code=409, detail="session id is already open")
    replica = hub.store.query_one(
        "SELECT origin_device_id, payload FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (row_id,),
    )
    if replica is None:
        return
    raise HTTPException(status_code=409, detail="session id is already open")


def _accept_event(
    hub: Hub, device: dict[str, Any], event: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None]:
    origin = event.get("origin_device_id")
    if isinstance(origin, str) and origin.lower().startswith("web:"):
        raise HTTPException(status_code=403, detail="web: origins are reserved")
    if origin != device["id"]:
        raise HTTPException(status_code=403, detail="cannot push events for another device")
    table = event.get("table")
    op = event.get("op")
    row_id = event.get("row_id")
    seq = event.get("origin_seq")
    payload = event.get("payload")
    occurred = event.get("occurred_at")
    if table not in ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f"unknown table: {table}")
    if op not in ALLOWED_OPS:
        raise HTTPException(status_code=400, detail=f"unknown op: {op}")
    if not isinstance(row_id, str) or row_id == "":
        raise HTTPException(status_code=400, detail="row_id is required")
    if not isinstance(seq, int) or seq < 1:
        raise HTTPException(status_code=400, detail="origin_seq must be a positive integer")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    if not isinstance(occurred, str) or occurred == "":
        raise HTTPException(status_code=400, detail="occurred_at is required")
    existing = hub.store.query_one(
        "SELECT * FROM ledger_event WHERE origin_device_id = ? AND origin_seq = ?",
        (origin, seq),
    )
    encoded = dumps(payload)
    if existing is not None:
        if (
            existing["table_name"] != table
            or existing["op"] != op
            or existing["row_id"] != row_id
            or existing["payload"] != encoded
            or existing["occurred_at"] != occurred
        ):
            raise HTTPException(status_code=409, detail=f"origin_seq {seq} already exists with different content")
        return [], None
    # Checked after the identical-replay answer, so a retried event stored before this
    # rule existed is still acknowledged instead of blocking the device's sequence.
    if table == "activity" and payload.get("type") in HUB_AUTHORED_ACTIVITY_TYPES:
        raise HTTPException(status_code=400, detail="assignment activity is hub-authored only")
    notify = _session_mail_notify(hub, device, table, op, payload)
    _reject_open_session_collision(hub, origin, table, op, row_id)
    last = hub.store.query_one(
        "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
        (origin,),
    )
    last_seq = int(last["m"]) if last is not None else 0
    if seq != last_seq + 1:
        raise HTTPException(status_code=409, detail=f"origin_seq gap: have {last_seq}, got {seq}")
    replica = hub.store.query_one(
        "SELECT origin_device_id, payload FROM row_replica WHERE table_name = ? AND row_id = ?",
        (table, row_id),
    )
    _validate_ping(hub, device, table, op, payload, replica)
    ping_ack = False
    if table == "session" and op == "update" and replica is None:
        raise HTTPException(status_code=404, detail="unknown session")
    replica_encoded = encoded
    if table == "ping" and op == "update" and replica is not None:
        previous = loads(replica["payload"])
        ping_ack = previous.get("to_login") == device["github_login"] and previous.get("id") == payload.get("id")
        if ping_ack and isinstance(previous, dict):
            previous["acked_at"] = payload.get("acked_at")
            replica_encoded = dumps(previous)
    if replica is not None and replica["origin_device_id"] != origin and not ping_ack:
        raise HTTPException(status_code=403, detail="row belongs to another device")
    write_origin = replica["origin_device_id"] if ping_ack and replica is not None else origin
    hub.store.execute(
        "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (origin, seq, table, op, row_id, encoded, occurred, utcnow()),
    )
    if op == "delete":
        hub.store.execute(
            "DELETE FROM row_replica WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        )
        return [], None
    updated_at = utcnow()
    if ping_ack:
        hub.store.execute(
            "UPDATE row_replica SET payload = ?, updated_at = ? WHERE table_name = ? AND row_id = ?",
            (replica_encoded, updated_at, table, row_id),
        )
        row = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        )
        return [], _replica_out(row) if row is not None else None
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(table_name, row_id) DO UPDATE SET origin_device_id = excluded.origin_device_id, "
        "github_login = excluded.github_login, payload = excluded.payload, updated_at = excluded.updated_at",
        (table, row_id, write_origin, device["github_login"], replica_encoded, updated_at),
    )
    return notify, {
        "table": table,
        "row_id": row_id,
        "origin_device_id": write_origin,
        "github_login": device["github_login"],
        "payload": loads(replica_encoded),
        "updated_at": updated_at,
    }


def _validate_match(match: Any) -> dict[str, Any]:
    if not isinstance(match, dict):
        raise HTTPException(status_code=400, detail="match must be an object")
    if len(match) > MAX_MATCH_PREDICATES:
        raise HTTPException(status_code=400, detail="at most 4 predicates per match")
    out: dict[str, Any] = {}
    for path, value in match.items():
        if path not in MATCH_PATHS:
            raise HTTPException(status_code=400, detail="unknown match path")
        if isinstance(value, str):
            if value == "":
                raise HTTPException(status_code=400, detail="match equality string must be non-empty")
            out[path] = value
            continue
        if isinstance(value, dict):
            if set(value.keys()) != {"in"}:
                raise HTTPException(status_code=400, detail="match object value must have only key in")
            in_list = value["in"]
            if not isinstance(in_list, list) or len(in_list) == 0:
                raise HTTPException(status_code=400, detail="in must be a non-empty list of strings")
            if len(in_list) > MAX_IN_VALUES:
                raise HTTPException(status_code=400, detail="at most 16 strings in each in")
            if not all(isinstance(item, str) for item in in_list):
                raise HTTPException(status_code=400, detail="in must be a non-empty list of strings")
            out[path] = {"in": list(in_list)}
            continue
        raise HTTPException(status_code=400, detail="match value must be a string or in-object")
    return out


def _match_path_lookup(payload: dict[str, Any], path: str) -> Any:
    if path == "type":
        if "type" not in payload:
            return _MISSING
        return payload["type"]
    if path.startswith("payload."):
        key = path[len("payload.") :]
        inner = payload.get("payload")
        if not isinstance(inner, dict) or key not in inner:
            return _MISSING
        return inner[key]
    return _MISSING


def _match_applies(match: dict[str, Any], payload: dict[str, Any]) -> bool:
    for path, pred in match.items():
        value = _match_path_lookup(payload, path)
        if value is _MISSING:
            return False
        if isinstance(pred, str):
            if value != pred:
                return False
            continue
        if value not in pred["in"]:
            return False
    return True


def _device_matches(hub: Hub, device_id: str) -> list[dict[str, Any]]:
    rows = hub.store.query(
        "SELECT match_json FROM device_subscription WHERE device_id = ? ORDER BY seq ASC",
        (device_id,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        loaded = loads(row["match_json"])
        if isinstance(loaded, dict):
            out.append(loaded)
    return out


def _subscriptions_response(hub: Hub, device_id: str) -> dict[str, Any]:
    return {"subscriptions": [{"match": m} for m in _device_matches(hub, device_id)]}


def _visible_replica_rows(hub: Hub, login: str) -> list[Any]:
    allowed = hub.visible(login)
    return hub.store.query(
        "SELECT * FROM row_replica WHERE github_login IN ({}) ORDER BY table_name ASC, row_id ASC".format(
            _placeholders(allowed)
        ),
        tuple(sorted(allowed)),
    )


def _subscription_snapshots(hub: Hub, device: dict[str, Any]) -> list[dict[str, Any]]:
    matches = _device_matches(hub, device["id"])
    if not matches:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _visible_replica_rows(hub, device["github_login"]):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not _trusted_hub_activity(row, payload):
            continue
        if not any(_match_applies(match, payload) for match in matches):
            continue
        key = (row["table_name"], row["row_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(_replica_out(row))
    return out


def _query_matching_rows(hub: Hub, login: str, match: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _visible_replica_rows(hub, login):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not _trusted_hub_activity(row, payload):
            continue
        if not _match_applies(match, payload):
            continue
        out.append(_replica_out(row))
        if len(out) >= QUERY_ROW_CAP:
            break
    return out


def _trusted_hub_activity(row: Any, payload: dict[str, Any]) -> bool:
    if (
        row["table_name"] != "activity"
        or payload.get("type") not in HUB_AUTHORED_ACTIVITY_TYPES
    ):
        return True
    login = row["github_login"]
    inner = payload.get("payload")
    return (
        isinstance(login, str)
        and row["origin_device_id"] == f"web:{login}"
        and isinstance(inner, dict)
        and inner.get("actor") == login
    )


def _enqueue_subscription_fanout(hub: Hub, snap: dict[str, Any]) -> None:
    payload = snap.get("payload")
    if not isinstance(payload, dict):
        return
    owner = snap.get("github_login")
    if not isinstance(owner, str):
        return
    devices = hub.store.query(
        "SELECT DISTINCT d.id AS device_id, d.github_login AS github_login "
        "FROM device d "
        "JOIN device_subscription s ON s.device_id = d.id "
        "WHERE d.revoked_at IS NULL"
    )
    for device in devices:
        device_id = device["device_id"]
        login = device["github_login"]
        if owner not in hub.visible(login):
            continue
        matches = _device_matches(hub, device_id)
        if not any(_match_applies(match, payload) for match in matches):
            continue
        queue = hub.device_queues.get(device_id)
        if queue is None:
            continue
        try:
            queue.put_nowait({"type": "subscription", "rows": [snap]})
        except asyncio.QueueFull:
            continue
