"""The auth-page double: the browser side of a Claude subscription login (ADR-011), served over HTTPS.

Flow (each page has ``<body data-page="…">`` so a browser driver can tell where it is):

1. ``GET /oauth/authorize?code=true&client_id=…&response_type=code&redirect_uri=<base>/oauth/code/callback&scope=…&
   code_challenge=…&code_challenge_method=S256&state=…`` (the URL the login-CLI double prints). Without a session:
   the e-mail form (``email``: input ``email``, button "Continue with email"). With a session: the authorize page
   (``authorize``: buttons "Authorize" and "Decline").
2. ``POST /login`` (``email``, ``next``): sends the magic-link mail through SMTP (the mail double) from `mail_from`,
   with ``X-Spam-Flag: YES`` when the scenario says ``junk``, and shows ``check-email``. The link is
   ``<base>/magic-link#<token>``.
3. ``GET /magic-link``: a page whose script posts the fragment to ``POST /magic-link/verify`` (``magic-link``);
   a valid, unused, unexpired token sets the session cookie and redirects (303) to the stored authorize URL.
4. ``POST /oauth/authorize`` (``decision`` = ``authorize`` or ``decline`` and the OAuth parameters): redirects to
   the callback with ``code`` and ``state`` (or ``error=access_denied``).
5. ``GET /oauth/code/callback``: the code page (``code``: ``<code id="authorization-code">CODE#STATE</code>``).
6. ``POST /v1/oauth/token`` (JSON, from the login-CLI double): exchanges code, verifier, state and redirect URI for
   the long-lived token (`agent_doubles.login_cli.codes.access_token`).

Error pages (``data-page="error"``, the reason in ``#error-code``) answer unexpected states: invalid parameters, an
unknown or used link, a missing session, a decline. Requests that the clients under test never send correctly
(invalid OAuth parameters, malformed token requests, unknown routes, a mail that cannot be sent) are failures of the
double's state; a wrong code or an expired link is a legitimate test case.

Scenario (``POST /_double/scenario``; resets every session, code and counter): ``junk`` (bool), ``fault`` (one of
`FAULTS`: ``email_page`` shows a human-verification page instead of sending the mail, ``no_mail`` shows
``check-email`` without sending, ``authorize_page`` and ``code_page`` show an error page, ``token`` answers the token
request with 500), ``expect`` (milestones of `MILESTONES` that must happen for ``done``) and ``seed``. Other control
routes: ``GET /_double/state`` (adds ``milestones``, ``scenario`` and ``urls``), ``GET /_double/journal``,
``POST /_double/reset`` (same as re-posting the scenario) and ``GET /_double/info``.

The pages run on `page_url` (HTTPS with a `DoubleCA`, plain HTTP without one), the control routes on `url` (always
plain HTTP, like every double). ``python -m agent_doubles.login_cli.authpage --ca-dir DIR --smtp HOST:PORT`` runs it
(`main`; common options as for every double, ``--port`` being the control port; ``--info-file`` receives `info`).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from email.utils import parseaddr
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import parse_qsl, urlencode, urlsplit

from agent_doubles.common.cli import EXIT_USAGE, add_server_arguments, poll_wait, serve_until_signal
from agent_doubles.common.journal import Entry, Journal, decode_body, redact_headers
from agent_doubles.common.server import POLL_INTERVAL, BodyError, DoubleHandler, DoubleServer, validate_host
from agent_doubles.common.state import state_document
from agent_doubles.login_cli import codes
from agent_doubles.mail.control import Action, ControlError, body_object, handle_control
from agent_doubles.mail.double import ca_name_list
from agent_doubles.mail.message import compose
from agent_doubles.mail.testca import DoubleCA, DoubleCAError

DOUBLE_NAME = "authpage"
DEFAULT_MAIL_FROM = "Claude <no-reply@auth.double.test>"
SESSION_COOKIE = "double_session"
MAGIC_LIFETIME = 900
CODE_LIFETIME = 600
FAULTS = frozenset({"email_page", "no_mail", "authorize_page", "code_page", "token"})
MILESTONES = (
    "email_page",
    "mail_sent",
    "signed_in",
    "authorize_page",
    "authorized",
    "declined",
    "code_page",
    "token",
)
# Values that grant access (magic-link tokens, codes, PKCE verifiers, states, issued tokens) never reach the journal.
# ``next`` is the stored authorize URL, which holds the state.
SECRET_FIELDS = frozenset({"token", "code", "code_verifier", "state", "access_token", "next"})
# RFC 7636: 43 to 128 unreserved characters.
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
IDLE_TIMEOUT = 300.0
OAUTH_FIELDS = (
    "client_id",
    "response_type",
    "redirect_uri",
    "scope",
    "code_challenge",
    "code_challenge_method",
    "state",
)


def _redacted(value: str) -> str:
    return f"<redacted {len(value)} chars>"


def redact_query(query: str) -> str:
    """`query` with the values of `SECRET_FIELDS` replaced by their length."""
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(name in SECRET_FIELDS for name, _ in pairs):
        return query
    return "&".join(f"{name}={_redacted(value) if name in SECRET_FIELDS else value}" for name, value in pairs)


def redact_body(body: Any) -> Any:
    """A journaled body (decoded JSON object or form text) without the values of `SECRET_FIELDS`."""
    if isinstance(body, dict):
        return {k: _redacted(str(v)) if k in SECRET_FIELDS else v for k, v in body.items()}
    if isinstance(body, str):
        return redact_query(body)
    return body


def page(kind: str, title: str, body: str) -> bytes:
    """A complete HTML page of `kind` (the ``data-page`` attribute)."""
    return (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)} (auth double)</title></head>\n'
        f'<body data-page="{kind}">\n<h1>{html.escape(title)}</h1>\n{body}\n</body></html>\n'
    ).encode()


def error_page(code: str, text: str) -> bytes:
    body = f'<p id="error-code">{html.escape(code)}</p>\n<p>{html.escape(text)}</p>'
    return page("error", "Something went wrong", body)


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{html.escape(value)}">'


@dataclass
class Grant:
    """An issued authorization code."""

    email: str
    state: str
    challenge: str
    redirect_uri: str
    issued: float
    used: bool = False


@dataclass
class Magic:
    """An issued magic-link token."""

    email: str
    next: str
    issued: float
    used: bool = False


@dataclass
class Scenario:
    junk: bool = False
    fault: str | None = None
    expect: tuple[str, ...] = ()
    seed: str = codes.DEFAULT_SEED

    def describe(self) -> dict[str, Any]:
        return {"junk": self.junk, "fault": self.fault, "expect": list(self.expect), "seed": self.seed}


def parse_scenario(body: Any) -> Scenario:
    data = body_object(body)
    unknown = set(data) - {"junk", "fault", "expect", "seed"}
    if unknown:
        raise ControlError(f"unknown scenario keys {sorted(unknown)}")
    junk = data.get("junk", False)
    fault = data.get("fault")
    expect = data.get("expect", [])
    seed = data.get("seed", codes.DEFAULT_SEED)
    if not isinstance(junk, bool):
        raise ControlError("junk must be a boolean")
    if fault is not None and fault not in FAULTS:
        raise ControlError(f"fault must be one of {sorted(FAULTS)}")
    if not isinstance(expect, list) or not all(isinstance(m, str) and m in MILESTONES for m in expect):
        raise ControlError(f"expect must list milestones of {list(MILESTONES)}")
    if not isinstance(seed, str) or not seed:
        raise ControlError("seed must be a non-empty string")
    return Scenario(junk, fault, tuple(expect), seed)


@dataclass
class _Run:
    """Everything a scenario switch resets."""

    scenario: Scenario
    magic: dict[str, Magic] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    milestones: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    requests: int = 0

    def next(self, kind: str) -> int:
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return self.counters[kind]


class _PageServer(ThreadingHTTPServer):
    """An HTTP server whose accepted sockets are wrapped in TLS; the handshake runs with the first read in the
    handler thread, so a stalled client never blocks the accept loop."""

    daemon_threads = True
    allow_reuse_address = True
    tls: ssl.SSLContext | None = None

    def get_request(self) -> tuple[Any, Any]:
        sock, address = self.socket.accept()
        sock.settimeout(IDLE_TIMEOUT)
        if self.tls is not None:
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address


class _PageServer6(_PageServer):
    address_family = socket.AF_INET6


class AuthPageDouble:
    """A running auth-page double (see the module documentation)."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        control_port: int = 0,
        ca: DoubleCA | None = None,
        smtp: tuple[str, int] | None = None,
        mail_from: str = DEFAULT_MAIL_FROM,
        scenario: Scenario | None = None,
        journal_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        public_host: str | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"invalid port {port}")
        self.ca = ca
        self.smtp = smtp
        self.mail_from = mail_from
        self.clock = clock
        self.lock = threading.RLock()
        self.journal = Journal(DOUBLE_NAME, journal_path)
        self.run = _Run(scenario or Scenario())
        server_class = _PageServer6 if ":" in validate_host(host) else _PageServer
        self._pages = server_class((host, port), _page_handler_for(self))
        self._pages.tls = None if ca is None else ca.server_context()
        self._page_thread: threading.Thread | None = None
        try:
            self._control = DoubleServer(_control_handler_for(self), host, control_port)
        except BaseException:
            self._pages.server_close()
            raise
        shown = public_host or host
        shown = f"[{shown}]" if ":" in shown else shown
        scheme = "https" if ca is not None else "http"
        self.page_url = f"{scheme}://{shown}:{self._pages.server_address[1]}"

    # --- lifecycle --------------------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The base URL of the control routes (plain HTTP)."""
        return self._control.url

    @property
    def redirect_uri(self) -> str:
        return self.page_url + codes.CALLBACK_PATH

    def start(self) -> Self:
        self._page_thread = threading.Thread(target=self._pages.serve_forever, args=(POLL_INTERVAL,), daemon=True)
        self._page_thread.start()
        self._control.start()
        return self

    def stop(self) -> None:
        if self._page_thread is not None:
            self._pages.shutdown()
            self._page_thread.join()
            self._page_thread = None
        self._pages.server_close()
        self._control.stop()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- scenario, state --------------------------------------------------------------------------------------

    def set_scenario(self, scenario: Scenario) -> None:
        with self.lock:
            self.run = _Run(scenario)
            self.journal.clear()

    def reached(self, milestone: str) -> None:
        with self.lock:
            self.run.milestones.append(milestone)

    def fail(self, text: str) -> None:
        with self.lock:
            self.run.failures.append(text)

    def info(self) -> dict[str, Any]:
        return {
            "control": self.url,
            "pages": self.page_url,
            "authorize": self.page_url + codes.AUTHORIZE_PATH,
            "token": self.page_url + codes.TOKEN_PATH,
            "redirect_uri": self.redirect_uri,
            "client_id": codes.CLIENT_ID,
            "ca_file": None if self.ca is None else str(self.ca.ca_file),
        }

    def state(self) -> dict[str, Any]:
        with self.lock:
            run = self.run
            done = all(m in run.milestones for m in run.scenario.expect)
            return state_document(
                DOUBLE_NAME,
                done=done,
                failures=run.failures,
                requests=run.requests,
                scenario=run.scenario.describe(),
                milestones=list(run.milestones),
                urls=self.info(),
            )

    # --- flow steps (called by the page handler) ---------------------------------------------------------------

    def oauth_problem(self, params: dict[str, str]) -> str | None:
        """Why `params` are no valid authorize request of the login-CLI double, or None."""
        expected = {
            "code": "true",
            "client_id": codes.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256",
        }
        for name, value in expected.items():
            if params.get(name) != value:
                return f"invalid {name}"
        if not params.get("scope") or not params.get("state"):
            return "missing scope or state"
        if len(params.get("code_challenge", "")) != 43:
            return "invalid code_challenge"
        return None

    def send_magic_link(self, email: str, next_url: str) -> str:
        """Issue a magic link for `email` and mail it; the token. Raises OSError/smtplib errors when sending fails."""
        with self.lock:
            run = self.run
            token = codes.magic_token(run.scenario.seed, run.next("magic"))
            run.magic[token] = Magic(email, next_url, self.clock())
            fault, junk = run.scenario.fault, run.scenario.junk
        if fault == "no_mail":
            return token
        if self.smtp is None:
            raise OSError("no SMTP server configured")
        link = f"{self.page_url}/magic-link#{token}"
        raw = compose(
            self.mail_from,
            [email],
            "Secure link to log in to Claude",
            when=self.clock(),
            text=f"Click the link below to log in:\n\n{link}\n\nThe link expires in 15 minutes.\n",
            html=f'<p>Click the link below to log in:</p><p><a href="{html.escape(link)}">Log in</a></p>',
            spam=junk,
        )
        # An explicit local name: smtplib would otherwise look up the host's FQDN, which can block for seconds.
        with smtplib.SMTP(self.smtp[0], self.smtp[1], local_hostname="localhost", timeout=10) as client:
            client.sendmail(parseaddr(self.mail_from)[1], [email], raw)
        self.reached("mail_sent")
        return token

    def use_magic(self, token: str) -> tuple[str, str] | None:
        """Session ID and next URL for a valid magic token (used once), or None."""
        with self.lock:
            run = self.run
            magic = run.magic.get(token)
            if magic is None or magic.used or self.clock() - magic.issued > MAGIC_LIFETIME:
                return None
            magic.used = True
            sid = codes.session_id(run.scenario.seed, run.next("session"))
            run.sessions[sid] = magic.email
        self.reached("signed_in")
        return sid, magic.next

    def session_email(self, cookie_header: str | None) -> str | None:
        if not cookie_header:
            return None
        # SimpleCookie skips what it cannot parse instead of raising.
        morsel = SimpleCookie(cookie_header).get(SESSION_COOKIE)
        if morsel is None:
            return None
        with self.lock:
            return self.run.sessions.get(morsel.value)

    def issue_code(self, email: str, params: dict[str, str]) -> str:
        with self.lock:
            run = self.run
            code = codes.auth_code(run.scenario.seed, run.next("code"))
            run.grants[code] = Grant(
                email, params["state"], params["code_challenge"], params["redirect_uri"], self.clock()
            )
        self.reached("authorized")
        return code

    def exchange(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The token endpoint's answer for a well-formed request."""
        invalid = {"error": "invalid_grant", "error_description": "Invalid 'code' in request."}
        with self.lock:
            run = self.run
            grant = run.grants.get(body["code"])
            if (
                grant is None
                or grant.used
                or self.clock() - grant.issued > CODE_LIFETIME
                or grant.state != body["state"]
                or grant.redirect_uri != body["redirect_uri"]
                or grant.challenge != codes.challenge_of(body["code_verifier"])
            ):
                return 400, invalid
            grant.used = True
            token = codes.access_token(run.scenario.seed, run.next("token"))
            seed = run.scenario.seed
        self.reached("token")
        return 200, {
            "token_type": "Bearer",
            "access_token": token,
            "expires_in": codes.TOKEN_LIFETIME,
            "scope": codes.SCOPE,
            "account": {"uuid": codes.account_uuid(seed, grant.email), "email_address": grant.email},
            "organization": {"uuid": codes.account_uuid(seed, "organization"), "name": "Double Organization"},
        }


TOKEN_FIELDS = ("grant_type", "code", "state", "client_id", "redirect_uri", "code_verifier")


def token_request_problem(body: Any) -> str | None:
    """Why `body` is no well-formed token request, or None."""
    if not isinstance(body, dict):
        return "body is not a JSON object"
    for name in TOKEN_FIELDS:
        if not isinstance(body.get(name), str) or not body[name]:
            return f"missing {name}"
    if body["grant_type"] != "authorization_code":
        return "unsupported grant_type"
    if body["client_id"] != codes.CLIENT_ID:
        return "unknown client_id"
    if not VERIFIER.fullmatch(body["code_verifier"]):
        return "invalid code_verifier"
    return None


def _page_handler_for(double: AuthPageDouble) -> type[BaseHTTPRequestHandler]:
    return type("AuthPageHandler", (_PageHandler,), {"double": double})


def _control_handler_for(double: AuthPageDouble) -> type[BaseHTTPRequestHandler]:
    return type("AuthControlHandler", (_ControlHandler,), {"double": double})


class _PageHandler(DoubleHandler):
    server_version = "auth-double/1"
    double: ClassVar[AuthPageDouble]

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def handle(self) -> None:
        try:
            super().handle()
        except (OSError, ssl.SSLError):
            # A failed handshake or a dropped connection ends this client only.
            self.close_connection = True

    def _route(self) -> None:
        parts = urlsplit(self.path)
        try:
            raw = self.read_body()
            problem: BodyError | None = None
        except BodyError as exc:
            raw, problem = b"", exc
        double = self.double
        entry = double.journal.open(
            self.command,
            parts.path,
            redact_query(parts.query),
            redact_headers(self.headers.items()),
            redact_body(decode_body(raw)),
        )
        with double.lock:
            double.run.requests += 1
        try:
            if problem is not None:
                self._error(entry, problem.status, "bad_request", problem.message, failure=True)
                return
            routes: dict[tuple[str, str], Callable[[Entry, dict[str, str], bytes], None]] = {
                ("GET", codes.AUTHORIZE_PATH): self._authorize_get,
                ("POST", codes.AUTHORIZE_PATH): self._authorize_post,
                ("POST", "/login"): self._login,
                ("GET", "/magic-link"): self._magic_page,
                ("POST", "/magic-link/verify"): self._magic_verify,
                ("GET", codes.CALLBACK_PATH): self._callback,
                ("POST", codes.TOKEN_PATH): self._token,
                ("GET", "/favicon.ico"): self._favicon,
            }
            handler = routes.get((self.command, parts.path))
            if handler is None:
                self._error(entry, 404, "not_found", "unknown page", failure=True)
                return
            handler(entry, dict(parse_qsl(parts.query)), raw)
        finally:
            double.journal.close(entry)

    # --- answers ----------------------------------------------------------------------------------------------

    def _html(self, entry: Entry, status: int, data: bytes, headers: Iterable[tuple[str, str]] = ()) -> None:
        entry.status = status
        self.send_body(status, data, "text/html; charset=utf-8", [("cache-control", "no-store"), *headers])

    def _error(self, entry: Entry, status: int, code: str, text: str, *, failure: bool = False) -> None:
        entry.note = f"{code}: {text}"
        if failure:
            self.double.fail(f"{self.command} {urlsplit(self.path).path}: {code}: {text}")
        self._html(entry, status, error_page(code, text))

    def _redirect(self, entry: Entry, location: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        entry.status = 303
        self.send_body(303, b"", "text/plain", [("location", location), *headers])

    def _form(self, raw: bytes) -> dict[str, str]:
        return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))

    # --- pages ------------------------------------------------------------------------------------------------

    def _authorize_get(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        problem = double.oauth_problem(query)
        if problem is not None:
            self._error(entry, 400, "invalid_request", problem, failure=True)
            return
        email = double.session_email(self.headers.get("cookie"))
        if email is None:
            double.reached("email_page")
            body = (
                '<form method="post" action="/login">\n'
                f"{hidden('next', self.path)}\n"
                '<label for="email">Email</label>\n'
                '<input type="email" id="email" name="email" autocomplete="email" required>\n'
                '<button type="submit" id="continue">Continue with email</button>\n</form>'
            )
            self._html(entry, 200, page("email", "Log in to Claude", body))
            return
        if double.run.scenario.fault == "authorize_page":
            self._error(entry, 500, "server_error", "the authorize page is unavailable")
            return
        double.reached("authorize_page")
        fields = "\n".join(hidden(name, query[name]) for name in OAUTH_FIELDS)
        body = (
            f'<p id="account">Signed in as {html.escape(email)}</p>\n'
            "<p>Claude Code would like to connect to your Claude account.</p>\n"
            f'<form method="post" action="{codes.AUTHORIZE_PATH}">\n{fields}\n'
            '<button type="submit" name="decision" value="authorize" id="authorize">Authorize</button>\n'
            '<button type="submit" name="decision" value="decline" id="decline">Decline</button>\n</form>'
        )
        self._html(entry, 200, page("authorize", "Authorize Claude Code", body))

    def _login(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        form = self._form(raw)
        email, next_url = form.get("email", ""), form.get("next", "")
        if parseaddr(email)[1] != email or "@" not in email or not next_url.startswith(codes.AUTHORIZE_PATH + "?"):
            self._error(entry, 400, "invalid_request", "invalid e-mail address or return URL", failure=True)
            return
        if double.run.scenario.fault == "email_page":
            body = '<p id="challenge">Please verify that you are a human before you continue.</p>'
            self._html(entry, 200, page("challenge", "Verify you are human", body))
            return
        try:
            double.send_magic_link(email, next_url)
        except (OSError, smtplib.SMTPException) as exc:
            self._error(entry, 502, "mail_failed", f"the e-mail could not be sent ({type(exc).__name__})", failure=True)
            return
        body = f'<p id="sent-to">We sent a secure link to {html.escape(email)}.</p>'
        self._html(entry, 200, page("check-email", "Check your email", body))

    def _magic_page(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        body = (
            '<form id="verify" method="post" action="/magic-link/verify">'
            '<input type="hidden" name="token" id="token"></form>\n'
            "<p>Logging you in…</p>\n"
            '<script>document.getElementById("token").value = location.hash.slice(1);'
            'document.getElementById("verify").submit();</script>'
        )
        self._html(entry, 200, page("magic-link", "Logging in", body))

    def _magic_verify(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        used = double.use_magic(self._form(raw).get("token", ""))
        if used is None:
            self._error(entry, 400, "invalid_magic_link", "the link is invalid, used or expired")
            return
        sid, next_url = used
        secure = "; Secure" if double.ca is not None else ""
        cookie = f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax{secure}"
        self._redirect(entry, next_url, [("set-cookie", cookie)])

    def _authorize_post(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        form = self._form(raw)
        email = double.session_email(self.headers.get("cookie"))
        if email is None:
            self._error(entry, 401, "not_signed_in", "there is no session")
            return
        params = {"code": "true", **{name: form.get(name, "") for name in OAUTH_FIELDS}}
        problem = double.oauth_problem(params)
        decision = form.get("decision")
        if problem is not None or decision not in ("authorize", "decline"):
            self._error(entry, 400, "invalid_request", problem or "invalid decision", failure=True)
            return
        if decision == "decline":
            double.reached("declined")
            target = urlencode({"error": "access_denied", "state": params["state"]})
        else:
            target = urlencode({"code": double.issue_code(email, params), "state": params["state"]})
        self._redirect(entry, f"{params['redirect_uri']}?{target}")

    def _callback(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        if "error" in query:
            reason = "access_denied" if query["error"] == "access_denied" else "invalid_request"
            self._error(entry, 400, reason, "the authorization was declined or failed")
            return
        if not query.get("code") or not query.get("state"):
            self._error(entry, 400, "invalid_request", "missing code or state", failure=True)
            return
        if double.run.scenario.fault == "code_page":
            self._error(entry, 500, "server_error", "the code page is unavailable")
            return
        double.reached("code_page")
        shown = html.escape(f"{query['code']}#{query['state']}")
        body = (
            "<p>Paste this code into Claude Code:</p>\n"
            f'<code id="authorization-code">{shown}</code>\n<button type="button" id="copy">Copy Code</button>'
        )
        self._html(entry, 200, page("code", "Authentication Code", body))

    def _token(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        double = self.double
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        problem = token_request_problem(body)
        if problem is not None:
            # recorded before the answer, so a client that has the answer also sees the failure
            double.fail(f"POST {codes.TOKEN_PATH}: {problem}")
            self._json(entry, 400, {"error": "invalid_request", "error_description": problem})
            return
        if double.run.scenario.fault == "token":
            self._json(entry, 500, {"error": "server_error", "error_description": "injected fault"})
            return
        status, payload = double.exchange(body)
        self._json(entry, status, payload)

    def _json(self, entry: Entry, status: int, payload: Any) -> None:
        entry.status = status
        entry.note = payload.get("error")
        self.send_json(status, payload, [("cache-control", "no-store")])

    def _favicon(self, entry: Entry, query: dict[str, str], raw: bytes) -> None:
        # Browsers ask for it on their own; not a failure.
        entry.status = 404
        self.send_body(404, b"", "image/x-icon")


class _ControlHandler(DoubleHandler):
    server_version = "auth-double-control/1"
    double: ClassVar[AuthPageDouble]

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        parts = urlsplit(self.path)
        try:
            raw = self.read_body()
            problem: BodyError | None = None
        except BodyError as exc:
            raw, problem = b"", exc
        double = self.double
        routes: dict[tuple[str, str], Action] = {
            ("GET", "/_double/state"): lambda b, q: (200, double.state()),
            ("GET", "/_double/journal"): lambda b, q: (200, double.journal.records()),
            ("GET", "/_double/info"): lambda b, q: (200, double.info()),
            ("POST", "/_double/scenario"): self._scenario,
            ("POST", "/_double/reset"): self._reset,
        }
        handle_control(self, routes, parts.path, query=dict(parse_qsl(parts.query)), raw=raw, problem=problem)

    def _scenario(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        self.double.set_scenario(parse_scenario(body))
        return 200, self.double.state()

    def _reset(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        self.double.set_scenario(self.double.run.scenario)
        return 200, self.double.state()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_doubles.login_cli.authpage", description="Auth-page double (Claude login pages)"
    )
    add_server_arguments(parser)
    parser.add_argument("--page-port", type=int, default=0, help="port of the pages (0: a free one)")
    parser.add_argument("--ca-dir", type=Path, help="runtime CA directory (reused when present); pages over HTTPS")
    parser.add_argument("--ca-name", action="append", default=[], help="further certificate name; repeatable")
    parser.add_argument("--smtp", help="HOST:PORT of the mail double's SMTP listener")
    parser.add_argument("--mail-from", default=DEFAULT_MAIL_FROM, help="From of the magic-link mail")
    parser.add_argument("--junk", action="store_true", help="mark magic-link mails as spam (Junk folder)")
    parser.add_argument("--fault", choices=sorted(FAULTS), help="unexpected page state to inject")
    parser.add_argument("--seed", default=codes.DEFAULT_SEED, help="seed of tokens and codes")
    parser.add_argument("--info-file", type=Path, help="write the URLs as JSON to this file")
    return parser


def main(argv: Sequence[str] | None = None, *, wait: Callable[[threading.Event], None] = poll_wait) -> int:
    """Run the auth-page double; ``--port``/``--port-file`` refer to the control server."""
    args = build_parser().parse_args(argv)
    try:
        smtp = None
        if args.smtp is not None:
            smtp_host, separator, smtp_port = args.smtp.rpartition(":")
            if not separator or not smtp_port.isdigit():
                raise ValueError("--smtp needs HOST:PORT")
            smtp = (smtp_host.strip("[]"), int(smtp_port))
        ca = None
        if args.ca_dir is not None:
            ca = DoubleCA.open_or_create(args.ca_dir, ca_name_list(args.host, args.ca_name))
        double = AuthPageDouble(
            host=args.host,
            port=args.page_port,
            control_port=args.port,
            ca=ca,
            smtp=smtp,
            mail_from=args.mail_from,
            scenario=Scenario(junk=args.junk, fault=args.fault, seed=args.seed),
            journal_path=args.journal,
        )
    except (OSError, ValueError, DoubleCAError) as exc:
        print(f"auth-page double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.info_file is not None:
        temporary = args.info_file.with_name(f".{args.info_file.name}.tmp")
        temporary.write_text(json.dumps(double.info(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, args.info_file)
    return serve_until_signal(double, args.port_file, wait)


if __name__ == "__main__":
    sys.exit(main())
