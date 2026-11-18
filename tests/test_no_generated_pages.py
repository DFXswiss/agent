from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from agent_core.app import STATIC
from tests.conftest import sign_in

GENERATED_PATHS = (
    "/docs",
    "/docs/",
    "/redoc",
    "/redoc/",
    "/docs/oauth2-redirect",
    "/openapi.json",
)
# Answers text/event-stream and never ends for a signed-in client, so the page walk skips it.
ENDLESS_STREAMS = frozenset({"/api/stream"})
REQUIRED_PAGES = frozenset({"/", "/session", "/pair"})

_URL = r"(https?://[^\s\"'`<>()]+)"
# An absolute URL in a position from which a browser loads or calls it.
REFERENCE_POSITIONS = (
    re.compile(r"\b(?:src|href)\s*=\s*[\"'`]?\s*" + _URL, re.IGNORECASE),
    re.compile(r"@import\s+(?:url\(\s*)?[\"']?\s*" + _URL, re.IGNORECASE),
    re.compile(r"\burl\(\s*[\"']?\s*" + _URL, re.IGNORECASE),
    re.compile(r"\b(?:fetch|import)\(\s*[\"'`]\s*" + _URL),
)


def foreign_references(text: str, own_host: str | None) -> list[str]:
    """Every absolute URL in a reference position whose host is not own_host.

    With own_host None every absolute URL in such a position counts as foreign.
    """
    found: list[str] = []
    for pattern in REFERENCE_POSITIONS:
        for match in pattern.finditer(text):
            url = match.group(1)
            if own_host is None or urlsplit(url).hostname != own_host.lower():
                found.append(url)
    return found


def _own_host(client: TestClient) -> str:
    host = urlsplit(client.app.state.hub.cfg.public_url).hostname
    assert isinstance(host, str) and host != ""
    return host


def _get_routes_without_parameters(app: FastAPI) -> list[str]:
    paths: list[str] = []
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        if route.param_convertors:
            continue
        if route.methods is not None and "GET" not in route.methods:
            continue
        paths.append(route.path)
    return paths


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", GENERATED_PATHS)
def test_generated_page_is_not_served_signed_out(hub: TestClient, method: str, path: str) -> None:
    r = hub.request(method, path, follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", GENERATED_PATHS)
def test_generated_page_is_not_served_signed_in(hub: TestClient, method: str, path: str) -> None:
    sign_in(hub, "code-alice")
    assert hub.get("/auth/me").status_code == 200
    r = hub.request(method, path, follow_redirects=False)
    assert r.status_code == 404


def test_no_route_for_generated_pages(hub: TestClient) -> None:
    paths = {route.path for route in hub.app.routes}
    assert "/" in paths
    assert paths.isdisjoint(GENERATED_PATHS)


def test_reference_check_catches_the_framework_pages() -> None:
    # A default application serves the pages the hub turns off. The check must flag
    # <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"> on /docs
    # and the stylesheet from fonts.googleapis.com on /redoc.
    with TestClient(FastAPI()) as client:
        docs = client.get("/docs")
        redoc = client.get("/redoc")
    assert docs.status_code == 200
    assert redoc.status_code == 200
    docs_hosts = {urlsplit(url).hostname for url in foreign_references(docs.text, "testserver")}
    redoc_hosts = {urlsplit(url).hostname for url in foreign_references(redoc.text, "testserver")}
    assert "cdn.jsdelivr.net" in docs_hosts
    assert "cdn.jsdelivr.net" in redoc_hosts
    assert "fonts.googleapis.com" in redoc_hosts
    own = '<link rel="canonical" href="http://testserver/"><script src="/app.js"></script>'
    assert foreign_references(own, "testserver") == []
    assert foreign_references(own, None) == ["http://testserver/"]


def test_hub_pages_reference_no_other_host(hub: TestClient) -> None:
    own_host = _own_host(hub)
    paths = _get_routes_without_parameters(hub.app)
    assert ENDLESS_STREAMS <= set(paths)
    pages: dict[str, str] = {}
    needs_session: list[str] = []
    for path in paths:
        r = hub.get(path, follow_redirects=False)
        if "text/html" in r.headers.get("content-type", ""):
            pages[path] = r.text
        elif r.status_code == 401:
            needs_session.append(path)
    sign_in(hub, "code-alice")
    assert hub.get("/auth/me").status_code == 200
    for path in needs_session:
        if path in ENDLESS_STREAMS:
            continue
        r = hub.get(path, follow_redirects=False)
        if "text/html" in r.headers.get("content-type", ""):
            pages[path] = r.text
    assert REQUIRED_PAGES <= set(pages)
    for path, body in pages.items():
        assert foreign_references(body, own_host) == [], path


def test_static_files_reference_no_other_host() -> None:
    files = sorted(path for path in STATIC.rglob("*") if path.is_file())
    names = {path.relative_to(STATIC).as_posix() for path in files}
    assert {"index.html", "session.html", "favicon.svg"} <= names
    for path in files:
        # latin-1 maps every byte, so a URL in a binary file is found as well; on disk the
        # hub's own address is still the __PUBLIC_URL__ placeholder, so no host is the hub's.
        text = path.read_bytes().decode("latin-1")
        assert foreign_references(text, None) == [], path.name
