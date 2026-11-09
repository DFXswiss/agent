from __future__ import annotations

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
