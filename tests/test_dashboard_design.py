"""The local dashboard serves the vendored DFX Work files and the orders page loads them."""

from __future__ import annotations

import http.client
import re
import tomllib
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from test_dashboard_control import _running_dashboard
from test_dashboard_pages import _StaticStore

pytestmark = pytest.mark.no_pg

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "agent_cli" / "static"
DESIGN = STATIC / "design"
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}
SECTION_HREFS = (
    "/usage",
    "/sessions",
    "/tasks",
    "/rounds",
    "/agents",
    "/checks",
    "/gates",
    "/work",
    "/pings",
)


def test_design_asset_serves_each_vendored_file() -> None:
    files = sorted(path for path in DESIGN.iterdir() if path.is_file())
    assert files
    for path in files:
        found = main_mod._design_asset("/design/" + path.name)
        assert found is not None, path.name
        served, media = found
        assert served == path.resolve()
        assert media == CONTENT_TYPES[path.suffix.lower()]


def test_design_asset_refuses_a_bad_name() -> None:
    # Each entry makes one earlier check false and the next check the first true one.
    refused = (
        "/nope",
        "/design",
        "/design/",
        "/design/nested/work.css",
        "/design/" + "a\\b.css",
        "/design/a%2Fb.css",
        "/design/.hidden.css",
        "/design/foo..bar.css",
        "/design/../main.py",
        "/design/a\x00.css",
        "/design/notes.bin",
        "/design/missing.css",
    )
    for path in refused:
        assert main_mod._design_asset(path) is None, path


def test_design_asset_stays_inside_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    design = tmp_path / "design"
    design.mkdir()
    outside = tmp_path / "outside.css"
    outside.write_text("outside", encoding="utf-8")
    (design / "escape.css").symlink_to(outside)
    (design / "folder.css").mkdir()
    (design / "Note.CSS").write_text("h1{}", encoding="utf-8")
    monkeypatch.setattr(main_mod, "DESIGN_DIR", design)

    assert main_mod._design_asset("/design/escape.css") is None
    assert main_mod._design_asset("/design/folder.css") is None
    found = main_mod._design_asset("/design/Note.CSS")
    assert found == ((design / "Note.CSS").resolve(), "text/css; charset=utf-8")


def _response(port: int, path: str) -> tuple[int, str, str | None, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    status = response.status
    content_type = response.getheader("Content-Type") or ""
    length = response.getheader("Content-Length")
    connection.close()
    return status, content_type, length, body


def test_design_route_serves_css_and_refuses_a_parent_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "open_store", lambda: _StaticStore())
    with _running_dashboard(monkeypatch) as port:
        status, content_type, length, body = _response(port, "/design/work.css")
        assert status == 200
        assert content_type.startswith("text/css")
        assert length == str(len(body))
        assert body == (DESIGN / "work.css").read_bytes()

        status, _, _, _ = _response(port, "/design/../main.py")
        assert status == 404


def test_orders_page_loads_the_design() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assets = (
        'href="/design/tokens.css"',
        'href="/design/brand.css"',
        'href="/design/motion-base.css"',
        'href="/design/work.css"',
        'href="/design/motion.css"',
        'src="/design/work.js"',
    )
    positions = [html.index(asset) for asset in assets]
    assert positions == sorted(positions)
    assert positions[-1] < html.index("<script>")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "fold-grid" in html
    assert "watchFold(" in html
    for href in SECTION_HREFS:
        assert html.count(f'href="{href}"') == 1


def test_package_data_ships_the_design_folder() -> None:
    parsed = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "static/design/*" in parsed["tool"]["setuptools"]["package-data"]["agent_cli"]


def test_brand_css_names_no_private_repository_and_its_fonts_exist() -> None:
    text = (DESIGN / "brand.css").read_text(encoding="utf-8")
    assert "joshuakrueger-dfx" not in text
    assert "DFX-design-pod" not in text
    urls = re.findall(r"url\('([^']+)'\)", text)
    assert urls
    for url in urls:
        assert "/" not in url
        assert (DESIGN / url).is_file()
