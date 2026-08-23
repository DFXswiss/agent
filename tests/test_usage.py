from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.store import Store, StoreError
from agent_cli.usage import scan_usage, usage_poll_due


SETTINGS = {"subscription_tier_display": "SuperGrok Heavy"}


def _auth_file(tmp_path: Path, *, expires_at: str = "2099-01-01T00:00:00Z") -> Path:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "https://auth.x.ai::test-client": {
                    "key": "test-token",
                    "expires_at": expires_at,
                    "auth_mode": "oidc",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _credits(*, used: float = 11.0) -> dict[str, Any]:
    return {
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-08-18T20:37:07.245706+00:00",
                "end": "2026-08-25T20:37:07.245706+00:00",
            },
            "creditUsagePercent": used,
            "onDemandCap": {"val": 0},
            "onDemandUsed": {"val": 0},
            "productUsage": [{"product": "GrokBuild", "usagePercent": used}],
            "isUnifiedBillingUser": True,
            "prepaidBalance": {"val": 0},
            "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
            "billingPeriodStart": "2026-08-18T20:37:07.245706+00:00",
            "billingPeriodEnd": "2026-08-25T20:37:07.245706+00:00",
        }
    }


def _owned_grok_session(store: Store, session_id: str = "s-grok") -> str:
    store.write(
        "session",
        "insert",
        session_id,
        {
            "id": session_id,
            "kind": "human",
            "status": "active",
            "runtime": {"provider": "grok"},
        },
    )
    return session_id


def test_scan_usage_inserts_snapshot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    session_id = _owned_grok_session(store)
    auth = _auth_file(tmp_path)

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        assert token == "test-token"
        return _credits(), SETTINGS

    activity_id = scan_usage(
        store,
        fetch=fetch,
        auth_path=auth,
        now=lambda: "2026-08-20T12:00:00Z",
    )
    assert isinstance(activity_id, str)
    row = store.row("activity", activity_id)
    assert row is not None
    assert row["type"] == "usage.snapshot"
    assert row["session_id"] == session_id
    assert row["execution_status"] == "done"
    payload = row["payload"]
    assert payload["vendor"] == "grok"
    assert payload["tier"] == "SuperGrok Heavy"
    assert payload["used_percent"] == 11.0
    assert payload["period_type"] == "USAGE_PERIOD_TYPE_WEEKLY"
    assert payload["period_start"] == "2026-08-18T20:37:07.245706+00:00"
    assert payload["period_end"] == "2026-08-25T20:37:07.245706+00:00"
    assert payload["products"] == [{"product": "GrokBuild", "used_percent": 11.0}]
    assert payload["prepaid_val"] == 0
    assert payload["on_demand_cap_val"] == 0
    assert payload["on_demand_used_val"] == 0
    assert payload["fetched_at"] == "2026-08-20T12:00:00Z"
    assert "key" not in payload
    assert "test-token" not in json.dumps(payload)


def test_scan_usage_dedups_identical_snapshot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _credits(), SETTINGS

    first = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:00:00Z")
    second = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:01:00Z")
    assert isinstance(first, str)
    assert second is None
    assert len([r for r in store.rows("activity") if r.get("type") == "usage.snapshot"]) == 1


def test_scan_usage_inserts_on_used_percent_change(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    used = {"v": 11.0}

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _credits(used=used["v"]), SETTINGS

    first = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:00:00Z")
    used["v"] = 12.0
    second = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:01:00Z")
    assert isinstance(first, str)
    assert isinstance(second, str)
    assert first != second
    rows = [r for r in store.rows("activity") if r.get("type") == "usage.snapshot"]
    assert len(rows) == 2


def test_scan_usage_missing_auth_file(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    missing = tmp_path / "missing-auth.json"
    with pytest.raises(StoreError, match="grok auth file not found"):
        scan_usage(
            store,
            fetch=lambda token: (_credits(), SETTINGS),
            auth_path=missing,
        )
    assert store.rows("activity") == []


def test_scan_usage_expired_token(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path, expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(StoreError, match="expired"):
        scan_usage(
            store,
            fetch=lambda token: (_credits(), SETTINGS),
            auth_path=auth,
        )
    assert store.rows("activity") == []


def test_scan_usage_rejects_monthly_billing_shape(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"monthlyLimit": 100, "used": 10, "history": []}, SETTINGS

    with pytest.raises(StoreError, match="config|currentPeriod"):
        scan_usage(store, fetch=fetch, auth_path=auth)
    assert store.rows("activity") == []


def test_scan_usage_missing_subscription_tier(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _credits(), {}

    with pytest.raises(StoreError, match="subscription_tier_display"):
        scan_usage(store, fetch=fetch, auth_path=auth)
    assert store.rows("activity") == []


def test_scan_usage_no_owned_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    auth = _auth_file(tmp_path)
    with pytest.raises(StoreError, match="no owned session"):
        scan_usage(
            store,
            fetch=lambda token: (_credits(), SETTINGS),
            auth_path=auth,
        )
    assert store.rows("activity") == []


def test_scan_usage_rejects_foreign_session_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.apply_replica_row(
        {
            "table": "session",
            "row_id": "foreign",
            "origin_device_id": "other-device",
            "payload": {"id": "foreign", "kind": "human", "status": "active"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    )
    auth = _auth_file(tmp_path)
    with pytest.raises(StoreError, match="no owned session"):
        scan_usage(
            store,
            fetch=lambda token: (_credits(), SETTINGS),
            auth_path=auth,
        )
    assert store.rows("activity") == []


def test_scan_usage_prefers_grok_provider_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    grok_id = _owned_grok_session(store, "s-grok")
    store.write(
        "session",
        "insert",
        "s-other",
        {
            "id": "s-other",
            "kind": "human",
            "status": "active",
            "runtime": {"provider": "codex"},
        },
    )
    auth = _auth_file(tmp_path)
    activity_id = scan_usage(
        store,
        fetch=lambda token: (_credits(), SETTINGS),
        auth_path=auth,
        now=lambda: "2026-08-20T12:00:00Z",
    )
    assert isinstance(activity_id, str)
    row = store.row("activity", activity_id)
    assert row is not None
    assert row["session_id"] == grok_id


def test_usage_poll_due() -> None:
    assert usage_poll_due(None, 0) is True
    assert usage_poll_due(100.0, 159.0, 60) is False
    assert usage_poll_due(100.0, 160.0, 60) is True
