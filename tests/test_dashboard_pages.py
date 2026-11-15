from __future__ import annotations

import http.client

import pytest

from agent_cli import main as main_mod
from test_dashboard_control import _running_dashboard

pytestmark = pytest.mark.no_pg

MOVED_SECTIONS = (
    ("usage", "Usage", "usage"),
    ("sessions", "Sessions", "sessions"),
    ("tasks", "Tasks", "tasks"),
    ("rounds", "Rounds", "rounds"),
    ("agents", "Agents", "agents"),
    ("checks", "Local checks", "checks"),
    ("gates", "Review gates", "gates"),
    ("work", "Open work", "work"),
    ("pings", "Pings", "pings"),
)


class _StaticStore:
    def close(self) -> None:
        pass


def _get(port: int, path: str) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", path)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    content_type = response.getheader("Content-Type") or ""
    connection.close()
    return status, content_type, raw


def test_dashboard_serves_the_main_page_and_every_subpage(monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "open_store", lambda: _StaticStore())
    with _running_dashboard(monkeypatch) as port:
        status, content_type, raw = _get(port, "/")
        assert status == 200
        assert content_type.startswith("text/html")
        main_page = raw.decode("utf-8")
        assert 'id="section-nav"' in main_page
        for name, _, table_id in MOVED_SECTIONS:
            assert f'id="{table_id}"' not in main_page
            assert f'href="/{name}"' in main_page

        status, _, raw = _get(port, "/index.html")
        assert status == 200
        assert raw.decode("utf-8") == main_page

        for name, _, table_id in MOVED_SECTIONS:
            status, content_type, raw = _get(port, f"/{name}")
            assert status == 200, name
            assert content_type.startswith("text/html")
            body = raw.decode("utf-8")
            assert f'id="{table_id}"' in body
            assert f'data-section="{name}"' in body
            assert '<a href="/">Back to dashboard</a>' in body

        status, _, raw = _get(port, "/unknown")
        assert status == 404
        assert b"404" in raw
