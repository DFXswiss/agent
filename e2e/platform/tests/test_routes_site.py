"""`POST /site`, `GET /site`: the web site of the platform browser scenario (E2E-66, KOR-44)."""

from __future__ import annotations

import dataclasses
import http.client
import socket
from typing import Any

import pytest
from orchestrator.context import Context
from orchestrator.routes import site
from test_routes import call, ctx  # noqa: F401 - the shared context fixture


def free_base() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1]) - 18


@pytest.fixture
def live(ctx: Context) -> Any:  # noqa: F811
    ctx.config = dataclasses.replace(ctx.config, port_base=free_base())
    yield ctx
    found = site._site_of(ctx)
    if found is not None and found.server is not None:
        found.server.shutdown()
        found.server.server_close()


def request(port: int, method: str, path: str, body: str = "", cookie: str = "") -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    connection.request(method, path, body=body.encode() if body else None, headers=headers)
    response = connection.getresponse()
    answer = (response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    connection.close()
    return answer


def test_invalid_bodies_and_counters_before_start(ctx: Context) -> None:  # noqa: F811
    for body in [None, [], {}, {"password": "x"}, {"password": "", "report": "r"}, {"password": 1, "report": "r"}]:
        assert call(ctx, "POST", "/site", body)[0] == 400
    status, answer = call(ctx, "GET", "/site")
    assert (status, answer["error"]) == (409, "not_started")


def test_sign_in_and_download(live: Context) -> None:
    status, answer = call(live, "POST", "/site", {"password": "pw-1", "report": "a,b\n1,2\n"})
    port = live.config.port("site")
    assert (status, answer) == (
        200,
        {"url": f"http://host.lima.internal:{port}", "local_url": f"http://127.0.0.1:{port}"},
    )
    assert b'id="password"' in request(port, "GET", "/")[2]
    assert request(port, "GET", "/login")[0] == 200
    assert request(port, "GET", "/account")[1]["location"] == "/login"
    assert request(port, "GET", "/report.csv")[1]["location"] == "/login"
    assert request(port, "GET", "/report.csv", cookie="site=forged; other=1")[0] == 303
    assert request(port, "GET", "/nothing")[0] == 404
    assert request(port, "POST", "/other", "user=ada")[0] == 404
    failed = request(port, "POST", "/login", "user=ada&password=wrong")
    assert failed[0] == 200
    assert b"Wrong user or password" in failed[2]
    # reconfigured: the new password and report count
    assert call(live, "POST", "/site", {"password": "pw-2", "report": "x,y\n"})[0] == 200
    status, headers, _ = request(port, "POST", "/login", "user=ada&password=pw-2")
    assert (status, headers["location"]) == (303, "/account")
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert b"Signed in as ada" in request(port, "GET", "/account", cookie=cookie)[2]
    status, headers, body = request(port, "GET", "/report.csv?x=1", cookie=cookie)
    assert (status, body, headers["content-disposition"]) == (200, b"x,y\n", 'attachment; filename="report.csv"')
    assert call(live, "GET", "/site") == (200, {"logins": 1, "failed_logins": 1, "downloads": 1})
