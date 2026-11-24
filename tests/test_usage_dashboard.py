from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import sign_in
from tests.test_newest_first import _insert_replica


def test_dashboard_html_has_usage_table(hub: TestClient) -> None:
    r = hub.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="usage"' in body
    assert 'id="lane-usage"' in body
    assert 'id="account-usage"' in body
    assert 'id="k-usage"' in body
    assert "fillUsage(data.grok" in body
    assert ">Usage<" in body
    assert "<h2>Usage</h2>" in body
    assert ".reverse(" not in body
    assert 'row.type !== "usage.snapshot"' in body
    assert "seen.has(key)" in body
    assert 'row._github_login || ""' in body
    assert 'function sessionLink(value)' in body
    assert 'const sessionId = typeof value === "string" ? value : ""' in body
    assert 'href="/session?id=${encodeURIComponent(sessionId)}"' in body
    assert 'sessionLink(row.session_id)' in body
    assert 'href="/sessions/${encodeURIComponent(row.session_id || "")}"' not in body


def test_api_state_usage_snapshots_newest_first(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="activity",
        row_id="usage-old",
        updated_at="2026-08-21T10:00:00Z",
        payload={
            "id": "usage-old",
            "type": "usage.snapshot",
            "payload": {
                "account_email": "alice@example.com",
                "provider": "grok",
                "tier": "super",
                "used_percent": 10,
                "period_end": "2026-08-31T00:00:00Z",
                "fetched_at": "2026-08-21T10:00:00Z",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="usage-new",
        updated_at="2026-08-22T21:00:00Z",
        payload={
            "id": "usage-new",
            "type": "usage.snapshot",
            "payload": {
                "account_email": "alice@example.com",
                "provider": "grok",
                "tier": "super",
                "used_percent": 42,
                "period_end": "2026-09-01T00:00:00Z",
                "fetched_at": "2026-08-22T21:00:00Z",
            },
        },
    )
    state = hub.get("/api/state").json()
    usage = [row for row in state["activity"] if row.get("type") == "usage.snapshot"]
    assert [row["id"] for row in usage] == ["usage-new", "usage-old"]
    for row in usage:
        inner = row["payload"]
        assert inner["account_email"] == "alice@example.com"
        assert inner["provider"] == "grok"
        assert inner["tier"] == "super"
        assert "used_percent" in inner
        assert "period_end" in inner
        assert "fetched_at" in inner
    assert usage[0]["payload"]["used_percent"] == 42
    assert usage[1]["payload"]["used_percent"] == 10
    assert usage[0]["_github_login"] == "alice"
