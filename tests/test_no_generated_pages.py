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
# Files under static/ by extension. Text files are scanned. Binary files are not: a string
# inside an image is not loaded by anything. Every file must have an extension from one list.
TEXT_EXTENSIONS = frozenset(
    {".html", ".svg", ".css", ".js", ".mjs", ".json", ".txt", ".webmanifest", ".xml"}
)
BINARY_EXTENSIONS = frozenset({".png", ".ico"})

# The rule: a page the hub serves, and a text file under static/, contains no literal that
# names another host, wherever it stands (attribute, style, script, string constant, comment).
# A literal names a host when it is a URL with the scheme http, https, ws or wss, or a
# protocol-relative URL; data: and blob: URLs name no other host and are not matched.
# The only exceptions are the hub's own host, xmlns values and the href of <a> and <area>.
# Limit: the check reads text. It does not find a host name that a script assembles at run
# time from parts; a policy enforced by the browser, planned with a later change, covers that.
#
# A scheme counts only when no word character, ".", "+", "-" or ":" precedes it (so the
# "https:" inside "blob:https://..." is not a URL of its own). A protocol-relative URL is two
# or more of "/" and "\" (browsers read "\" as "/") at the start of the text or after a quote,
# an opening bracket, "=", "," or white space. Both forms must be followed by a host character,
# so a "// comment" line, "</script>" and a path such as "a//b" are not URLs.
_HOST_AND_REST = r"[\w\[%][^\s\"'`<>()\\]*"
_FOREIGN_URL = re.compile(
    r"(?<![\w.+:-])(?:https?|wss?):[/\\]*" + _HOST_AND_REST
    + r"|(?<![^\s\"'`(\[{=,])[/\\]{2,}" + _HOST_AND_REST,
    re.IGNORECASE,
)
_LEADING = re.compile(r"(?:(?:https?|wss?):)?[/\\]*", re.IGNORECASE)
# An attribute value: double quoted, single quoted or unquoted.
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>]+)"
_TAG = re.compile(r"<([a-z][\w:.-]*)(\s[^<>]*)?>", re.IGNORECASE)
_XMLNS = re.compile(r"(?<=\s)xmlns(?::[\w.-]+)?\s*=\s*" + _VALUE, re.IGNORECASE)
_HREF = re.compile(r"(?<=\s)href\s*=\s*" + _VALUE, re.IGNORECASE)


def _without_exceptions(text: str) -> str:
    """text with the values of xmlns attributes and of href in <a> and <area> removed."""

    def strip(tag: re.Match[str]) -> str:
        attributes = _XMLNS.sub("", tag.group(2) or "")
        if tag.group(1).lower() in ("a", "area"):
            attributes = _HREF.sub("", attributes)
        return "<" + tag.group(1) + attributes + ">"

    return _TAG.sub(strip, text)


def _host(url: str) -> str | None:
    leading = _LEADING.match(url)
    rest = url[leading.end():] if leading is not None else url
    return urlsplit("//" + rest.replace("\\", "/")).hostname


def foreign_references(text: str, own_host: str | None) -> list[str]:
    """Every URL literal in text whose host is not own_host (with None: every URL literal)."""
    own = None if own_host is None else own_host.lower()
    urls = [m.group(0) for m in _FOREIGN_URL.finditer(_without_exceptions(text))]
    return [url for url in urls if own is None or _host(url) != own]


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
    # A default application serves the pages the hub turns off: /docs loads Swagger UI from
    # cdn.jsdelivr.net, /redoc its stylesheet from fonts.googleapis.com.
    with TestClient(FastAPI()) as client:
        docs = client.get("/docs")
        redoc = client.get("/redoc")
    assert docs.status_code == 200
    assert redoc.status_code == 200
    docs_hosts = {_host(url) for url in foreign_references(docs.text, "testserver")}
    redoc_hosts = {_host(url) for url in foreign_references(redoc.text, "testserver")}
    assert "cdn.jsdelivr.net" in docs_hosts
    assert "fonts.googleapis.com" in redoc_hosts


OWN_HOST = "hub.example"

# (snippet, own host, the exact list of reported literals).
REFERENCE_CASES: list[tuple[str, str | None, list[str]]] = [
    # Reported.
    ('<script src="https://cdn.example/x.js"></script>', OWN_HOST, ["https://cdn.example/x.js"]),
    ('<script src="//cdn.example/x.js"></script>', OWN_HOST, ["//cdn.example/x.js"]),
    (r'<script src="/\cdn.example/x.js"></script>', OWN_HOST, [r"/\cdn.example/x.js"]),
    ('<link rel="stylesheet" href="//fonts.example/a.css">', OWN_HOST, ["//fonts.example/a.css"]),
    ('@import"https://fonts.example/a.css";', OWN_HOST, ["https://fonts.example/a.css"]),
    ("@font-face{src:url(//fonts.example/f.woff2)}", OWN_HOST, ["//fonts.example/f.woff2"]),
    ('const CDN = "https://cdn.example"; s.src = CDN + "/x.js"', OWN_HOST, ["https://cdn.example"]),
    ('fetch("https://api.example/x")', OWN_HOST, ["https://api.example/x"]),
    ('new WebSocket("wss://stream.example/s")', OWN_HOST, ["wss://stream.example/s"]),
    (
        '<meta http-equiv="refresh" content="0;url=https://evil.example/">',
        OWN_HOST,
        ["https://evil.example/"],
    ),
    ('<form action="https://collect.example/post">', OWN_HOST, ["https://collect.example/post"]),
    ('<img srcset="/a.png 1x, //cdn.example/b.png 2x">', OWN_HOST, ["//cdn.example/b.png"]),
    ('s.src = "HTTPS://cdn.example/x.js";', OWN_HOST, ["HTTPS://cdn.example/x.js"]),
    ('<img src="//hub.example@evil.example/x">', OWN_HOST, ["//hub.example@evil.example/x"]),
    ("<!-- mirrored from https://old.example/page -->", OWN_HOST, ["https://old.example/page"]),
    ('<link rel="canonical" href="https://other.example/">', OWN_HOST, ["https://other.example/"]),
    ('<a data-href="https://other.example/">x</a>', OWN_HOST, ["https://other.example/"]),
    ('<link rel="canonical" href="https://hub.example/">', None, ["https://hub.example/"]),
    # Not reported.
    ('<link rel="canonical" href="https://hub.example/">', OWN_HOST, []),
    ('<script src="https://hub.example/app.js"></script>', OWN_HOST, []),
    ('<script src="/app.js"></script><script type="module" src="./m.js"></script>', OWN_HOST, []),
    ('<a href="#top">Top</a>', OWN_HOST, []),
    ('<img src="data:image/png;base64,AAAA">', OWN_HOST, []),
    ('<img src="blob:https://elsewhere.example/1">', OWN_HOST, []),
    (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        OWN_HOST,
        [],
    ),
    ('<a href="https://example.org/help">Help</a>', OWN_HOST, []),
    ('<area shape="default" href="https://example.org/map">', OWN_HOST, []),
    ('<A HREF="https://example.org/help">Help</A><AREA HREF="//example.org/map">', OWN_HOST, []),
    ('<div data-href="/x"></div>', OWN_HOST, []),
    ('parsed.protocol === "https:" && parsed.host === "github.com"', OWN_HOST, []),
    ('new URL("/x", location.href)', OWN_HOST, []),
    ('<path fill="url(#paint0)"/>', OWN_HOST, []),
    ("// see the notes", OWN_HOST, []),
    ('<script>\n  // see the notes\n  const p = "a//b";\n</script>', OWN_HOST, []),
]


@pytest.mark.parametrize(("snippet", "own_host", "expected"), REFERENCE_CASES)
def test_reference_check(snippet: str, own_host: str | None, expected: list[str]) -> None:
    assert foreign_references(snippet, own_host) == expected


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
        suffix = path.suffix.lower()
        assert suffix in TEXT_EXTENSIONS | BINARY_EXTENSIONS, path.name
        if suffix in BINARY_EXTENSIONS:
            continue
        # On disk the hub's own address is still the __PUBLIC_URL__ placeholder, so no host
        # is the hub's.
        text = path.read_text(encoding="utf-8")
        assert foreign_references(text, None) == [], path.name
