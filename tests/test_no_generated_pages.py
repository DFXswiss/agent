from __future__ import annotations

import html
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

# Literals ignored when the whole literal equals one of them (a name, not an address to load).
# Add a name here when a static file needs one; an SVG exported from a drawing tool often
# carries xmlns:xlink="http://www.w3.org/1999/xlink", which is reported until it is added.
XML_NAMESPACES = frozenset({"http://www.w3.org/2000/svg"})

# The rule: a page the hub serves, and a text file under static/, contains no literal that
# names another host, wherever it stands (attribute, style, script, string, comment, text).
# A literal starts either with the scheme http, https, ws or wss, ":" and one or more of "/"
# and "\" (browsers read "\" as "/"; anywhere except right after "blob:"), or with two or
# more "/" at the start of the text or after white space, a C0 control character, a quote,
# "(", "[", "{", "=", "," or ";". In both forms the character after the slashes must be able
# to start a host or userinfo: a letter, a digit, "[", "%", "@" or "~". So "// note",
# "</script>", "a//b", "https:" alone, "rows:1fr" in CSS, regular expressions such as
# /\/+$/ and /\/api\//, strings such as "\\d+", template literals such as
# `//${location.host}/x`, data: and blob: URLs are not literals (a data: payload that itself
# contains a literal, such as base64 starting with "//", is reported).
# The authority runs from there to the first "/", "\", "?", "#" or the end of the text and, when
# a quote directly precedes the literal, to the next same quote. It is read twice: with tab and
# line breaks removed (as the URL parser does) and up to the first white space (where an
# unquoted attribute value or a srcset entry ends). In each reading the host is the part after
# the last "@", without port, in lower case. The text is searched as it is and after
# html.unescape, so character references are decoded. The only exceptions: a literal whose
# hosts in both readings equal the hub's host (port and scheme are not compared), and a literal
# that equals a name in XML_NAMESPACES.
# Limits (not found), each pinned by a row in REFERENCE_CASES: a host name a script assembles
# at run time ("https://" + cdn, `wss://${host}/s`); a scheme directly followed by the host,
# without "/" or "\" ("wss:cdn.example"); a tab or line break inside the scheme, between the
# slashes, or between the slashes and the authority; "\" in an address without a scheme
# ("/\cdn.example", "\\cdn.example"; finding these would also report regular expressions
# such as /\s+/); any encoding other than HTML character references, such as script or CSS
# escapes ("\x2f\x2f", "\2f\2f "); a namespace name from XML_NAMESPACES used as a load
# address. Binary files (BINARY_EXTENSIONS) are not read.
# Found although escaped: "\/" after a scheme ("https:\/\/cdn.example"), and a CSS-escaped
# colon whose closing space puts "//" after white space ("https\3a //cdn.example").
# Harmless text that is also reported, so the check fails closed: a URL in visible text, a
# line comment with a letter or digit right after "//" ("//comment"), and the own address
# without a path where no quote directly precedes it. A policy enforced by the browser,
# planned with a later change, covers what reading the text cannot.
_START = r"(?=[^\W_]|[\[%@~])"
_URL_START = re.compile(
    r"(?<!blob:)(?:https?|wss?):[/\\]+"
    + _START
    + r"|(?<![^\s\x00-\x1f\"'`(\[{=,;])/{2,}"
    + _START,
    re.IGNORECASE,
)
_LEADING = re.compile(r"(?:(?:https?|wss?):)?[/\\]*", re.IGNORECASE)
_AUTHORITY = re.compile(r"[^/\\?#]*")
_REST = re.compile(r"[^\s\"'`<>()]*")
_BREAKS = re.compile(r"[\t\n\r]")
_PORT = re.compile(r":[^:\]]*$")


def _hosts(authority: str) -> tuple[str, str]:
    """The host of authority with tab and line breaks removed, and up to the first white space."""
    readings = (_BREAKS.sub("", authority), re.split(r"\s", authority, maxsplit=1)[0])
    hosts = [_PORT.sub("", reading.rpartition("@")[2]).lower() for reading in readings]
    return hosts[0], hosts[1]


def _host(url: str) -> str:
    """The host of a reported literal (first reading)."""
    return _hosts(_AUTHORITY.match(url, _LEADING.match(url).end()).group(0))[0]


def foreign_references(text: str, own_host: str | None) -> list[str]:
    """Every URL literal in text whose host is not own_host (with None: every URL literal)."""
    own = None if own_host is None else own_host.lower()
    found: dict[str, None] = {}
    for source in (text, html.unescape(text)):
        pos = 0
        while (match := _URL_START.search(source, pos)) is not None:
            start = match.end()
            end = _AUTHORITY.match(source, start).end()
            before = source[match.start() - 1] if match.start() > 0 else ""
            if before in ("\"", "'", "`"):
                quote = source.find(before, start, end)
                end = end if quote == -1 else quote
            literal = source[match.start():end] + _REST.match(source, end).group(0)
            pos = match.start() + len(literal)
            if literal in XML_NAMESPACES:
                continue
            if own is not None and _hosts(source[start:end]) == (own, own):
                continue
            found[literal] = None
    return list(found)


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
    ('<script src="\x01//cdn.example/x.js">', OWN_HOST, ["//cdn.example/x.js"]),
    ('<script src="https:/cdn.example/x.js"></script>', OWN_HOST, ["https:/cdn.example/x.js"]),
    ('<img src="//[2001:db8::1]/x.png">', OWN_HOST, ["//[2001:db8::1]/x.png"]),
    ('<img src="//%63dn.example/x.png">', OWN_HOST, ["//%63dn.example/x.png"]),
    # Found although escaped (see the comment above).
    (r's.src = "https:\/\/cdn.example\/x.js";', OWN_HOST, [r"https:\/\/cdn.example\/x.js"]),
    (r"@import url(https\3a //cdn.example/a.css);", OWN_HOST, ["//cdn.example/a.css"]),
    # Harmless text that is reported (accepted false reports, see the comment above).
    ("<p>See https://example.org/docs</p>", OWN_HOST, ["https://example.org/docs"]),
    ("x = y //comment", OWN_HOST, ["//comment"]),
    ("see https://hub.example now", OWN_HOST, ["https://hub.example now"]),
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
    ('<a href="https://other.example/">x</a>', OWN_HOST, ["https://other.example/"]),
    ('<AREA HREF="//other.example/map">', OWN_HOST, ["//other.example/map"]),
    ('<script src="//@cdn.example/x.js"></script>', OWN_HOST, ["//@cdn.example/x.js"]),
    ('<script src="https://~@cdn.example/x.js">', OWN_HOST, ["https://~@cdn.example/x.js"]),
    (r'const u = "\nhttps://cdn.example/x";', OWN_HOST, ["https://cdn.example/x"]),
    ('<meta http-equiv="refresh" content="0;//evil.example/">', OWN_HOST, ["//evil.example/"]),
    ('<script src="&#x2F;&#x2F;cdn.example/x.js">', OWN_HOST, ["//cdn.example/x.js"]),
    (
        '<script src="https&#58;//cdn.example/x.js">',
        OWN_HOST,
        ["//cdn.example/x.js", "https://cdn.example/x.js"],
    ),
    (
        "<form action=\"https://hub.example'@evil.example/post\">",
        OWN_HOST,
        ["https://hub.example'@evil.example/post"],
    ),
    (
        '<form action="https://hub.example @evil.example/post">',
        OWN_HOST,
        ["https://hub.example @evil.example/post"],
    ),
    (
        '<img src="https://hub.example\n.evil.example/x">',
        OWN_HOST,
        ["https://hub.example\n.evil.example/x"],
    ),
    (
        "<img data-x=' xmlns=' src=\"https://cdn.example/x.png\" data-y=' '>",
        OWN_HOST,
        ["https://cdn.example/x.png"],
    ),
    (
        '<svg xmlns="http://www.w3.org/2000/svg.evil.example/x">',
        OWN_HOST,
        ["http://www.w3.org/2000/svg.evil.example/x"],
    ),
    ('<svg xmlns:xlink="http://www.w3.org/1999/xlink">', OWN_HOST, ["http://www.w3.org/1999/xlink"]),
    # Not reported.
    ('<link rel="canonical" href="https://hub.example/">', OWN_HOST, []),
    ('<script src="https://hub.example/app.js"></script>', OWN_HOST, []),
    ('<script src="HTTPS://HUB.EXAMPLE:8443/app.js"></script>', OWN_HOST, []),
    ('<script src="/app.js"></script><script type="module" src="./m.js"></script>', OWN_HOST, []),
    ('<a href="#top">Top</a>', OWN_HOST, []),
    ('<img src="data:image/png;base64,AAAA">', OWN_HOST, []),
    ('<img src="blob:https://elsewhere.example/1">', OWN_HOST, []),
    ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>', None, []),
    ('<div data-href="/x"></div>', OWN_HOST, []),
    ('parsed.protocol === "https:" && parsed.host === "github.com"', OWN_HOST, []),
    ('new URL("/x", location.href)', OWN_HOST, []),
    ('<path fill="url(#paint0)"/>', OWN_HOST, []),
    ("// see the notes", OWN_HOST, []),
    ('<script>\n  // see the notes\n  const p = "a//b";\n</script>', OWN_HOST, []),
    ("https:", OWN_HOST, []),
    ('<div style="grid-template-rows:1fr auto"></div>', OWN_HOST, []),
    ("a{grid-template-rows:auto}", OWN_HOST, []),
    ("x.windows:focus{}", OWN_HOST, []),
    ("allows:true", OWN_HOST, []),
    ("https:x", OWN_HOST, []),
    ("a // b", OWN_HOST, []),
    # Ordinary script text: no host can start after the slashes, or the slashes are escaped.
    (r'path.replace(/\/+$/, "")', OWN_HOST, []),
    (r"s.split(/\//)", OWN_HOST, []),
    (r"const r = /\/api\//;", OWN_HOST, []),
    (r"text.split(/\s+/)", OWN_HOST, []),
    (r'new RegExp("\\d+")', OWN_HOST, []),
    ("`wss://${location.host}/s`", OWN_HOST, []),
    ("`//${location.host}/x`", OWN_HOST, []),
    # Limits of the check (not found, see the comment above).
    ('s.src = "https://" + cdn + "/x.js";', OWN_HOST, []),
    ('new WebSocket("wss:cdn.example/s")', OWN_HOST, []),
    ('<script src="ht\ttps://cdn.example/x.js">', OWN_HOST, []),
    ('<script src="https:/\t/cdn.example/x.js">', OWN_HOST, []),
    ('<script src="https://\ncdn.example/x.js">', OWN_HOST, []),
    # Mixed or backward slashes without a scheme. Accepting "/\" would also report /\s+/.
    (r'<script src="/\cdn.example/x.js"></script>', OWN_HOST, []),
    (r'<img src="\\cdn.example/x.png">', OWN_HOST, []),
    (r's.src = "\x2f\x2fcdn.example/x.js";', OWN_HOST, []),
    (r"@import url(\2f\2f cdn.example/a.css);", OWN_HOST, []),
    ('<script src="http://www.w3.org/2000/svg"></script>', OWN_HOST, []),
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
