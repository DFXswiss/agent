from __future__ import annotations

import http.client
import json

import pytest

from agent_cli import main as main_mod
from driver_fixtures import (
    DECLARED_DRIVERS,
    WINDOWS_ACCOUNT_FILE,
    WINDOWS_DRIVER_ID,
    driver_document,
    windows_driver,
    write_drivers,
)
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
    def __init__(self, agent_home, provider: str = "driver-a") -> None:
        self.home = agent_home
        self.provider = provider

    def snapshot(self) -> dict:
        return {
            "generated_at": "2026-11-16T12:00:00Z",
            "sessions": [],
            "activity": [
                {
                    "type": "usage.snapshot",
                    "_origin_device_id": "device-a",
                    "payload": {
                        "vendor": self.provider,
                        "provider": self.provider,
                        "account_email": "seat-1@example.com",
                    },
                }
            ],
        }

    def device_id(self) -> str:
        return "device-a"

    def meta_prefix(self, prefix: str) -> dict[str, str]:
        assert prefix == "usage.checked."
        return {
            f"usage.checked.{WINDOWS_DRIVER_ID}.seat": json.dumps(
                {"checked_at": "2026-11-16T12:30:00Z", "identity": "seat-1@example.com"}
            )
        }

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

    write_drivers(tmp_path)
    monkeypatch.setattr(main_mod, "open_store", lambda: _StateStore(tmp_path))
    with _running_dashboard(monkeypatch) as port:
        state = _state(port)
        assert state["subscriptions"]["configured"] is False
        assert state["subscriptions"]["error"] is None
        assert [row["listed"] for row in state["subscriptions"]["rows"]] == [False]
        assert len(state["usage"]) == 1
        overview = {entry["provider"]: entry for entry in state["usage_overview"]}
        assert list(overview) == list(DECLARED_DRIVERS)
        # A snapshot without a figure is no data, not 0 %.
        assert overview["driver-a"]["status"] == "no data"
        assert [a["account"] for a in overview["driver-a"]["accounts"]] == ["seat-1@example.com"]
        assert overview["driver-b"]["summary"] == "Vendor B not read"

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

        # Without usage_accounts no row has a check time.
        assert [row["checked_at"] for row in rows] == [None, None]
        assert _state(port)["subscriptions"]["check_error"] is None

        # A broken file is data on the page, not a broken endpoint.
        path.write_text("{", encoding="utf-8")
        subscriptions = _state(port)["subscriptions"]
        assert subscriptions["error"] == "Cannot read ai-subscriptions.json"
        assert [row["listed"] for row in subscriptions["rows"]] == [False]


def test_state_carries_the_check_time_of_each_account(monkeypatch, tmp_path) -> None:
    import json

    monkeypatch.setattr(
        main_mod, "open_store", lambda: _StateStore(tmp_path, WINDOWS_DRIVER_ID)
    )
    (tmp_path / "ai-subscriptions.json").write_text(
        json.dumps(
            {
                "subscriptions": [
                    {"provider": WINDOWS_DRIVER_ID, "account": "Seat-1@example.com"},
                    {"provider": WINDOWS_DRIVER_ID, "account": "seat-2@example.com"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # The configured account "seat" logs in as seat-1; its check time joins
    # the row that shows that login, by the account name in the meta key.
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": "seat-1@example.com"}}),
        encoding="utf-8",
    )
    document = driver_document()
    document["drivers"][WINDOWS_DRIVER_ID] = windows_driver()
    write_drivers(tmp_path, document)
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "driver-a", "second": "driver-b"},
                "accounts": {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
                "usage_accounts": ["seat"],
            }
        ),
        encoding="utf-8",
    )
    with _running_dashboard(monkeypatch) as port:
        state = _state(port)
    subscriptions = state["subscriptions"]
    assert subscriptions["check_error"] is None
    rows = subscriptions["rows"]
    assert [(row["account"], row["checked_at"]) for row in rows] == [
        ("Seat-1@example.com", "2026-11-16T12:30:00Z"),
        ("seat-2@example.com", None),
    ]
    assert rows[0]["usage"]["payload"]["account_email"] == "seat-1@example.com"
    overview = {entry["provider"]: entry for entry in state["usage_overview"]}
    assert [
        (a["account"], a["checked_at"]) for a in overview[WINDOWS_DRIVER_ID]["accounts"]
    ] == [
        ("Seat-1@example.com", "2026-11-16T12:30:00Z"),
        ("seat-2@example.com", None),
    ]
