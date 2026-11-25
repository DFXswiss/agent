"""A small web site for the platform browser scenario (E2E-66, KOR-44; docs/platform/spec/13-work-vm.md section 13).

`POST /site` (`{"password": "<text>", "report": "<text>"}`) starts (or reconfigures) the site on the run's port
`site` of the loopback and answers `{"url": "http://host.lima.internal:<port>", "local_url": "http://127.0.0.1:<port>"}`:
the browser VM of a session reaches the device's loopback through Lima's user network in self mode (TEST_MODE, no host
filter), so the site is hermetic. `GET /site` answers the counters `{"logins": n, "failed_logins": n, "downloads": n}`.

Pages: `/` and `/login` (a form with the fields `#user` and `#password` and the button "Sign in"), `POST /login`
(user `ada` with the configured password sets a session cookie and redirects to `/account`; anything else shows the
form again with "Wrong user or password"), `/account` ("Signed in as ada" and the link "Download report" to
`/report.csv`) and `/report.csv` (the configured report as an attachment). Without the cookie `/account` and
`/report.csv` redirect to `/login`.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

ATTRIBUTE = "_site"
USER = "ada"
VM_HOST = "host.lima.internal"
FORM_LIMIT = 4096
LOGIN_PAGE = (
    "<!doctype html><html><head><title>Example sign-in</title></head><body><h1>Sign in</h1>{error}"
    '<form method="post" action="/login"><label for="user">User</label><input id="user" name="user">'
    '<label for="password">Password</label><input id="password" name="password" type="password">'
    '<button type="submit">Sign in</button></form></body></html>'
)
ACCOUNT_PAGE = (
    "<!doctype html><html><head><title>Account</title></head><body><h1>Signed in as ada</h1>"
    '<p><a id="report" href="/report.csv" download>Download report</a></p></body></html>'
)


@dataclass
class Site:
    password: str
    report: bytes
    tokens: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=lambda: {"logins": 0, "failed_logins": 0, "downloads": 0})
    server: ThreadingHTTPServer | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, name: str) -> None:
        with self.lock:
            self.counters[name] += 1


def handler_for(site: Site) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # quiet: the run keeps its own logs
            return

        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str) -> None:
            self._send(200, text.encode(), {"Content-Type": "text/html; charset=utf-8"})

        def _redirect(self, target: str, cookie: str | None = None) -> None:
            headers = {"Location": target}
            if cookie is not None:
                headers["Set-Cookie"] = f"site={cookie}; Path=/; HttpOnly"
            self._send(303, b"", headers)

        def _signed_in(self) -> bool:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "site" and value in site.tokens:
                    return True
            return False

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/login"):
                self._html(LOGIN_PAGE.format(error=""))
            elif path == "/account":
                self._html(ACCOUNT_PAGE) if self._signed_in() else self._redirect("/login")
            elif path == "/report.csv":
                if not self._signed_in():
                    self._redirect("/login")
                    return
                site.count("downloads")
                self._send(
                    200,
                    site.report,
                    {"Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="report.csv"'},
                )
            else:
                self._send(404, b"not found", {"Content-Type": "text/plain"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if self.path != "/login" or not 0 <= length <= FORM_LIMIT:
                self._send(404, b"not found", {"Content-Type": "text/plain"})
                return
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if form.get("user") == [USER] and form.get("password") == [site.password]:
                token = secrets.token_hex(16)
                site.tokens.add(token)
                site.count("logins")
                self._redirect("/account", token)
                return
            site.count("failed_logins")
            self._html(LOGIN_PAGE.format(error='<p role="alert">Wrong user or password</p>'))

    return Handler


def _site_of(ctx: Context) -> Site | None:
    found = getattr(ctx, ATTRIBUTE, None)
    return found if isinstance(found, Site) else None


def post_site(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if (
        not isinstance(body, dict)
        or set(body) != {"password", "report"}
        or not all(isinstance(value, str) and 1 <= len(value) <= 65536 for value in body.values())
    ):
        raise RouteError(400, "invalid", 'body must be {"password": "<text>", "report": "<text>"}')
    port = ctx.config.port("site")
    site = _site_of(ctx)
    if site is None:
        site = Site(password=body["password"], report=body["report"].encode())
        site.server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(site))
        site.server.daemon_threads = True
        threading.Thread(target=site.server.serve_forever, name="e2e-site", daemon=True).start()
        setattr(ctx, ATTRIBUTE, site)
    else:
        site.password, site.report = body["password"], body["report"].encode()
    return 200, {"url": f"http://{VM_HOST}:{port}", "local_url": f"http://127.0.0.1:{port}"}


def get_site(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    site = _site_of(ctx)
    if site is None:
        raise RouteError(409, "not_started", "POST /site first")
    with site.lock:
        return 200, dict(site.counters)


ROUTES = [Route("POST", "/site", post_site), Route("GET", "/site", get_site)]
