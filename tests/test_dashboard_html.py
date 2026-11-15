from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_cli.main import _usage_dashboard_rows

pytestmark = pytest.mark.no_pg

STATIC = (
    Path(__file__).resolve().parents[1] / "src" / "agent_cli" / "static"
)
PAGE = STATIC / "index.html"
SECTION_PAGE = STATIC / "section.html"
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


def test_dashboard_html_contains_usage_table() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
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

    html = SECTION_PAGE.read_text(encoding="utf-8")
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

    device = html.index('<p>Device <code id="dev">')
    orders = html.index("<h2>Orders</h2>")
    events = html.index("<h2>Order events</h2>")
    nav = html.index('id="section-nav"')
    assert device < orders < events < nav
    for _, title, _ in MOVED_SECTIONS:
        assert f"<h2>{title}</h2>" not in html
    assert "session-row" not in html
    assert "tr.order-row" in html
    assert "<script src" not in html
    assert "<link" not in html
    assert "<form" not in html
    without_github = html.replace("https://github.com/", "")
    assert "http://" not in without_github
    assert "https://" not in without_github
    assert ".reverse(" not in html


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
    for text in ("Submit order", "Add skipped check", "Remove", "Back to dashboard"):
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
    assert '"order " + result.order_id' in html
    assert "resultLine.textContent = e.message;" in html
    assert "<form" not in html
    assert "<script src" not in html
    assert "<link" not in html
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
    assert events < nav


def test_each_subpage_carries_its_section_and_a_way_back() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    for name, title, table_id in MOVED_SECTIONS:
        assert f'data-section="{name}"' in html
        assert f"<h2>{title}</h2>" in html
        assert f'id="{table_id}"' in html
        assert f'href="/{name}"' in html
    assert '<a href="/">Back to dashboard</a>' in html
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
    for element_id in ("od-reply", "od-override", "od-cancel", "od-report"):
        start = code.index(
            f'document.getElementById("{element_id}").addEventListener("click"'
        )
        handler = code[start : code.index("\n});\n", start)]
        assert "const id = selectedOrderId;" in handler
        assert "order_id: id" in handler
        assert "order_id: selectedOrderId" not in handler
        assert handler.count("if (selectedOrderId === id)") == 2
