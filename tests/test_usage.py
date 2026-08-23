from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_cli.store import Store, StoreError
from agent_cli.usage import (
    CREDITS_URL,
    SETTINGS_URL,
    fetch_credits_and_settings,
    last_usage_snapshot,
    scan_usage,
    usage_poll_due,
)


SETTINGS = {"subscription_tier_display": "SuperGrok Heavy"}


def _auth_file(
    tmp_path: Path,
    *,
    expires_at: str = "2099-01-01T00:00:00Z",
    email: str = "user@example.com",
) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "https://auth.x.ai::test-client": {
                    "key": "test-token",
                    "expires_at": expires_at,
                    "email": email,
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
    assert payload["provider"] == "grok"
    assert payload["account_email"] == "user@example.com"
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


def test_scan_usage_dedups_against_latest_snapshot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    used = {"v": 11.0}

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _credits(used=used["v"]), SETTINGS

    first = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:00:00Z")
    used["v"] = 12.0
    second = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:01:00Z")
    third = scan_usage(store, fetch=fetch, auth_path=auth, now=lambda: "2026-08-20T12:02:00Z")
    assert isinstance(first, str)
    assert isinstance(second, str)
    assert third is None
    assert len([r for r in store.rows("activity") if r.get("type") == "usage.snapshot"]) == 2


def test_scan_usage_rejects_nan_credit_usage_percent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    credits = _credits()
    credits["config"]["creditUsagePercent"] = float("nan")

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return credits, SETTINGS

    with pytest.raises(StoreError, match="finite"):
        scan_usage(store, fetch=fetch, auth_path=auth)


def test_scan_usage_rejects_inf_credit_usage_percent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    credits = _credits()
    credits["config"]["creditUsagePercent"] = float("inf")

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return credits, SETTINGS

    with pytest.raises(StoreError, match="finite"):
        scan_usage(store, fetch=fetch, auth_path=auth)


def test_scan_usage_rejects_overflow_credit_usage_percent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    credits = _credits()
    credits["config"]["creditUsagePercent"] = 10**400

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return credits, SETTINGS

    with pytest.raises(StoreError, match="finite"):
        scan_usage(store, fetch=fetch, auth_path=auth)


def test_fetch_credits_and_settings_success_via_mock_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == CREDITS_URL:
            return httpx.Response(200, json=_credits())
        if str(request.url) == SETTINGS_URL:
            return httpx.Response(200, json=SETTINGS)
        return httpx.Response(404, json={"error": "unexpected"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        credits, settings = fetch_credits_and_settings("test-token", client=client)
    assert credits == _credits()
    assert settings == SETTINGS
    assert len(requests) == 2
    assert str(requests[0].url) == CREDITS_URL
    assert str(requests[1].url) == SETTINGS_URL
    for request in requests:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["x-xai-token-auth"] == "xai-grok-cli"


def test_fetch_credits_and_settings_http_500_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(StoreError) as excinfo:
            fetch_credits_and_settings("test-token", client=client)
    assert "test-token" not in str(excinfo.value)


def test_fetch_credits_and_settings_invalid_json_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(StoreError, match="invalid JSON") as excinfo:
            fetch_credits_and_settings("test-token", client=client)
    assert "test-token" not in str(excinfo.value)


def test_last_usage_snapshot_via_last_own_activity_insert(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path)
    activity_id = scan_usage(
        store,
        fetch=lambda token: (_credits(), SETTINGS),
        auth_path=auth,
        now=lambda: "2026-08-20T12:00:00Z",
    )
    assert isinstance(activity_id, str)
    event = store.last_own_activity_insert("usage.snapshot")
    assert event is not None
    assert event["type"] == "usage.snapshot"
    assert event["id"] == activity_id
    snapshot = last_usage_snapshot(store)
    assert snapshot is not None
    assert snapshot["vendor"] == "grok"
    assert snapshot["used_percent"] == 11.0
    assert last_usage_snapshot(store, vendor="other") is None


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


def test_scan_usage_missing_email(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth = _auth_file(tmp_path, email="")
    with pytest.raises(StoreError, match="email"):
        scan_usage(
            store,
            fetch=lambda token: (_credits(), SETTINGS),
            auth_path=auth,
        )
    assert store.rows("activity") == []


def test_scan_usage_inserts_on_account_email_change(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_grok_session(store)
    auth_a = _auth_file(tmp_path, email="a@example.com")

    def fetch(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _credits(), SETTINGS

    first = scan_usage(store, fetch=fetch, auth_path=auth_a, now=lambda: "2026-08-20T12:00:00Z")
    auth_b = _auth_file(tmp_path, email="b@example.com")
    second = scan_usage(store, fetch=fetch, auth_path=auth_b, now=lambda: "2026-08-20T12:01:00Z")
    assert isinstance(first, str)
    assert isinstance(second, str)
    assert first != second
    rows = [r for r in store.rows("activity") if r.get("type") == "usage.snapshot"]
    assert len(rows) == 2
    emails = {r["payload"]["account_email"] for r in rows}
    assert emails == {"a@example.com", "b@example.com"}


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
