from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_cli.main import _usage_dashboard_rows

pytestmark = pytest.mark.no_pg


def test_dashboard_html_contains_usage_table() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="usage"' in html
    assert 'typeof p.used_percent === "number"' in html
    assert 'document.querySelector("#usage tbody").innerHTML = (data.usage || []).map(ev => {' in html
    assert ".reverse(" not in html


def test_dashboard_usage_rows_separate_openrouter_accounts_and_show_usd() -> None:
    snapshots = [
        {
            "type": "usage.snapshot",
            "payload": {
                "vendor": "openrouter",
                "provider": "openrouter",
                "account": "router-a",
                "usage_usd": 1.25,
                "limit_usd": 10.0,
                "limit_reset": "2026-12-01T00:00:00Z",
            },
        },
        {
            "type": "usage.snapshot",
            "payload": {
                "vendor": "openrouter",
                "provider": "openrouter",
                "account": "router-b",
                "usage_usd": 2.5,
                "limit_usd": 20.0,
                "limit_reset": "2026-12-02T00:00:00Z",
            },
        },
    ]

    rows = _usage_dashboard_rows(snapshots)

    assert [row["payload"]["account"] for row in rows] == ["router-a", "router-b"]
    assert [row["payload"]["usage_usd"] for row in rows] == [1.25, 2.5]
    assert [row["payload"]["limit_usd"] for row in rows] == [10.0, 20.0]
    assert [row["payload"]["limit_reset"] for row in rows] == [
        "2026-12-01T00:00:00Z",
        "2026-12-02T00:00:00Z",
    ]

    grok = {
        "type": "usage.snapshot",
        "payload": {"provider": "grok", "account_email": "user@example.com"},
    }
    assert _usage_dashboard_rows([grok, grok]) == [grok]

    legacy = {
        "type": "usage.snapshot",
        "payload": {"vendor": "openrouter", "account": "router-c"},
    }
    assert _usage_dashboard_rows([legacy]) == [legacy]

    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'const openrouter = (p.provider || p.vendor) === "openrouter";' in html
    assert 'const account = openrouter ? p.account : p.account_email;' in html
    assert 'typeof p.usage_usd === "number"' in html
    assert 'typeof p.limit_usd === "number"' in html
    assert 'p.limit_reset' in html
    assert 'const reset = openrouter ? p.limit_reset : p.period_end;' in html
    assert (
        '"$" + p.usage_usd + " / $" + '
        '(typeof p.limit_usd === "number" ? p.limit_usd : "unlimited")'
    ) in html


def test_dashboard_usage_rows_separate_openrouter_alias_per_device() -> None:
    def snapshot(device: str, usage_usd: float) -> dict:
        return {
            "type": "usage.snapshot",
            "_origin_device_id": device,
            "payload": {
                "provider": "openrouter",
                "account": "shared-alias",
                "usage_usd": usage_usd,
            },
        }

    device_a = snapshot("device-a", 1.0)
    device_b = snapshot("device-b", 2.0)

    rows = _usage_dashboard_rows([device_a, device_b])

    assert rows == [device_a, device_b]


def test_dashboard_usage_rows_keep_newest_openrouter_snapshot_per_device_alias() -> None:
    def snapshot(usage_usd: float) -> dict:
        return {
            "type": "usage.snapshot",
            "_origin_device_id": "device-a",
            "payload": {
                "provider": "openrouter",
                "account": "shared-alias",
                "usage_usd": usage_usd,
            },
        }

    newest = snapshot(2.0)
    older = snapshot(1.0)

    rows = _usage_dashboard_rows([newest, older])

    assert rows == [newest]


def test_orders_view_markup() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    ids = (
        "orders-note",
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

    assert "<h2>Orders</h2>" in html
    assert "<h3>New order</h3>" in html
    assert "<h2>Order events</h2>" in html
    for text in (
        "Submit order",
        "Add skipped check",
        "Remove",
        "Order",
        "Reply",
        "Override",
        "Cancel order",
        "Load report",
        "wake only",
        "unacknowledged",
        "Acknowledge",
        "Orders are not enabled on this device.",
    ):
        assert text in html
    for placeholder in (
        "owner/name",
        "title",
        "base",
        "worker",
        "notify session",
        "max rounds",
        "order text",
        "reply",
        "reason",
    ):
        assert f'placeholder="{placeholder}"' in html
    assert 'resultLine.textContent = "order " + result.order_id;' in html

    device = html.index('<p>Device <code id="dev">')
    orders = html.index("<h2>Orders</h2>")
    events = html.index("<h2>Order events</h2>")
    usage = html.index("<h2>Usage</h2>")
    assert device < orders < events < usage
    assert html.count("session-row") == 6
    assert "tr.order-row" in html
    assert "<script src" not in html
    assert "<link" not in html
    assert "<form" not in html
    without_github = html.replace("https://github.com/", "")
    assert "http://" not in without_github
    assert "https://" not in without_github
    assert ".reverse(" not in html


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
