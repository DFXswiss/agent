from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_cli.ai_accounts import AISubscription
from agent_cli.drivers import parse_driver_document
from agent_cli.main import (
    TASK_STATES,
    _subscription_dashboard_rows,
    _subscriptions_dashboard,
    _usage_dashboard_rows,
    _usage_duration_text,
    _usage_overview,
)
from agent_cli.order_ops import ENDED_STATES, OPEN_STATES
from agent_cli.preconditions import CHECK_STATUSES
from agent_cli.rpc_usage import rpc_snapshot
from agent_cli.usage import period_snapshot, spend_snapshot
from driver_fixtures import (
    RPC_ACCOUNT_FILE,
    RPC_DRIVER_ID,
    RPC_LABEL,
    WINDOWS_ACCOUNT_FILE,
    WINDOWS_CREDENTIAL_LABEL,
    WINDOWS_DRIVER_ID,
    driver_document,
    rpc_driver,
    windows_driver,
    write_drivers,
)
from test_rpc_usage import ADAPTER as RPC_ADAPTER
from test_rpc_usage import _window as rpc_window
from test_rpc_usage import limits as rpc_limits
from test_rpc_usage import result as rpc_result
from test_rpc_usage import write_auth as rpc_write_auth
from test_usage import PERIOD_DRIVER, SPEND_DRIVER, _key_body, _period_documents, _usage
from test_usage_windows import mapped, usage_body

pytestmark = pytest.mark.no_pg

STATIC = (
    Path(__file__).resolve().parents[1] / "src" / "agent_cli" / "static"
)
PAGE = STATIC / "index.html"
SECTION_PAGE = STATIC / "section.html"
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


def test_dashboard_html_contains_usage_table() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    assert 'id="usage"' in html
    assert 'typeof p.used_percent === "number"' in html
    assert (
        'setHtml(document.querySelector("#usage tbody"), '
        "(subscriptions.rows || []).map(row => subscriptionRowHtml(row, now))"
    ) in html
    # The ages are computed against the time of each refresh tick.
    tick = html.split("async function tick() {", 1)[1]
    assert tick.index("const now = Date.now();") < tick.index("subscriptionRowHtml(row, now)")
    assert ".reverse(" not in html


def test_dashboard_usage_rows_separate_spend_accounts_and_show_usd() -> None:
    snapshots = [
        {
            "type": "usage.snapshot",
            "payload": {
                "vendor": "driver-d",
                "provider": "driver-d",
                "account": "spend-a",
                "usage_usd": 1.25,
                "limit_usd": 10.0,
                "limit_reset": "2026-12-01T00:00:00Z",
            },
        },
        {
            "type": "usage.snapshot",
            "payload": {
                "vendor": "driver-d",
                "provider": "driver-d",
                "account": "spend-b",
                "usage_usd": 2.5,
                "limit_usd": 20.0,
                "limit_reset": "2026-12-02T00:00:00Z",
            },
        },
    ]

    rows = _usage_dashboard_rows(snapshots)

    assert [row["payload"]["account"] for row in rows] == ["spend-a", "spend-b"]
    assert [row["payload"]["usage_usd"] for row in rows] == [1.25, 2.5]
    assert [row["payload"]["limit_usd"] for row in rows] == [10.0, 20.0]
    assert [row["payload"]["limit_reset"] for row in rows] == [
        "2026-12-01T00:00:00Z",
        "2026-12-02T00:00:00Z",
    ]

    period = {
        "type": "usage.snapshot",
        "payload": {"provider": "driver-a", "account_email": "user@example.com"},
    }
    assert _usage_dashboard_rows([period, period]) == [period]

    legacy = {
        "type": "usage.snapshot",
        "payload": {"vendor": "driver-d", "account": "spend-c"},
    }
    assert _usage_dashboard_rows([legacy]) == [legacy]

    html = SECTION_PAGE.read_text(encoding="utf-8")
    assert "const spend = p.account !== undefined;" in html
    assert 'typeof p.usage_usd === "number"' in html
    assert 'typeof p.limit_usd === "number"' in html
    assert "reset: idOrText(p.limit_reset)," in html
    assert "reset: idOrText(p.period_end)," in html
    assert (
        '"$" + p.usage_usd + " / $" + '
        '(typeof p.limit_usd === "number" ? p.limit_usd : "unlimited")'
    ) in html


def test_dashboard_usage_rows_tell_the_shapes_apart_by_fields_not_driver() -> None:
    # The same driver id may record both shapes; the fields decide the identity.
    spend = {
        "type": "usage.snapshot",
        "_origin_device_id": "device-a",
        "payload": {"provider": "driver-x", "account": "user@example.com", "usage_usd": 1.0},
    }
    period = {
        "type": "usage.snapshot",
        "_origin_device_id": "device-b",
        "payload": {"provider": "driver-x", "account_email": "user@example.com"},
    }
    other_device_spend = {**spend, "_origin_device_id": "device-c"}

    rows = _usage_dashboard_rows([spend, period, other_device_spend])

    assert rows == [spend, period, other_device_spend]
    assert _usage_dashboard_rows([{"type": "usage.snapshot", "payload": "broken"}]) == [
        {"type": "usage.snapshot", "payload": "broken"}
    ]


def test_dashboard_usage_rows_separate_spend_alias_per_device() -> None:
    def snapshot(device: str, usage_usd: float) -> dict:
        return {
            "type": "usage.snapshot",
            "_origin_device_id": device,
            "payload": {
                "provider": "driver-d",
                "account": "shared-alias",
                "usage_usd": usage_usd,
            },
        }

    device_a = snapshot("device-a", 1.0)
    device_b = snapshot("device-b", 2.0)

    rows = _usage_dashboard_rows([device_a, device_b])

    assert rows == [device_a, device_b]


def test_dashboard_usage_rows_keep_newest_spend_snapshot_per_device_alias() -> None:
    def snapshot(usage_usd: float) -> dict:
        return {
            "type": "usage.snapshot",
            "_origin_device_id": "device-a",
            "payload": {
                "provider": "driver-d",
                "account": "shared-alias",
                "usage_usd": usage_usd,
            },
        }

    newest = snapshot(2.0)
    older = snapshot(1.0)

    rows = _usage_dashboard_rows([newest, older])

    assert rows == [newest]


def _snapshot(provider: str, account: str, device: str = "device-a", **extra: object) -> dict:
    key = "account" if provider == "driver-d" else "account_email"
    return {
        "type": "usage.snapshot",
        "_origin_device_id": device,
        "payload": {"provider": provider, "vendor": provider, key: account, **extra},
    }


def test_subscription_rows_join_inventory_with_newest_usage() -> None:
    windowed = _snapshot("driver-a", "Seat-1@Example.com", tier="max_20x")
    period = _snapshot("driver-b", "login@example.com")
    spend_a = _snapshot("driver-d", "router-a", "device-a")
    spend_b = _snapshot("driver-d", "router-a", "device-b")
    stray = _snapshot("driver-a", "unlisted@example.com")
    usage = _usage_dashboard_rows([windowed, period, spend_a, spend_b, stray])
    inventory = (
        AISubscription("driver-a", "seat-1@example.com", "Team seat 1", "active", "free text"),
        AISubscription("driver-a", "seat-2@example.com", "Team seat 2"),
        AISubscription("driver-d", "ROUTER-A"),
        AISubscription("driver-b", "login@example.com", status="paused"),
        AISubscription("driver-c", "seat-1@example.com"),
    )

    rows = _subscription_dashboard_rows(inventory, usage)

    assert [(row["provider"], row["account"], row["listed"], row["usage"]) for row in rows] == [
        ("driver-a", "seat-1@example.com", True, windowed),
        ("driver-a", "seat-2@example.com", True, None),
        ("driver-d", "ROUTER-A", True, spend_a),
        ("driver-d", "ROUTER-A", True, spend_b),
        ("driver-b", "login@example.com", True, period),
        ("driver-c", "seat-1@example.com", True, None),
        ("driver-a", "unlisted@example.com", False, stray),
    ]
    assert rows[0]["label"] == "Team seat 1"
    assert rows[0]["status"] == "active"
    assert rows[0]["note"] == "free text"
    assert rows[-1]["label"] is None and rows[-1]["note"] is None


def test_rpc_rows_join_by_login_email_and_share_the_window_rendering() -> None:
    rpc = _snapshot(RPC_DRIVER_ID, "Seat-1@Example.com", tier="pro")
    windowed = _snapshot(WINDOWS_DRIVER_ID, "seat-1@example.com")
    usage = _usage_dashboard_rows([rpc, windowed])
    inventory = (AISubscription(RPC_DRIVER_ID, "seat-1@example.com", "Seat 1"),)
    rows = _subscription_dashboard_rows(inventory, usage)
    assert [(row["provider"], row["account"], row["listed"], row["usage"]) for row in rows] == [
        (RPC_DRIVER_ID, "seat-1@example.com", True, rpc),
        (WINDOWS_DRIVER_ID, "seat-1@example.com", False, windowed),
    ]
    html = SECTION_PAGE.read_text(encoding="utf-8")
    cells = section_function(html, "usageCells")
    # One rendering path for every window snapshot; the driver id decides nothing.
    assert 'if (p.windows && typeof p.windows === "object") {' in cells
    assert cells.count("windowLines(p)") == 1
    assert "provider" not in cells
    assert '"limit reached"' in cells
    assert '"free resets: "' in cells


def test_subscription_rows_without_inventory_keep_every_snapshot() -> None:
    snapshot = _snapshot("driver-a", "login@example.com")
    legacy = {"type": "usage.snapshot", "payload": {"vendor": "driver-a", "account_email": "old@example.com"}}
    rows = _subscription_dashboard_rows((), [snapshot, legacy])
    assert [(row["provider"], row["account"], row["listed"]) for row in rows] == [
        ("driver-a", "login@example.com", False),
        ("driver-a", "old@example.com", False),
    ]


_NO_CHECKS: dict[str, str] = {}


def _check(checked_at: str, identity: str) -> str:
    """A meta value as the poller writes it after a successful read."""
    return json.dumps({"checked_at": checked_at, "identity": identity})


class _RecordingChecks(dict):
    """Check times that record every key the dashboard looks up."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.keys_read: list[str] = []

    def __contains__(self, key: object) -> bool:
        assert isinstance(key, str)
        self.keys_read.append(key)
        return super().__contains__(key)


def test_subscriptions_dashboard_reports_file_state_as_data(tmp_path: Path) -> None:
    snapshot = _snapshot("driver-a", "login@example.com")
    assert _subscriptions_dashboard(tmp_path, [snapshot], _NO_CHECKS, "device-a") == {
        "configured": False,
        "error": None,
        "check_error": None,
        "rows": [
            {**row, "checked_at": None}
            for row in _subscription_dashboard_rows((), [snapshot])
        ],
    }
    path = tmp_path / "ai-subscriptions.json"
    path.write_text(
        json.dumps({"subscriptions": [{"provider": "driver-a", "account": "login@example.com"}]}),
        encoding="utf-8",
    )
    state = _subscriptions_dashboard(tmp_path, [snapshot], _NO_CHECKS, "device-a")
    assert state["configured"] is True and state["error"] is None
    assert state["rows"][0]["listed"] is True and state["rows"][0]["usage"] == snapshot

    path.write_text(json.dumps({"subscriptions": [{"provider": "Driver-A"}]}), encoding="utf-8")
    state = _subscriptions_dashboard(tmp_path, [snapshot], _NO_CHECKS, "device-a")
    assert state["configured"] is False
    assert "provider must be a lowercase token" in state["error"]
    assert [row["listed"] for row in state["rows"]] == [False]


def _usage_accounts_config(
    home: Path, accounts: dict[str, dict[str, str]], selected: list[str]
) -> None:
    """Placeholder drivers with a windows driver, and the given usage accounts."""
    document = driver_document()
    document["drivers"][WINDOWS_DRIVER_ID] = windows_driver()
    write_drivers(home, document)
    (home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "driver-a", "second": "driver-b"},
                "accounts": accounts,
                "usage_accounts": selected,
            }
        ),
        encoding="utf-8",
    )


def _login(config_dir: Path, email: str) -> None:
    config_dir.mkdir()
    (config_dir / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": email}}), encoding="utf-8"
    )


def test_subscription_rows_carry_the_check_time_of_their_configured_account(
    tmp_path: Path,
) -> None:
    _login(tmp_path / "seat", "Seat-1@Example.com")
    (tmp_path / "logged-out").mkdir()
    _usage_accounts_config(
        tmp_path,
        {
            "seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(tmp_path / "seat")},
            "logged-out": {
                "provider": WINDOWS_DRIVER_ID,
                "config_dir": str(tmp_path / "logged-out"),
            },
            "spend-a": {"provider": "driver-d", "config_dir": str(tmp_path / "spend-a")},
            "spend-b": {"provider": "driver-d", "config_dir": str(tmp_path / "spend-b")},
        },
        ["seat", "logged-out", "spend-a", "spend-b"],
    )
    meta = _RecordingChecks(
        {
            f"usage.checked.{WINDOWS_DRIVER_ID}.seat": _check(
                "2026-11-16T12:30:00Z", "seat-1@example.com"
            ),
            f"usage.checked.{WINDOWS_DRIVER_ID}.logged-out": _check(
                "2026-11-16T12:31:00Z", "gone@example.com"
            ),
            "usage.checked.driver-d.spend-a": _check("2026-11-16T12:32:00Z", "spend-a"),
        }
    )

    seat = _snapshot(
        WINDOWS_DRIVER_ID, "seat-1@example.com", "device-b", fetched_at="2026-11-16T09:00:00Z"
    )
    spend_here = _snapshot("driver-d", "spend-a", "device-a")
    spend_there = _snapshot("driver-d", "spend-a", "device-b")
    spend_unchecked = _snapshot("driver-d", "spend-b", "device-a")
    unread = _snapshot("driver-a", "login@example.com")
    usage = [seat, spend_here, spend_there, spend_unchecked, unread]

    state = _subscriptions_dashboard(tmp_path, usage, meta, "device-a")

    assert state["check_error"] is None
    assert [(row["account"], row["checked_at"]) for row in state["rows"]] == [
        # A windows login is one subscription: this device's check applies to
        # the newest snapshot of that login from any device.
        ("seat-1@example.com", "2026-11-16T12:30:00Z"),
        # Spend account names are device-local; another device's name is not ours.
        ("spend-a", "2026-11-16T12:32:00Z"),
        ("spend-a", None),
        ("spend-b", None),
        ("login@example.com", None),
    ]
    # Existing fields stay; the snapshot time stays the last change.
    assert state["rows"][0]["usage"]["payload"]["fetched_at"] == "2026-11-16T09:00:00Z"
    # The keys name configured accounts, never a login address.
    keys = meta.keys_read
    assert sorted(keys) == sorted(
        f"usage.checked.{provider}.{name}"
        for provider, name in (
            (WINDOWS_DRIVER_ID, "seat"),
            (WINDOWS_DRIVER_ID, "logged-out"),
            ("driver-d", "spend-a"),
            ("driver-d", "spend-b"),
        )
    )
    assert not any("@" in key for key in keys)

    # A listed windows subscription matches by its account even without a
    # snapshot; a listed spend account without a snapshot of this device does not.
    inventory = tmp_path / "ai-subscriptions.json"
    inventory.write_text(
        json.dumps(
            {
                "subscriptions": [
                    {"provider": WINDOWS_DRIVER_ID, "account": "SEAT-1@example.com"},
                    {"provider": "driver-d", "account": "spend-a"},
                ]
            }
        ),
        encoding="utf-8",
    )
    state = _subscriptions_dashboard(tmp_path, [], meta, "device-a")
    assert [(row["usage"], row["checked_at"]) for row in state["rows"]] == [
        (None, "2026-11-16T12:30:00Z"),
        (None, None),
    ]

    # The overview carries the same check time on each account entry.
    state = _subscriptions_dashboard(tmp_path, usage, meta, "device-a")
    overview = _overview(state["rows"])
    assert [
        (a["account"], a["checked_at"]) for a in overview[WINDOWS_DRIVER_ID]["accounts"]
    ] == [("SEAT-1@example.com", "2026-11-16T12:30:00Z")]
    assert [(a["account"], a["checked_at"]) for a in overview["driver-d"]["accounts"]] == [
        ("spend-a", "2026-11-16T12:32:00Z"),
        ("spend-a", None),
        ("spend-b", None),
    ]


def test_subscription_check_times_report_unreadable_configuration(tmp_path: Path) -> None:
    snapshot = _snapshot("driver-a", "login@example.com")
    (tmp_path / "ai-accounts.json").write_text("{", encoding="utf-8")
    state = _subscriptions_dashboard(tmp_path, [snapshot], _NO_CHECKS, "device-a")
    assert state["check_error"] == "Cannot read ai-accounts.json"
    assert [row["checked_at"] for row in state["rows"]] == [None]

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / WINDOWS_ACCOUNT_FILE).write_text("{", encoding="utf-8")
    _usage_accounts_config(
        tmp_path,
        {"broken": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(broken)}},
        ["broken"],
    )
    state = _subscriptions_dashboard(
        tmp_path,
        [snapshot],
        {
            f"usage.checked.{WINDOWS_DRIVER_ID}.broken": _check(
                "2026-11-16T12:30:00Z", "login@example.com"
            )
        },
        "device-a",
    )
    assert state["check_error"] == (
        f"broken: {WINDOWS_CREDENTIAL_LABEL} account file is not valid JSON"
    )
    assert [row["checked_at"] for row in state["rows"]] == [None]


def test_subscription_check_time_does_not_follow_a_new_login(tmp_path: Path) -> None:
    """A check read one login; another login in the same directory is not checked."""
    seat = tmp_path / "seat"
    _login(seat, "Seat-1@Example.com")
    _usage_accounts_config(
        tmp_path,
        {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
        ["seat"],
    )
    checks = {
        f"usage.checked.{WINDOWS_DRIVER_ID}.seat": _check(
            "2026-11-16T12:30:00Z", "seat-1@example.com"
        )
    }
    old = _snapshot(WINDOWS_DRIVER_ID, "seat-1@example.com", "device-a")
    new = _snapshot(WINDOWS_DRIVER_ID, "seat-2@example.com", "device-b")

    state = _subscriptions_dashboard(tmp_path, [old, new], checks, "device-a")
    assert [(row["account"], row["checked_at"]) for row in state["rows"]] == [
        ("seat-1@example.com", "2026-11-16T12:30:00Z"),
        ("seat-2@example.com", None),
    ]

    # Another login in the same directory; this device has not read it yet,
    # so the old check time stays with no row, neither the old nor the new login.
    (seat / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": "seat-2@example.com"}}), encoding="utf-8"
    )
    state = _subscriptions_dashboard(tmp_path, [old, new], checks, "device-a")
    assert state["check_error"] is None
    assert [(row["account"], row["checked_at"]) for row in state["rows"]] == [
        ("seat-1@example.com", None),
        ("seat-2@example.com", None),
    ]


def _rpc_accounts_config(home: Path, accounts: dict[str, dict[str, str]]) -> None:
    """Placeholder drivers with an rpc driver, every given account selected."""
    document = driver_document()
    document["drivers"][RPC_DRIVER_ID] = rpc_driver()
    write_drivers(home, document)
    (home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "driver-a", "second": "driver-b"},
                "accounts": accounts,
                "usage_accounts": list(accounts),
            }
        ),
        encoding="utf-8",
    )


def test_subscription_rows_carry_the_check_time_of_an_rpc_login(tmp_path: Path) -> None:
    """An rpc account matches by the identity of its login, like a windows account."""
    seat = tmp_path / "seat"
    rpc_write_auth(seat, email="Seat-1@Example.com")
    logged_out = tmp_path / "logged-out"
    logged_out.mkdir()
    _rpc_accounts_config(
        tmp_path,
        {
            "seat": {"provider": RPC_DRIVER_ID, "config_dir": str(seat)},
            "logged-out": {"provider": RPC_DRIVER_ID, "config_dir": str(logged_out)},
        },
    )
    checks = _RecordingChecks(
        {
            f"usage.checked.{RPC_DRIVER_ID}.seat": _check(
                "2026-11-16T12:30:00Z", "seat-1@example.com"
            ),
            f"usage.checked.{RPC_DRIVER_ID}.logged-out": _check(
                "2026-11-16T12:31:00Z", "gone@example.com"
            ),
        }
    )
    # Another device's snapshot of the same login still gets this device's check.
    seat_row = _snapshot(
        RPC_DRIVER_ID, "seat-1@example.com", "device-b", fetched_at="2026-11-16T09:00:00Z"
    )
    other = _snapshot(RPC_DRIVER_ID, "seat-2@example.com", "device-a")

    state = _subscriptions_dashboard(tmp_path, [seat_row, other], checks, "device-a")

    assert state["check_error"] is None
    assert [(row["account"], row["checked_at"]) for row in state["rows"]] == [
        ("seat-1@example.com", "2026-11-16T12:30:00Z"),
        ("seat-2@example.com", None),
    ]
    assert sorted(checks.keys_read) == [
        f"usage.checked.{RPC_DRIVER_ID}.logged-out",
        f"usage.checked.{RPC_DRIVER_ID}.seat",
    ]

    # An rpc provider is no spend provider: a listed login without a snapshot
    # still gets its check time.
    (tmp_path / "ai-subscriptions.json").write_text(
        json.dumps({"subscriptions": [{"provider": RPC_DRIVER_ID, "account": "SEAT-1@example.com"}]}),
        encoding="utf-8",
    )
    state = _subscriptions_dashboard(tmp_path, [], checks, "device-a")
    assert [(row["usage"], row["checked_at"]) for row in state["rows"]] == [
        (None, "2026-11-16T12:30:00Z")
    ]
    (tmp_path / "ai-subscriptions.json").unlink()

    # Another login in the same directory has not been read: no row gets the old time.
    rpc_write_auth(seat, email="seat-2@example.com")
    state = _subscriptions_dashboard(tmp_path, [seat_row, other], checks, "device-a")
    assert state["check_error"] is None
    assert [row["checked_at"] for row in state["rows"]] == [None, None]

    # An unreadable account file is reported, never matched.
    (seat / RPC_ACCOUNT_FILE).write_text("{", encoding="utf-8")
    state = _subscriptions_dashboard(tmp_path, [seat_row], checks, "device-a")
    assert state["check_error"] is not None
    assert state["check_error"].startswith(f"seat: {RPC_LABEL} account file")
    assert [row["checked_at"] for row in state["rows"]] == [None]


def test_subscription_check_time_reports_a_malformed_value(tmp_path: Path) -> None:
    _usage_accounts_config(
        tmp_path,
        {"spend-a": {"provider": "driver-d", "config_dir": str(tmp_path / "spend-a")}},
        ["spend-a"],
    )
    snapshot = _snapshot("driver-d", "spend-a", "device-a")
    for value, reason in (
        ("2026-11-16T12:30:00Z", "is not valid JSON"),
        ("[]", "is not an object"),
        (json.dumps({"identity": "spend-a"}), "has no checked_at"),
        (json.dumps({"checked_at": "2026-11-16T12:30:00Z"}), "has no identity"),
    ):
        state = _subscriptions_dashboard(
            tmp_path, [snapshot], {"usage.checked.driver-d.spend-a": value}, "device-a"
        )
        assert state["check_error"] == f"spend-a: usage check time {reason}"
        assert [row["checked_at"] for row in state["rows"]] == [None]


def test_subscriptions_section_markup() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    assert "<h1>Subscriptions</h1>" in html
    assert "<h2>Subscriptions</h2>" not in html
    assert "<h2>Usage</h2>" not in html
    assert '<a href="/usage" title="Subscriptions">' in html
    index = PAGE.read_text(encoding="utf-8")
    usage = index.split('href="/usage"', 1)[1].split("</a>", 1)[0]
    assert "Subscriptions" in usage
    header = html.split('<table id="usage"><thead><tr>', 1)[1].split("</tr>", 1)[0]
    assert header.count("<th>") == 12
    for column in ("Provider", "Account", "Label", "Status", "Note", "Listed", "Tier", "Used", "Reset"):
        assert f"<th>{column}</th>" in header
    # The raw snapshot time is replaced by two ages: last check and last change.
    assert header.endswith("<th>Owner</th><th>Checked</th><th>Changed</th>")
    assert "<th>Fetched</th>" not in html
    assert "${ageCell(row.checked_at, now)}${ageCell(p.fetched_at, now)}</tr>" in html
    assert "<td>${idOrText(p.fetched_at)}</td>" not in html
    # Both ages use the shared elapsedText; the page adds no second age helper.
    assert "function ageText(" not in html
    cell = section_function(html, "ageCell")
    assert 'return "<td>—</td>";' in cell
    assert "return `<td>${idOrText(value)}</td>`;" in cell
    assert "Number.isNaN(timestampMs(value))" in cell
    assert 'return `<td title="${esc(value)}">${esc(elapsedText(value, now))} ago</td>`;' in cell
    assert "const SUBSCRIPTION_COLUMNS = 12;" in html
    # A file error reuses the page's error line, only on this section.
    assert "const inventoryError = subscriptionsErrorText(subscriptions);" in html
    assert 'if (section === "usage" && inventoryError) {' in html
    assert "err.textContent = inventoryError;" in html
    assert "|| subscriptionsEmptyHtml(subscriptions));" in html


def section_function(html: str, name: str) -> str:
    start = html.index(f"\n    function {name}(") + 1
    return html[start : html.index("\n    }\n", start) + 7]


def test_subscription_rows_render_in_node() -> None:
    if NODE is None:
        pytest.skip("node is not installed; the static tests above cover the source")
    html = SECTION_PAGE.read_text(encoding="utf-8")
    script = re.findall(r"<script>(.*?)</script>", html, flags=re.S)[0]
    helpers = "\n".join(
        line for line in script.splitlines()
        if line.startswith(("    function empty(", "    const SUBSCRIPTION_COLUMNS", "    const NO_USAGE", "    const NO_INVENTORY"))
    )
    functions = "\n".join(
        section_function(html, name)
        for name in (
            "percentText",
            "windowLines",
            "usageCells",
            "ageCell",
            "subscriptionRowHtml",
            "subscriptionsEmptyHtml",
            "subscriptionsErrorText",
        )
    )
    # work.js brings esc, RING and chip; the shared block brings idOrText and stateIcon.
    shared = section_runtime(html)
    harness = helpers + "\n" + shared + "\n" + functions + """
const now = Date.parse("2026-11-16T12:04:30Z");
const windowed = {
  provider: "driver-a", account: "seat-1@example.com", label: "Seat <1>", status: "active", note: "a & b", listed: true,
  checked_at: "2026-11-16T12:04:00Z",
  usage: { _origin_device_id: "device-a", payload: {
    provider: "driver-a", vendor: "driver-a", account_email: "seat-1@example.com", tier: "max_20x", fetched_at: "2026-11-16T12:00:00Z",
    windows: {
      five_hour: null,
      weekly: { used_percent: 14.0, resets_at: "2026-11-23T10:00:00+00:00" },
      weekly_models: [{ model: "<b>Model B</b>", used_percent: 21.5, resets_at: null }],
    },
  } },
};
const missing = { provider: "driver-a", account: "seat-2@example.com", label: null, status: null, note: null, listed: true, checked_at: null, usage: null };
const rpc = {
  provider: "driver-r", account: "seat-1@example.com", label: null, status: null, note: null, listed: true,
  checked_at: "2026-11-16T12:04:00Z",
  usage: { _origin_device_id: "device-a", payload: {
    provider: "driver-r", vendor: "driver-r", account_email: "seat-1@example.com", tier: "pro", fetched_at: "2026-11-16T12:00:00Z",
    windows: {
      five_hour: null,
      weekly: { used_percent: 100.0, resets_at: "2026-11-19T13:53:00+00:00" },
      weekly_models: [],
      other: [{ window_minutes: 1440, used_percent: 7.5, resets_at: null }],
    },
    limit_reached: true, reset_credits_available: 1,
  } },
};
const rpcQuiet = { ...rpc, usage: { ...rpc.usage, payload: { ...rpc.usage.payload,
  windows: { five_hour: { used_percent: 3.0, resets_at: "2026-11-16T15:00:00+00:00" }, weekly: null, weekly_models: [], other: [] },
  limit_reached: false, reset_credits_available: 0 } } };
const stray = { provider: "driver-d", account: "router-a", listed: false, checked_at: null,
  usage: { _origin_device_id: "device-b", payload: { provider: "driver-d", account: "router-a", usage_usd: 1.25, limit_usd: null, limit_reset: "monthly" } } };
console.log(JSON.stringify({
  cells: [
    ageCell("2026-11-16T12:04:00Z", now),
    ageCell("2026-11-16T12:05:00Z", now),
    ageCell("2026-11-16T12:00:00", now),
    ageCell(null, now),
    ageCell(undefined, now),
    ageCell("", now),
    ageCell("<not a time>", now),
  ],
  windowed: subscriptionRowHtml(windowed, now),
  older: subscriptionRowHtml({ ...windowed, checked_at: "2026-11-14T09:00:00Z" }, now),
  rpc: subscriptionRowHtml(rpc, now),
  rpcQuiet: subscriptionRowHtml(rpcQuiet, now),
  missing: subscriptionRowHtml(missing, now),
  stray: subscriptionRowHtml(stray, now),
  unknownStatus: subscriptionRowHtml({ ...missing, status: "on-hold" }, now),
  checkError: subscriptionsErrorText({ configured: true, error: null, check_error: "seat: <broken>", rows: [] }),
  bothErrors: subscriptionsErrorText({ configured: false, error: "subscriptions must be a list", check_error: "seat: broken", rows: [] }),
  absent: subscriptionsEmptyHtml({ configured: false, error: null, rows: [] }),
  absentError: subscriptionsErrorText({ configured: false, error: null, rows: [] }),
  invalid: subscriptionsEmptyHtml({ configured: false, error: "subscriptions must be a list", rows: [] }),
  invalidError: subscriptionsErrorText({ configured: false, error: "subscriptions must be a list", rows: [] }),
  configuredEmpty: subscriptionsEmptyHtml({ configured: true, error: null, rows: [] }),
  iconActive: stateIcon("active"),
  iconUnknown: stateIcon("on-hold"),
  iconEmpty: stateIcon(""),
  iconNull: stateIcon(null),
}));
"""
    completed = subprocess.run(
        [NODE, "-"], input=harness, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    windowed = out["windowed"]
    assert "<td>Seat &lt;1&gt;</td>" in windowed
    assert "<td>a &amp; b</td>" in windowed
    assert "<td>listed</td>" in windowed
    active = out["iconActive"]
    assert "state-unknown" not in active
    assert '<span class="label">' not in active
    assert 'role="img"' in active and 'aria-label="active"' in active
    assert (
        '<td title="active" aria-label="active">' + active + "</td>"
    ) in windowed
    unknown = out["iconUnknown"]
    assert "state-unknown" in unknown
    assert '<span class="label">' not in unknown
    assert 'aria-label="on-hold"' in unknown
    assert (
        '<td title="on-hold" aria-label="on-hold">' + unknown + "</td>"
    ) in out["unknownStatus"]
    assert out["iconEmpty"] == "—" and out["iconNull"] == "—"
    assert "<td>—</td>" in out["missing"]
    assert "<td>max_20x</td>" in windowed
    assert "<td>5h: —<br>Week: 14%<br>Week &lt;b&gt;Model B&lt;/b&gt;: 21.5%</td>" in windowed
    assert "<td>5h: —<br>Week: 2026-11-23T10:00:00+00:00<br>Week &lt;b&gt;Model B&lt;/b&gt;: —</td>" in windowed
    assert "<b>" not in windowed
    assert "limit reached" not in windowed and "free resets" not in windowed
    rpc = out["rpc"]
    assert "<td>driver-r</td>" in rpc
    assert "<td>pro</td>" in rpc
    assert (
        "<td>5h: —<br>Week: 100%<br>1440 min: 7.5%<br>limit reached<br>free resets: 1</td>"
    ) in rpc
    assert "<td>5h: —<br>Week: 2026-11-19T13:53:00+00:00<br>1440 min: —</td>" in rpc
    # An rpc row shows Checked and Changed like a windows row.
    assert rpc.endswith(
        '<td title="2026-11-16T12:04:00Z">30 s ago</td>'
        '<td title="2026-11-16T12:00:00Z">4 min ago</td></tr>'
    )
    assert rpc.count("<td") == 12
    quiet = out["rpcQuiet"]
    assert "<td>5h: 3%<br>Week: —</td>" in quiet
    assert "<td>5h: 2026-11-16T15:00:00+00:00<br>Week: —</td>" in quiet
    assert "limit reached" not in quiet and "free resets" not in quiet
    assert out["cells"] == [
        '<td title="2026-11-16T12:04:00Z">30 s ago</td>',
        # A time in the future (clock skew) is never a negative age.
        '<td title="2026-11-16T12:05:00Z">0 s ago</td>',
        # A time without a zone designator is UTC, as the shared timestampMs reads it.
        '<td title="2026-11-16T12:00:00">4 min ago</td>',
        "<td>—</td>",
        "<td>—</td>",
        "<td>—</td>",
        "<td>&lt;not a time&gt;</td>",
    ]
    # Checked (row.checked_at) then Changed (the snapshot's fetched_at), each
    # with the exact time as its title.
    assert windowed.endswith(
        '<td title="2026-11-16T12:04:00Z">30 s ago</td>'
        '<td title="2026-11-16T12:00:00Z">4 min ago</td></tr>'
    )
    assert out["older"].endswith(
        '<td title="2026-11-14T09:00:00Z">2 d ago</td>'
        '<td title="2026-11-16T12:00:00Z">4 min ago</td></tr>'
    )
    assert out["missing"].endswith("<td>—</td><td>—</td></tr>")
    assert out["stray"].endswith("<td>—</td><td>—</td></tr>")
    assert out["checkError"] == "usage checks: seat: <broken>"
    assert out["bothErrors"] == (
        "ai-subscriptions.json: subscriptions must be a list; usage checks: seat: broken"
    )
    assert out["missing"].count("<td>") == 12
    assert windowed.count("<td") == 12
    assert "<td>No usage: no paired device polls this login</td>" in out["missing"]
    assert "0%" not in out["missing"]
    assert "<td>not listed</td>" in out["stray"]
    assert "<td>$1.25 / $unlimited</td>" in out["stray"]
    assert "<td>monthly</td>" in out["stray"]
    absent = "No subscription inventory: $AGENT_HOME/ai-subscriptions.json is absent"
    assert absent in out["absent"]
    assert out["absentError"] is None
    assert absent not in out["invalid"]
    assert out["invalid"] == "<tr><td colspan='12'>—</td></tr>"
    assert out["invalidError"] == "ai-subscriptions.json: subscriptions must be a list"
    assert out["configuredEmpty"] == "<tr><td colspan='12'>—</td></tr>"


OVERVIEW_NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
# Period, spend, windows and rpc usage drivers; driver-b, driver-c and driver-e have no usage section.
OVERVIEW_DRIVERS = parse_driver_document(
    {
        **driver_document(),
        "drivers": {
            **driver_document()["drivers"],
            WINDOWS_DRIVER_ID: windows_driver(),
            RPC_DRIVER_ID: rpc_driver(),
        },
    }
).drivers


def _recorded(payload: dict, device: str = "device-a") -> dict:
    return {"type": "usage.snapshot", "_origin_device_id": device, "payload": payload}


def _spend(body: dict, fetched_at: str = "2026-11-17T09:00:00+00:00") -> dict:
    return spend_snapshot(SPEND_DRIVER, _usage(SPEND_DRIVER), "router-a", {"key": body}, fetched_at)


def _epoch(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def _rpc(raw: dict, email: str, fetched_at: str) -> dict:
    return rpc_snapshot(RPC_DRIVER_ID, RPC_ADAPTER, raw, email, fetched_at)


def _overview_rows() -> list[dict]:
    """Subscription rows built from the payloads the usage adapters store."""
    seat_1 = mapped("seat-1@example.com", "max_20x", usage_body(), "2026-11-17T09:56:00+00:00")
    seat_3 = mapped("seat-3@example.com", "max_5x", usage_body(weekly=32.0), "2026-11-17T08:00:00+00:00")
    period = period_snapshot(
        PERIOD_DRIVER,
        _usage(PERIOD_DRIVER),
        _period_documents(used=0.0),
        "2026-11-17T09:59:30+00:00",
        "period@example.com",
    )
    period["period_end"] = "2026-11-20T14:00:00+00:00"
    spend = _spend(_key_body())
    rpc = _rpc(
        rpc_result(
            primary=rpc_window(12.5, 300, _epoch(2026, 11, 17, 12, 0)),
            secondary=rpc_window(40, 10080, _epoch(2026, 11, 19, 10, 0)),
        ),
        "rpc-1@example.com",
        "2026-11-17T09:58:00+00:00",
    )
    usage = _usage_dashboard_rows([_recorded(p) for p in (seat_1, seat_3, period, spend, rpc)])
    inventory = (
        AISubscription(WINDOWS_DRIVER_ID, "seat-1@example.com", "Team seat 1"),
        AISubscription(WINDOWS_DRIVER_ID, "seat-2@example.com"),
        AISubscription("driver-b", "seat-1@example.com"),
    )
    return _subscription_dashboard_rows(inventory, usage)


def _overview(rows: list[dict]) -> dict[str, dict]:
    return {entry["provider"]: entry for entry in _usage_overview(rows, OVERVIEW_NOW, OVERVIEW_DRIVERS)}


def test_usage_duration_text() -> None:
    assert _usage_duration_text(-5) == "<1 min"
    assert _usage_duration_text(59) == "<1 min"
    assert _usage_duration_text(4 * 60 + 59) == "4 min"
    assert _usage_duration_text(3 * 3600 + 12 * 60) == "3 h 12 min"
    assert _usage_duration_text(5 * 86400 + 19 * 3600 + 59 * 60) == "5 d 19 h"


def test_usage_overview_one_entry_per_provider() -> None:
    overview = _overview(_overview_rows())
    assert list(overview) == [
        "driver-a",
        "driver-b",
        "driver-c",
        "driver-d",
        "driver-e",
        RPC_DRIVER_ID,
        WINDOWS_DRIVER_ID,
    ]

    windows = overview[WINDOWS_DRIVER_ID]
    # The name is the driver's display_name; the headline is the weekly window of the most used account.
    assert windows["name"] == "Login"
    assert (windows["used_percent"], windows["window"], windows["resets_in"]) == (32.0, "Week", "6 d 0 h")
    assert windows["summary"] == "Login 32% 6 d 0 h"
    assert windows["status"] is None and windows["recorded"] == "2026-11-17T09:56:00+00:00"
    assert [(w["name"], w["used_percent"], w["resets_in"]) for w in windows["windows"]] == [
        ("Week", 32.0, "6 d 0 h"),
        ("Week model-x", 21.0, "6 d 0 h"),
    ]
    assert [
        (a["account"], a["label"], a["used_percent"], a["status"], a["recorded"]) for a in windows["accounts"]
    ] == [
        ("seat-1@example.com", "Team seat 1", 14.0, None, "2026-11-17T09:56:00+00:00"),
        ("seat-2@example.com", None, None, "no data", None),
        ("seat-3@example.com", None, 32.0, None, "2026-11-17T08:00:00+00:00"),
    ]

    assert [a["has_snapshot"] for a in windows["accounts"]] == [True, False, True]

    # An rpc adapter is read like a windows adapter, not "not read".
    rpc = overview[RPC_DRIVER_ID]
    assert rpc["status"] is None
    assert rpc["summary"] == "Vendor R 40% 2 d 0 h"
    assert [(w["name"], w["used_percent"], w["resets_in"]) for w in rpc["windows"]] == [
        ("5h", 12.5, "2 h 0 min"),
        ("Week", 40.0, "2 d 0 h"),
    ]
    assert [(a["account"], a["used_percent"], a["recorded"]) for a in rpc["accounts"]] == [
        ("rpc-1@example.com", 40.0, "2026-11-17T09:58:00+00:00")
    ]

    # A driver without a usage section in drivers.json is not read.
    unread = overview["driver-b"]
    assert unread["status"] == "not read" and unread["used_percent"] is None
    assert unread["summary"] == "Vendor B not read"
    assert [(a["account"], a["status"]) for a in unread["accounts"]] == [("seat-1@example.com", "not read")]
    # A driver without a display_name is named by its id.
    assert overview["driver-e"]["summary"] == "driver-e not read"

    period = overview["driver-a"]
    assert period["summary"] == "Vendor A 0% 3 d 4 h"
    assert period["used_percent"] == 0.0 and period["recorded"] == "2026-11-17T09:59:30+00:00"
    assert [(w["name"], w["used_percent"]) for w in period["windows"]] == [("Period", 0.0), ("product-a", 0.0)]

    spend = overview["driver-d"]
    assert spend["summary"] == "Vendor D 12.5% 13 d 14 h"
    assert spend["accounts"][0]["text"] == "$1.25 / $10"


def test_usage_overview_lists_every_configured_driver_and_every_row_provider() -> None:
    stray = _snapshot("driver-x", "seat-9@example.com")
    rows = _subscription_dashboard_rows((), [stray])
    overview = {entry["provider"]: entry for entry in _usage_overview(rows, OVERVIEW_NOW, {})}
    # Without a usage section the id is the name; a recorded snapshot means the
    # provider is read somewhere, so it is "no data" rather than "not read".
    assert list(overview) == ["driver-x"]
    assert overview["driver-x"]["status"] == "no data"
    assert overview["driver-x"]["summary"] == "driver-x no data"


def test_usage_overview_without_data_never_shows_zero() -> None:
    overview = _overview([])
    assert [(p, e["status"], e["summary"]) for p, e in overview.items()] == [
        ("driver-a", "no data", "Vendor A no data"),
        ("driver-b", "not read", "Vendor B not read"),
        ("driver-c", "not read", "Vendor C not read"),
        ("driver-d", "no data", "Vendor D no data"),
        ("driver-e", "not read", "driver-e not read"),
        (RPC_DRIVER_ID, "no data", "Vendor R no data"),
        (WINDOWS_DRIVER_ID, "no data", "Login no data"),
    ]
    unlimited = _key_body()
    unlimited["data"].update({"limit": None, "limit_remaining": None, "limit_reset": "monthly"})
    empty = mapped("seat-1@example.com", None, {"five_hour": None, "seven_day": None}, "x")
    rpc_empty = _rpc(
        {"limits": rpc_limits(primary=None, secondary=None, plan=None, reached=None)},
        "rpc-1@example.com",
        "x",
    )
    rows = _subscription_dashboard_rows(
        (), [_recorded(_spend(unlimited)), _recorded(empty), _recorded(rpc_empty)]
    )
    overview = _overview(rows)
    spend = overview["driver-d"]
    assert spend["used_percent"] is None and spend["status"] is None
    assert spend["summary"] == "Vendor D $1.25 / unlimited (monthly)"
    # A window snapshot without any window is no data, for the windows and the rpc adapter alike.
    for provider in (WINDOWS_DRIVER_ID, RPC_DRIVER_ID):
        entry = overview[provider]
        assert entry["status"] == "no data"
        assert entry["accounts"][0]["used_percent"] is None
        assert entry["accounts"][0]["status"] == "no data"
        assert entry["accounts"][0]["recorded"] is None


def test_usage_overview_without_readable_drivers_claims_no_provider_unread() -> None:
    # drivers.json could not be read: only recorded providers, and none is "not read".
    rows = _subscription_dashboard_rows((AISubscription("driver-b", "seat-1@example.com"),), [])
    overview = _usage_overview(rows, OVERVIEW_NOW, None)
    assert [(e["provider"], e["status"], e["summary"]) for e in overview] == [
        ("driver-b", "no data", "driver-b no data")
    ]
    assert overview[0]["accounts"][0]["status"] == "no data"

    # A provider without a usage section whose snapshot another device recorded is no data, not "not read".
    stray = {**mapped("seat-1@example.com", None, {"five_hour": None, "seven_day": None}, "x"), "provider": "driver-b"}
    rows = _subscription_dashboard_rows((), [_recorded(stray)])
    assert _overview(rows)["driver-b"]["status"] == "no data"


def test_usage_overview_windows_headline_is_weekly_only() -> None:
    body = usage_body(five_hour={"utilization": 80.0, "resets_at": "2026-11-17T12:00:00+00:00"})
    body.update({"seven_day": None, "limits": []})
    only_five_hour = mapped("seat-1@example.com", None, body, "2026-11-17T09:56:00+00:00")
    rpc_five_hour = _rpc(
        rpc_result(primary=rpc_window(80, 300, _epoch(2026, 11, 17, 12, 0))),
        "rpc-1@example.com",
        "2026-11-17T09:56:00+00:00",
    )
    rows = _subscription_dashboard_rows((), [_recorded(only_five_hour), _recorded(rpc_five_hour)])
    overview = _overview(rows)
    for provider, name in ((WINDOWS_DRIVER_ID, "Login"), (RPC_DRIVER_ID, "Vendor R")):
        entry = overview[provider]
        # The five-hour window is shown, but never as the weekly headline.
        assert [(w["name"], w["used_percent"], w["resets_in"]) for w in entry["windows"]] == [
            ("5h", 80.0, "2 h 0 min")
        ]
        assert entry["used_percent"] is None and entry["window"] is None
        assert entry["status"] == "no data" and entry["summary"] == f"{name} no data"
        assert entry["accounts"][0]["used_percent"] is None
        assert entry["accounts"][0]["status"] == "no data"

    weekly = mapped("seat-2@example.com", None, usage_body(), "2026-11-17T09:56:00+00:00")
    rows = _subscription_dashboard_rows((), [_recorded(only_five_hour), _recorded(weekly)])
    assert _overview(rows)[WINDOWS_DRIVER_ID]["summary"] == "Login 14% 6 d 0 h"


def test_usage_overview_names_windows_of_other_lengths() -> None:
    rpc = _rpc(
        rpc_result(primary=rpc_window(7.5, 1440, None), secondary=rpc_window(40, 10080, None)),
        "rpc-1@example.com",
        "2026-11-17T09:56:00+00:00",
    )
    entry = _overview(_subscription_dashboard_rows((), [_recorded(rpc)]))[RPC_DRIVER_ID]
    assert [(w["name"], w["used_percent"], w["resets_in"]) for w in entry["windows"]] == [
        ("Week", 40.0, None),
        ("1440 min", 7.5, None),
    ]
    assert entry["summary"] == "Vendor R 40%"


def test_usage_overview_skips_malformed_windows_of_other_lengths() -> None:
    """Replicated data may hold anything; only integer window lengths are named."""
    rpc = _rpc(
        rpc_result(primary=rpc_window(40, 10080, None)),
        "rpc-1@example.com",
        "2026-11-17T09:56:00+00:00",
    )
    rpc["windows"]["other"] = [
        "junk",
        {"window_minutes": True, "used_percent": 2},
        {"window_minutes": "300", "used_percent": 3},
        {"window_minutes": 1440, "used_percent": 1},
    ]
    entry = _overview(_subscription_dashboard_rows((), [_recorded(rpc)]))[RPC_DRIVER_ID]
    assert [(w["name"], w["used_percent"]) for w in entry["windows"]] == [
        ("Week", 40.0),
        ("1440 min", 1.0),
    ]


def test_usage_overview_ignores_times_outside_the_utc_range() -> None:
    edge = "0001-01-01T00:00:00+14:00"
    snapshot = mapped("seat-1@example.com", None, usage_body(), edge)
    snapshot["windows"]["weekly"]["resets_at"] = edge
    rows = _subscription_dashboard_rows((), [_recorded(snapshot)])
    windows = _overview(rows)[WINDOWS_DRIVER_ID]
    assert windows["used_percent"] == 14.0 and windows["resets_in"] is None
    assert windows["recorded"] is None and windows["summary"] == "Login 14%"


def test_usage_overview_ignores_numbers_outside_the_float_range() -> None:
    snapshot = mapped("seat-1@example.com", None, usage_body(), "2026-11-17T09:56:00+00:00")
    snapshot["windows"]["weekly"]["used_percent"] = 10**400
    snapshot["windows"]["weekly_models"][0]["used_percent"] = float("inf")
    key = _key_body()
    key["data"].update({"limit": 1e308, "limit_remaining": -1e308})
    rows = _subscription_dashboard_rows((), [_recorded(snapshot), _recorded(_spend(key))])
    overview = _overview(rows)
    windows = overview[WINDOWS_DRIVER_ID]
    assert [(w["name"], w["used_percent"]) for w in windows["windows"]] == [("Week", None), ("Week model-x", None)]
    assert windows["used_percent"] is None and windows["status"] == "no data"
    assert overview["driver-d"]["used_percent"] is None
    assert overview["driver-d"]["summary"] == "Vendor D $1.25 / $1e+308"
    json.dumps(list(overview.values()), allow_nan=False)


def test_usage_overview_markup() -> None:
    index = PAGE.read_text(encoding="utf-8")
    assert '<p>Subscriptions: <span id="usage-summary">—</span></p>' in index
    assert 'setHtml(document.getElementById("usage-summary"), usageSummaryHtml(data.usage_overview || []));' in index
    html = SECTION_PAGE.read_text(encoding="utf-8")
    assert '<div id="usage-overview"></div>' in html
    assert 'id="usage-detailed"' in html and 'id="usage-sort"' in html
    # The viewer's open providers follow the details element, outside "detailed".
    assert 'addEventListener("toggle", event => {' in html
    assert "if (details.open) usageOpen.add(details.dataset.provider);" in html
    assert "usageOverview = data.usage_overview || [];" in html


def test_usage_overview_renders_in_node() -> None:
    if NODE is None:
        pytest.skip("node is not installed; the static tests above cover the source")
    html = SECTION_PAGE.read_text(encoding="utf-8")
    script = re.findall(r"<script>(.*?)</script>", html, flags=re.S)[0]
    helpers = section_runtime(html) + "\n" + "\n".join(
        line for line in script.splitlines() if line.startswith("    const NO_USAGE")
    )
    functions = "\n".join(
        section_function(html, name) for name in ("usageMeter", "usageLineHtml", "usageOverviewHtml")
    )
    overview = list(_overview(_overview_rows()).values())
    overview[-1]["accounts"][0]["label"] = "<b>seat</b>"
    harness = helpers + "\n" + functions + f"""
const overview = {json.dumps(overview)};
const NOW = Date.parse("2026-11-17T10:00:00Z");
console.log(JSON.stringify({{
  closed: usageOverviewHtml(overview, new Set(), false, false, NOW),
  open: usageOverviewHtml(overview, new Set(["driver-w"]), false, false, NOW),
  detailed: usageOverviewHtml(overview, new Set(), true, false, NOW),
  sorted: usageOverviewHtml(overview, new Set(), false, true, NOW),
  none: usageOverviewHtml([], new Set(), false, false, NOW),
  iconNotRead: stateIcon("not read"),
  iconNoData: stateIcon("no data"),
}}));
"""
    completed = subprocess.run(
        [NODE, "-"], input=harness, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    closed = out["closed"]
    assert closed.count('<details class="usage-provider"') == 7
    assert " open>" not in closed
    assert (
        '<summary><span class="usage-line"><span><b>Login</b></span><span>6 d 0 h</span>'
        '<meter min="0" max="100" low="70" high="90" optimum="0" value="32" title="32%"></meter>'
        "<span>32%</span></span></summary>"
    ) in closed
    assert (
        '<summary><span class="usage-line"><span><b>Vendor R</b></span><span>2 d 0 h</span>'
        '<meter min="0" max="100" low="70" high="90" optimum="0" value="40" title="40%"></meter>'
        "<span>40%</span></span></summary>"
    ) in closed
    not_read = out["iconNotRead"]
    assert "state-unknown" not in not_read and '<span class="label">' not in not_read
    assert 'role="img"' in not_read and 'aria-label="not read"' in not_read
    assert ('<span title="not read" aria-label="not read">' + not_read + "</span>") in closed
    # A listed account without a snapshot carries the table's explanation on its
    # name; its icon keeps the state word as title.
    no_data = out["iconNoData"]
    assert "state-unknown" not in no_data and '<span class="label">' not in no_data
    assert 'aria-label="no data"' in no_data
    assert (
        '<span class="usage-line"><span title="No usage: no paired device polls this login">'
        "seat-2@example.com</span><span></span>"
        '<span title="no data" aria-label="no data">' + no_data + "</span>"
    ) in out["open"]
    assert out["open"].count("No usage: no paired device polls this login") == 1
    assert "0%" not in closed.split('data-provider="driver-b"', 1)[1].split("</summary>", 1)[0]
    # The browser renders the age of the raw timestamp with the shared elapsedText.
    assert '<span class="usage-muted" title="2026-11-17T09:56:00+00:00">recorded 4 min ago</span>' in closed
    assert "recorded 2 h ago" in closed
    assert '<p class="usage-muted" title="2026-11-17T09:59:30+00:00">recorded 30 s ago</p>' in closed
    assert "&lt;b&gt;seat&lt;/b&gt;" in closed and "<b>seat</b>" not in closed
    assert '<details class="usage-provider" data-provider="driver-w" open>' in out["open"]
    assert out["open"].count(" open>") == 1
    assert out["detailed"].count(" open>") == 7
    order = re.findall(r'data-provider="([a-z-]+)"', out["sorted"])
    assert order == ["driver-r", "driver-w", "driver-d", "driver-a", "driver-b", "driver-c", "driver-e"]
    assert out["none"] == '<p class="usage-muted">—</p>'


def test_usage_overview_shows_when_each_account_was_checked() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    overview_html = section_function(html, "usageOverviewHtml")
    assert "checked ${esc(elapsedText(a.checked_at, now))} ago" in overview_html
    assert 'title="${esc(a.checked_at)}"' in overview_html
    if NODE is None:
        pytest.skip("node is not installed; the static checks above cover the source")
    script = re.findall(r"<script>(.*?)</script>", html, flags=re.S)[0]
    helpers = section_runtime(html) + "\n" + "\n".join(
        line for line in script.splitlines() if line.startswith("    const NO_USAGE")
    )
    functions = "\n".join(
        section_function(html, name) for name in ("usageMeter", "usageLineHtml", "usageOverviewHtml")
    )

    def account(name: str, checked_at: str | None, recorded: str | None) -> dict:
        return {
            "account": name,
            "label": None,
            "listed": True,
            "origin": "device-a",
            "has_snapshot": recorded is not None,
            "status": None if recorded else "no data",
            "used_percent": 10.0 if recorded else None,
            "resets_in": None,
            "text": None,
            "recorded": recorded,
            "checked_at": checked_at,
        }

    overview = [
        {
            "provider": "driver-w",
            "name": "Login",
            "status": None,
            "used_percent": 10.0,
            "window": "Week",
            "resets_in": None,
            "text": None,
            "summary": "Login 10%",
            "recorded": "2026-11-17T09:56:00Z",
            "windows": [],
            "accounts": [
                account("seat-1@example.com", "2026-11-17T09:59:30Z", "2026-11-17T09:56:00Z"),
                account("seat-2@example.com", None, "2026-11-17T08:00:00Z"),
                account("seat-3@example.com", "2026-11-17T09:58:00Z", None),
            ],
        }
    ]
    harness = helpers + "\n" + functions + f"""
const NOW = Date.parse("2026-11-17T10:00:00Z");
console.log(JSON.stringify(usageOverviewHtml({json.dumps(overview)}, new Set(), true, false, NOW)));
"""
    completed = subprocess.run(
        [NODE, "-"], input=harness, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    shown = json.loads(completed.stdout)
    # checked (the last successful read) comes before recorded (the last change).
    assert (
        'seat-1@example.com<br><span class="usage-muted" title="2026-11-17T09:59:30Z">'
        'checked 30 s ago</span><br><span class="usage-muted" title="2026-11-17T09:56:00Z">'
        "recorded 4 min ago</span>"
    ) in shown
    # Without a check time nothing is shown for it.
    assert (
        'seat-2@example.com<br><span class="usage-muted" title="2026-11-17T08:00:00Z">'
        "recorded 2 h ago</span>"
    ) in shown
    assert (
        'seat-3@example.com<br><span class="usage-muted" title="2026-11-17T09:58:00Z">'
        "checked 2 min ago</span></span>"
    ) in shown
    assert shown.count("checked ") == 2


def design_esc() -> str:
    """The one esc index.html uses. It lives in the design script, not on the page."""
    text = (STATIC / "design" / "work.js").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("const esc = "):
            return line
    raise AssertionError("work.js does not define esc")


def test_usage_summary_renders_in_node() -> None:
    if NODE is None:
        pytest.skip("node is not installed; the static tests above cover the source")
    index = static_page("index.html")
    harness = (
        section_runtime(index)
        + section_function(index, "usageSummaryHtml")
        + f"""
console.log(JSON.stringify({{
  line: usageSummaryHtml({json.dumps(list(_overview(_overview_rows()).values()))}),
  none: usageSummaryHtml([]),
}}));
"""
    )
    completed = subprocess.run(
        [NODE, "-"], input=harness, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    icon = (
        '<span title="not read" aria-label="not read">'
        '<span class="ring" role="img" aria-label="not read">'
        '<svg viewBox="0 0 16 16" aria-hidden="true">'
        '<g class="dash-spin"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--neutral-500)" '
        'stroke-width="2" stroke-dasharray="2.4 2.3"/></g></svg></span></span>'
    )
    assert out["line"] == (
        f"Vendor A 0% 3 d 4 h · Vendor B {icon} · Vendor C {icon}"
        f" · Vendor D 12.5% 13 d 14 h · driver-e {icon} · Vendor R 40% 2 d 0 h · Login 32% 6 d 0 h"
    )
    assert out["none"] == "—"


def static_page(name: str) -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / name
    ).read_text(encoding="utf-8")


def test_orders_view_markup() -> None:
    html = static_page("index.html")
    ids = (
        "orders-note",
        "order-new",
        "orders",
        "order-detail",
        "od-id",
        "od-body",
        "od-question",
        "od-opinion",
        "od-reply-body",
        "od-reply",
        "od-override-check",
        "od-override-reason",
        "od-override",
        "od-cancel-reason",
        "od-cancel",
        "od-done-reason",
        "od-done",
        "order-report",
        "od-report",
        "od-report-body",
        "od-err",
        "ev-wake",
        "ev-unacked",
        "order-events",
    )
    for element_id in ids:
        assert html.count(f'id="{element_id}"') == 1

    for element_id in (
        "order-submit",
        "os-repo",
        "os-title",
        "os-base",
        "os-worker",
        "os-notify",
        "os-max-rounds",
        "os-body",
        "os-skips",
        "os-add-skip",
        "os-send",
        "os-result",
    ):
        assert html.count(f'id="{element_id}"') == 0

    assert "<h2>Orders</h2>" in html
    assert "<h2>Order events</h2>" in html
    assert html.count("New order") == 1
    assert 'href="/orders/new"' in html
    for text in (
        "Order",
        "Reply",
        "Override",
        "Cancel order",
        "Mark done",
        "Load report",
        "wake only",
        "unacknowledged",
        "Acknowledge",
        "Orders are not enabled on this device.",
    ):
        assert text in html
    for placeholder in ("reply", "reason"):
        assert f'placeholder="{placeholder}"' in html
    for placeholder in (
        "owner/name",
        "title",
        "base",
        "worker",
        "notify session",
        "max rounds",
        "order text",
    ):
        assert f'placeholder="{placeholder}"' not in html

    device = html.index('Device <code id="dev"')
    orders = html.index("<h2>Orders</h2>")
    events = html.index("<h2>Order events</h2>")
    nav = html.index('id="section-nav"')
    assert nav < device < orders < events
    for _, title, _ in MOVED_SECTIONS:
        assert f"<h2>{title}</h2>" not in html
    assert "session-row" not in html
    assert "tr.order-row" in html
    for asset in (
        'href="/design/tokens.css"',
        'href="/design/brand.css"',
        'href="/design/motion-base.css"',
        'href="/design/work.css"',
        'href="/design/motion.css"',
        'src="/design/work.js"',
    ):
        assert asset in html
    assert 'id="composer"' in html
    assert "<form" in html
    without_github = html.replace("https://github.com/", "")
    assert "http://" not in without_github
    assert "https://" not in without_github
    assert ".reverse(" not in html


def test_top_message_reads_the_feed_messages_not_the_feed_children() -> None:
    body = section_function(static_page("index.html"), "topMessage")
    events = body.index('getElementById("feed-events").children')
    card = body.index('getElementById("od-body")')
    report = body.index('getElementById("order-report")')
    park = body.index("nodes.push(park)")
    assert events < card < report < park
    assert "park.hidden" in body
    assert "feed.children" not in body
    assert ".getBoundingClientRect().bottom > limit + 8" in body


def test_new_order_subpage_markup() -> None:
    html = static_page("orders_new.html")
    ids = (
        "order-submit",
        "os-repo",
        "os-title",
        "os-base",
        "os-worker",
        "os-notify",
        "os-max-rounds",
        "os-body",
        "os-skips",
        "os-add-skip",
        "os-send",
        "os-result",
    )
    for element_id in ids:
        assert html.count(f'id="{element_id}"') == 1

    assert "<h1>New order</h1>" in html
    assert 'href="/"' in html
    assert html.count('id="orders-note"') == 1
    assert html.count("Orders are not enabled on this device.") == 1
    for text in ("Submit order", "Skip a check", "Remove", "Back to dashboard"):
        assert text in html
    for placeholder in (
        "owner/name",
        "title",
        "base",
        "worker",
        "notify session",
        "max rounds",
        "order text",
        "reason",
    ):
        assert f'placeholder="{placeholder}"' in html
    assert 'fetch("/api/orders/submit"' in html
    assert '"order " + idOrText(result.order_id)' in html
    assert "resultLine.textContent = e.message;" in html
    assert "<form" not in html
    assert re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', html) == ["/design/work.js"]
    assert re.findall(r'<link\b[^>]*\bhref="([^"]+)"', html) == [
        "/design/tokens.css",
        "/design/brand.css",
        "/design/motion-base.css",
        "/design/work.css",
        "/design/motion.css",
    ]
    assert "http://" not in html
    assert "https://" not in html


def test_main_page_omits_moved_sections_and_links_to_their_subpages() -> None:
    html = PAGE.read_text(encoding="utf-8")
    for name, title, table_id in MOVED_SECTIONS:
        assert f"<h2>{title}</h2>" not in html
        assert f'id="{table_id}"' not in html
        assert f'href="/{name}"' in html
        assert html.count(f'href="/{name}"') == 1
    assert 'id="session-detail"' not in html
    assert 'id="detail-controls"' not in html
    assert html.count('id="section-nav"') == 1

    events = html.index("<h2>Order events</h2>")
    nav = html.index('id="section-nav"')
    assert nav < events


def test_each_subpage_carries_its_section_and_a_way_back() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    for name, title, table_id in MOVED_SECTIONS:
        assert f'data-section="{name}"' in html
        assert f"<h1>{title}</h1>" in html
        assert f'id="{table_id}"' in html
        assert f'href="/{name}"' in html
    assert 'href="/"' in html
    assert 'title="Orders"' in html
    assert ">Orders</span></a>" in html
    assert 'href="/usage"' in html
    assert "setInterval(tick, 2000);" in html


def test_orders_script_is_a_second_element() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 2
    assert "setInterval(tick, 2000);" in scripts[0]
    for name in ("ORDER_VIEW", "orderCall", "ordersTick"):
        assert name not in scripts[0]

    code = scripts[1]
    for signature in (
        "function orderCall(",
        "function renderOrderTable(",
        "function renderOrderObject(",
        "function ordersTick(",
        "function selectOrder(",
    ):
        assert signature in code
    assert code.index("/* ORDER_VIEW_BEGIN */") < code.index(
        "setInterval(ordersTick, 2000);"
    )
    assert code.count("fetch(") == 2
    assert 'fetch("/api/orders/schema")' in code
    for forbidden in (
        "127.0.0.1",
        "localhost",
        "7845",
        "location.",
        "/api/state",
        "postControl",
    ):
        assert forbidden not in code
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        assert code.count(opening) == code.count(closing)
    assert code.count("`") % 2 == 0


def test_order_actions_drop_answers_for_another_order() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    code = re.findall(r"<script>(.*?)</script>", html, flags=re.S)[1]
    for element_id in ("od-reply", "od-override", "od-cancel", "od-done", "od-report"):
        start = code.index(
            f'document.getElementById("{element_id}").addEventListener("click"'
        )
        handler = code[start : code.index("\n});\n", start)]
        assert "const id = selectedOrderId;" in handler
        assert "order_id: id" in handler
        assert "order_id: selectedOrderId" not in handler
        assert handler.count("if (selectedOrderId === id)") == 2


PAGES = ("index.html", "section.html", "orders_new.html")
COMMON_BEGIN, COMMON_END = "/* DASHBOARD_COMMON_BEGIN */", "/* DASHBOARD_COMMON_END */"
UUID = "d98c4782-f505-442f-bae7-54a2937f5578"
# Status values written outside a shared constant: session register / close,
# lane and agent rows, `agent work set`, intent execution and the report's CI view.
SESSION_STATUSES = ("active", "closed")
AGENT_STATUSES = ("working", "done", "finished")
WORK_STATUSES = ("open", "done", "cancelled")
INTENT_STATUSES = ("pending", "done", "error")
CI_STATES = ("green", "failed", "pending", "unknown")
# Order states, including the ended state "done" next to "merged".
ORDER_STATES = OPEN_STATES + ENDED_STATES


def common_block(html: str) -> str:
    assert html.count(COMMON_BEGIN) == 1
    assert html.count(COMMON_END) == 1
    return html.split(COMMON_BEGIN, 1)[1].split(COMMON_END, 1)[0]


def design_script() -> str:
    source = (STATIC / "design" / "work.js").read_text(encoding="utf-8")
    # The harness uses `now` as a timestamp. The design script's clock has the same name.
    return source.replace("const now = () =>", "const clockNow = () =>", 1)


def section_runtime(html: str) -> str:
    """Load the design script, then the shared block, so stateIcon can call chip."""
    return (
        "function matchMedia(){return {matches:false};}\n"
        + design_script()
        + "\nconst document = { addEventListener() {} };\n"
        + common_block(html)
    )


def orders_script() -> str:
    return re.findall(r"<script>(.*?)</script>", static_page("index.html"), flags=re.S)[1]


def top_level_function(code: str, name: str) -> str:
    start = code.index(f"\nfunction {name}(") + 1
    return code[start : code.index("\n}\n", start) + 3]


def test_every_page_carries_the_same_shared_block() -> None:
    blocks = {name: common_block(static_page(name)) for name in PAGES}
    assert len(set(blocks.values())) == 1
    scripts = re.findall(r"<script>(.*?)</script>", static_page("index.html"), flags=re.S)
    assert COMMON_BEGIN in scripts[0]
    for name in PAGES:
        html = static_page(name)
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "button.copy-id { " in style
        assert "cursor: pointer;" in style.split("button.copy-id { ", 1)[1].split("}", 1)[0]
        assert "button.copy-id.copied {" in style
        assert "button.copy-id.copy-failed {" in style
        assert "url(" not in style
        assert html.count('<script src="/design/work.js"></script>') == 1
        assert html.count("<script src") == 1
        assert html.count("<img") == 1
        assert '<img src="/design/logo-dark.svg"' in html


def test_uuid_pattern_matches_only_whole_uuids() -> None:
    block = common_block(static_page("index.html"))
    whole = re.compile(re.search(r"const UUID_PATTERN = /(.+?)/i;", block).group(1), re.I)
    assert whole.search(UUID)
    assert whole.search(UUID.upper())
    for value in (
        UUID[:8],
        UUID + "0",
        "x" + UUID,
        UUID[:-1] + "g",
        UUID.replace("-", ""),
        "a" * 40,
        "owner/name",
        "",
    ):
        assert not whole.search(value), value

    quoted = re.search(r"const QUOTED_UUID_PATTERN = /(.+?)/gi;", block).group(1)
    escaped = (
        f"{{&quot;id&quot;: &quot;{UUID}&quot;, "
        f"&quot;note&quot;: &quot;see {UUID}&quot;}}"
    )
    assert re.findall(quoted, escaped, flags=re.I) == [UUID]


def test_uuid_shows_its_first_8_characters_on_a_copy_button() -> None:
    block = common_block(static_page("index.html"))
    chip = block.split("function idChip(", 1)[1].split("\n    }\n", 1)[0]
    assert "value.slice(0, 8)" in chip
    assert (
        '<button type="button" class="${className}" '
        'data-copy="${esc(value)}" title="${esc(value)}">${esc(text)}</button>'
    ) in chip
    assert (
        'typeof value === "string" && UUID_PATTERN.test(value) '
        "? idChip(value) : esc(value)"
    ) in block
    assert "function jsonHtml(value, indent) {" in block

    section = static_page("section.html")
    for expr in (
        "s.id",
        "s._origin_device_id",
        "t.id",
        "t._origin_device_id",
        "a.id",
        "a.task_id",
        "r.task_id",
        "c.task_id",
        "g.task_id",
        "w.session_id",
        "ev._origin_device_id",
    ):
        assert f"<td>${{idOrText({expr})}}</td>" in section, expr
        assert f"<td>${{esc({expr})}}</td>" not in section, expr
    assert 'setHtml(document.getElementById("detail-id"), idOrText(s.id));' in section
    assert 'setHtml(document.getElementById("detail-pre"), jsonHtml({' in section
    for name in ("index.html", "section.html"):
        assert (
            'setHtml(document.getElementById("dev"), idOrText(data.device_id));'
            in static_page(name)
        )

    code = orders_script()
    cell = top_level_function(code, "orderCell")
    assert "return idOrText(value);" in cell
    assert 'if (typeof value === "object") return jsonHtml(value);' in cell
    assert 'setHtml(document.getElementById("od-id"), idOrText(id));' in code
    assert '"order " + idOrText(result.order_id)' in static_page("orders_new.html")


def test_session_controls_start_the_interactive_driver_of_the_session() -> None:
    section = static_page("section.html")
    assert (
        '<button type="button" class="btn btn-ghost btn-sm jk-press" '
        'id="ctl-start-ai">Start AI session</button>'
    ) in section
    assert (
        'postControl(s.id, { action: "start", payload: { interactive: true } })'
        in section
    )
    assert "provider:" not in section


def test_the_full_value_stays_the_key_for_lookups_and_calls() -> None:
    section = static_page("section.html")
    assert 'data-id="${esc(s.id)}"' in section
    assert "tr.onclick = () => selectSession(tr.dataset.id);" in section
    assert "postControl(s.id, " in section
    code = orders_script()
    assert "rows[i].dataset.id = listed.orders[i].order_id;" in code
    assert 'data-event="${esc(row.id)}"' in code
    assert "selectOrder(row.dataset.id)" in code
    assert "order_id: id" in code


def test_copy_writes_the_full_value_and_shows_success_or_failure() -> None:
    block = common_block(static_page("index.html"))
    copy = block.split("async function copyId(value) {", 1)[1].split("\n    }\n", 1)[0]
    assert "await navigator.clipboard.writeText(value);" in copy
    assert 'showCopyFeedback(value, "Copy failed", "copy-failed");' in copy
    assert 'showCopyFeedback(value, "Copied", "copied");' in copy
    assert copy.index("writeText") < copy.index('"Copy failed"') < copy.index('"Copied"')
    assert "const COPY_FEEDBACK_MS = 1500;" in block
    feedback = block.split("function showCopyFeedback(", 1)[1].split("\n    }\n", 1)[0]
    assert "}, COPY_FEEDBACK_MS);" in feedback
    assert "copyFeedback.delete(value);" in feedback
    paint = block.split("function paintCopyButtons(", 1)[1].split("\n    }\n", 1)[0]
    assert "feedback ? feedback.text : value.slice(0, 8)" in paint


def test_copy_click_does_not_reach_the_row() -> None:
    block = common_block(static_page("index.html"))
    start = block.index('document.addEventListener("click", event => {')
    listener = block[start : block.index("}, true);", start) + len("}, true);")]
    assert 'event.target.closest("button.copy-id")' in listener
    assert "event.stopPropagation();" in listener
    assert "copyId(button.dataset.copy);" in listener
    # Rows select through bubbling handlers, which the capturing listener stops first.
    assert "tr.onclick = () => selectSession(tr.dataset.id);" in static_page("section.html")
    assert (
        'document.getElementById("orders").addEventListener("click", event => {'
        in orders_script()
    )


def test_refresh_writes_only_changes_and_keeps_focus_without_scrolling() -> None:
    block = common_block(static_page("index.html"))
    write = block.split("function setHtml(el, html) {", 1)[1].split("\n    }\n", 1)[0]
    assert "if (shownHtml.get(el) === html) return;" in write
    assert write.index("copyFocus()") < write.index("el.innerHTML = html;")
    assert write.index("el.innerHTML = html;") < write.index("restoreCopyFocus(focus);")
    focus = block.split("function copyFocus() {", 1)[1].split("\n    }\n", 1)[0]
    assert "index: copyButtons(scope, value).indexOf(active)" in focus
    restore = block.split("function restoreCopyFocus(focus) {", 1)[1].split("\n    }\n", 1)[0]
    assert "buttons[Math.min(focus.index, buttons.length - 1)]" in restore
    assert "next.focus({ preventScroll: true });" in restore
    assert ".focus()" not in block

    # Every element a refresh fills with copy buttons goes through setHtml.
    section = static_page("section.html")
    for _name, _title, table_id in MOVED_SECTIONS:
        assert f'setHtml(document.querySelector("#{table_id} tbody"), ' in section
    pages = (static_page("index.html"), section)
    for html in pages:
        script = html.split(COMMON_END, 1)[1]
        assert ".innerHTML = " not in script.replace("controls.innerHTML = ", "")
        assert "copyFocus()" not in script
    assert "orderHtml" not in orders_script()


def test_every_agent_state_word_resolves_to_a_ring_kind_and_unknown_words_get_state_unknown() -> None:
    from test_dashboard_section_design import (
        EXPECTED_STATE_RING,
        ring_table,
        state_ring_table,
    )

    ring = ring_table((STATIC / "design" / "work.js").read_text(encoding="utf-8"))
    produced = (
        set(TASK_STATES)
        | set(ORDER_STATES)
        | set(CHECK_STATUSES)
        | set(SESSION_STATUSES)
        | set(AGENT_STATUSES)
        | set(WORK_STATUSES)
        | set(INTENT_STATUSES)
        | set(CI_STATES)
    )
    kinds = {item[0] for item in ring.values()}
    for name in PAGES:
        html = static_page(name)
        extra = state_ring_table(html)
        assert extra == EXPECTED_STATE_RING
        assert set(extra) & set(ring) == set()
        merged = {**ring, **extra}
        assert produced <= set(merged)
        for word in produced:
            assert merged[word][0] in kinds
        block = common_block(html)
        assert block.index("Object.assign(RING, STATE_RING);") < block.index(
            "function stateIcon(value)"
        )
        assert "STATE_ICONS" not in block and "STATE_FALLBACK_ICON" not in block
        state = block.split("function stateIcon(value) {", 1)[1].split("\n    }\n", 1)[0]
        assert 'const word = String(value).toLowerCase();' in state
        assert "const known = Object.hasOwn(RING, word);" in state
        assert "chip(word)" in state
        assert 'class="ring state-unknown"' in state
        # The word is the title of the whole cell, not of the glyph.
        assert "title=" not in state
        assert 'return "—";' in state
        attributes = block.split("function stateCellAttributes(value) {", 1)[1].split(
            "\n    }\n", 1
        )[0]
        assert 'if (value === null || value === undefined || value === "") return "";' in attributes
        assert 'return ` title="${esc(value)}" aria-label="${esc(value)}"`;' in attributes


def test_state_and_status_cells_show_icons_and_keep_their_classes() -> None:
    section = static_page("section.html")
    for expr in ("s.status", "t.state", "a.status", "w.status", "row.status"):
        if expr == "row.status":
            # Free text from the inventory: no class, only the shared state display.
            assert (
                f"<td${{stateCellAttributes({expr})}}>${{stateIcon({expr})}}</td>" in section
            )
        else:
            assert (
                f'<td class="${{esc({expr})}}"${{stateCellAttributes({expr})}}>${{stateIcon({expr})}}</td>'
                in section
            )
        assert f"${{esc({expr})}}</td>" not in section
        assert f"${{idOrText({expr})}}</td>" not in section
        assert f'title="${{esc({expr})}}"' not in section
    assert ".failed {" in section
    for rule in (".implementing, .working, .open {", ".done, .approved {", ".merged {"):
        assert rule not in section
    code = orders_script()
    columns = top_level_function(code, "orderColumns")
    assert (
        'if (field === "state" || field === "status") '
        "return [{ field, label: field, cell: stateIcon, attributes: stateCellAttributes }];"
    ) in columns
    td = top_level_function(code, "orderTd")
    assert 'const attributes = column.attributes ? column.attributes(value) : "";' in td
    assert "return `<td${attributes}>${column.cell(value)}</td>`;" in td
    for function in ("orderTableHtml", "orderRowsHtml"):
        body = top_level_function(code, function)
        assert "orderTd(column, " in body
        assert "<td>${column.cell(" not in body


def test_repository_shows_as_organization_and_repo() -> None:
    code = orders_script()
    columns = top_level_function(code, "orderColumns")
    assert 'if (field === "repo") {' in columns
    assert '{ field, label: "Organization", cell: value => orderCell(repoPart(value, 0)) }' in columns
    assert '{ field, label: "Repo", cell: value => orderCell(repoPart(value, 1)) }' in columns
    assert "function repoPart(value, index) {" in code
    for function in ("orderTableHtml", "orderRowsHtml"):
        assert "orderColumns" in top_level_function(code, function)
    render = top_level_function(code, "renderOrderObject")
    assert "orderRowsHtml(field, value)" in render
    assert "orderRowsHtml(key, value[key])" in render


def test_pull_request_url_shows_as_its_number() -> None:
    code = orders_script()
    cell = top_level_function(code, "orderCell")
    source = re.search(r"const pull = /(.+?)/\.exec\(value\.slice\(GITHUB\.length\)\);", cell).group(1)
    pull = re.compile(source.replace("\\/", "/"))
    assert pull.search("OWNER/NAME/pull/3").group(1) == "3"
    assert pull.search("OWNER/NAME/pull/3/").group(1) == "3"
    for rest in (
        "OWNER/NAME/pull/3#issuecomment-1",
        "OWNER/NAME/pull/3/files",
        "OWNER/NAME/pull/3//",
        "OWNER/NAME/pull/3/#issuecomment-1",
        "OWNER/NAME/issues/3",
        "OWNER/NAME",
    ):
        assert not pull.search(rest), rest
    assert 'const GITHUB = "https://github.com/";' in code
    assert (
        '<a href="${esc(value)}" target="_blank" rel="noopener" '
        'title="${esc(value)}">#${esc(pull[1])}</a>'
    ) in cell
    assert '<a href="${esc(value)}" target="_blank" rel="noopener">${esc(value)}</a>' in cell


def test_orders_list_has_no_head_column_and_the_detail_keeps_it() -> None:
    code = orders_script()
    builder = top_level_function(code, "listItemsHtml")
    assert 'ORDER_VIEW.list.nested.orders.filter(field => field !== "head")' in builder
    assert "renderOrderTable(" in code
    view = json.loads(
        code.split("const ORDER_VIEW = ", 1)[1].split("/* ORDER_VIEW_END */", 1)[0].strip()[:-1]
    )
    assert "head" in view["list"]["nested"]["orders"]
    assert "head" in view["show"]["fields"]
    assert "head" in view["report"]["fields"]
    assert 'renderOrderObject(document.getElementById("od-body"), shown, "show");' in code
    assert 'renderOrderObject(document.getElementById("od-report-body"), result, "report");' in code


def test_updated_at_shows_as_updated_before_with_the_timestamp_as_title() -> None:
    code = orders_script()
    assert 'const ELAPSED_LABELS = new Map([["updated_at", "updated_before"]]);' in code
    columns = top_level_function(code, "orderColumns")
    assert (
        "return [{ field, label: ELAPSED_LABELS.get(field), cell: elapsedCell, "
        'attributes: value => (isTimestamp(value) ? ` title="${esc(value)}"` : "") }];'
    ) in columns
    cell = top_level_function(code, "elapsedCell")
    assert "if (isTimestamp(value)) return esc(elapsedText(value, Date.now()));" in cell
    # An empty string shows the placeholder orderCell shows for a missing value.
    assert 'return orderCell(value === "" ? null : value);' in cell
    assert 'if (value === null || value === undefined) return "—";' in top_level_function(
        code, "orderCell"
    )
    elapsed = section_function(common_block(static_page("index.html")), "elapsedText")
    for unit in ('" s"', '" min"', '" h"', '" d"'):
        assert unit in elapsed
    assert "setInterval(ordersTick, 2000);" in code


def test_a_timestamp_without_a_zone_counts_as_utc() -> None:
    code = orders_script()
    shared = common_block(static_page("index.html"))
    # Every parse of a timestamp goes through timestampMs, which the shared block
    # carries with elapsedText on every page.
    assert "function timestampMs(" not in code and "function elapsedText(" not in code
    assert "(now - timestampMs(timestamp))" in section_function(shared, "elapsedText")
    assert (
        'typeof value === "string" && !Number.isNaN(timestampMs(value))'
        in top_level_function(code, "isTimestamp")
    )
    assert "Date.parse(" not in section_function(shared, "elapsedText")
    for name in ("isTimestamp", "elapsedCell"):
        assert "Date.parse(" not in top_level_function(code, name), name
    parse = section_function(shared, "timestampMs")
    assert (
        "return Date.parse(zoneless ? `${zoneless[1]}T${zoneless[2]}Z` : value);"
        in parse
    )
    zoneless = re.compile(
        re.search(r"const zoneless = /(.+?)/\.exec\(value\);", parse).group(1)
    )
    for value, utc in (
        ("2026-11-14T08:09:10", "2026-11-14T08:09:10Z"),
        ("2026-11-14T08:09", "2026-11-14T08:09Z"),
        ("2026-11-14T08:09:10.123", "2026-11-14T08:09:10.123Z"),
        ("2026-11-14 08:09:10", "2026-11-14T08:09:10Z"),
    ):
        match = zoneless.search(value)
        assert match, value
        assert f"{match.group(1)}T{match.group(2)}Z" == utc
    for value in (
        "2026-11-14T08:09:10Z",
        "2026-11-14T08:09:10.123Z",
        "2026-11-14T08:09:10+02:00",
        "2026-11-14T08:09:10-0500",
        "2026-11-14",
        "not a time",
        "",
    ):
        assert not zoneless.search(value), value


NODE = shutil.which("node")


def page_behaviour() -> dict:
    """Run the shared block and the order cell functions in Node with a stub DOM."""
    if NODE is None:
        pytest.skip("node is not installed; the static tests above cover the source")
    index = static_page("index.html")
    first = re.findall(r"<script>(.*?)</script>", index, flags=re.S)[0]
    helpers = (
        "function matchMedia(){return {matches:false};}\n"
        + design_script()
        + "\n"
        + "\n".join(
            line for line in first.splitlines() if line.startswith("    function empty(")
        )
    )
    code = orders_script()
    constants = "\n".join(
        re.search(rf"^const {name} = .*;$", code, flags=re.M).group(0)
        for name in ("GITHUB", "ELAPSED_LABELS")
    )
    functions = "\n".join(
        top_level_function(code, name)
        for name in (
            "orderCell",
            "isTimestamp",
            "elapsedCell",
            "repoPart",
            "orderColumns",
            "orderTd",
            "orderRowsHtml",
            "orderTableHtml",
        )
    )
    harness = """
class Element {
  constructor(fields) { Object.assign(this, fields); }
}
const listeners = [];
let buttons = [];
const document = {
  activeElement: null,
  addEventListener(type, handler, capture) { listeners.push({ type, handler, capture }); },
  querySelectorAll() { return buttons; },
};
const writes = [];
let clipboardFails = false;
const navigator = { clipboard: { writeText: async value => {
  writes.push(value);
  if (clipboardFails) throw new Error("denied");
} } };
const timers = [];
function setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; }
function clearTimeout() {}
const settle = () => new Promise(resolve => setImmediate(resolve));
""" + helpers + "\n" + common_block(index) + "\n" + constants + "\n" + functions + """
(async () => {
  const UUID = "__UUID__";
  const out = {};
  out.chip = idOrText(UUID);
  out.plain = idOrText("d98c4782");
  out.number = idOrText(7);
  out.json = jsonHtml({ id: UUID, note: "see " + UUID });
  out.known = stateIcon("merged");
  out.knownCase = stateIcon("Merged");
  out.unknown = stateIcon("mystery");
  out.none = stateIcon(null);
  out.cellAttributes = stateCellAttributes("merged");
  out.noCellAttributes = [null, undefined, ""].map(stateCellAttributes);

  const click = listeners.find(item => item.type === "click");
  out.capture = click.capture === true;
  const button = new Element({
    dataset: { copy: UUID }, textContent: UUID.slice(0, 8), className: "copy-id",
    closest(selector) { return selector === "button.copy-id" ? this : null; },
  });
  buttons = [button];
  let stopped = false;
  click.handler({ target: button, stopPropagation() { stopped = true; } });
  await settle();
  out.stopped = stopped;
  out.writes = writes.slice();
  out.success = { text: button.textContent, className: button.className, rerender: idChip(UUID) };
  out.delay = timers[0].delay;
  timers[0].callback();
  out.reset = { text: button.textContent, className: button.className, rerender: idChip(UUID) };
  clipboardFails = true;
  click.handler({ target: button, stopPropagation() {} });
  await settle();
  out.failure = { text: button.textContent, className: button.className };
  timers[timers.length - 1].callback();
  navigator.clipboard = undefined;
  click.handler({ target: button, stopPropagation() {} });
  await settle();
  out.missing = button.textContent;
  timers[timers.length - 1].callback();
  let otherStopped = false;
  click.handler({ target: new Element({ closest() { return null; } }), stopPropagation() { otherStopped = true; } });
  out.otherStopped = otherStopped;

  // A scope whose buttons a write replaces with the next rendered set.
  const shown = [];
  let rendered = [];
  const scope = { isConnected: true, querySelectorAll() { return shown; } };
  const host = { writes: 0, set innerHTML(html) {
    this.writes += 1;
    shown.forEach(item => { item.isConnected = false; });
    shown.splice(0, shown.length, ...rendered);
  } };
  const focused = [];
  const copyButton = (copy, name) => new Element({
    dataset: { copy }, isConnected: true,
    matches(selector) { return selector === "button.copy-id"; },
    closest(selector) { return selector === "[id]" ? scope : this; },
    focus(options) { focused.push({ name, options }); },
  });
  shown.push(copyButton(UUID, "old first"), copyButton("other", "old other"), copyButton(UUID, "old second"));
  document.activeElement = shown[2];
  rendered = [copyButton(UUID, "first"), copyButton("other", "other"), copyButton(UUID, "second")];
  setHtml(host, "one");
  setHtml(host, "one");
  out.htmlWrites = host.writes;
  out.sameOccurrence = focused.splice(0);
  document.activeElement = shown[2];
  rendered = [copyButton(UUID, "only"), copyButton("other", "other")];
  setHtml(host, "two");
  out.lastLeft = focused.splice(0);
  document.activeElement = shown[0];
  rendered = [copyButton("other", "other")];
  setHtml(host, "three");
  out.noneLeft = focused.splice(0);

  out.pr = orderCell("https://github.com/OWNER/NAME/pull/3");
  out.prSlash = orderCell("https://github.com/OWNER/NAME/pull/3/");
  out.prFiles = orderCell("https://github.com/OWNER/NAME/pull/3/files");
  out.comment = orderCell("https://github.com/OWNER/NAME/pull/3#issuecomment-1");
  out.repo = orderRowsHtml("repo", "OWNER/NAME");
  out.noSlash = orderRowsHtml("repo", "plain");
  out.stateRow = orderRowsHtml("state", "mystery");
  out.noStateRow = orderRowsHtml("status", null);
  out.badTimeRow = orderRowsHtml("updated_at", "not a time");
  const at = Date.parse("2026-11-14T00:00:00Z");
  out.elapsed = [45, 300, 7200, 259200, -5].map(seconds => elapsedText("2026-11-14T00:00:00Z", at + seconds * 1000));
  out.badTime = elapsedCell("not a time");
  out.missingTimes = ["", null, undefined].map(elapsedCell);
  out.emptyTimeRow = orderRowsHtml("updated_at", "");
  out.offset = new Date(at).getTimezoneOffset();
  out.parsed = [
    "2026-11-14T00:00:00", "2026-11-14T00:00", "2026-11-14T00:00:00.000", "2026-11-14 00:00:00",
    "2026-11-14T00:00:00Z", "2026-11-14T09:00:00+09:00",
  ].map(value => timestampMs(value) - at);
  out.zoneless = elapsedText("2026-11-14T00:00:00", at + 7200 * 1000);
  const updated = new Date(Date.now() - 300000).toISOString();
  out.updated = updated;
  // The same instant without its zone designator.
  out.zonelessUpdated = updated.slice(0, 19);
  out.zonelessRow = orderRowsHtml("updated_at", out.zonelessUpdated);
  out.table = orderTableHtml(
    [{ order_id: UUID, state: "running", repo: "OWNER/NAME", pr_url: "https://github.com/OWNER/NAME/pull/3", updated_at: updated }],
    ["order_id", "state", "repo", "pr_url", "updated_at"],
  );
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__UUID__", UUID)
    completed = subprocess.run(
        [NODE, "-"],
        input=harness,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        # A zone away from UTC, so that a timestamp read as local time shows.
        env={**os.environ, "TZ": "Asia/Tokyo"},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_page_behaviour_in_node() -> None:
    out = page_behaviour()
    chip = (
        f'<button type="button" class="copy-id" data-copy="{UUID}" '
        f'title="{UUID}">{UUID[:8]}</button>'
    )
    assert out["chip"] == chip
    assert out["plain"] == "d98c4782"
    assert out["number"] == "7"
    assert out["json"] == f"{{&quot;id&quot;:&quot;{chip}&quot;,&quot;note&quot;:&quot;see {UUID}&quot;}}"
    assert out["known"] == (
        '<span class="ring" role="img" aria-label="merged"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="8" cy="8" r="7" fill="var(--navy-800)"/>'
        '<path d="M5 8.2l2 2 4-4.2" fill="none" stroke="#fff" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg></span>'
    )
    assert out["knownCase"] == out["known"]
    unknown = (
        '<span class="ring state-unknown" role="img" aria-label="mystery">'
        '<svg viewBox="0 0 16 16" aria-hidden="true">'
        '<g class="dash-spin"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--neutral-500)" '
        'stroke-width="2" stroke-dasharray="2.4 2.3"/></g></svg></span>'
    )
    assert out["unknown"] == unknown
    assert out["none"] == "—"
    assert out["cellAttributes"] == ' title="merged" aria-label="merged"'
    assert out["noCellAttributes"] == ["", "", ""]

    assert out["capture"] is True
    assert out["stopped"] is True
    assert out["writes"] == [UUID]
    assert out["success"]["text"] == "Copied"
    assert out["success"]["className"] == "copy-id copied"
    assert ">Copied</button>" in out["success"]["rerender"]
    assert out["delay"] == 1500
    assert out["reset"]["text"] == UUID[:8]
    assert out["reset"]["className"] == "copy-id"
    assert out["reset"]["rerender"] == chip
    assert out["failure"] == {"text": "Copy failed", "className": "copy-id copy-failed"}
    assert out["missing"] == "Copy failed"
    assert out["otherStopped"] is False
    # A refresh writes only changed HTML and refocuses the same occurrence of the
    # value (or the last one left) without scrolling the page.
    assert out["htmlWrites"] == 1
    assert out["sameOccurrence"] == [{"name": "second", "options": {"preventScroll": True}}]
    assert out["lastLeft"] == [{"name": "only", "options": {"preventScroll": True}}]
    assert out["noneLeft"] == []

    assert out["pr"] == (
        '<a href="https://github.com/OWNER/NAME/pull/3" target="_blank" rel="noopener" '
        'title="https://github.com/OWNER/NAME/pull/3">#3</a>'
    )
    assert out["prSlash"] == (
        '<a href="https://github.com/OWNER/NAME/pull/3/" target="_blank" rel="noopener" '
        'title="https://github.com/OWNER/NAME/pull/3/">#3</a>'
    )
    assert out["prFiles"] == (
        '<a href="https://github.com/OWNER/NAME/pull/3/files" target="_blank" '
        'rel="noopener">https://github.com/OWNER/NAME/pull/3/files</a>'
    )
    assert out["comment"] == (
        '<a href="https://github.com/OWNER/NAME/pull/3#issuecomment-1" target="_blank" '
        'rel="noopener">https://github.com/OWNER/NAME/pull/3#issuecomment-1</a>'
    )
    assert out["repo"] == (
        "<tr><th>Organization</th><td>OWNER</td></tr><tr><th>Repo</th><td>NAME</td></tr>"
    )
    assert out["noSlash"] == (
        "<tr><th>Organization</th><td>—</td></tr><tr><th>Repo</th><td>plain</td></tr>"
    )
    assert out["stateRow"] == (
        f'<tr><th>state</th><td title="mystery" aria-label="mystery">{unknown}</td></tr>'
    )
    assert out["noStateRow"] == "<tr><th>status</th><td>—</td></tr>"
    assert out["elapsed"] == ["45 s", "5 min", "2 h", "3 d", "0 s"]
    assert out["badTime"] == "not a time"
    assert out["badTimeRow"] == "<tr><th>updated_before</th><td>not a time</td></tr>"
    # A missing or empty timestamp shows the placeholder, never an empty cell.
    assert out["missingTimes"] == ["—", "—", "—"]
    assert out["emptyTimeRow"] == "<tr><th>updated_before</th><td>—</td></tr>"
    # The harness runs away from UTC; a timestamp without a zone still counts as UTC.
    assert out["offset"] != 0
    assert out["parsed"] == [0, 0, 0, 0, 0, 0]
    assert out["zoneless"] == "2 h"
    assert out["zonelessRow"] == (
        f'<tr><th>updated_before</th><td title="{out["zonelessUpdated"]}">5 min</td></tr>'
    )

    table = out["table"]
    head = re.findall(r"<th>(.*?)</th>", table)
    assert head == ["order_id", "state", "Organization", "Repo", "pr_url", "updated_before"]
    assert chip in table
    # The word is the title and accessible name of the whole cell; the exact
    # timestamp is the title of its cell and never its accessible name.
    assert (
        '<td title="running" aria-label="running"><span class="ring" role="img" aria-label="running">'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><circle class="track" cx="8" cy="8" r="6"/>'
        '<g class="spin"><circle class="arc jk-value-arc-c" cx="8" cy="8" r="6" stroke="var(--blue-500)" '
        'data-to="18.85" style="stroke-dasharray:18.85 37.70"/></g></svg></span></td>'
    ) in table
    assert "<td>OWNER</td><td>NAME</td>" in table
    assert ">#3</a>" in table
    assert f'<td title="{out["updated"]}">5 min</td>' in table
