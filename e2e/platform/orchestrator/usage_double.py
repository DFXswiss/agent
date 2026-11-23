"""Subscription usage double: the OAuth usage endpoint the runner polls for Claude subscription quotas (05-runner.md
section 10.6; 07-testing.md section 2.1 E2E-16 "subscription quota double").

    python -m orchestrator.usage_double --port N [--journal F.jsonl]

Stdlib only; started by `routes/quota.py` on the runner device with the agent checkout's Python. Prints its URL as the
first line (like the doubles of the agent checkout) and serves:

- `GET /api/oauth/usage`: the scenario's document, but only for `Authorization: Bearer <token>` (the scenario's token;
  with `token_prefix` instead: any bearer token of that shape) with `oauth-2025-04-20` among the values of
  `anthropic-beta`; anything else answers `401` like the real endpoint and is recorded as a failure. A scenario may
  answer every poll with `status` (e.g. `403`: the endpoint refuses the token, quotas "not available").
- `GET /_double/state`: `{double, scenario, requests, failures, done, ok}` - `done` once `min_requests` polls with the
  right credential arrived, `ok` = done and no failure. Before a scenario is loaded the scenario is `idle`: every
  request is a failure (the run's final check: no request while idle).
- `POST /_double/scenario`: `{"name", "document", "token" | "token_prefix", "status"?, "min_requests"?}` (resets the
  counters).
- Every other route: `404` and a failure.

The journal (one JSON line per request) keeps the path, the status and the header names with the credential headers
reduced to their length; tokens are never written.

Live runs only (`--live <name>=<https upstream>`, repeatable): requests to `/live/<name>/…` go through the live
upstream bridge (`orchestrator.live_bridge`; also `PATCH`/`DELETE` and the bridge's `/codeload/…` path for GitHub,
LVT-01) to the real provider instead (not counted as polls; journal `<journal>-bridge.jsonl`); the state then also
names the upstreams and the number of bridged requests.
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from orchestrator.live_bridge import Bridge, BridgeError, parse_upstream
from orchestrator.live_bridge import state as bridge_state

USAGE_PATH = "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
IDLE = "idle"
SECRET_HEADERS = frozenset({"authorization", "x-api-key", "cookie"})
MAX_BODY = 256 * 1024
SCENARIO_KEYS = frozenset({"name", "document", "token", "token_prefix", "status", "min_requests"})
UNAUTHORIZED: dict[str, Any] = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "invalid credentials"},
}


class ScenarioError(ValueError):
    """A scenario body is malformed."""


@dataclass
class Scenario:
    name: str = IDLE
    document: Any = None
    token: str | None = None
    token_prefix: str | None = None
    status: int = 200
    min_requests: int = 1


def parse_scenario(body: Any) -> Scenario:
    if not isinstance(body, dict) or set(body) - SCENARIO_KEYS:
        raise ScenarioError("body must be {name, document, token | token_prefix, status?, min_requests?}")
    name, token, prefix = body.get("name"), body.get("token"), body.get("token_prefix")
    status, minimum = body.get("status", 200), body.get("min_requests", 1)
    if not isinstance(name, str) or not name or name == IDLE:
        raise ScenarioError("name must be a non-empty string other than idle")
    if (token is None) == (prefix is None) or not isinstance(token or prefix, str) or not (token or prefix):
        raise ScenarioError("exactly one of token and token_prefix must be a non-empty string")
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
        raise ScenarioError("status must be an HTTP status")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ScenarioError("min_requests must be a non-negative integer")
    return Scenario(name, body.get("document"), token, prefix, status, minimum)


@dataclass
class UsageDouble:
    """The double's state; `answer` is called by the request handler under the lock."""

    journal: Path | None = None
    scenario: Scenario = field(default_factory=Scenario)
    requests: int = 0
    good: int = 0
    failures: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    bridge: Bridge | None = None

    def state(self) -> dict[str, Any]:
        with self.lock:
            done = self.good >= self.scenario.min_requests
            return bridge_state(self.bridge) | {
                "double": "usage",
                "scenario": self.scenario.name,
                "requests": self.requests,
                "good_requests": self.good,
                "failures": list(self.failures),
                "done": done,
                "ok": done and not self.failures,
            }

    def load(self, body: Any) -> dict[str, Any]:
        scenario = parse_scenario(body)
        with self.lock:
            self.scenario, self.requests, self.good, self.failures = scenario, 0, 0, []
        return {"scenario": scenario.name}

    def credential_ok(self, headers: Mapping[str, str]) -> bool:
        given = headers.get("authorization", "")
        betas = [part.strip() for part in headers.get("anthropic-beta", "").split(",")]
        if OAUTH_BETA not in betas or not given.startswith("Bearer "):
            return False
        token = given[len("Bearer ") :]
        if self.scenario.token is not None:
            return hmac.compare_digest(token.encode(), self.scenario.token.encode())
        assert self.scenario.token_prefix is not None
        return token.startswith(self.scenario.token_prefix) and len(token) > len(self.scenario.token_prefix)

    def answer(self, method: str, path: str, headers: Mapping[str, str]) -> tuple[int, Any]:
        body: Any
        with self.lock:
            self.requests += 1
            if self.scenario.name == IDLE:
                status, body = 404, {"error": "idle"}
                self.failures.append(f"request while idle: {method} {path}")
            elif method != "GET" or path.split("?", 1)[0] != USAGE_PATH:
                status, body = 404, {"error": "not_found"}
                self.failures.append(f"unhandled route {method} {path}")
            elif not self.credential_ok(headers):
                status, body = 401, UNAUTHORIZED
                self.failures.append("usage poll without the subscription credential or the oauth beta")
            else:
                self.good += 1
                status = self.scenario.status
                body = self.scenario.document if status == 200 else UNAUTHORIZED
            self.write_journal(method, path, headers, status)
        return status, body

    def write_journal(self, method: str, path: str, headers: Mapping[str, str], status: int) -> None:
        if self.journal is None:
            return
        shown = {
            name: (f"<redacted {len(value)} chars>" if name in SECRET_HEADERS else value)
            for name, value in sorted(headers.items())
        }
        record = {"seq": self.requests, "method": method, "path": path, "headers": shown, "status": status}
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def handler_for(double: UsageDouble) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if double.bridge is not None and double.bridge.forward(self, "GET"):
                return
            if self.path == "/_double/state":
                self.send(200, double.state())
                return
            headers = {name.lower(): value for name, value in self.headers.items()}
            self.send(*double.answer("GET", self.path, headers))

        def bridged(self, method: str) -> None:
            """`PATCH`/`DELETE` exist only for the bridge (GitHub); the double itself answers them as unknown."""
            if double.bridge is not None and double.bridge.forward(self, method):
                return
            headers = {name.lower(): value for name, value in self.headers.items()}
            self.send(*double.answer(method, self.path, headers))

        def do_PATCH(self) -> None:
            self.bridged("PATCH")

        def do_DELETE(self) -> None:
            self.bridged("DELETE")

        def do_POST(self) -> None:
            if double.bridge is not None and double.bridge.forward(self, "POST"):
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY))
            if self.path != "/_double/scenario":
                headers = {name.lower(): value for name, value in self.headers.items()}
                self.send(*double.answer("POST", self.path, headers))
                return
            try:
                self.send(200, double.load(json.loads(raw or b"null")))
            except ValueError as exc:
                self.send(400, {"error": "invalid", "detail": str(exc)})

    return Handler


def serve(port: int, journal: Path | None, bridge: Bridge | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), handler_for(UsageDouble(journal, bridge=bridge)))


def main(argv: Sequence[str] | None = None, run: Callable[[ThreadingHTTPServer], None] | None = None) -> int:
    """Serves until the process is terminated (`run` replaces `serve_forever` in the tests)."""
    parser = argparse.ArgumentParser(prog="python -m orchestrator.usage_double", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--live", action="append", default=[], help="<name>=<https upstream>; live runs only")
    args = parser.parse_args(argv)
    try:
        upstreams = [parse_upstream(text) for text in args.live]
    except BridgeError as exc:
        parser.error(str(exc))
    bridge = None
    if upstreams:
        journal = args.journal.with_name(args.journal.stem + "-bridge.jsonl") if args.journal else None
        bridge = Bridge({upstream.name: upstream for upstream in upstreams}, journal)
    server = serve(args.port, args.journal, bridge)
    print(f"http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        (run or (lambda s: s.serve_forever(poll_interval=0.2)))(server)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
