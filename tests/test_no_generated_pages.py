from __future__ import annotations

import re
from collections.abc import Callable
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

# The rule: no script or other resource from another host loads or runs on a page of the
# hub's origin. A position below is one from which the browser loads, runs or sends to a
# URL without the reader choosing to navigate there.
#
# A URL that can reach another host: a scheme (http, https, ws, wss) or a protocol-relative
# start. Browsers treat "\" like "/" in these URLs, so "/\host", "\\host" and "\/\/host" are
# protocol-relative as well. "https:host" without slashes is matched too (stricter: on a page
# with another scheme it resolves to that host). data: and blob: URLs load nothing from another
# host and are not matched.
#
# Covered positions:
# - "src": the src attribute of any element (script, img, iframe, embed, source, track, audio,
#   video, input type=image) and a script's ".src = ..." assignment; loaded on parse or assign.
# - "srcset": srcset and the imagesrcset of <link rel=preload>; every candidate URL is checked,
#   because the browser picks one by screen and any of them can load.
# - "href": href (and xlink:href) of every element except <a> and <area>: <link> loads
#   stylesheets, icons, preloads and connects for preconnect/dns-prefetch; <base> moves every
#   relative URL of the page to its host; SVG <image>, <use>, <script> and <feImage> load it.
#   A static link to another host in an <a> or <area> element is allowed by this test: the
#   reader follows it, that is navigation, not a load.
# - "meta-refresh": <meta http-equiv="refresh" content="N;url=...">; the browser goes there
#   without any action of the reader.
# - "form-action", "formaction": the action of a <form> (or ".action = ...") and formaction of a
#   button; submitting sends the fields there, and a script can submit without a click.
# - "poster" (video poster image), "object-data" (<object data> or ".data = ..."), "background"
#   (the legacy background attribute of body and table elements, still loaded by browsers),
#   "manifest" (<html manifest>; application caches are removed from current major browsers,
#   included as the stricter choice): loaded without the reader acting.
# - "ping": <a ping> / <area ping>; the browser posts to every listed URL when the reader
#   follows the link, and the reader chose only the href, not the ping target.
# - "css-import", "css-url", "css-image-set": @import "...", url(...) and the string form of
#   image-set(...) in CSS.
# - "script-call": fetch(), import(), new EventSource/WebSocket/WebTransport/Worker/
#   SharedWorker/Request/Audio(), navigator.sendBeacon(), worklet addModule().
# - "xhr-open": xhr.open("GET", "...").
# - "import-scripts": every argument of importScripts(...).
# - "static-import": import ... from "...", import "...", export ... from "...".
# - "set-attribute": setAttribute(name, "...") for the attributes above.
# - "navigation": location = / location.href = / location.assign() / location.replace() /
#   window.open() with a literal URL; a script leaves the page without the reader choosing.
# - "script-data": the JSON of <script type="importmap"> and <script type="speculationrules">;
#   the browser loads mapped modules and prefetches or prerenders the listed URLs.
#
# Not covered, on purpose:
# - href of <a> and <area> (see above).
# - ".href = ..." and setAttribute("href", ...) in a script: the text does not tell an <a> from a
#   <link>; the browser-enforced policy covers it.
# - navigator.serviceWorker.register(...): browsers reject a worker script from another origin
#   with a SecurityError, so it cannot load one.
# - <meta property=... content> and <meta name=... content> (og:url, og:image, twitter:image):
#   read by the crawlers of other sites; the browser showing the page loads nothing.
# - xmlns, itemtype, cite and longdesc: identifiers or descriptions the browser does not fetch.
#
# Limit: this is a text check. A URL that a script builds at run time (for example from an API
# answer, as the pages do for links to the code host through safeHref) is not found here. The
# browser-enforced policy that closes this gap is planned with a later change.
_URL = r"((?:(?:https?|wss?):[/\\]*|[/\\]{2,})[^\s\"'`<>()\\]+)"
_URL_AT_START = re.compile(_URL, re.IGNORECASE)
_LEADING = re.compile(r"(?:(?:https?|wss?):)?[/\\]*", re.IGNORECASE)
# An attribute value: double, single or back quoted, or unquoted. Exactly one group is set.
_VALUE = r"(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`|([^\s\"'`<>]+))"
_TABLE_TAGS = r"(?:body|table|thead|tbody|tfoot|tr|td|th)"
_LOAD_ATTRIBUTES = r"(?:src|(?:image)?srcset|ping|poster|data|action|formaction|background|manifest)"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Group 1 is the URL.
REFERENCE_POSITIONS: dict[str, re.Pattern[str]] = {
    "src": _rx(r"(?<![\w-])src\s*=\s*[\"'`]?\s*" + _URL),
    "href": _rx(r"<(?!(?:a|area)\b)[a-z][\w:.-]*[^<>]*?\bhref\s*=\s*[\"'`]?\s*" + _URL),
    "meta-refresh": _rx(
        r"<meta\b(?=[^<>]*\bhttp-equiv\s*=\s*[\"']?\s*refresh\b)[^<>]*?\bcontent\s*=\s*[\"']?"
        r"\s*[\d.]*\s*[;,]?\s*(?:url\s*=\s*)?[\"']?\s*" + _URL
    ),
    "form-action": _rx(r"(?:<form\b[^<>]*?\s|\.)action\s*=\s*[\"'`]?\s*" + _URL),
    "formaction": _rx(r"(?<![\w-])formaction\s*=\s*[\"'`]?\s*" + _URL),
    "poster": _rx(r"(?<![\w-])poster\s*=\s*[\"'`]?\s*" + _URL),
    "object-data": _rx(r"(?:<object\b[^<>]*?\s|\.)data\s*=\s*[\"'`]?\s*" + _URL),
    "background": _rx(r"(?:<" + _TABLE_TAGS + r"\b[^<>]*?\s|\.)background\s*=\s*[\"'`]?\s*" + _URL),
    "manifest": _rx(r"<html\b[^<>]*?\smanifest\s*=\s*[\"']?\s*" + _URL),
    "css-import": _rx(r"@import\s+[\"']\s*" + _URL),
    "css-url": _rx(r"\burl\(\s*[\"']?\s*" + _URL),
    "script-call": _rx(
        r"(?:\bfetch|\bimport|\bnew\s+(?:EventSource|WebSocket|WebTransport|Worker|SharedWorker"
        r"|Request|Audio)|\bnavigator\.sendBeacon|\.addModule)\(\s*[\"'`]\s*" + _URL
    ),
    "xhr-open": _rx(r"\.open\(\s*[\"'`][a-z]+[\"'`]\s*,\s*[\"'`]\s*" + _URL),
    "static-import": _rx(
        r"(?<![@.\w$])(?:import\s*|(?:import|export)\b[^;\"'`()]*?\bfrom\s*)[\"']\s*" + _URL
    ),
    "navigation": _rx(
        r"(?:\blocation(?:\.href)?\s*=\s*|\blocation\.(?:assign|replace)\(\s*|\bwindow\.open\(\s*)"
        r"[\"'`]\s*" + _URL
    ),
}


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


_SRCSET_URL = re.compile(r"[\s,]*(\S+)")
_SRCSET_DESCRIPTORS = re.compile(r"[^,(]*(?:\([^)]*\)[^,(]*)*")


def _srcset_urls(value: str) -> list[str]:
    # The candidate parse of the HTML standard: a URL is a run without white space (trailing
    # commas removed); its descriptors run to the next comma outside parentheses.
    urls: list[str] = []
    pos = 0
    while (match := _SRCSET_URL.match(value, pos)) is not None:
        url = match.group(1)
        pos = match.end()
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            descriptors = _SRCSET_DESCRIPTORS.match(value, pos)
            assert descriptors is not None
            pos = descriptors.end()
        if url:
            urls.append(url)
    return urls


def _candidates(value: str) -> list[str]:
    # srcset candidates and white-space separated words (ping, single URLs); the union is
    # the stricter reading when the attribute is not known.
    return _unique(_srcset_urls(value) + value.split())


_QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'|`([^`]*)`")


def _quoted_strings(value: str) -> list[str]:
    return [next(g for g in m.groups() if g is not None) for m in _QUOTED.finditer(value)]


# The groups hold a value; the function splits it into the strings checked as URLs.
LIST_POSITIONS: dict[str, tuple[re.Pattern[str], Callable[[str], list[str]]]] = {
    "srcset": (_rx(r"(?<![\w-])(?:image)?srcset\s*=\s*" + _VALUE), _candidates),
    "ping": (_rx(r"(?:<(?:a|area)\b[^<>]*?\s|\.)ping\s*=\s*" + _VALUE), _candidates),
    "set-attribute": (
        _rx(r"\bsetAttribute\(\s*[\"'`]" + _LOAD_ATTRIBUTES + r"[\"'`]\s*,\s*" + _VALUE),
        _candidates,
    ),
    "import-scripts": (_rx(r"\bimportScripts\(([^()]*)\)"), _quoted_strings),
    "css-image-set": (_rx(r"\bimage-set\(([^;{}<>]*)"), _quoted_strings),
    "script-data": (
        _rx(
            r"<script\b[^<>]*?\btype\s*=\s*[\"']?\s*(?:importmap|speculationrules)\b[^<>]*>"
            r"([^<]*)"
        ),
        _quoted_strings,
    ),
}


def references_by_position(text: str) -> list[tuple[str, str]]:
    """Every (position name, URL) in text that can reach another host, whatever its host."""
    found: list[tuple[str, str]] = []
    for name, pattern in REFERENCE_POSITIONS.items():
        for match in pattern.finditer(text):
            found.append((name, match.group(1)))
    for name, (pattern, split) in LIST_POSITIONS.items():
        for match in pattern.finditer(text):
            value = next((g for g in match.groups() if g is not None), "")
            for token in split(value):
                url = _URL_AT_START.match(token.strip())
                if url is not None:
                    found.append((name, url.group(1)))
    return found


def _host(url: str) -> str | None:
    leading = _LEADING.match(url)
    rest = url[leading.end():] if leading is not None else url
    return urlsplit("//" + rest.replace("\\", "/")).hostname


def foreign_references(text: str, own_host: str | None) -> list[str]:
    """Every URL in a reference position whose host is not own_host.

    With own_host None every URL in such a position counts as foreign.
    """
    own = None if own_host is None else own_host.lower()
    return [url for _, url in references_by_position(text) if own is None or _host(url) != own]


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


OWN_HOST = "hub.example"

# (position, snippet, the one reference reported). Every position has at least one.
POSITIVE_CASES: list[tuple[str, str, str]] = [
    ("src", '<script src="//cdn.example/x.js"></script>', "//cdn.example/x.js"),
    ("src", "<img src=HTTPS://CDN.EXAMPLE/a.png>", "HTTPS://CDN.EXAMPLE/a.png"),
    ("src", r'<script src="/\cdn.example/x.js"></script>', r"/\cdn.example/x.js"),
    ("src", '<script src="//hub.example@evil.example/x.js"></script>', "//hub.example@evil.example/x.js"),
    ("src", 'img.src = "https://cdn.example/a.png";', "https://cdn.example/a.png"),
    ("srcset", '<img srcset="/a.png 1x, //cdn.example/b.png 2x">', "//cdn.example/b.png"),
    ("srcset", '<img srcset="/a.png 1x,//cdn.example/b.png 2x">', "//cdn.example/b.png"),
    (
        "srcset",
        '<link rel="preload" as="image" imagesrcset="https://cdn.example/c.png 480w">',
        "https://cdn.example/c.png",
    ),
    ("href", '<link rel="stylesheet" href="//cdn.example/x.css">', "//cdn.example/x.css"),
    ("href", '<base href="https://cdn.example/">', "https://cdn.example/"),
    ("href", '<svg><image href="https://cdn.example/i.png"/></svg>', "https://cdn.example/i.png"),
    ("href", '<use xlink:href="//cdn.example/s.svg#i"/>', "//cdn.example/s.svg#i"),
    ("meta-refresh", '<meta http-equiv="refresh" content="0;url=//evil.example/">', "//evil.example/"),
    (
        "meta-refresh",
        "<meta content=\"5; URL='https://evil.example/x'\" http-equiv=\"Refresh\">",
        "https://evil.example/x",
    ),
    (
        "form-action",
        '<form action="https://collect.example/post" method="post">',
        "https://collect.example/post",
    ),
    ("form-action", 'form.action = "//collect.example/post";', "//collect.example/post"),
    ("formaction", '<button formaction="//collect.example/post">Send</button>', "//collect.example/post"),
    ("poster", '<video poster="https://cdn.example/p.jpg"></video>', "https://cdn.example/p.jpg"),
    ("object-data", '<object data="//cdn.example/x.pdf"></object>', "//cdn.example/x.pdf"),
    ("object-data", 'obj.data = "https://cdn.example/x.pdf";', "https://cdn.example/x.pdf"),
    ("background", '<body background="//cdn.example/bg.png">', "//cdn.example/bg.png"),
    ("manifest", '<html manifest="//cdn.example/app.appcache">', "//cdn.example/app.appcache"),
    ("ping", '<a href="/docs" ping="/count https://track.example/p">x</a>', "https://track.example/p"),
    ("css-import", '<style>@import "//fonts.example/a.css";</style>', "//fonts.example/a.css"),
    ("css-import", "@import 'https://fonts.example/b.css' screen;", "https://fonts.example/b.css"),
    ("css-url", "<style>@font-face{src:url(//fonts.example/f.woff2)}</style>", "//fonts.example/f.woff2"),
    (
        "css-url",
        "<div style=\"background:url('https://cdn.example/bg.png')\"></div>",
        "https://cdn.example/bg.png",
    ),
    (
        "css-image-set",
        '.x{background-image:image-set("//cdn.example/a.png" 1x, "/b.png" 2x)}',
        "//cdn.example/a.png",
    ),
    ("css-image-set", "-webkit-image-set('https://cdn.example/a.png' 1x)", "https://cdn.example/a.png"),
    ("script-call", 'fetch("//api.example/x")', "//api.example/x"),
    ("script-call", 'import("https://cdn.example/m.js")', "https://cdn.example/m.js"),
    ("script-call", 'new EventSource("https://events.example/s")', "https://events.example/s"),
    ("script-call", 'new WebSocket("wss://ws.example/s")', "wss://ws.example/s"),
    ("script-call", 'new WebSocket("ws://ws.example/s")', "ws://ws.example/s"),
    ("script-call", 'new WebTransport("https://wt.example/")', "https://wt.example/"),
    ("script-call", 'new Worker("//cdn.example/w.js")', "//cdn.example/w.js"),
    ("script-call", 'new SharedWorker("https://cdn.example/w.js")', "https://cdn.example/w.js"),
    ("script-call", 'new Request("https://api.example/x")', "https://api.example/x"),
    ("script-call", 'new Audio("//cdn.example/a.mp3")', "//cdn.example/a.mp3"),
    ("script-call", 'navigator.sendBeacon("https://track.example/b", body)', "https://track.example/b"),
    ("script-call", 'CSS.paintWorklet.addModule("https://cdn.example/p.js")', "https://cdn.example/p.js"),
    ("xhr-open", 'xhr.open("GET", "https://api.example/x")', "https://api.example/x"),
    ("import-scripts", 'importScripts("/a.js", "https://cdn.example/b.js")', "https://cdn.example/b.js"),
    ("static-import", 'import { x } from "https://cdn.example/m.js";', "https://cdn.example/m.js"),
    ("static-import", 'import "//cdn.example/side.js";', "//cdn.example/side.js"),
    ("static-import", 'export * from "https://cdn.example/m.js";', "https://cdn.example/m.js"),
    ("set-attribute", 'el.setAttribute("src", "//cdn.example/x.js")', "//cdn.example/x.js"),
    (
        "set-attribute",
        'img.setAttribute("srcset", "/a.png 1x, https://cdn.example/b.png 2x")',
        "https://cdn.example/b.png",
    ),
    ("set-attribute", 'a.setAttribute("ping", "/count https://track.example/p")', "https://track.example/p"),
    ("navigation", 'location.href = "https://elsewhere.example/";', "https://elsewhere.example/"),
    ("navigation", 'window.location.replace("//elsewhere.example/")', "//elsewhere.example/"),
    ("navigation", 'window.open("https://elsewhere.example/")', "https://elsewhere.example/"),
    (
        "script-data",
        '<script type="importmap">{"imports": {"m": "https://cdn.example/m.js"}}</script>',
        "https://cdn.example/m.js",
    ),
    (
        "script-data",
        '<script type="speculationrules">{"prerender": [{"urls": ["https://elsewhere.example/next"]}]}'
        "</script>",
        "https://elsewhere.example/next",
    ),
]

# (position, snippet that looks like a reference, what is found with no own host). Nothing in
# a snippet is reported for the hub's own host; a URL of the hub's own host is still found
# when no host counts as the hub's, which shows that the position matched.
NEGATIVE_CASES: list[tuple[str, str, list[str]]] = [
    ("src", '<script src="/app.js"></script>', []),
    ("src", '<img src="data:image/png;base64,AAAA">', []),
    ("src", '<img src="blob:https://elsewhere.example/1">', []),
    ("src", '<img data-src="https://elsewhere.example/a.png">', []),
    ("src", '<script src="https://hub.example/app.js"></script>', ["https://hub.example/app.js"]),
    ("srcset", '<img srcset="/a.png 1x, data:image/png;base64,AAAA 2x">', []),
    ("srcset", '<img srcset="https://hub.example/a.png 2x">', ["https://hub.example/a.png"]),
    ("href", '<a href="https://elsewhere.example/">x</a>', []),
    ("href", '<A HREF="//elsewhere.example/">x</A>', []),
    ("href", '<area shape="default" href="https://elsewhere.example/">', []),
    ("href", '<link rel="icon" href="/favicon.svg">', []),
    ("href", '<link rel="canonical" href="https://hub.example/">', ["https://hub.example/"]),
    ("meta-refresh", '<meta http-equiv="refresh" content="30">', []),
    ("meta-refresh", '<meta property="og:url" content="https://elsewhere.example/">', []),
    (
        "meta-refresh",
        '<meta http-equiv="refresh" content="0; url=https://hub.example/">',
        ["https://hub.example/"],
    ),
    ("form-action", '<form id="ping-form" action="/api/pings">', []),
    ("form-action", '<form action="https://hub.example/api/pings">', ["https://hub.example/api/pings"]),
    ("formaction", '<button formaction="/api/pings">Send</button>', []),
    ("poster", '<video poster="/p.jpg"></video>', []),
    ("object-data", '<object data="data:image/svg+xml,%3Csvg%3E"></object>', []),
    ("object-data", '<div data-url="https://elsewhere.example/"></div>', []),
    ("background", '<td style="background: #fff">x</td>', []),
    ("manifest", '<html lang="en" manifest="/app.appcache">', []),
    ("ping", '<a href="https://elsewhere.example/" ping="/count">x</a>', []),
    ("css-import", '@import "/a.css";', []),
    ("css-url", "src:url(data:font/woff2;base64,AAAA)", []),
    ("css-url", '<path fill="url(#paint0)"/>', []),
    ("css-url", "background:url(/bg.png)", []),
    ("css-image-set", 'image-set("/a.png" 1x, "data:image/png;base64,AAAA" 2x)', []),
    ("script-call", 'fetch("/api/state")', []),
    ("script-call", 'new EventSource("/api/stream")', []),
    ("script-call", 'new WebSocket("wss://hub.example/s")', ["wss://hub.example/s"]),
    ("xhr-open", 'xhr.open("POST", "/api/pings")', []),
    ("import-scripts", 'importScripts("/a.js")', []),
    ("static-import", 'import { x } from "./m.js";', []),
    ("set-attribute", 'el.setAttribute("src", "/x.js")', []),
    ("set-attribute", 'a.setAttribute("href", "https://elsewhere.example/")', []),
    ("navigation", 'location.href = "/";', []),
    ("script-data", '<script type="importmap">{"imports": {"m": "/static/m.js"}}</script>', []),
]


def test_every_reference_position_has_a_positive_and_a_negative_case() -> None:
    positions = set(REFERENCE_POSITIONS) | set(LIST_POSITIONS)
    assert not set(REFERENCE_POSITIONS) & set(LIST_POSITIONS)
    assert {name for name, _, _ in POSITIVE_CASES} == positions
    assert {name for name, _, _ in NEGATIVE_CASES} == positions


@pytest.mark.parametrize(("position", "snippet", "reference"), POSITIVE_CASES)
def test_reference_check_reports_the_position(position: str, snippet: str, reference: str) -> None:
    assert references_by_position(snippet) == [(position, reference)]
    assert foreign_references(snippet, OWN_HOST) == [reference]


@pytest.mark.parametrize(("position", "snippet", "found_without_own_host"), NEGATIVE_CASES)
def test_reference_check_does_not_report_the_look_alike(
    position: str, snippet: str, found_without_own_host: list[str]
) -> None:
    assert foreign_references(snippet, OWN_HOST) == [], position
    assert foreign_references(snippet, None) == found_without_own_host, position
    if found_without_own_host:
        assert {name for name, _ in references_by_position(snippet)} == {position}


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
