"""The live upstream bridge of the LIVE tests (07-testing.md sections 2.4 and 6; never part of a hermetic run).

The guest model gateway accepts plain `http://` only to a loopback upstream, and the run's VMs reach the runner
device only through the reverse tunnels of `orchestrator.tunnels` (ADR-034, ADR-036). For a live test the runners'
upstreams therefore point at this bridge on the runner device's loopback, which forwards each request unchanged
over HTTPS to the real provider:

    http://127.0.0.1:<port>/live/<name>/<path>  →  <upstream of name>/<path>

Upstreams are fixed by the run (`--live openrouter=https://openrouter.ai/api --live
anthropic=https://api.anthropic.com --live github=https://api.github.com --live codeload=https://codeload.github.com`)
and only these hosts are accepted (`LIVE_HOSTS`). GitHub (LIVE-02, LVT-01): the runner's GitHub API URL is
`<bridge>/live/github_<secret>` and its web URL the bridge's origin; a tarball redirect to
`https://codeload.github.com/...` is rewritten to `<bridge>/codeload/...` (the runner follows one redirect only to the
codeload path of its web URL, `agent_runner.github.client`), which the bridge forwards to the `codeload` upstream -
only with the redirect's short-lived `token` query (private repositories), never under `/live/codeload/`. The bridge's
port is also tunnelled into the run's VMs, so the GitHub upstream's name is a per-run secret
(`routes/quota.github_name`); without it and without such a token a guest reaches no GitHub. Hop-by-hop headers,
`host`, `content-length` and `accept-encoding` are dropped; everything else - the credential headers the gateway (or
the runner's quota polls) set - passes unchanged, and the answer streams back unchanged (server-sent events included)
with `Connection: close`. The bridge never logs or stores a header or a body: its journal holds method, path (without
query), upstream, status, byte counts and the address the upstream connection reached (`peer`; the TLS host name check
proved it is the live host, so the run's egress check allows exactly these addresses for the bridge process).
"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

PREFIX = "/live/"
LIVE_HOSTS = frozenset({"openrouter.ai", "api.anthropic.com", "api.github.com", "codeload.github.com"})
# The bridge path of the codeload upstream (`agent_runner.github.client.codeload_allowed`: `<web origin>/codeload/...`).
CODELOAD = "codeload"
CODELOAD_PREFIX = f"/{CODELOAD}/"
DROPPED = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "host",
        "content-length",
        "accept-encoding",
    }
)
TIMEOUT_S = 300.0
CHUNK = 64 * 1024
MAX_BODY = 32 * 1024 * 1024


class BridgeError(ValueError):
    """An upstream argument is malformed or names a host that is not a live provider."""


@dataclass(frozen=True)
class Upstream:
    name: str
    host: str
    port: int
    path: str


def parse_upstream(text: str) -> Upstream:
    """`<name>=https://<live host>[:port][/path]`."""
    name, separator, url = text.partition("=")
    parts = urlsplit(url)
    if not separator or not name.isidentifier() or parts.scheme != "https" or parts.hostname not in LIVE_HOSTS:
        raise BridgeError(f"--live takes <name>=https://<one of {', '.join(sorted(LIVE_HOSTS))}>[/path]")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise BridgeError("an upstream URL has no query, fragment or user part")
    return Upstream(name, parts.hostname, parts.port or 443, parts.path.rstrip("/"))


class Connection(Protocol):
    def request(self, method: str, url: str, body: bytes | None = None, headers: Mapping[str, str] = ...) -> None: ...

    def getresponse(self) -> http.client.HTTPResponse: ...

    def close(self) -> None: ...


def https_connection(upstream: Upstream) -> Connection:
    """Directly (no proxy variables are consulted), with the system's trust store and host name check."""
    return http.client.HTTPSConnection(
        upstream.host, upstream.port, timeout=TIMEOUT_S, context=ssl.create_default_context()
    )


@dataclass
class Bridge:
    upstreams: Mapping[str, Upstream]
    journal: Path | None = None
    connect: Callable[[Upstream], Connection] = https_connection
    forwarded: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def target(self, path: str) -> tuple[Upstream, str] | None:
        """The upstream and the upstream path of a bridge path, None for any other path."""
        codeload = self.upstreams.get(CODELOAD)
        if codeload is not None and path.startswith(CODELOAD_PREFIX):
            # only a redirect's own URL (with GitHub's short-lived `token`): nothing a guest could ask for itself
            query = urlsplit(path).query
            if not any(part.startswith("token=") and len(part) > len("token=") for part in query.split("&")):
                return None
            return codeload, f"{codeload.path}/{path[len(CODELOAD_PREFIX) :]}"
        if not path.startswith(PREFIX):
            return None
        name, _sep, rest = path[len(PREFIX) :].partition("/")
        upstream = self.upstreams.get(name)
        if upstream is None or name == CODELOAD:  # codeload only through its redirect path above
            return None
        return upstream, f"{upstream.path}/{rest}"

    def forward(self, handler: BaseHTTPRequestHandler, method: str) -> bool:
        """Answers the handler's request through its upstream; False when the path is not a bridge path."""
        found = self.target(handler.path)
        if found is None:
            return False
        upstream, path = found
        length = int(handler.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.answer_error(handler, 413, "body_too_large", str(length))
            return True
        body = handler.rfile.read(length) if length else None
        headers = {name: value for name, value in handler.headers.items() if name.lower() not in DROPPED}
        headers["Accept-Encoding"] = "identity"
        started, status, sent, peer = time.monotonic(), 502, 0, None
        connection = self.connect(upstream)
        try:
            try:
                connection.request(method, path, body, headers)
                peer = peer_address(connection)  # before the answer: a closing answer drops the socket
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                self.answer_error(handler, 502, "upstream_unreachable", type(exc).__name__)
                return True
            status = response.status
            handler.send_response(status)
            for name, value in response.getheaders():
                if name.lower() not in DROPPED:
                    handler.send_header(name, self.location(value, handler) if name.lower() == "location" else value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            while chunk := response.read1(CHUNK):
                handler.wfile.write(chunk)
                handler.wfile.flush()
                sent += len(chunk)
            handler.close_connection = True
        finally:
            connection.close()
            entry = {"method": method, "path": path.split("?", 1)[0], "upstream": upstream.name, "status": status}
            entry |= {"request_bytes": len(body or b""), "response_bytes": sent, "peer": peer}
            self.record(entry | {"seconds": round(time.monotonic() - started, 3)})
        return True

    def location(self, value: str, handler: BaseHTTPRequestHandler) -> str:
        """A redirect to the codeload upstream becomes the bridge's codeload path on the origin the client used."""
        codeload, host = self.upstreams.get(CODELOAD), handler.headers.get("Host")
        parts = urlsplit(value)
        if codeload is None or not host or parts.scheme != "https" or parts.hostname != codeload.host:
            return value
        if parts.username or (parts.port or 443) != codeload.port or not parts.path.startswith(f"{codeload.path}/"):
            return value
        query = f"?{parts.query}" if parts.query else ""
        return f"http://{host}{CODELOAD_PREFIX}{parts.path[len(codeload.path) + 1 :]}{query}"

    @staticmethod
    def answer_error(handler: BaseHTTPRequestHandler, status: int, code: str, detail: str) -> None:
        data = json.dumps({"error": code, "detail": detail}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.forwarded += 1
            if self.journal is None:
                return
            with self.journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")


def peer_address(connection: Connection) -> str | None:
    """The address of the upstream connection's socket (None once it is gone)."""
    sock = getattr(connection, "sock", None)
    try:
        return str(sock.getpeername()[0]) if sock is not None else None
    except OSError:
        return None


def peers(journal_text: str) -> set[str]:
    """The upstream addresses a bridge journal records."""
    found: set[str] = set()
    for line in journal_text.splitlines():
        try:
            peer = json.loads(line).get("peer")
        except (ValueError, AttributeError):
            continue
        if isinstance(peer, str):
            found.add(peer)
    return found


def state(bridge: Bridge | None) -> dict[str, Any]:
    return {} if bridge is None else {"bridged": bridge.forwarded, "upstreams": sorted(bridge.upstreams)}
