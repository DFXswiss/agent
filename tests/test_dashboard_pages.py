from __future__ import annotations

import http.client

import pytest

from agent_cli import main as main_mod
from test_dashboard_control import _running_dashboard

pytestmark = pytest.mark.no_pg

MOVED_SECTIONS = (
    ("usage", "Subscriptions", "usage"),
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


class _StateStore:
    def __init__(self, agent_home) -> None:
        self.home = agent_home

    def snapshot(self) -> dict:
        return {
            "generated_at": "2026-11-16T12:00:00Z",
            "sessions": [],
            "activity": [
                {
                    "type": "usage.snapshot",
                    "_origin_device_id": "device-a",
                    "payload": {
                        "vendor": "driver-a",
                        "provider": "driver-a",
                        "account_email": "seat-1@example.com",
                    },
                }
            ],
        }

    def device_id(self) -> str:
        return "device-a"

    def close(self) -> None:
        pass


def _state(port: int) -> dict:
    import json

    status, content_type, raw = _get(port, "/api/state")
    assert status == 200
    assert content_type.startswith("application/json")
    return json.loads(raw)


def test_state_carries_the_subscription_overview(monkeypatch, tmp_path) -> None:
    import json

    monkeypatch.setattr(main_mod, "open_store", lambda: _StateStore(tmp_path))
    with _running_dashboard(monkeypatch) as port:
        state = _state(port)
        assert state["subscriptions"]["configured"] is False
        assert state["subscriptions"]["error"] is None
        assert [row["listed"] for row in state["subscriptions"]["rows"]] == [False]
        assert len(state["usage"]) == 1
        overview = {entry["provider"]: entry for entry in state["usage_overview"]}
        assert {"claude", "codex", "grok", "openrouter"} <= set(overview)
        # A snapshot without windows is no data, not 0 %.
        assert overview["claude"]["status"] == "no data"
        assert [a["account"] for a in overview["claude"]["accounts"]] == ["seat-1@example.com"]
        assert overview["codex"]["summary"] == "Codex not read"

        path = tmp_path / "ai-subscriptions.json"
        path.write_text(
            json.dumps(
                {
                    "subscriptions": [
                        {"provider": "driver-a", "account": "Seat-1@example.com", "label": "Seat 1"},
                        {"provider": "driver-a", "account": "seat-2@example.com"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        rows = _state(port)["subscriptions"]["rows"]
        assert [(row["account"], row["label"], row["usage"] is not None) for row in rows] == [
            ("Seat-1@example.com", "Seat 1", True),
            ("seat-2@example.com", None, False),
        ]

        # A broken file is data on the page, not a broken endpoint.
        path.write_text("{", encoding="utf-8")
        subscriptions = _state(port)["subscriptions"]
        assert subscriptions["error"] == "Cannot read ai-subscriptions.json"
        assert [row["listed"] for row in subscriptions["rows"]] == [False]
