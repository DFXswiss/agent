"""U-GST-control (API process): port 7700 server, routing, registry, configuration, standard-library rule."""

from __future__ import annotations

import ast
import http.client
import importlib
import io
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import config as config_module
from agent_guest import registry
from agent_guest import server as server_module
from agent_guest.apisock import ApiClient
from agent_guest.config import DEFAULTS, ConfigError, GuestConfig, load_config
from agent_guest.routing import (
    BodyReader,
    HttpError,
    Request,
    Response,
    Route,
    Router,
    compile_path,
    error_response,
    json_response,
)
from agent_guest.schema import SchemaSet
from agent_guest.server import ApiContext, ApiHttpServer, create_server, main


def get(port: int, method: str, target: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


# ---------------------------------------------------------------- API process


def test_health_through_the_control_process(api_port: int, api_server: ApiHttpServer) -> None:
    status, headers, body = get(api_port, "GET", "/v1/health")
    assert (status, json.loads(body)) == (200, {"state": "ok", "guest_version": "0.8.0"})
    assert headers["Content-Type"] == "application/json"
    assert api_server.server_address[0] == "127.0.0.1"


def test_health_without_control(guest_config: GuestConfig, short_tmp: Path) -> None:
    server = create_server(replace(guest_config, api_port=0, api_socket=str(short_tmp / "gone.sock")))
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        status, _, body = get(server.server_address[1], "GET", "/v1/health")
        assert (status, json.loads(body)) == (503, {"error": "unavailable"})
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(("method", "target"), [("GET", "/"), ("GET", "/mcp/x"), ("POST", "/v1/health"), ("PUT", "/x")])
def test_unknown_routes(api_port: int, method: str, target: str) -> None:
    status, _, body = get(api_port, method, target, body=b"" if method != "GET" else None)
    assert (status, json.loads(body)) == (404, {"error": "not_found"})


def slow(context: Any, request: Request) -> Response:
    time.sleep(1)
    return json_response({"late": True})


RELEASE = threading.Event()
FINISHED = threading.Event()


def blocked(context: Any, request: Request) -> Response:
    """Holds its handler until the test releases it (at most 30 s), so the answer before it is not a matter of time."""
    RELEASE.wait(30)
    FINISHED.set()
    return json_response({"late": True})


def crash(context: Any, request: Request) -> Response:
    raise RuntimeError("internal detail")


def refuse(context: Any, request: Request) -> Response:
    raise HttpError("id_conflict", detail=["/a"])


def echo(context: Any, request: Request) -> Response:
    return json_response({"params": request.params, "query": request.query, "data": request.data})


def raw(context: Any, request: Request) -> Response:
    assert request.stream is not None
    first = request.stream.read(2)
    if request.query.get("all") == "1":
        rest = request.stream.read()
        return json_response({"first": first.decode(), "rest": rest.decode()})
    return json_response({"first": first.decode()})


def chunked(context: Any, request: Request) -> Response:
    return Response(body=iter([b"ab", b"", b"cd"]), content_type="text/plain", headers={"X-Test": "1"})


def chunked_close(context: Any, request: Request) -> Response:
    assert request.stream is not None
    return Response(body=iter([b"x"]), content_type="text/plain")


TEST_ROUTES = [
    Route("GET", "/slow", slow, body="none"),
    Route("GET", "/slow-allowed", slow, body="none", timeout=3),
    Route("GET", "/blocked", blocked, body="none"),
    Route("GET", "/crash", crash, body="none"),
    Route("POST", "/refuse", refuse),
    Route("POST", "/items/{id}", echo, path_params={"id": "common.json#/$defs/uuid"}),
    Route("POST", "/model/*", echo),
    Route("POST", "/raw", raw, body="raw", max_body=10),
    Route("GET", "/chunked", chunked, body="none"),
    Route("POST", "/chunked", chunked_close, body="raw"),
]


@pytest.fixture
def lab_port(guest_config: GuestConfig, schemas: SchemaSet, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    context = ApiContext(config=guest_config, schemas=schemas, control=ApiClient("/nonexistent"))
    server = ApiHttpServer(replace(guest_config, api_port=0), Router(TEST_ROUTES), context)
    server.handler_timeout = 0.3
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_handler_timeout(lab_port: int) -> None:
    # the answer comes while the handler still runs (it ends only when released below)
    RELEASE.clear()
    FINISHED.clear()
    started = time.monotonic()
    try:
        status, headers, body = get(lab_port, "GET", "/blocked")
        assert not FINISHED.is_set()
        # the lab's 0.3 s limit, not the default of 10 s (a margin a loaded device cannot use up)
        assert time.monotonic() - started < 5
    finally:
        RELEASE.set()
    assert (status, json.loads(body)) == (503, {"error": "unavailable"})
    assert headers["Connection"] == "close"


def test_route_timeout_overrides_the_default(lab_port: int) -> None:
    status, _, body = get(lab_port, "GET", "/slow-allowed")
    assert (status, json.loads(body)) == (200, {"late": True})


def test_handler_slots_are_bounded(
    guest_config: GuestConfig, schemas: SchemaSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "MAX_HANDLERS", 1)
    context = ApiContext(config=guest_config, schemas=schemas, control=ApiClient("/nonexistent"))
    server = ApiHttpServer(replace(guest_config, api_port=0), Router(TEST_ROUTES), context)
    server.handler_timeout = 0.2
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert get(port, "GET", "/slow")[0] == 503
        status, _, body = get(port, "GET", "/chunked")
        assert (status, json.loads(body)) == (503, {"error": "unavailable"})
        # the slot is free again once the slow handler (1 s) has ended
        started = time.monotonic()
        while (served := get(port, "GET", "/chunked")[0]) != 200 and time.monotonic() - started < 30:
            time.sleep(0.05)
        assert served == 200
    finally:
        server.shutdown()
        server.server_close()


def test_connections_are_bounded(
    guest_config: GuestConfig, schemas: SchemaSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "MAX_CONNECTIONS", 1)
    context = ApiContext(config=guest_config, schemas=schemas, control=ApiClient("/nonexistent"))
    server = ApiHttpServer(replace(guest_config, api_port=0), Router(TEST_ROUTES), context)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as held:
            held.sendall(b"GET /chunked HTTP/1.1\r\nHost: x\r\n\r\n")
            assert held.recv(12) == b"HTTP/1.1 200"
            with socket.create_connection(("127.0.0.1", port), timeout=5) as refused:
                try:
                    refused.sendall(b"GET /chunked HTTP/1.1\r\nHost: x\r\n\r\n")
                    answer = refused.recv(100)
                except ConnectionResetError:
                    answer = b""
                assert answer == b""
        # The slot of the held connection is released by its server thread; poll until a new connection is served.
        served = False
        deadline = time.monotonic() + 5
        while not served and time.monotonic() < deadline:
            try:
                served = get(port, "GET", "/chunked")[0] == 200
            except (OSError, http.client.HTTPException):
                time.sleep(0.05)
        assert served
    finally:
        server.shutdown()
        server.server_close()


def test_handler_crash_leaks_nothing(lab_port: int, capsys: pytest.CaptureFixture[str]) -> None:
    status, _, body = get(lab_port, "GET", "/crash")
    assert (status, json.loads(body)) == (503, {"error": "unavailable"})
    assert "internal detail" in capsys.readouterr().err


def test_handler_error_with_detail(lab_port: int) -> None:
    status, _, body = get(lab_port, "POST", "/refuse", body=b"{}")
    assert (status, json.loads(body)) == (409, {"error": "id_conflict", "detail": {"pointers": ["/a"]}})


def test_path_params_and_prefix_routes(lab_port: int) -> None:
    item = "01a1ee56-5e00-7003-8f1b-340691d99f03"
    status, _, body = get(lab_port, "POST", f"/items/{item}?a=1&b=", body=b'{"k": [1]}')
    assert (status, json.loads(body)) == (
        200,
        {"params": {"id": item}, "query": {"a": "1", "b": ""}, "data": {"k": [1]}},
    )
    assert get(lab_port, "POST", "/items/nope", body=b"{}")[0] == 404
    assert get(lab_port, "POST", f"/items/{item}/more", body=b"{}")[0] == 404
    status, _, body = get(lab_port, "POST", "/model/v1/messages", body=b"1")
    assert json.loads(body)["params"] == {"rest": "v1/messages"}


def test_raw_bodies(lab_port: int) -> None:
    status, _, body = get(lab_port, "POST", "/raw?all=1", body=b"abcdef")
    assert (status, json.loads(body)) == (200, {"first": "ab", "rest": "cdef"})
    status, headers, body = get(lab_port, "POST", "/raw", body=b"abcdef")
    assert (status, json.loads(body)) == (200, {"first": "ab"})
    assert headers["Connection"] == "close"
    assert get(lab_port, "POST", "/raw", body=b"x" * 11)[0] == 413


def test_chunked_responses(lab_port: int) -> None:
    status, headers, body = get(lab_port, "GET", "/chunked")
    assert (status, body, headers["X-Test"], headers["Transfer-Encoding"]) == (200, b"abcd", "1", "chunked")
    status, headers, body = get(lab_port, "POST", "/chunked", body=b"unread")
    assert (status, body, headers["Connection"]) == (200, b"x", "close")


def test_body_reader_stops_at_its_length() -> None:
    reader = BodyReader(io.BytesIO(b"abcdef"), 4)
    assert reader.read(3) == b"abc"
    assert reader.read(10) == b"d"
    assert reader.read() == b""
    short = BodyReader(io.BytesIO(b"ab"), 4)
    assert short.read(2) == b"ab"
    with pytest.raises(HttpError) as error:
        short.read()
    assert error.value.code == "request_invalid"
    assert short.remaining == 0


def test_routing_helpers() -> None:
    assert compile_path("/v1/sessions/{id}/prompts").fullmatch("/v1/sessions/abc/prompts")
    assert compile_path("/hooks/*").fullmatch("/hooks/PreToolUse")
    assert not compile_path("/v1/x").fullmatch("/v1/xy")
    with pytest.raises(ValueError, match="duplicate route"):
        Router([Route("GET", "/a", echo), Route("GET", "/a", echo)])
    router = Router([Route("GET", "/a", echo), Route("POST", "/a", echo)])
    assert [route.method for route in router.routes] == ["GET", "POST"]
    assert router.match("PUT", "/a") is None
    assert HttpError("unavailable").status == 503
    assert HttpError("x", 418).status == 418
    body = error_response(HttpError("not_found", detail=["/a"] * 40)).body
    assert isinstance(body, bytes)
    assert json.loads(body)["detail"]["pointers"] == ["/a"] * 32


def test_api_main_runs_until_sigterm(guest_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[ApiHttpServer] = []
    original = server_module.create_server

    def capture(config: GuestConfig) -> ApiHttpServer:
        created.append(original(config))
        return created[0]

    monkeypatch.setattr(server_module, "create_server", capture)

    def stop_when_ready() -> None:
        deadline = time.monotonic() + 10
        while not created and time.monotonic() < deadline:
            time.sleep(0.02)
        port = created[0].server_address[1]
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=stop_when_ready, daemon=True)
    thread.start()
    previous = signal.getsignal(signal.SIGTERM)
    assert main([], guest_env) == 0
    thread.join(10)
    assert signal.getsignal(signal.SIGTERM) is previous


def test_api_main_refusals(
    guest_env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["x"], guest_env) == 2
    assert main([], {**guest_env, "AGENT_GUEST_API_PORT": "x"}) == 1
    assert capsys.readouterr().err.endswith("agent-guest-api: config_invalid AGENT_GUEST_API_PORT\n")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert main([], {**guest_env, "AGENT_GUEST_API_PORT": str(port)}) == 1
    assert "agent-guest-api: os_error" in capsys.readouterr().err

    def broken() -> list[Route]:
        raise registry.RegistryError("registry_invalid", "x")

    monkeypatch.setattr(registry, "api_routes", broken)
    assert main([], guest_env) == 1
    assert capsys.readouterr().err == "agent-guest-api: registry_invalid x\n"


# ---------------------------------------------------------------- registry


def make_package(root: Path, name: str, modules: dict[str, str]) -> str:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub").mkdir()
    (package / "sub" / "__init__.py").write_text("raise RuntimeError('sub-packages are not visited')", encoding="utf-8")
    for module, source in modules.items():
        (package / f"{module}.py").write_text(source, encoding="utf-8")
    importlib.invalidate_caches()  # the path finder caches directory listings (coarse mtime on some filesystems)
    return name


DUPLICATE = "ROUTES = [Route('GET', '/a', h)]\n"
ROUTE_SOURCE = (
    "from agent_guest.routing import Route, json_response\ndef h(context, request):\n    return json_response({})\n"
)


@pytest.fixture
def packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def test_registry_discovers_in_sorted_order(packages: Path) -> None:
    name = make_package(
        packages,
        "reg_ok",
        {
            "b": ROUTE_SOURCE + "ROUTES = [Route('GET', '/b', h)]\nAPI_OPS = {'b.op': h}\n",
            "a": ROUTE_SOURCE + "ROUTES = (Route('GET', '/a', h),)\n",
            "c": "API_OPS = {}\n",
        },
    )
    assert [route.path for route in registry.control_routes(name)] == ["/a", "/b"]
    assert list(registry.api_ops(name)) == ["b.op"]
    tools = make_package(packages, "reg_tools", {"z": "TOOL = 'tool-z'\n", "y": "TOOL = 'tool-y'\n"})
    assert registry.mcp_tools(tools) == {"y": "tool-y", "z": "tool-z"}
    assert list(registry.mcp_tools(tools)) == ["y", "z"]
    addons = make_package(packages, "reg_addons", {"one": "ADDON = 1\n"})
    assert registry.hook_addons(addons) == {"one": 1}


@pytest.mark.parametrize(
    ("modules", "call", "code"),
    [
        ({"a": "X = 1\n"}, "control_routes", "registry_invalid"),
        ({"a": "ROUTES = ['x']\n"}, "control_routes", "registry_invalid"),
        ({"a": "ROUTES = 'x'\n"}, "control_routes", "registry_invalid"),
        (
            {
                "a": ROUTE_SOURCE + "ROUTES = [Route('GET', '/a', h)]\n",
                "b": ROUTE_SOURCE + "ROUTES = [Route('GET', '/a', h)]\n",
            },
            "control_routes",
            "registry_duplicate",
        ),
        ({"a": "API_OPS = [1]\n"}, "api_ops", "registry_invalid"),
        ({"a": "API_OPS = {1: print}\n"}, "api_ops", "registry_invalid"),
        ({"a": "API_OPS = {'x': 1}\n"}, "api_ops", "registry_invalid"),
        ({"a": "API_OPS = {'x': print}\n", "b": "API_OPS = {'x': print}\n"}, "api_ops", "registry_duplicate"),
        ({"a": "X = 1\n"}, "api_routes", "registry_invalid"),
        ({"a": "ROUTES = [1]\n"}, "api_routes", "registry_invalid"),
        ({"a": "X = 1\n"}, "mcp_tools", "registry_invalid"),
        ({"a": "X = 1\n"}, "hook_addons", "registry_invalid"),
    ],
)
def test_registry_refusals(packages: Path, modules: dict[str, str], call: str, code: str) -> None:
    name = make_package(packages, f"reg_{call}_{abs(hash(json.dumps(modules))) % 10**8}", modules)
    with pytest.raises(registry.RegistryError) as error:
        getattr(registry, call)(name)
    assert error.value.code == code


def test_registry_of_this_package() -> None:
    paths = [(route.method, route.path) for route in registry.control_routes()]
    assert ("GET", "/v1/health") in paths
    assert ("POST", "/v1/pending/{id}/answer") in paths
    assert {"journal.append", "pending.create", "pending.wait"} <= set(registry.api_ops())
    assert ("GET", "/v1/health") in [(route.method, route.path) for route in registry.api_routes()]
    assert isinstance(registry.mcp_tools(), dict)
    assert isinstance(registry.hook_addons(), dict)


def test_control_routes_match_the_contract(contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "guest" / "INDEX.json").read_text(encoding="utf-8"))
    contract = {(row["method"], row["path"]): row for row in index["endpoints"]}
    for route in registry.control_routes():
        row = contract[(route.method, route.path)]
        assert route.request_schema == row["request"]
        # request_kind of contracts/guest/INDEX.json: query -> no body, query+tar -> raw tar body, body -> JSON
        assert {"query": "none", "query+tar": "raw"}.get(row["request_kind"], "json") == route.body


# ---------------------------------------------------------------- configuration


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("AGENT_GUEST_"):
            monkeypatch.delenv(key)
    config = load_config()
    assert config.control_socket == "/run/agent-guest/control.sock"
    assert config.api_socket == "/run/agent-guest/api.sock"
    assert (config.control_group, config.api_group) == ("agent-ctl", "platform")
    assert config.control_token_file == "/etc/agent-guest/control-token"
    assert config.journal == "/var/lib/agent-guest/journal.db"
    assert (config.api_host, config.api_port) == ("127.0.0.1", 7700)
    assert config.build_file == "/opt/agent-guest/build.json"
    assert config.contracts == str(Path(registry.__file__).resolve().parent.parent / "contracts")
    assert config.tools_seconds == 25
    assert (config.orch_socket, config.orch_group) == ("/run/agent-guest/orch.sock", "agent-orch")
    assert config.orch_mcp_socket == "/run/agent-orch/mcp.sock"
    assert load_config({}) == config
    assert load_config({"AGENT_GUEST_TOOLS_SECONDS": "30"}).tools_seconds == 30


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TYPO", "1"),
        ("JOURNAL", "relative.db"),
        ("CONTROL_SOCKET", "/run/x\x00"),
        ("API_SOCKET", "/run/x\ny"),
        ("CONTROL_GROUP", "Bad"),
        ("API_GROUP", ""),
        ("API_PORT", "65536"),
        ("API_PORT", "07700"),
        ("API_PORT", "-1"),
        ("TOOLS_SECONDS", "0"),
        ("TOOLS_SECONDS", "31"),
        ("TOOLS_SECONDS", "07"),
        ("TOOLS_SECONDS", "1.5"),
    ],
)
def test_config_refusals(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_config({f"AGENT_GUEST_{name}": value})
    assert error.value.name == name
    assert str(error.value) == f"config_invalid AGENT_GUEST_{name}"


def test_config_names_are_documented() -> None:
    for name in DEFAULTS:
        assert f"AGENT_GUEST_{name}" in (config_module.__doc__ or "")


# ---------------------------------------------------------------- standard library only


def test_guest_imports_only_the_standard_library(repo_root: Path) -> None:
    allowed = set(sys.stdlib_module_names) | {"agent_guest", "__future__"}
    offenders = []
    for path in sorted((repo_root / "src" / "agent_guest").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            offenders += [f"{path.name}: {name}" for name in names if name.split(".")[0] not in allowed]
    assert offenders == []
