"""Security headers and CSP on every response type (03-api.md section 1), delivery of the web build and the SPA
fallback (06-web.md sections 2.4, 10.1, 10.2), and the absence of the legacy routes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from agent_hub.auth.keys import KeyContext, ResolvedKey
from agent_hub.config import HubConfig, load_config
from agent_hub.http.headers import CSP
from agent_hub.http.routing import HubRouter
from agent_hub.static_routes import SPA_ROUTES, asset_path
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
EXPECTED_HEADERS = {
    "content-security-policy": EXPECTED_CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "publickey-credentials-get=(self), publickey-credentials-create=(self)",
}
INDEX_HTML = b'<!doctype html><html><head><script type="module" src="/assets/app-1a2b.js"></script></head></html>'

router = HubRouter()


@router.get("/v1/test/loose-csp", auth="anon", right="anon")
async def loose_csp() -> JSONResponse:
    return JSONResponse({}, headers={"Content-Security-Policy": "default-src *", "Cache-Control": "max-age=60"})


def failing_resolver(_context: KeyContext, _key_id: str) -> ResolvedKey | None:
    raise RuntimeError("resolver bug")


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_bytes(INDEX_HTML)
    (tmp_path / "assets" / "app-1a2b.js").write_text("console.log(1)\n")
    (tmp_path / "assets" / "app-3c4d.css").write_text("body{}\n")
    (tmp_path / "secret.txt").write_text("outside assets\n")
    return tmp_path


@pytest.fixture
def web(
    migrated_db: HubDatabase, static_dir: Path, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> Iterator[HubHarness]:
    config = load_config(hub_environ(migrated_db, AGENT_HUB_STATIC_DIR=str(static_dir)))
    resolvers = {"device.": failing_resolver}
    yield from start_hub(config, hub_pool, clock, routers=[("test", router)], resolvers=resolvers)


def assert_security_headers(response: httpx.Response) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


def test_csp_constant_is_the_specified_one() -> None:
    assert EXPECTED_CSP == CSP


def test_headers_on_every_response_type(web: HubHarness) -> None:
    responses = [
        web.anon.get("/v1/config"),
        web.anon.get("/v1/nothing"),
        web.anon.post("/v1/nothing"),
        web.anon.get("/assets/app-1a2b.js"),
        web.anon.get("/assets/missing.js"),
        web.anon.get("/"),
        web.anon.get("/o/123/unknown/screen"),
        web.anon.request("HEAD", "/v1/config"),
        web.anon.post("/v1/nothing", content=b"x" * (1024 * 1024 + 1)),
        web.base.get("/v1/config", headers={"Agent-Key": "x"}),
        web.root.get("/v1/config"),
        web.anon.get("/v1/test/loose-csp"),
    ]
    for response in responses:
        assert_security_headers(response)


def test_route_headers_cannot_weaken_the_policy(web: HubHarness) -> None:
    response = web.anon.get("/v1/test/loose-csp")
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["cache-control"] == "max-age=60"


def test_api_responses_are_not_stored(web: HubHarness) -> None:
    assert web.anon.get("/v1/config").headers["cache-control"] == "no-store"
    assert web.anon.get("/v1/nothing").headers["cache-control"] == "no-store"


def test_failure_below_the_pipeline_is_unavailable_with_headers(
    web: HubHarness, caplog: pytest.LogCaptureFixture
) -> None:
    response = web.client(web.keys.device()).get("/v1/config")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
    assert "resolver bug" not in response.text
    assert_security_headers(response)
    assert "failed before a response" in caplog.text
    head = web.base.request("HEAD", "/v1/config", headers=dict(response.request.headers))
    assert head.status_code == 503
    assert head.content == b""


def test_assets_are_immutable(web: HubHarness) -> None:
    response = web.anon.get("/assets/app-1a2b.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)\n"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"].startswith("text/javascript")
    assert web.anon.get("/assets/app-3c4d.css").headers["content-type"].startswith("text/css")


@pytest.mark.parametrize(
    "path",
    ["/assets/missing.js", "/assets/../secret.txt", "/assets/%2e%2e/secret.txt", "/assets/..%2fsecret.txt", "/assets/"],
)
def test_assets_outside_or_missing(web: HubHarness, path: str) -> None:
    response = web.base.get(path)
    assert response.status_code == 404
    assert "outside assets" not in response.text
    if response.request.url.path.startswith("/assets/"):
        assert response.json() == {"error": "not_found"}


def test_asset_path_stays_inside(static_dir: Path) -> None:
    assert asset_path(static_dir, "app-1a2b.js") == (static_dir / "assets" / "app-1a2b.js").resolve()
    assert asset_path(static_dir, "../secret.txt") is None
    assert asset_path(static_dir, "/etc/hosts") is None
    assert asset_path(static_dir, "") is None


def concrete(path: str) -> str:
    return (
        path.replace("{org}", "0192f3a0-0000-7000-8000-000000000001")
        .replace("{wg}", "wg1")
        .replace("{task}", "task1")
        .replace("{session}", "s1")
    )


@pytest.mark.parametrize("path", SPA_ROUTES)
def test_every_spa_route_serves_the_page(web: HubHarness, path: str) -> None:
    response = web.anon.get(concrete(path))
    assert response.status_code == 200
    assert response.content == INDEX_HTML
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_spa_routes_are_the_routes_of_the_web_spec() -> None:
    assert len(SPA_ROUTES) == 20
    assert "/invite" in SPA_ROUTES
    assert "/o/{org}/s/{session}" in SPA_ROUTES


@pytest.mark.parametrize("path", ["/o", "/unknown", "/o/1/t/2/extra", "/assets", "/favicon.ico"])
def test_other_paths_serve_the_page_with_404(web: HubHarness, path: str) -> None:
    response = web.anon.get(path)
    assert response.status_code == 404
    assert response.content == INDEX_HTML
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/state"),
        ("GET", "/api/key/whoami"),
        ("POST", "/sync/push"),
        ("GET", "/auth/me"),
        ("GET", "/auth/login"),
        ("GET", "/pair"),
        ("POST", "/pair/claim"),
        ("GET", "/health"),
        ("GET", "/v1/nothing"),
    ],
)
def test_legacy_routes_do_not_exist(web: HubHarness, method: str, path: str) -> None:
    response = web.anon.request(method, path)
    assert response.status_code == 404
    if method == "GET" and not path.startswith("/v1/"):
        assert response.content == INDEX_HTML
    else:
        assert response.json() == {"error": "not_found"}


def test_without_a_build_the_page_is_unavailable(
    migrated_db: HubDatabase, tmp_path: Path, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> None:
    # An empty static directory (only `.keep`), independent of a web build in the package directory (WEB-01).
    (tmp_path / ".keep").write_text("")
    config = load_config(hub_environ(migrated_db, AGENT_HUB_STATIC_DIR=str(tmp_path)))
    for hub in start_hub(config, hub_pool, clock):
        response = hub.anon.get("/login")
        assert response.status_code == 503
        assert response.json() == {"error": "unavailable"}
        assert hub.anon.get("/unknown").status_code == 503


def test_static_dir_of_the_config(migrated_db: HubDatabase, static_dir: Path) -> None:
    config: HubConfig = load_config(hub_environ(migrated_db, AGENT_HUB_STATIC_DIR=str(static_dir)))
    assert config.static_dir == static_dir.resolve()
