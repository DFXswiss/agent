"""Orchestrator parts of the full live tests (LVT-01): GitHub and codeload through the live bridge, the bridge's
journaled upstream addresses in the egress check, the operator's live values on standard input, and the control
routes `POST /live/github` and `POST /live/catalog`."""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from orchestrator import cli, config, live_bridge, run, usage_double
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.node import LocalNode, NodeError
from orchestrator.routes import live, quota
from orchestrator.run import Run
from orchestrator.services import Double, Runner
from support import CORE, World
from test_routes_model import FakeNode, _request, call, failed, ok
from test_run import config_of, summary, tools_of

TARBALL = "https://codeload.github.com/octo/live/legacy.tar.gz/refs/heads/main?token=short-lived"


class GitHubStandIn(BaseHTTPRequestHandler):
    """api.github.com and codeload.github.com in one: a tarball redirect, a PATCH echo and the tarball itself."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def reply(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/repos/octo/live/tarball/"):
            self.reply(302, b"", {"Location": TARBALL})
        elif self.path.startswith("/repos/octo/live/elsewhere"):
            self.reply(302, b"", {"Location": "https://github.com/octo/live"})
        else:
            self.reply(200, json.dumps({"path": self.path}).encode())

    def do_DELETE(self) -> None:
        self.reply(204, b"")

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.reply(200, json.dumps({"patched": self.path, "body": self.rfile.read(length).decode()}).encode())


@pytest.fixture
def github() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), GitHubStandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def serving(bridge: live_bridge.Bridge | None, journal: Path | None = None) -> tuple[ThreadingHTTPServer, str]:
    server = usage_double.serve(0, journal, bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def raw_get(url: str) -> http.client.HTTPResponse:
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=10)
    connection.request("GET", f"{parts.path}?{parts.query}" if parts.query else parts.path)
    return connection.getresponse()


# ---------------------------------------------------------------------------------------------------- live bridge


def test_bridge_brings_github_and_codeload_through_the_bridge(tmp_path: Path, github: int) -> None:
    upstreams = {
        "github": live_bridge.parse_upstream("github=https://api.github.com"),
        "codeload": live_bridge.parse_upstream("codeload=https://codeload.github.com"),
    }
    connect = lambda _upstream: http.client.HTTPConnection("127.0.0.1", github, timeout=10)  # noqa: E731
    journal = tmp_path / "bridge.jsonl"
    server, url = serving(live_bridge.Bridge(upstreams, journal, connect))
    try:
        redirected = raw_get(f"{url}/live/github/repos/octo/live/tarball/0123abc")
        assert redirected.status == 302
        origin = url.removeprefix("http://")
        assert redirected.getheader("Location") == (
            f"http://{origin}/codeload/octo/live/legacy.tar.gz/refs/heads/main?token=short-lived"
        )
        tarball = raw_get(redirected.getheader("Location") or "")
        assert json.loads(tarball.read()) == {"path": "/octo/live/legacy.tar.gz/refs/heads/main?token=short-lived"}
        elsewhere = raw_get(f"{url}/live/github/repos/octo/live/elsewhere")
        assert elsewhere.getheader("Location") == "https://github.com/octo/live"
        patch = urllib.request.Request(
            f"{url}/live/github/repos/octo/live/git/refs/heads/x", b'{"sha":"1"}', method="PATCH"
        )
        with urllib.request.urlopen(patch, timeout=10) as response:
            assert json.loads(response.read()) == {
                "patched": "/repos/octo/live/git/refs/heads/x",
                "body": '{"sha":"1"}',
            }
        delete = urllib.request.Request(f"{url}/live/github/repos/octo/live/git/refs/heads/x", method="DELETE")
        with urllib.request.urlopen(delete, timeout=10) as response:
            assert response.status == 204
    finally:
        server.shutdown()
        server.server_close()
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [(line["method"], line["upstream"], line["path"]) for line in lines] == [
        ("GET", "github", "/repos/octo/live/tarball/0123abc"),
        ("GET", "codeload", "/octo/live/legacy.tar.gz/refs/heads/main"),
        ("GET", "github", "/repos/octo/live/elsewhere"),
        ("PATCH", "github", "/repos/octo/live/git/refs/heads/x"),
        ("DELETE", "github", "/repos/octo/live/git/refs/heads/x"),
    ]
    assert {line["peer"] for line in lines} == {"127.0.0.1"}
    assert "short-lived" not in journal.read_text()
    assert live_bridge.peers(journal.read_text()) == {"127.0.0.1"}


class Handler:
    def __init__(self, host: str | None) -> None:
        self.headers = {"Host": host} if host else {}


@pytest.mark.parametrize(
    ("location", "rewritten"),
    [
        (TARBALL, "http://127.0.0.1:9/codeload/octo/live/legacy.tar.gz/refs/heads/main?token=short-lived"),
        (
            "https://codeload.github.com/octo/live/archive.tar.gz",
            "http://127.0.0.1:9/codeload/octo/live/archive.tar.gz",
        ),
        ("http://codeload.github.com/octo/live", None),
        ("https://example.com/octo/live", None),
        ("https://user@codeload.github.com/octo/live", None),
        ("https://codeload.github.com:8443/octo/live", None),
        ("https://codeload.github.com", None),
    ],
)
def test_bridge_rewrites_only_codeload_redirects(location: str, rewritten: str | None) -> None:
    bridge = live_bridge.Bridge({"codeload": live_bridge.parse_upstream("codeload=https://codeload.github.com")})
    assert bridge.location(location, Handler("127.0.0.1:9")) == (rewritten or location)  # type: ignore[arg-type]
    assert bridge.location(location, Handler(None)) == location  # type: ignore[arg-type]
    without = live_bridge.Bridge({"github": live_bridge.parse_upstream("github=https://api.github.com")})
    assert without.location(location, Handler("127.0.0.1:9")) == location  # type: ignore[arg-type]
    assert without.target("/codeload/octo/live") is None
    assert bridge.target("/codeload/octo/live/legacy.tar.gz/main") is None  # no redirect token: not a bridge path
    assert bridge.target("/codeload/octo/live/x?token=") is None
    assert bridge.target("/live/codeload/octo/live/x?token=t") is None
    assert bridge.target("/codeload/octo/live/x?a=1&token=t") is not None


class Sock:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def getpeername(self) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class WithSock:
    def __init__(self, sock: Any) -> None:
        self.sock = sock


def test_peer_address_and_journal_peers() -> None:
    assert live_bridge.peer_address(WithSock(Sock(("198.51.100.4", 443)))) == "198.51.100.4"  # type: ignore[arg-type]
    assert live_bridge.peer_address(WithSock(Sock(OSError("gone")))) is None  # type: ignore[arg-type]
    assert live_bridge.peer_address(WithSock(None)) is None  # type: ignore[arg-type]
    text = "\n".join(['{"peer": "198.51.100.4"}', '{"peer": null}', "[1]", "not json", '{"peer": "203.0.113.9"}'])
    assert live_bridge.peers(text) == {"198.51.100.4", "203.0.113.9"}


def test_usage_double_refuses_patch_without_a_bridge(tmp_path: Path) -> None:
    server, url = serving(None, tmp_path / "j.jsonl")
    try:
        assert _request(url, "PATCH", {"a": 1}) == {"status": 404, "body": {"error": "idle"}}
        assert _request(url, "DELETE") == {"status": 404, "body": {"error": "idle"}}
        assert _request(f"{url}/_double/state")["body"]["failures"] == [
            "request while idle: PATCH /",
            "request while idle: DELETE /",
        ]
    finally:
        server.shutdown()
        server.server_close()


def test_quota_starts_the_bridge_with_the_github_upstreams() -> None:
    assert set(quota.LIVE_UPSTREAMS) == {"openrouter", "anthropic", "github", "codeload"}
    for name, url in quota.LIVE_UPSTREAMS.items():
        assert live_bridge.parse_upstream(f"{name}={url}").host in run.LIVE_HOSTS


# ---------------------------------------------------------------------------------------------------- egress check


def test_live_egress_allows_the_addresses_the_bridge_journaled(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = {"pid": 4242, "command": "Python"}
    records = [
        {**bridge, "remote": "192.0.2.10:443", "host": "192.0.2.10", "port": 443},
        {**bridge, "remote": "192.0.2.11:443", "host": "192.0.2.11", "port": 443},
    ]
    monkeypatch.setattr(run, "violations", lambda _records: [dict(r, device="hub-device") for r in records])
    monkeypatch.setattr(LocalNode, "processes", lambda _self: {"double-usage": {"pid": 4242}})
    read: list[str] = []
    original = LocalNode.read_text

    def read_text(self: LocalNode, path: str) -> str:
        if not path.endswith("/" + run.LIVE_BRIDGE_JOURNAL):
            return original(self, path)
        read.append(path)
        return '{"peer": "192.0.2.10"}\n'

    monkeypatch.setattr(LocalNode, "read_text", read_text)
    live_run = Run(config_of(world, live=True), world.environ, tools_of(world, live_addresses=set))
    assert live_run.execute() == run.EXIT_FAILED
    failures = summary(world)["failures"]
    assert "egress to 192.0.2.11:443 by Python on hub-device" in failures
    assert not any("192.0.2.10" in failure for failure in failures)
    assert len(read) == 1


def test_bridge_peers_without_a_journal(tmp_path: Path) -> None:
    class Gone(FakeNode):
        def read_text(self, path: str) -> str:
            raise NodeError(f"cannot read {path}")

    settings = RunConfig(
        topology="t1",
        core_src=CORE,
        agent_src=tmp_path,
        artifacts=tmp_path / "a",
        port_base=21000,
        vm_prefix="wp-t-",
        live=True,
    )
    assert Run.bridge_peers(Context(settings, Gone(), Gone(), "/py")) == set()


def test_live_settings_reach_playwright_but_not_the_scan(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []
    clean_env = config.clean_env

    def recording(environ: Any, extra: Any, *args: Any) -> dict[str, str]:
        if "E2E_CONTROL_URL" in extra:
            seen.append(dict(extra))
        return clean_env(environ, extra, *args)

    monkeypatch.setattr(run, "clean_env", recording)
    world.set(pw_model="1", hub_commands="audit verify")
    environ = {**world.environ, "AGENT_LIVE_GITHUB_REPO": "octo/live", "E2E_LIVE_GITHUB_TOKEN": "gh-token-0123456789"}
    live_run = Run(config_of(world, live=True), environ, tools_of(world, live_addresses=set))
    assert "octo/live" not in live_run.secrets.values
    assert "gh-token-0123456789" in live_run.secrets.values
    live_run.execute()
    assert seen[-1]["AGENT_LIVE_GITHUB_REPO"] == "octo/live"
    assert seen[-1]["E2E_LIVE_GITHUB_TOKEN"] == "gh-token-0123456789"
    hermetic = Run(config_of(world), environ, tools_of(world))
    hermetic.execute()
    assert "AGENT_LIVE_GITHUB_REPO" not in seen[-1]


# ---------------------------------------------------------------------------------------------------- --live-stdin


def test_read_live_values() -> None:
    lines = ["E2E_LIVE_OPENROUTER_KEY=sk-x=y\n", "AGENT_LIVE_GITHUB_REPO=octo/live\r\n", "\n", "IGNORED=after"]
    assert cli.read_live_values(lines) == {"E2E_LIVE_OPENROUTER_KEY": "sk-x=y", "AGENT_LIVE_GITHUB_REPO": "octo/live"}
    assert cli.read_live_values([]) == {}
    for bad in (["PATH=/bin"], ["E2E_LIVE_KEY"], ["E2E_LIVE_KEY=é"], ["E2E_LIVE_KEY="], ["e2e_live_key=x"]):
        with pytest.raises(cli.LiveInputError) as refused:
            cli.read_live_values(bad)
        assert "line 1" in str(refused.value)
    with pytest.raises(cli.LiveInputError):
        cli.read_live_values([f"E2E_LIVE_K{n}=v" for n in range(cli.MAX_LIVE_LINES + 1)])
    with pytest.raises(cli.LiveInputError) as hidden:
        cli.read_live_values(["sk-or-secret-value-without-name"])
    assert "secret" not in str(hidden.value)


def test_cli_live_stdin(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[dict[str, str]] = []

    def execute(self: run.Run) -> int:
        seen.append(self.environ)
        return 0

    monkeypatch.setattr(run.Run, "execute", execute)
    monkeypatch.setattr(cli, "install_sigterm", lambda: None)
    base = ["--topology", "t1", "--artifacts", str(world.artifacts)]
    assert cli.main([*base, "--live-stdin"], world.environ, stdin=[]) == cli.EXIT_USAGE
    assert "--live-stdin needs --live" in capsys.readouterr().err
    assert cli.main([*base, "--live", "--live-stdin"], world.environ, stdin=["nope"]) == cli.EXIT_USAGE
    assert "line 1" in capsys.readouterr().err
    assert cli.main([*base, "--live", "--live-stdin"], world.environ, stdin=["E2E_LIVE_A=secret-a\n"]) == 0
    assert seen[-1]["E2E_LIVE_A"] == "secret-a"
    monkeypatch.setattr("sys.stdin", io.StringIO("AGENT_LIVE_B=b\n"))
    assert cli.main([*base, "--live", "--live-stdin"], world.environ) == 0
    assert seen[-1]["AGENT_LIVE_B"] == "b"
    assert cli.main([*base, "--live"], world.environ, stdin=["E2E_LIVE_C=never-read"]) == 0
    assert "E2E_LIVE_C" not in seen[-1]


# ---------------------------------------------------------------------------------------------------- routes


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def ctx(tmp_path: Path, node: FakeNode) -> Context:
    config = RunConfig(
        topology="t1",
        core_src=CORE,
        agent_src=tmp_path,
        artifacts=tmp_path / "a",
        port_base=21000,
        vm_prefix="wp-t-",
        live=True,
    )
    context = Context(config, node, node, "/agent/python", scratch=str(tmp_path))
    context.core_on_runner, context.runner_run_dir, context.runner_scratch = "/core", "/run-dir", "/scratch"
    context.runners["runner-1"] = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "/scratch/r1"})
    node.alive_names.add("runner-1")
    return context


def test_live_github(ctx: Context, node: FakeNode) -> None:
    assert call(ctx, "POST", "/live/github", {"x": 1})[0] == 400
    status, answer = call(ctx, "POST", "/live/github", {})
    assert status == 200
    secret = quota.github_name(ctx)
    assert re.fullmatch(r"github_[0-9a-f]{32}", secret)
    assert answer == {
        "api_url": f"http://127.0.0.1:21011/live/{secret}",
        "web_url": "http://127.0.0.1:21011",
        "restarted": ["runner-1"],
    }
    assert ctx.runners["runner-1"].env[live.API_VARIABLE] == answer["api_url"]
    assert ctx.runners["runner-1"].env[live.WEB_VARIABLE] == answer["web_url"]
    name, argv, _env, _cwd = node.spawned[0]
    assert name == "double-usage"
    assert f"{secret}=https://api.github.com" in argv
    assert "codeload=https://codeload.github.com" in argv
    assert call(ctx, "POST", "/live/github", None)[1]["restarted"] == []


def test_live_routes_need_a_live_run(ctx: Context) -> None:
    hermetic = Context(
        RunConfig(
            topology="t1", core_src=CORE, agent_src=Path("/x"), artifacts=Path("/a"), port_base=21000, vm_prefix="wp-t-"
        ),
        ctx.hub_node,
        ctx.runner_node,
        "/py",
    )
    assert call(hermetic, "POST", "/live/github", {})[0] == 409
    assert call(hermetic, "POST", "/live/catalog", {"providers_allowed": []})[0] == 409


PACKAGED = {"version": 1, "access": {"platform": {"default": "m", "allowed": ["m"], "providers_allowed": []}}}


def test_live_catalog(ctx: Context, node: FakeNode) -> None:
    bad: Any
    for bad in (
        None,
        [],
        {},
        {"providers_allowed": "x"},
        {"providers_allowed": [1]},
        {"providers_allowed": ["\n"]},
        {"providers_allowed": ["a"] * 65},
        {"providers_allowed": [], "more": 1},
    ):
        assert call(ctx, "POST", "/live/catalog", bad)[0] == 400
    written: list[tuple[list[str], dict[str, str] | None]] = []

    def respond(argv: list[str]) -> dict[str, Any]:
        if argv[:3] == ["/agent/python", "-I", "-c"]:
            return ok(json.dumps(PACKAGED) + "\n")
        written.append((argv, node.runs[-1][1]))
        return ok()

    node.respond = respond
    status, answer = call(ctx, "POST", "/live/catalog", {"providers_allowed": ["LVT01 none"]})
    assert (status, answer) == (200, {"path": "/scratch/live-model-catalog.json", "restarted": ["runner-1"]})
    argv, env = written[0]
    assert argv[-1] == "/scratch/live-model-catalog.json"
    assert env is not None
    assert json.loads(env["E2E_CATALOG"])["access"]["platform"]["providers_allowed"] == ["LVT01 none"]
    assert ctx.runners["runner-1"].env[live.CATALOG_VARIABLE] == answer["path"]
    assert node.runs[0][0][3] == live.PACKAGED_SCRIPT

    node.respond = lambda argv: ok(json.dumps(PACKAGED)) if "-I" in argv else failed(stderr="disk full")
    assert call(ctx, "POST", "/live/catalog", {"providers_allowed": []}) == (
        500,
        {"error": "failed", "detail": "model catalog not written: disk full"},
    )
    node.respond = lambda _argv: ok('{"access": {}}')
    assert call(ctx, "POST", "/live/catalog", {"providers_allowed": []})[0] == 500
    node.respond = lambda _argv: ok("not json")
    assert call(ctx, "POST", "/live/catalog", {"providers_allowed": []})[0] == 500
    node.respond = lambda _argv: failed(stderr="no module")
    assert call(ctx, "POST", "/live/catalog", {"providers_allowed": []})[1]["detail"].endswith("no module")


def test_packaged_script_reads_the_catalog_module() -> None:
    compile(live.PACKAGED_SCRIPT, "packaged", "exec")


def test_live_journal(ctx: Context, node: FakeNode, monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(path: str) -> str:
        raise NodeError(f"cannot read {path}")

    monkeypatch.setattr(node, "read_text", gone)
    assert call(ctx, "GET", "/live/journal") == (200, {"entries": []})
    monkeypatch.undo()
    lines = [
        {"method": "GET", "upstream": "github", "path": "/user/installations", "status": 403, "peer": "192.0.2.1"},
        {"method": "PATCH", "upstream": "github_0a1b", "path": "/repos/o/r/git/refs/heads/b", "status": 200},
    ]
    text = "".join(json.dumps(line) + "\n" for line in lines) + '{"method": "GE'
    node.files["/run-dir/" + run.LIVE_BRIDGE_JOURNAL] = text
    entries = call(ctx, "GET", "/live/journal")[1]["entries"]
    assert [entry["upstream"] for entry in entries] == ["github", "github"]
    assert entries[1] == {"method": "PATCH", "upstream": "github", "path": "/repos/o/r/git/refs/heads/b", "status": 200}


def test_live_model_double(ctx: Context, node: FakeNode) -> None:
    ctx.doubles["model"] = Double(node, "model", 21009, 21010)
    assert call(ctx, "POST", "/live/model-double", {"x": 1})[0] == 400
    status, answer = call(ctx, "POST", "/live/model-double", {})
    assert (status, answer["restarted"]) == (200, ["runner-1"])
    for name in live.MODEL_VARIABLES:
        assert ctx.runners["runner-1"].env[name] == answer["url"]
    assert call(ctx, "POST", "/live/model-double", None)[1]["restarted"] == []
