"""Control API: route matching, discovery, dispatch, the HTTP server."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from orchestrator import control
from orchestrator.control import ControlServer, Route, RouteError, discover, dispatch, not_available


def handler(ctx: Any, params: dict[str, str], body: Any) -> tuple[int, Any]:
    if params.get("name") == "boom":
        raise ValueError("kaput")
    if params.get("name") == "later":
        raise not_available("the thing", "PKG-01")
    return 200, {"params": params, "body": body, "ctx": ctx}


ROUTES = [Route("GET", "/things/{name}", handler), Route("POST", "/things", handler)]


def test_match_and_dispatch() -> None:
    assert ROUTES[0].match("GET", "/things/a-1.b") == {"name": "a-1.b"}
    assert ROUTES[0].match("POST", "/things/a") is None
    assert ROUTES[0].match("GET", "/things/a/b") is None
    assert dispatch(ROUTES, "c", "GET", "/things/x", None) == (200, {"params": {"name": "x"}, "body": None, "ctx": "c"})
    assert dispatch(ROUTES, "c", "GET", "/things/boom", None) == (
        500,
        {"error": "failed", "detail": "ValueError: kaput"},
    )
    status, payload = dispatch(ROUTES, "c", "GET", "/things/later", None)
    assert status == 501
    assert payload == {
        "error": "not_available",
        "detail": "the thing is not available yet (arrives with PKG-01)",
    }
    assert dispatch(ROUTES, "c", "GET", "/nothing", None)[0] == 404
    assert RouteError(400, "invalid", "x").code == "invalid"


def make_package(tmp_path: Path, name: str, modules: dict[str, str]) -> str:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for module, text in modules.items():
        (pkg / f"{module}.py").write_text(text)
    return name


def test_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    real = discover()
    assert {(r.method, r.pattern) for r in real} >= {("GET", "/state"), ("POST", "/links/{name}/cut")}
    route = "from orchestrator.control import Route\nROUTES = [Route('GET', '/x', lambda c, p, b: (200, {}))]\n"
    with pytest.raises(ValueError, match="GET /x declared by a and b"):
        discover(make_package(tmp_path, "dup_routes", {"a": route, "b": route}))
    with pytest.raises(TypeError, match=r"bad_routes\.a\.ROUTES must be a list of Route"):
        discover(make_package(tmp_path, "bad_routes", {"a": "ROUTES = ['x']\n"}))
    with pytest.raises(TypeError, match=r"none_routes\.a\.ROUTES"):
        discover(make_package(tmp_path, "none_routes", {"a": "X = 1\n"}))
    sys.modules.pop("dup_routes", None)


def request(url: str, method: str = "GET", data: bytes | None = None, token: str = "t0k") -> tuple[int, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_server(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ControlServer("ctx", 0, "t0k", ROUTES)
    server.start()
    try:
        assert request(f"{server.url}/things/a", token="wrong")[0] == 401
        assert request(f"{server.url}/things/a?q=1")[1]["params"] == {"name": "a"}
        assert request(f"{server.url}/things", "POST", b'{"k": 2}')[1]["body"] == {"k": 2}
        assert request(f"{server.url}/things", "POST", b"{oops") == (
            400,
            {"error": "invalid", "detail": "body is not JSON"},
        )
        monkeypatch.setattr(control, "MAX_BODY", 2)
        assert request(f"{server.url}/things", "POST", b'{"k": 2}')[0] == 413
    finally:
        server.stop()
    default = ControlServer("ctx", 0, "t0k")
    default.stop()
