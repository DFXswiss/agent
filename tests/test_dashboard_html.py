from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli.ai_accounts import AISubscription
from agent_cli.main import (
    TASK_STATES,
    _subscription_dashboard_rows,
    _subscriptions_dashboard,
    _usage_dashboard_rows,
)
from agent_cli.order_ops import ENDED_STATES, OPEN_STATES
from agent_cli.preconditions import CHECK_STATUSES

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
        "(subscriptions.rows || []).map(subscriptionRowHtml)"
    ) in html
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
    assert 'if (provider === "openrouter") {' in html
    assert 'typeof p.usage_usd === "number"' in html
    assert 'typeof p.limit_usd === "number"' in html
    assert "reset: idOrText(p.limit_reset)," in html
    assert "reset: idOrText(p.period_end)," in html
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


def _snapshot(provider: str, account: str, device: str = "device-a", **extra: object) -> dict:
    key = "account" if provider == "openrouter" else "account_email"
    return {
        "type": "usage.snapshot",
        "_origin_device_id": device,
        "payload": {"provider": provider, "vendor": provider, key: account, **extra},
    }


def test_subscription_rows_join_inventory_with_newest_usage() -> None:
    claude = _snapshot("claude", "Seat-1@Example.com", tier="max_20x")
    grok = _snapshot("grok", "grok@example.com")
    router_a = _snapshot("openrouter", "router-a", "device-a")
    router_b = _snapshot("openrouter", "router-a", "device-b")
    stray = _snapshot("claude", "unlisted@example.com")
    usage = _usage_dashboard_rows([claude, grok, router_a, router_b, stray])
    inventory = (
        AISubscription("claude", "seat-1@example.com", "Team seat 1", "active", "free text"),
        AISubscription("claude", "seat-2@example.com", "Team seat 2"),
        AISubscription("openrouter", "ROUTER-A"),
        AISubscription("grok", "grok@example.com", status="paused"),
        AISubscription("chatgpt", "seat-1@example.com"),
    )

    rows = _subscription_dashboard_rows(inventory, usage)

    assert [(row["provider"], row["account"], row["listed"], row["usage"]) for row in rows] == [
        ("claude", "seat-1@example.com", True, claude),
        ("claude", "seat-2@example.com", True, None),
        ("openrouter", "ROUTER-A", True, router_a),
        ("openrouter", "ROUTER-A", True, router_b),
        ("grok", "grok@example.com", True, grok),
        ("chatgpt", "seat-1@example.com", True, None),
        ("claude", "unlisted@example.com", False, stray),
    ]
    assert rows[0]["label"] == "Team seat 1"
    assert rows[0]["status"] == "active"
    assert rows[0]["note"] == "free text"
    assert rows[-1]["label"] is None and rows[-1]["note"] is None


def test_subscription_rows_without_inventory_keep_every_snapshot() -> None:
    grok = _snapshot("grok", "grok@example.com")
    legacy = {"type": "usage.snapshot", "payload": {"vendor": "grok", "account_email": "old@example.com"}}
    rows = _subscription_dashboard_rows((), [grok, legacy])
    assert [(row["provider"], row["account"], row["listed"]) for row in rows] == [
        ("grok", "grok@example.com", False),
        ("grok", "old@example.com", False),
    ]


def test_subscriptions_dashboard_reports_file_state_as_data(tmp_path: Path) -> None:
    grok = _snapshot("grok", "grok@example.com")
    assert _subscriptions_dashboard(tmp_path, [grok]) == {
        "configured": False,
        "error": None,
        "rows": _subscription_dashboard_rows((), [grok]),
    }
    path = tmp_path / "ai-subscriptions.json"
    path.write_text(
        json.dumps({"subscriptions": [{"provider": "grok", "account": "grok@example.com"}]}),
        encoding="utf-8",
    )
    state = _subscriptions_dashboard(tmp_path, [grok])
    assert state["configured"] is True and state["error"] is None
    assert state["rows"][0]["listed"] is True and state["rows"][0]["usage"] == grok

    path.write_text(json.dumps({"subscriptions": [{"provider": "Grok"}]}), encoding="utf-8")
    state = _subscriptions_dashboard(tmp_path, [grok])
    assert state["configured"] is False
    assert "provider must be a lowercase token" in state["error"]
    assert [row["listed"] for row in state["rows"]] == [False]


def test_subscriptions_section_markup() -> None:
    html = SECTION_PAGE.read_text(encoding="utf-8")
    assert "<h2>Subscriptions</h2>" in html
    assert "<h2>Usage</h2>" not in html
    assert '<a href="/usage">Subscriptions</a>' in html
    assert '<a href="/usage">Subscriptions</a>' in PAGE.read_text(encoding="utf-8")
    header = html.split('<table id="usage"><thead><tr>', 1)[1].split("</tr>", 1)[0]
    assert header.count("<th>") == 11
    for column in ("Provider", "Account", "Label", "Status", "Note", "Listed", "Tier", "Used", "Reset"):
        assert f"<th>{column}</th>" in header
    assert "const SUBSCRIPTION_COLUMNS = 11;" in html
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
        if line.startswith(("    function esc(", "    function empty(", "    const SUBSCRIPTION_COLUMNS", "    const NO_USAGE", "    const NO_INVENTORY"))
    )
    functions = "\n".join(
        section_function(html, name)
        for name in (
            "percentText",
            "claudeWindows",
            "usageCells",
            "subscriptionRowHtml",
            "subscriptionsEmptyHtml",
            "subscriptionsErrorText",
        )
    )
    # The shared block brings idOrText, stateIcon and stateCellAttributes.
    shared = "const document = { addEventListener() {} };\n" + common_block(html)
    harness = helpers + "\n" + shared + "\n" + functions + """
const claude = {
  provider: "claude", account: "seat-1@example.com", label: "Seat <1>", status: "active", note: "a & b", listed: true,
  usage: { _origin_device_id: "device-a", payload: {
    provider: "claude", vendor: "claude", account_email: "seat-1@example.com", tier: "max_20x", fetched_at: "2026-11-16T12:00:00Z",
    windows: {
      five_hour: null,
      weekly: { used_percent: 14.0, resets_at: "2026-11-23T10:00:00+00:00" },
      weekly_models: [{ model: "<b>Fable</b>", used_percent: 21.5, resets_at: null }],
    },
  } },
};
const missing = { provider: "claude", account: "seat-2@example.com", label: null, status: null, note: null, listed: true, usage: null };
const stray = { provider: "openrouter", account: "router-a", listed: false,
  usage: { _origin_device_id: "device-b", payload: { provider: "openrouter", account: "router-a", usage_usd: 1.25, limit_usd: null, limit_reset: "monthly" } } };
console.log(JSON.stringify({
  claude: subscriptionRowHtml(claude),
  missing: subscriptionRowHtml(missing),
  stray: subscriptionRowHtml(stray),
  unknownStatus: subscriptionRowHtml({ ...missing, status: "on-hold" }),
  absent: subscriptionsEmptyHtml({ configured: false, error: null, rows: [] }),
  absentError: subscriptionsErrorText({ configured: false, error: null, rows: [] }),
  invalid: subscriptionsEmptyHtml({ configured: false, error: "subscriptions must be a list", rows: [] }),
  invalidError: subscriptionsErrorText({ configured: false, error: "subscriptions must be a list", rows: [] }),
  configuredEmpty: subscriptionsEmptyHtml({ configured: true, error: null, rows: [] }),
}));
"""
    completed = subprocess.run(
        [NODE, "-"], input=harness, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    claude = out["claude"]
    assert "<td>Seat &lt;1&gt;</td>" in claude
    assert "<td>a &amp; b</td>" in claude
    assert "<td>listed</td>" in claude
    active = state_icons(html)["active"]
    assert (
        '<td title="active" aria-label="active"><span class="state-icon" role="img" '
        f'aria-label="active">{active}&#xFE0E;</span></td>'
    ) in claude
    assert (
        '<td title="on-hold" aria-label="on-hold"><span class="state-icon state-unknown" '
        'role="img" aria-label="on-hold">◌&#xFE0E;</span></td>'
    ) in out["unknownStatus"]
    assert "<td>—</td>" in out["missing"]
    assert "<td>max_20x</td>" in claude
    assert "<td>5h: —<br>Week: 14%<br>Week &lt;b&gt;Fable&lt;/b&gt;: 21.5%</td>" in claude
    assert "<td>5h: —<br>Week: 2026-11-23T10:00:00+00:00<br>Week &lt;b&gt;Fable&lt;/b&gt;: —</td>" in claude
    assert "<b>" not in claude
    assert out["missing"].count("<td>") == 11
    assert "<td>No usage: no paired device polls this login</td>" in out["missing"]
    assert "0%" not in out["missing"]
    assert "<td>not listed</td>" in out["stray"]
    assert "<td>$1.25 / $unlimited</td>" in out["stray"]
    assert "<td>monthly</td>" in out["stray"]
    absent = "No subscription inventory: $AGENT_HOME/ai-subscriptions.json is absent"
    assert absent in out["absent"]
    assert out["absentError"] is None
    assert absent not in out["invalid"]
    assert out["invalid"] == "<tr><td colspan='11'>—</td></tr>"
    assert out["invalidError"] == "ai-subscriptions.json: subscriptions must be a list"
    assert out["configuredEmpty"] == "<tr><td colspan='11'>—</td></tr>"


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
    assert "<h2>Order events</h2>" not in html
    assert html.count("New order") == 1
    assert 'href="/orders/new"' in html
    for text in (
        "Order",
        "Reply",
        "Override",
        "Cancel order",
        "Mark done",
        "Load report",
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
    nav = html.index('id="section-nav"')
    assert device < orders < nav
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
    assert '"order " + idOrText(result.order_id)' in html
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
    assert '<a href="/events">Order events</a>' in html
    assert html.count('href="/events"') == 1


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


def test_order_events_page_keeps_the_table_and_the_detail_reads_the_checkpoint() -> None:
    main = static_page("index.html")
    code = orders_script()
    assert 'orderCall("events"' not in code
    assert 'orderCall("ack"' not in code
    assert "/api/orders/events" not in code.split("/* ORDER_VIEW_END */", 1)[1]
    assert 'const digest = cp !== null && cp.digest ? cp.digest : "";' in code
    assert 'cp !== null && cp.opinion !== null && typeof cp.opinion === "object"' in code
    assert 'getElementById("od-question")' in code
    assert 'getElementById("od-opinion")' in code
    assert 'key !== "digest" && key !== "opinion"' in code
    assert "wake only" not in main
    assert "unacknowledged" not in main
    assert "Acknowledge" not in main

    html = static_page("section.html")
    assert 'data-section="events"' in html
    assert "<h2>Order events</h2>" in html
    assert html.count('id="order-events"') == 1
    assert html.count('id="ev-wake"') == 1
    assert html.count('id="ev-unacked"') == 1
    assert html.count('id="events-note"') == 1
    assert "wake only" in html
    assert "unacknowledged" in html
    assert ">Acknowledge</button>" in html
    assert 'href="/events">Order events</a>' in html
    assert html.count('href="/events"') == 1
    assert 'orderCall("events"' in html
    assert 'orderCall("ack"' in html
    assert '"path":"/api/orders/events"' in html
    assert '"path":"/api/orders/ack"' in html
    assert "ORDER_VIEW.events.nested.events" in html
    assert 'if (section === "events")' in html
    assert "setInterval(eventsTick, 2000);" in html
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


def orders_script() -> str:
    return re.findall(r"<script>(.*?)</script>", static_page("index.html"), flags=re.S)[1]


def top_level_function(code: str, name: str) -> str:
    start = code.index(f"\nfunction {name}(") + 1
    return code[start : code.index("\n}\n", start) + 3]


def state_icons(html: str) -> dict[str, str]:
    literal = common_block(html).split("const STATE_ICONS = ", 1)[1].split(";", 1)[0]
    return json.loads(literal)


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
        assert "<script src" not in html
        assert "<img" not in html
        assert "url(" not in style


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


def test_the_full_value_stays_the_key_for_lookups_and_calls() -> None:
    section = static_page("section.html")
    assert 'data-id="${esc(s.id)}"' in section
    assert "tr.onclick = () => selectSession(tr.dataset.id);" in section
    assert "postControl(s.id, " in section
    code = orders_script()
    assert "rows[i].dataset.id = listed.orders[i].order_id;" in code
    assert 'data-event="${esc(row.id)}"' not in code
    assert 'data-event="${esc(row.id)}"' in static_page("section.html")
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


def test_state_icon_mapping_covers_every_value_with_distinct_outcomes() -> None:
    icons = state_icons(static_page("index.html"))
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
    assert produced - set(icons) == set()
    for word, icon in icons.items():
        assert 1 <= len(icon) <= 2, word
        assert not icon.isalnum(), word

    families = (
        ("running", "active", "working"),
        ("waiting",),
        ("gate-blocked",),
        ("ready",),
        ("merged", "done"),
        ("failed",),
        ("cancelled",),
        ("ja",),
        ("nein",),
        ("pending",),
    )
    family_icons = []
    for family in families:
        assert len({icons[word] for word in family}) == 1, family
        family_icons.append(icons[family[0]])
    assert len(set(family_icons)) == len(families)

    block = common_block(static_page("index.html"))
    fallback = re.search(r'const STATE_FALLBACK_ICON = "(.+?)";', block).group(1)
    assert fallback not in icons.values()
    state = block.split("function stateIcon(value) {", 1)[1].split("\n    }\n", 1)[0]
    assert "const known = Object.hasOwn(STATE_ICONS, word);" in state
    assert "known ? STATE_ICONS[word] : STATE_FALLBACK_ICON" in state
    assert 'role="img" aria-label="${esc(word)}"' in state
    # The word is the title of the whole cell, not of the glyph.
    assert "title=" not in state
    assert 'return "—";' in state
    attributes = block.split("function stateCellAttributes(value) {", 1)[1].split("\n    }\n", 1)[0]
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
    for rule in (".failed {", ".implementing, .working, .open {", ".done, .approved {", ".merged {"):
        assert rule in section
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
    assert (
        "renderOrderTable(table, listed.orders, "
        'ORDER_VIEW.list.nested.orders.filter(field => field !== "head"));'
    ) in code
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
    elapsed = top_level_function(code, "elapsedText")
    for unit in ('" s"', '" min"', '" h"', '" d"'):
        assert unit in elapsed
    assert "setInterval(ordersTick, 2000);" in code


def test_a_timestamp_without_a_zone_counts_as_utc() -> None:
    code = orders_script()
    # Every parse of a timestamp goes through timestampMs.
    assert "(now - timestampMs(timestamp))" in top_level_function(code, "elapsedText")
    assert (
        'typeof value === "string" && !Number.isNaN(timestampMs(value))'
        in top_level_function(code, "isTimestamp")
    )
    for name in ("elapsedText", "isTimestamp", "elapsedCell"):
        assert "Date.parse(" not in top_level_function(code, name), name
    parse = top_level_function(code, "timestampMs")
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
    helpers = "\n".join(
        line for line in first.splitlines()
        if line.startswith(("    function esc(", "    function empty("))
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
            "timestampMs",
            "elapsedText",
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
        '<span class="state-icon" role="img" aria-label="merged">✔&#xFE0E;</span>'
    )
    unknown = '<span class="state-icon state-unknown" role="img" aria-label="mystery">◌&#xFE0E;</span>'
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
        '<td title="running" aria-label="running"><span class="state-icon" role="img" '
        'aria-label="running">▶&#xFE0E;</span></td>'
    ) in table
    assert "<td>OWNER</td><td>NAME</td>" in table
    assert ">#3</a>" in table
    assert f'<td title="{out["updated"]}">5 min</td>' in table
