"""Control routes of SCN-05 (model access, budget, session environment, credential scan, login doubles, usage
double) against a scripted node, and the usage double itself."""

from __future__ import annotations

import hashlib
import http.client
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from orchestrator import live_bridge, usage_double
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import RouteError, discover, dispatch
from orchestrator.node import Node
from orchestrator.routes import failover, login, model, plaintext, quota
from orchestrator.services import Double, Hub, Runner
from support import CORE

SESSION = "01a20172-b86a-789c-89ab-7aa8af7fa995"
PREFIX = "wp-t-e2e-"
VM = f"{PREFIX}r1-arp-0000aaaa"
KEY = "platform-key-0123456789abcdef0123"
TOKEN = "token-of-the-login-double-0123456789"
API_KEY = "api-key-of-the-user-0123456789abcd"
Result = dict[str, Any]


def ok(stdout: str = "") -> Result:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


def failed(rc: int = 1, stderr: str = "boom") -> Result:
    return {"rc": rc, "stdout": "", "stderr": stderr}


class FakeNode(Node):
    """Scripted device: `respond(argv)` answers `run`, `answer(method, url, body)` answers `http`."""

    def __init__(self) -> None:
        self.name = "fake"
        self.runs: list[tuple[list[str], dict[str, str] | None]] = []
        self.https: list[tuple[str, str, Any]] = []
        self.spawned: list[tuple[str, list[str], dict[str, str], str | None]] = []
        self.alive_names: set[str] = set()
        self.stopped: list[str] = []
        self.files: dict[str, str] = {}
        self.respond: Callable[[list[str]], Result] = lambda _argv: ok()
        self.answer: Callable[[str, str, Any], Result] = lambda _m, _u, _b: {"status": 200, "body": {"ok": True}}

    def call(self, op: str, /, **args: Any) -> Any:  # noqa: PLR0911 - one answer per device method
        if op == "run":
            self.runs.append((args["argv"], args.get("env")))
            return self.respond(args["argv"])
        if op == "http":
            self.https.append((args["method"], args["url"], args["body"]))
            return self.answer(args["method"], args["url"], args["body"])
        if op == "spawn":
            self.spawned.append((args["name"], args["argv"], args["env"], args.get("cwd")))
            self.alive_names.add(args["name"])
            return 1
        if op == "wait_line":
            return "http://127.0.0.1:1" if args["name"].startswith("double-") else "runner.run enrolled=yes"
        if op == "alive":
            return args["name"] in self.alive_names
        if op == "stop":
            self.stopped.append(args["name"])
            self.alive_names.discard(args["name"])
            return 0
        if op == "log_tail":
            return "runner.run enrolled=yes"
        if op == "read_text":
            return self.files[args["path"]]
        if op == "processes":
            return {name: {"alive": name in self.alive_names} for name, *_rest in self.spawned}
        raise AssertionError(op)


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def ctx(tmp_path: Path, node: FakeNode) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    context = Context(config, node, node, "/agent/python", scratch=str(tmp_path))
    context.core_on_runner, context.runner_run_dir, context.runner_scratch = "/core", "/run-dir", "/scratch"
    context.doubles["model"] = Double(node, "model", 21009, 21010)
    env = {"AGENT_RUNNER_HOME": "/scratch/runner-1", "AGENT_RUNNER_OPENROUTER_KEY_FILE": "/scratch/key"}
    context.runners["runner-1"] = Runner(node, "runner-1", "/agent/python", env)
    node.alive_names.add("runner-1")
    node.files["/scratch/key"] = KEY + "\n"
    return context


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(discover(), ctx, method, path, body)


def token_run(argv: list[str]) -> Result | None:
    """The derivation of a login-double token (TOKEN_SCRIPT), else None."""
    if argv[:3] == ["/agent/python", "-I", "-c"] and argv[3] == model.TOKEN_SCRIPT:
        return ok(TOKEN + "\n")
    return None


# ---------------------------------------------------------------------------------------------------- usage double


def test_parse_scenario_accepts_a_token_or_a_prefix() -> None:
    scenario = usage_double.parse_scenario({"name": "q", "document": {"a": 1}, "token": "t", "min_requests": 2})
    assert (scenario.token, scenario.token_prefix, scenario.status, scenario.min_requests) == ("t", None, 200, 2)
    scenario = usage_double.parse_scenario({"name": "q", "token_prefix": "sk-", "status": 403})
    assert (scenario.token, scenario.token_prefix, scenario.status, scenario.document) == (None, "sk-", 403, None)


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"name": "q", "token": "t", "extra": 1},
        {"name": "", "token": "t"},
        {"name": "idle", "token": "t"},
        {"name": 1, "token": "t"},
        {"name": "q"},
        {"name": "q", "token": "t", "token_prefix": "p"},
        {"name": "q", "token": 5},
        {"name": "q", "token": ""},
        {"name": "q", "token": "t", "status": True},
        {"name": "q", "token": "t", "status": 100},
        {"name": "q", "token": "t", "status": "200"},
        {"name": "q", "token": "t", "min_requests": -1},
        {"name": "q", "token": "t", "min_requests": False},
    ],
)
def test_parse_scenario_refuses(body: Any) -> None:
    with pytest.raises(usage_double.ScenarioError):
        usage_double.parse_scenario(body)


BETA = {"anthropic-beta": "other-beta, oauth-2025-04-20"}


def test_usage_double_answers_and_records(tmp_path: Path) -> None:
    journal = tmp_path / "usage.jsonl"
    double = usage_double.UsageDouble(journal)
    assert double.state() | {"failures": []} == {
        "double": "usage",
        "scenario": "idle",
        "requests": 0,
        "good_requests": 0,
        "failures": [],
        "done": False,
        "ok": False,
    }
    assert double.answer("GET", "/api/oauth/usage", {}) == (404, {"error": "idle"})
    assert double.load({"name": "q", "document": {"five_hour": {}}, "token": "secret-token"}) == {"scenario": "q"}
    assert double.state()["failures"] == []
    assert double.answer("POST", "/api/oauth/usage", {})[0] == 404
    assert double.answer("GET", "/other", {})[0] == 404
    assert double.answer("GET", "/api/oauth/usage", {"authorization": "Bearer secret-token"})[0] == 401
    assert double.answer("GET", "/api/oauth/usage", {**BETA, "authorization": "Basic x"})[0] == 401
    assert double.answer("GET", "/api/oauth/usage", {**BETA, "authorization": "Bearer wrong"})[0] == 401
    good = {**BETA, "authorization": "Bearer secret-token"}
    assert double.answer("GET", "/api/oauth/usage?x=1", good) == (200, {"five_hour": {}})
    state = double.state()
    assert (state["requests"], state["good_requests"], state["done"], state["ok"]) == (6, 1, True, False)
    assert len(state["failures"]) == 5
    text = journal.read_text()
    assert "secret-token" not in text
    assert json.loads(text.splitlines()[-1])["headers"]["authorization"] == "<redacted 19 chars>"

    double.load({"name": "refused", "token_prefix": "sk-", "status": 403, "min_requests": 1})
    assert double.answer("GET", "/api/oauth/usage", {**BETA, "authorization": "Bearer sk-"})[0] == 401
    assert double.answer("GET", "/api/oauth/usage", {**BETA, "authorization": "Bearer sk-abc"}) == (
        403,
        usage_double.UNAUTHORIZED,
    )
    double.load({"name": "fine", "token_prefix": "sk-", "min_requests": 0})
    assert double.state()["ok"] is True
    no_journal = usage_double.UsageDouble()
    no_journal.load({"name": "q", "token": "t"})
    assert no_journal.answer("GET", "/api/oauth/usage", {**BETA, "authorization": "Bearer t"})[0] == 200


def _request(url: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> Result:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read())}


def test_usage_double_http_server(tmp_path: Path) -> None:
    server = usage_double.serve(0, tmp_path / "j.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _request(f"{url}/_double/state")["body"]["scenario"] == "idle"
        assert _request(f"{url}/_double/scenario", "POST", {"name": "q"})["status"] == 400
        loaded = _request(f"{url}/_double/scenario", "POST", {"name": "q", "token": "tok", "document": [1]})
        assert loaded == {"status": 200, "body": {"scenario": "q"}}
        headers = {"Authorization": "Bearer tok", "anthropic-beta": "oauth-2025-04-20"}
        assert _request(f"{url}/api/oauth/usage", headers=headers) == {"status": 200, "body": [1]}
        assert _request(f"{url}/api/oauth/usage", "POST", {})["status"] == 404
        assert _request(f"{url}/_double/state")["body"]["requests"] == 2
    finally:
        server.shutdown()
        server.server_close()


def test_usage_double_main_prints_its_url(capsys: pytest.CaptureFixture[str]) -> None:
    served: list[int] = []
    assert usage_double.main(["--port", "0"], run=lambda server: served.append(server.server_address[1])) == 0
    assert capsys.readouterr().out == f"http://127.0.0.1:{served[0]}\n"


def test_usage_double_serves_until_terminated(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "orchestrator.usage_double", "--port", "0", "--journal", str(tmp_path / "j")],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        url = process.stdout.readline().strip()
        assert _request(f"{url}/_double/state")["body"]["double"] == "usage"
    finally:
        process.terminate()
        process.wait(10)
        assert process.stdout is not None
        process.stdout.close()


# ---------------------------------------------------------------------------------------------------- model routes


def test_platform_key_and_tokens(ctx: Context, node: FakeNode) -> None:
    assert model.platform_key(ctx) == KEY
    node.respond = lambda argv: token_run(argv) or failed()
    assert model.subscription_token(ctx, "seed-1") == TOKEN
    assert node.runs[-1][0][4:] == ["seed-1", "1"]
    with pytest.raises(RouteError, match=r"1\.\.64"):
        model.subscription_token(ctx, "bad seed")
    node.respond = lambda _argv: failed(stderr="no module")
    with pytest.raises(RouteError, match="no module"):
        model.subscription_token(ctx, "seed-1")
    ctx.runners["runner-1"].env.pop(model.KEY_FILE)
    with pytest.raises(RouteError, match="no runner has a platform key"):
        model.platform_key(ctx)


def test_credential_values(ctx: Context, node: FakeNode) -> None:
    node.respond = lambda argv: token_run(argv) or failed()
    assert model.credential_values(ctx, {"values": [API_KEY], "login_seeds": ["s1"]}) == [API_KEY, KEY, TOKEN]
    assert model.credential_values(ctx, None) == [KEY]
    for body in (
        {"values": "x"},
        {"values": ["short"]},
        {"values": [1]},
        {"values": [API_KEY] * 17},
        {"login_seeds": ["bad seed"]},
    ):
        with pytest.raises(RouteError):
            model.credential_values(ctx, body)
    ctx.runners["runner-1"].env.pop(model.KEY_FILE)
    assert model.credential_values(ctx, {}) == []


def test_needles_hold_a_prefix_and_the_digest() -> None:
    rows = json.loads(model.needles([API_KEY, "0123456789abcdef"]))
    assert rows[0] == [API_KEY[:8], hashlib.sha256(API_KEY.encode()).hexdigest(), len(API_KEY)]
    assert rows[1][0] == "01234567"


def test_scan_script_finds_values(tmp_path: Path) -> None:
    (tmp_path / "plain").write_text(f"x {API_KEY} y")
    (tmp_path / "half").write_text(API_KEY[:20] + "-but-not-the-rest-of-it")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "twice").write_text(f"{API_KEY[:20]}..{API_KEY}")
    (tmp_path / "link").symlink_to(tmp_path / "plain")
    (tmp_path / "dirlink").symlink_to(tmp_path / "sub")
    done = subprocess.run(
        [sys.executable, "-I", "-c", model.SCAN_SCRIPT, model.needles([API_KEY]), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    found = json.loads(done.stdout)
    assert found["files"] == 3
    assert sorted(item["source"] for item in found["found"]) == [str(tmp_path / "plain"), str(tmp_path / "sub/twice")]


def test_post_scenario(ctx: Context, node: FakeNode) -> None:
    node.respond = lambda argv: token_run(argv) or failed()
    node.answer = lambda _m, _u, body: {"status": 200, "body": {"name": body["scenario"]["name"], "access": "x"}}
    assert call(ctx, "POST", "/model/scenario", {"scenario": {"name": "s"}, "access": None}) == (
        200,
        {"double": {"name": "s"}, "access": None},
    )
    assert node.https[-1][2] == {"scenario": {"name": "s"}, "access": None}
    assert call(ctx, "POST", "/model/scenario", {"scenario": {"name": "s"}, "access": {"kind": "platform"}})[0] == 200
    assert node.https[-1][2]["access"] == {"kind": "openrouter", "token": KEY}
    body = {"scenario": {"name": "s"}, "access": {"kind": "api_key", "token": API_KEY}}
    assert call(ctx, "POST", "/model/scenario", body)[1]["access"] == "api_key"
    assert node.https[-1][2]["access"] == {"kind": "api_key", "token": API_KEY}
    body = {"scenario": {"name": "s"}, "access": {"kind": "subscription", "login_seed": "s1"}}
    assert call(ctx, "POST", "/model/scenario", body)[0] == 200
    assert node.https[-1][2]["access"] == {"kind": "subscription", "token": TOKEN}
    body = {"scenario": {"name": "s"}, "access": {"kind": "subscription", "token": API_KEY}}
    assert call(ctx, "POST", "/model/scenario", body)[0] == 200
    assert node.https[-1][2]["access"]["token"] == API_KEY
    bad: Any
    for bad in (
        None,
        {"scenario": []},
        {"scenario": {}, "access": "platform"},
        {"scenario": {}, "access": {"kind": "other"}},
        {"scenario": {}, "access": {"kind": "api_key"}},
        {"scenario": {}, "access": {"kind": "api_key", "token": "short"}},
    ):
        assert call(ctx, "POST", "/model/scenario", bad)[0] == 400
    node.answer = lambda _m, _u, _b: {"status": 400, "body": "broken scenario"}
    assert call(ctx, "POST", "/model/scenario", {"scenario": {}, "access": None}) == (
        400,
        {"error": "invalid", "detail": "model double refused the scenario: broken scenario"},
    )
    del ctx.doubles["model"]
    assert call(ctx, "POST", "/model/scenario", {"scenario": {}})[0] == 409


def test_post_budget_restarts_the_hub(ctx: Context, node: FakeNode) -> None:
    assert call(ctx, "POST", "/hub/budget", {"usd": "0.001"})[0] == 409
    ctx.hub = Hub(node, "/hub/python", {"A": "1"}, "127.0.0.1", 21006, sleep=lambda _s: None)
    assert call(ctx, "POST", "/hub/budget", {"usd": "0.001"}) == (200, {"usd": "0.001"})
    assert ctx.hub.env[model.BUDGET] == "0.001"
    assert node.stopped == ["hub"]
    assert node.spawned[-1][0] == "hub"
    assert call(ctx, "POST", "/hub/budget", {"usd": None}) == (200, {"usd": None})
    assert model.BUDGET not in ctx.hub.env
    for bad in (None, {"usd": 1}, {"usd": "-1"}, {"usd": "0.0000001"}, {}):
        assert call(ctx, "POST", "/hub/budget", bad)[0] == 400


def test_post_failover_restarts_the_hub_with_the_deadline(ctx: Context, node: FakeNode) -> None:
    """KOR-58 (E2E-67): the device failure deadline within a test; null takes the setting away again."""
    body = {"online_seconds": 30, "failover_seconds": 60}
    assert call(ctx, "POST", "/hub/failover", body)[0] == 409
    ctx.hub = Hub(node, "/hub/python", {"A": "1"}, "127.0.0.1", 21006, sleep=lambda _s: None)
    assert call(ctx, "POST", "/hub/failover", body) == (200, body)
    assert (ctx.hub.env[failover.ONLINE], ctx.hub.env[failover.FAILOVER]) == ("30", "60")
    assert node.stopped == ["hub"]
    assert node.spawned[-1][0] == "hub"
    assert call(ctx, "POST", "/hub/failover", {"failover_seconds": 120}) == (
        200,
        {"online_seconds": None, "failover_seconds": 120},
    )
    assert failover.ONLINE not in ctx.hub.env
    nulls = {"online_seconds": None, "failover_seconds": None}
    assert call(ctx, "POST", "/hub/failover", nulls) == (200, nulls)
    assert call(ctx, "POST", "/hub/failover", {}) == (200, nulls)
    assert failover.FAILOVER not in ctx.hub.env
    bad: Any
    for bad in (
        None,
        [],
        {"other": 1},
        {"online_seconds": 0},
        {"online_seconds": 3601},
        {"failover_seconds": 86401},
        {"failover_seconds": "60"},
        {"failover_seconds": True},
        {"online_seconds": 30, "failover_seconds": 59},
        {"failover_seconds": 59},
        {"online_seconds": 200},
    ):
        assert call(ctx, "POST", "/hub/failover", bad)[0] == 400, bad


def test_post_hub_restart_stops_and_starts_the_hub(ctx: Context, node: FakeNode) -> None:
    """KOR-36 (E2E-39): a release-like restart with the unchanged environment."""
    assert call(ctx, "POST", "/hub/restart", {})[0] == 409
    ctx.hub = Hub(node, "/hub/python", {"A": "1"}, "127.0.0.1", 21006, sleep=lambda _s: None)
    assert call(ctx, "POST", "/hub/restart", {}) == (200, {"restarted": True})
    assert node.stopped == ["hub"]
    assert node.spawned[-1][0] == "hub"
    assert ctx.hub.env == {"A": "1"}


def vm_responder(environ: str, *, pid: str = "4242", host: Result | None = None, vm: Result | None = None) -> Any:
    def respond(argv: list[str]) -> Result:  # noqa: PLR0911 - one answer per scripted command
        found = token_run(argv)
        if found is not None:
            return found
        if argv[:2] == ["limactl", "list"]:
            return ok(json.dumps({"name": VM, "status": "Running", "memory": 0}) + "\n")
        if argv[:2] == ["limactl", "shell"]:
            inner = argv[7:]
            if inner[:3] == ["tmux", "-S", "/run/agent-guest/tmux.sock"]:
                return ok()
            if inner[:2] == ["systemctl", "show"]:
                return ok(pid + "\n")
            if inner[:1] == ["cat"]:
                return ok(environ)
            if inner[:3] == ["python3", "-I", "-c"]:
                return vm or ok(json.dumps({"files": 7, "found": [{"source": "/srv/x", "value": 0}]}))
        if argv[:3] == ["/agent/python", "-I", "-c"]:
            return host or ok(json.dumps({"files": 3, "found": []}))
        return failed()

    return respond


def environ_of(**values: str) -> str:
    return "".join(f"{name}={value}\0" for name, value in values.items())


def test_env_check(ctx: Context, node: FakeNode) -> None:
    session_token = "s" * 43
    node.respond = vm_responder(
        environ_of(
            ANTHROPIC_AUTH_TOKEN=session_token,
            PLATFORM_SESSION_TOKEN=session_token,
            ANTHROPIC_API_KEY="",
            ANTHROPIC_BASE_URL="http://127.0.0.1:7700/model",
            ANTHROPIC_MODEL="deepseek/x",
            LEAK=f"prefix{API_KEY}",
        )
    )
    status, answer = call(ctx, "POST", f"/sessions/{SESSION}/env-check", {"values": [API_KEY], "login_seeds": ["s"]})
    assert status == 200
    assert answer == {
        "vm": VM,
        "pid": 4242,
        "names": [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "LEAK",
            "PLATFORM_SESSION_TOKEN",
        ],
        "model": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:7700/model", "ANTHROPIC_MODEL": "deepseek/x"},
        "session_token": {"present": True, "same": True, "length": 43},
        "anthropic_api_key_empty": True,
        "credential_findings": ["LEAK"],
        "credentials_checked": 3,
    }
    node.respond = vm_responder(environ_of(ANTHROPIC_AUTH_TOKEN="a" * 20, ANTHROPIC_API_KEY="k"))
    answer = call(ctx, "POST", f"/sessions/{SESSION}/env-check", {})[1]
    assert answer["session_token"] == {"present": False, "same": False, "length": 20}
    assert answer["anthropic_api_key_empty"] is False
    node.respond = vm_responder("", pid="0")
    assert call(ctx, "POST", f"/sessions/{SESSION}/env-check", {})[0] == 409
    node.respond = vm_responder("", pid="")
    assert call(ctx, "POST", f"/sessions/{SESSION}/env-check", {})[0] == 409
    node.respond = lambda argv: token_run(argv) or ok("")
    assert call(ctx, "POST", f"/sessions/{SESSION}/env-check", {})[0] == 404


def test_credential_scan(ctx: Context, node: FakeNode, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_plaintext(_ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
        seen.append(body)
        findings = [{"source": "hub-log", "canary": TOKEN, "encoding": "plain"}]
        return 200, {"findings": findings, "sources": {"hub-db-dump": 10, "hub-log": 5}, "sse_frames": 1}

    monkeypatch.setattr(plaintext, "post_plaintext", fake_plaintext)
    node.respond = vm_responder("")
    status, answer = call(ctx, "POST", "/checks/credentials", {"values": [API_KEY], "login_seeds": ["s"]})
    assert status == 200
    assert answer == {
        "findings": [{"source": f"{VM}:/srv/x", "value": 0}, {"source": "hub:hub-log", "value": 2}],
        "scanned": {VM: 7, "runner-device": 3, "hub:hub-db-dump": 10, "hub:hub-log": 5},
        "values": 3,
    }
    assert seen == [{"canaries": [API_KEY, KEY, TOKEN]}]
    host = next(argv for argv, _env in node.runs if argv[:4] == ["/agent/python", "-I", "-c", model.SCAN_SCRIPT])
    assert host[5:] == ["/scratch/runner-1", "/run-dir"]
    vm_scan = next(argv for argv, _env in node.runs if argv[:2] == ["limactl", "shell"] and "python3" in argv)
    assert vm_scan[-len(model.VM_ROOTS) :] == list(model.VM_ROOTS)
    assert all(API_KEY not in arg and KEY not in arg for argv, _env in node.runs for arg in argv)

    node.respond = vm_responder("", vm=failed(stderr="vm broke"))
    assert call(ctx, "POST", "/checks/credentials", {"values": [API_KEY]}) == (
        500,
        {"error": "failed", "detail": f"credential scan of {VM} exited with 1: vm broke"},
    )
    node.respond = vm_responder("", host=failed(stderr="disk gone"))
    assert call(ctx, "POST", "/checks/credentials", {"values": [API_KEY]}) == (
        500,
        {"error": "failed", "detail": "credential scan of the runner device exited with 1: disk gone"},
    )
    ctx.runners["runner-1"].env.pop(model.KEY_FILE)
    assert call(ctx, "POST", "/checks/credentials", {})[0] == 400


def test_credential_scan_skips_a_vm_deleted_meanwhile(
    ctx: Context, node: FakeNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plaintext, "post_plaintext", lambda *_a: (200, {"findings": [], "sources": {}}))
    respond: Callable[[list[str]], Result] = vm_responder("", vm=failed(rc=255, stderr="connection refused"))
    listed: list[int] = []

    def gone_after_first_list(argv: list[str]) -> Result:
        if argv[:2] == ["limactl", "list"]:
            listed.append(1)
            if len(listed) > 1:
                return ok("")
        return respond(argv)

    node.respond = gone_after_first_list
    answer = call(ctx, "POST", "/checks/credentials", {"values": [API_KEY]})[1]
    assert answer["scanned"] == {"runner-device": 3}


# ---------------------------------------------------------------------------------------------------- login routes


def test_login_doubles(ctx: Context, node: FakeNode) -> None:
    node.answer = lambda _m, url, _b: {"status": 201 if url.endswith("/accounts") else 200, "body": {}}
    body = {"seed": "s1", "junk": True, "fault": None, "account": {"user": "a@example.test", "password": "pw"}}
    status, answer = call(ctx, "POST", "/login/doubles", body)
    assert status == 200
    assert answer == {
        "pages": "https://127.0.0.1:21012",
        "imaps": {"host": "127.0.0.1", "port": 21018},
        "restarted": ["runner-1"],
    }
    names = [spawned[0] for spawned in node.spawned]
    assert names == ["double-mail", "double-authpage", "runner-1"]
    mail_argv, auth_argv = node.spawned[0][1], node.spawned[1][1]
    assert mail_argv[:3] == ["/agent/python", "-m", "agent_doubles.mail"]
    assert mail_argv[mail_argv.index("--ca-dir") + 1] == "/scratch/login-ca"
    assert auth_argv[auth_argv.index("--mail-from") + 1] == login.MAIL_FROM
    assert auth_argv[auth_argv.index("--smtp") + 1] == "127.0.0.1:21017"
    env = ctx.runners["runner-1"].env
    assert json.loads(env["AGENT_RUNNER_LOGIN_ARGV"])[-4:] == [
        "--auth-url",
        "https://127.0.0.1:21012",
        "--ca-file",
        "/scratch/login-ca/ca.pem",
    ]
    assert (env["AGENT_RUNNER_LOGIN_AUTOMATION"], env["SSL_CERT_FILE"]) == ("on", "/scratch/login-ca/ca.pem")
    posts = [(url.rsplit(":", 1)[1], payload) for _m, url, payload in node.https]
    assert posts == [
        ("21016/_double/accounts", {"user": "a@example.test", "password": "pw"}),
        ("21015/_double/scenario", {"seed": "s1", "junk": True, "fault": None}),
    ]
    assert set(ctx.doubles) == {"model", "mail", "authpage"}

    # Started once; the runners keep their environment.
    assert call(ctx, "POST", "/login/doubles", {"seed": "s2", "fault": "email_page"})[1]["restarted"] == []
    assert len(node.spawned) == 3
    assert node.https[-1][2] == {"seed": "s2", "junk": False, "fault": "email_page"}

    node.answer = lambda _m, _u, _b: {"status": 400, "body": "bad seed"}
    assert call(ctx, "POST", "/login/doubles", {"seed": ""}) == (
        400,
        {"error": "invalid", "detail": "authpage double refused /_double/scenario: bad seed"},
    )
    for bad in (
        None,
        {"seed": "s", "other": 1},
        {"seed": "s", "fault": "nope"},
        {"seed": "s", "account": "a"},
        {"seed": "s", "account": {"user": "a"}},
        {"seed": "s", "account": {"user": "a", "password": ""}},
    ):
        assert call(ctx, "POST", "/login/doubles", bad)[0] == 400


def test_login_doubles_restart_only_running_runners(ctx: Context, node: FakeNode) -> None:
    node.alive_names.discard("runner-1")
    assert call(ctx, "POST", "/login/doubles", {"seed": "s"})[1]["restarted"] == []
    assert ctx.runners["runner-1"].env["AGENT_RUNNER_LOGIN_AUTOMATION"] == "on"
    assert ctx.runners["runner-1"].env["AGENT_RUNNER_LOGIN_SANDBOX"] == "process"


def test_magic_link(ctx: Context, node: FakeNode) -> None:
    body = {"user": "a@example.test", "password": "pw", "folder": "Junk"}
    assert call(ctx, "POST", "/login/magic-link", body)[0] == 409
    ctx.doubles["mail"] = Double(node, "mail", 21016, 21016)
    node.respond = lambda _argv: ok(json.dumps({"messages": 1, "link": "https://127.0.0.1:1/magic-link#t"}))
    assert call(ctx, "POST", "/login/magic-link", body) == (
        200,
        {"messages": 1, "link": "https://127.0.0.1:1/magic-link#t"},
    )
    argv, env = node.runs[-1]
    assert argv[4:] == ["127.0.0.1", "21019", "a@example.test", "Junk"]
    assert env == {"PATH": "/usr/bin:/bin", "E2E_MAILBOX_PASSWORD": "pw"}
    assert "pw" not in argv
    node.respond = lambda _argv: failed(stderr="login refused")
    assert call(ctx, "POST", "/login/magic-link", body)[0] == 500
    for bad in (None, {**body, "folder": "Spam"}, {**body, "user": ""}, {**body, "password": 1}):
        assert call(ctx, "POST", "/login/magic-link", bad)[0] == 400


def test_magic_script_reads_the_newest_link() -> None:
    compile(login.MAGIC_SCRIPT, "magic", "exec")
    assert "https://[A-Za-z0-9.:-]+/magic-link#" in login.MAGIC_SCRIPT


def test_login_dirs(ctx: Context, node: FakeNode) -> None:
    node.respond = lambda _argv: ok("login-01\nlogin-02\n")
    assert call(ctx, "GET", "/runners/runner-1/login-dirs") == (200, {"dirs": ["login-01", "login-02"]})
    assert node.runs[-1][0][-1] == "/scratch/runner-1"
    node.respond = lambda _argv: failed()
    assert call(ctx, "GET", "/runners/runner-1/login-dirs")[0] == 500
    assert call(ctx, "GET", "/runners/runner-9/login-dirs")[0] == 404


def test_login_dirs_script(tmp_path: Path) -> None:
    script = 'for d in "$1"/tmp/login-*; do [ -e "$d" ] && echo "${d##*/}"; done; true'
    empty = subprocess.run(["/bin/sh", "-c", script, "sh", str(tmp_path)], capture_output=True, text=True, check=True)
    assert empty.stdout == ""
    (tmp_path / "tmp" / "login-abc").mkdir(parents=True)
    found = subprocess.run(["/bin/sh", "-c", script, "sh", str(tmp_path)], capture_output=True, text=True, check=True)
    assert found.stdout == "login-abc\n"


# ---------------------------------------------------------------------------------------------------- quota route


def test_quota_usage(ctx: Context, node: FakeNode) -> None:
    node.respond = lambda argv: token_run(argv) or failed()
    body = {"name": "q", "document": {"five_hour": {}}, "login_seed": "s1"}
    status, answer = call(ctx, "POST", "/quota/usage", body)
    assert (status, answer) == (200, {"url": "http://127.0.0.1:21011/api/oauth/usage", "restarted": ["runner-1"]})
    name, argv, _env, cwd = node.spawned[0]
    assert (name, argv[:3], cwd) == (
        "double-usage",
        ["/agent/python", "-m", "orchestrator.usage_double"],
        "/core/e2e/platform",
    )
    assert node.https[-1][2] == {"name": "q", "document": {"five_hour": {}}, "token": TOKEN}
    assert ctx.runners["runner-1"].env[quota.VARIABLE] == answer["url"]
    assert call(ctx, "POST", "/quota/usage", {"name": "q2", "token_prefix": "sk-"})[1]["restarted"] == []
    assert node.https[-1][2] == {"name": "q2", "token_prefix": "sk-"}
    assert len(node.spawned) == 2
    assert call(ctx, "POST", "/quota/usage", {"name": "q", "login_seed": 5})[0] == 400
    assert call(ctx, "POST", "/quota/usage", [])[0] == 400
    node.answer = lambda _m, _u, _b: {"status": 400, "body": {"error": "invalid"}}
    assert call(ctx, "POST", "/quota/usage", {"name": "q"})[0] == 400


# ---------------------------------------------------------------------------------------------------- live bridge


class Upstream(BaseHTTPRequestHandler):
    """A stand-in for the provider: echoes what arrived, streams a long body for `/stream`."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        seen = {"path": self.path, "body": self.rfile.read(length).decode(), "headers": dict(self.headers.items())}
        data = json.dumps(seen).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Generation-Id", "gen-1")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(3):
            chunk = b"event: ping\ndata: {}\n\n" * 5000
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def provider() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_parse_upstream() -> None:
    upstream = live_bridge.parse_upstream("openrouter=https://openrouter.ai/api/")
    assert upstream == live_bridge.Upstream("openrouter", "openrouter.ai", 443, "/api")
    assert live_bridge.parse_upstream("anthropic=https://api.anthropic.com:8443").port == 8443
    for bad in (
        "openrouter",
        "1x=https://openrouter.ai",
        "x=http://openrouter.ai",
        "x=https://example.com",
        "x=https://openrouter.ai/api?a=1",
        "x=https://user@openrouter.ai",
    ):
        with pytest.raises(live_bridge.BridgeError):
            live_bridge.parse_upstream(bad)


def test_bridge_forwards_and_streams(tmp_path: Path, provider: int) -> None:
    upstreams = {"openrouter": live_bridge.Upstream("openrouter", "openrouter.ai", 443, "/api")}
    connect = lambda _upstream: http.client.HTTPConnection("127.0.0.1", provider, timeout=10)  # noqa: E731
    journal = tmp_path / "bridge.jsonl"
    bridge = live_bridge.Bridge(upstreams, journal, connect)
    assert bridge.target("/live/other/v1") is None
    assert bridge.target("/_double/state") is None
    server = usage_double.serve(0, None, bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        headers = {"Authorization": "Bearer secret-key", "Accept-Encoding": "gzip", "Content-Type": "application/json"}
        request = urllib.request.Request(f"{url}/live/openrouter/v1/messages?x=1", b'{"m":1}', headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 201
            assert response.headers["X-Generation-Id"] == "gen-1"
            assert response.headers["Connection"] == "close"
            seen = json.loads(response.read())
        assert seen["path"] == "/api/v1/messages?x=1"
        assert seen["body"] == '{"m":1}'
        assert seen["headers"]["Authorization"] == "Bearer secret-key"
        assert seen["headers"]["Accept-Encoding"] == "identity"
        assert seen["headers"]["Host"] == f"127.0.0.1:{provider}"
        with urllib.request.urlopen(f"{url}/live/openrouter/stream", timeout=10) as response:
            body = response.read()
        assert body.count(b"event: ping") == 15000
        assert _request(f"{url}/_double/state")["body"] | {"failures": []} == {
            "bridged": 2,
            "upstreams": ["openrouter"],
            "double": "usage",
            "scenario": "idle",
            "requests": 0,
            "good_requests": 0,
            "failures": [],
            "done": False,
            "ok": False,
        }
    finally:
        server.shutdown()
        server.server_close()
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [(line["method"], line["path"], line["status"]) for line in lines] == [
        ("POST", "/api/v1/messages", 201),
        ("GET", "/api/stream", 200),
    ]
    assert lines[0]["request_bytes"] == 7
    assert "secret-key" not in journal.read_text()


def test_bridge_reports_an_unreachable_upstream() -> None:
    upstreams = {"anthropic": live_bridge.Upstream("anthropic", "api.anthropic.com", 443, "")}
    closed = lambda _upstream: http.client.HTTPConnection("127.0.0.1", 9, timeout=5)  # noqa: E731
    server = usage_double.serve(0, None, live_bridge.Bridge(upstreams, None, closed))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        answer = _request(f"{url}/live/anthropic/api/oauth/usage")
        assert answer == {"status": 502, "body": {"error": "upstream_unreachable", "detail": "ConnectionRefusedError"}}
        assert _request(f"{url}/live/anthropic/v1/messages", "POST", {"a": 1})["status"] == 502
        too_long = {"Content-Length": str(live_bridge.MAX_BODY + 1)}
        big = urllib.request.Request(f"{url}/live/anthropic/v1/messages", method="POST", headers=too_long)
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(big, timeout=5)
        assert refused.value.code == 413
        refused.value.close()
        assert _request(f"{url}/api/oauth/usage", "POST", {})["status"] == 404
    finally:
        server.shutdown()
        server.server_close()


class BrokenConnection:
    def request(self, *_args: Any, **_kw: Any) -> None:
        return

    def getresponse(self) -> http.client.HTTPResponse:
        raise http.client.BadStatusLine("garbage")

    def close(self) -> None:
        return


def test_bridge_reports_a_broken_upstream_answer() -> None:
    upstreams = {"openrouter": live_bridge.Upstream("openrouter", "openrouter.ai", 443, "/api")}
    server = usage_double.serve(0, None, live_bridge.Bridge(upstreams, None, lambda _u: BrokenConnection()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        answer = _request(f"http://127.0.0.1:{server.server_address[1]}/live/openrouter/v1/key")
        assert answer == {"status": 502, "body": {"error": "upstream_unreachable", "detail": "BadStatusLine"}}
    finally:
        server.shutdown()
        server.server_close()


def test_https_connection_uses_the_system_trust() -> None:
    connection = live_bridge.https_connection(live_bridge.Upstream("x", "openrouter.ai", 443, ""))
    assert isinstance(connection, http.client.HTTPSConnection)
    assert (connection.host, connection.port, connection.timeout) == ("openrouter.ai", 443, live_bridge.TIMEOUT_S)


def test_usage_double_main_with_live_upstreams(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    servers: list[Any] = []
    argv = ["--port", "0", "--journal", str(tmp_path / "u.jsonl"), "--live", "openrouter=https://openrouter.ai/api"]
    assert usage_double.main(argv, run=servers.append) == 0
    handler = servers[0].RequestHandlerClass
    assert handler is not None
    assert usage_double.main(["--port", "0", "--live", "anthropic=https://api.anthropic.com"], run=lambda _s: None) == 0
    with pytest.raises(SystemExit):
        usage_double.main(["--port", "0", "--live", "x=https://example.com"], run=lambda _s: None)
    assert "--live takes" in capsys.readouterr().err


def test_live_upstreams(ctx: Context, node: FakeNode, tmp_path: Path) -> None:
    assert call(ctx, "POST", "/live/upstreams", {"platform_key": KEY}) == (
        409,
        {"error": "invalid", "detail": "the live upstreams exist only in a live run (run.sh --live)"},
    )
    ctx.config = RunConfig(
        topology="t1",
        core_src=CORE,
        agent_src=tmp_path,
        artifacts=tmp_path / "a",
        port_base=21000,
        vm_prefix="wp-t-",
        live=True,
    )
    ctx.runners["runner-1"].env.update({"PATH": "/usr/bin", "HOME": "/home/t"})
    ctx.runners["runner-2"] = Runner(node, "runner-2", "/agent/python", {"AGENT_RUNNER_HOME": "/scratch/runner-2"})
    status, answer = call(ctx, "POST", "/live/upstreams", {"platform_key": KEY})
    assert status == 200
    assert answer == {"base": "http://127.0.0.1:21011/live", "restarted": ["runner-1"]}
    names = [spawned[0] for spawned in node.spawned]
    assert names == ["double-usage", "tunnels-live", "runner-1"]
    usage_argv = node.spawned[0][1]
    assert usage_argv[-8:] == [
        "--live",
        "openrouter=https://openrouter.ai/api",
        "--live",
        "anthropic=https://api.anthropic.com",
        "--live",
        f"{quota.github_name(ctx)}=https://api.github.com",
        "--live",
        "codeload=https://codeload.github.com",
    ]
    assert node.spawned[1][1][-4:] == ["--prefix", "wp-t-e2e-", "--port", "21011"]
    assert node.spawned[1][2] == {"PATH": "/usr/bin", "HOME": "/home/t"}
    key_write = next(env for argv, env in node.runs if argv[:2] == ["/bin/sh", "-c"])
    assert key_write == {"E2E_KEY": KEY, "PATH": "/usr/bin:/bin"}
    env = ctx.runners["runner-1"].env
    assert env["AGENT_RUNNER_MODEL_UPSTREAM_OPENROUTER"] == "http://127.0.0.1:21011/live/openrouter"
    assert env["AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC"] == "http://127.0.0.1:21011/live/anthropic"
    assert env[quota.VARIABLE] == "http://127.0.0.1:21011/live/anthropic/api/oauth/usage"

    # Again with a key: upstreams unchanged, the platform runner restarts for the new key; without a key nothing.
    status, answer = call(ctx, "POST", "/live/upstreams", {"platform_key": KEY})
    assert answer["restarted"] == ["runner-1"]
    assert call(ctx, "POST", "/live/upstreams", {"platform_key": None})[1]["restarted"] == []
    node.alive_names.discard("runner-1")
    assert call(ctx, "POST", "/live/upstreams", {"platform_key": KEY})[1]["restarted"] == []
    assert names == ["double-usage", "tunnels-live", "runner-1"]
    node.alive_names.discard("tunnels-live")
    call(ctx, "POST", "/live/upstreams", {"platform_key": None})
    assert [spawned[0] for spawned in node.spawned].count("tunnels-live") == 2  # an ended tunnel watcher is restarted
    for bad in (None, {"platform_key": 5}, {"platform_key": "short"}):
        assert call(ctx, "POST", "/live/upstreams", bad)[0] == 400
