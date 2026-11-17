from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_cli.drivers import UsageSpec, parse_driver_document
from agent_cli.store import Store, StoreError
from agent_cli.usage import (
    AccountUsageError,
    AuthStale,
    _MISSING,
    _resolve,
    fetch_usage_documents,
    is_spend_snapshot,
    last_usage_snapshot,
    period_snapshot,
    scan_account_usage,
    scan_session_usage,
    spend_snapshot,
    usage_poll_due,
    usage_unchanged,
)
from driver_fixtures import accounts_document, driver_document, write_accounts, write_drivers

PERIOD_DRIVER = "driver-a"
SPEND_DRIVER = "driver-d"
USAGE_SESSION = "session-a"
SPEND_ACCOUNT = "driver-d-account"
ENTRY_PREFIX = "https://auth.example.test::"
BILLING_URL = "https://usage.example.test/v1/billing"
PLAN_URL = "https://usage.example.test/v1/plan"
KEY_URL = "https://gateway.example.test/api/v1/key"
PLAN = {"tier": "Plan A"}


def _usage(driver: str, document: dict[str, Any] | None = None) -> UsageSpec:
    spec = parse_driver_document(document or driver_document()).drivers[driver].usage
    assert spec is not None
    return spec


def _billing(*, used: float = 11.0) -> dict[str, Any]:
    return {
        "used": used,
        "period": {
            "type": "PERIOD_WEEKLY",
            "start": "2026-08-18T20:37:07.245706+00:00",
            "end": "2026-08-25T20:37:07.245706+00:00",
        },
        "products": [{"name": "product-a", "used": used}],
        "prepaid": {"val": 0},
        "cap": {"val": 0},
        "on_demand": {"val": 0},
        "ignored": "not stored",
    }


def _period_documents(*, used: float = 11.0) -> dict[str, Any]:
    return {"billing": _billing(used=used), "plan": dict(PLAN)}


def _key_body(*, usage: float = 1.25) -> dict[str, Any]:
    return {
        "data": {
            "usage": usage,
            "usage_daily": 0.25,
            "usage_weekly": 0.75,
            "usage_monthly": 1.0,
            "limit": 10.0,
            "limit_remaining": 8.75,
            "limit_reset": "2026-12-01T00:00:00Z",
            "label": "must-not-be-stored",
            "id": "must-not-be-stored",
        }
    }


def _configure(
    home: Path,
    *,
    usage_session: str | None = USAGE_SESSION,
    usage_accounts: list[str] | None = None,
    extra_spend_accounts: tuple[str, ...] = (),
) -> dict[str, Path]:
    """Placeholder drivers and accounts whose directories exist under ``home``."""
    write_drivers(home)
    document = accounts_document()
    directories: dict[str, Path] = {}
    for driver in (PERIOD_DRIVER, SPEND_DRIVER):
        directory = home / "profiles" / driver
        directory.mkdir(parents=True)
        document["accounts"][f"{driver}-account"]["config_dir"] = str(directory)
        directories[driver] = directory
    for name in extra_spend_accounts:
        directory = home / "profiles" / name
        directory.mkdir(parents=True)
        document["accounts"][name] = {"provider": SPEND_DRIVER, "config_dir": str(directory)}
        directories[name] = directory
    if usage_session is not None:
        document["usage_session"] = usage_session
    if usage_accounts is not None:
        document["usage_accounts"] = usage_accounts
    write_accounts(home, document)
    return directories


def _credential(
    directory: Path,
    *,
    expires_at: str = "2099-01-01T00:00:00Z",
    email: str = "user@example.com",
) -> Path:
    path = directory / "auth.json"
    path.write_text(
        json.dumps(
            {
                f"{ENTRY_PREFIX}client": {
                    "key": "test-token",
                    "expires_at": expires_at,
                    "email": email,
                    "refresh_token": "ignored",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _key(directory: Path, token: str = "spend-secret") -> None:
    (directory / "tool-d-key").write_text(token + "\n", encoding="utf-8")


def _owned_session(
    store: Store, session_id: str = USAGE_SESSION, provider: str = PERIOD_DRIVER
) -> str:
    store.write(
        "session",
        "insert",
        session_id,
        {
            "id": session_id,
            "kind": "human",
            "status": "active",
            "runtime": {"provider": provider},
        },
    )
    return session_id


def _period_store(tmp_path: Path, **credential: str) -> Store:
    directories = _configure(tmp_path)
    _credential(directories[PERIOD_DRIVER], **credential)
    store = Store(tmp_path)
    _owned_session(store)
    return store


def _spend_store(tmp_path: Path, *, key: bool = True) -> tuple[Store, dict[str, Path]]:
    directories = _configure(tmp_path, usage_session=None, usage_accounts=[SPEND_ACCOUNT])
    if key:
        _key(directories[SPEND_DRIVER])
    return Store(tmp_path), directories


def _period_fetch(**overrides: Any) -> Any:
    def fetch(driver: str, spec: UsageSpec, token: str) -> dict[str, Any]:
        assert driver == PERIOD_DRIVER
        assert spec.snapshot == "period"
        assert token == "test-token"
        return {**_period_documents(), **overrides}

    return fetch


def _snapshots(store: Store) -> list[dict[str, Any]]:
    return [row for row in store.rows("activity") if row.get("type") == "usage.snapshot"]


def test_scan_session_usage_inserts_snapshot(tmp_path: Path) -> None:
    store = _period_store(tmp_path)

    activity_id = scan_session_usage(
        store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z"
    )

    assert isinstance(activity_id, str)
    row = store.row("activity", activity_id)
    assert row is not None
    assert row["type"] == "usage.snapshot"
    assert row["session_id"] == USAGE_SESSION
    assert row["execution_status"] == "done"
    assert row["payload"] == {
        "vendor": PERIOD_DRIVER,
        "provider": PERIOD_DRIVER,
        "account_email": "user@example.com",
        "tier": "Plan A",
        "used_percent": 11.0,
        "period_type": "PERIOD_WEEKLY",
        "period_start": "2026-08-18T20:37:07.245706+00:00",
        "period_end": "2026-08-25T20:37:07.245706+00:00",
        "products": [{"product": "product-a", "used_percent": 11.0}],
        "prepaid_val": 0,
        "on_demand_cap_val": 0,
        "on_demand_used_val": 0,
        "fetched_at": "2026-08-20T12:00:00Z",
    }
    assert "test-token" not in json.dumps(row["payload"])


def test_scan_session_usage_dedups_identical_snapshot(tmp_path: Path) -> None:
    store = _period_store(tmp_path)

    first = scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z")
    second = scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:01:00Z")

    assert isinstance(first, str)
    assert second is None
    assert len(_snapshots(store)) == 1


def test_scan_session_usage_inserts_on_change_and_dedups_against_latest(
    tmp_path: Path,
) -> None:
    store = _period_store(tmp_path)
    used = {"v": 11.0}

    def fetch(_driver: str, _spec: UsageSpec, _token: str) -> dict[str, Any]:
        return _period_documents(used=used["v"])

    first = scan_session_usage(store, fetch=fetch, now=lambda: "2026-08-20T12:00:00Z")
    used["v"] = 12.0
    second = scan_session_usage(store, fetch=fetch, now=lambda: "2026-08-20T12:01:00Z")
    third = scan_session_usage(store, fetch=fetch, now=lambda: "2026-08-20T12:02:00Z")

    assert isinstance(first, str)
    assert isinstance(second, str)
    assert first != second
    assert third is None
    assert len(_snapshots(store)) == 2


def test_scan_session_usage_inserts_on_account_email_change(tmp_path: Path) -> None:
    store = _period_store(tmp_path, email="a@example.com")
    first = scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z")
    _credential(tmp_path / "profiles" / PERIOD_DRIVER, email="b@example.com")
    second = scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:01:00Z")

    assert isinstance(first, str)
    assert isinstance(second, str)
    assert {row["payload"]["account_email"] for row in _snapshots(store)} == {
        "a@example.com",
        "b@example.com",
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**400])
def test_scan_session_usage_rejects_a_non_finite_used_percent(
    tmp_path: Path, value: Any
) -> None:
    store = _period_store(tmp_path)
    billing = _billing()
    billing["used"] = value

    with pytest.raises(StoreError, match="used_percent at /billing/used must be a finite number"):
        scan_session_usage(store, fetch=_period_fetch(billing=billing))
    assert _snapshots(store) == []


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        ({"billing": {"limit": 100, "history": []}}, "period_type at /billing/period/type is missing"),
        ({"plan": {}}, "tier at /plan/tier is missing"),
        ({"plan": {"tier": ""}}, "tier at /plan/tier must be a non-empty string"),
        ({"billing": {**_billing(), "used": True}}, "used_percent at /billing/used must be a number"),
        ({"billing": {**_billing(), "products": None}}, "products at /billing/products must be a list"),
        ({"billing": {**_billing(), "products": ["x"]}}, "products at /billing/products item 0 must be an object"),
        (
            {"billing": {**_billing(), "products": [{"name": ""}]}},
            "products[0].product at /name must be a non-empty string",
        ),
        ({"billing": {**_billing(), "cap": {"val": 0.5}}}, "on_demand_cap_val at /billing/cap/val must be an integer"),
        ({"billing": {**_billing(), "prepaid": 0}}, "prepaid_val at /billing/prepaid/val is missing"),
    ],
)
def test_scan_session_usage_names_the_field_and_pointer_of_a_bad_response(
    tmp_path: Path, documents: dict[str, Any], message: str
) -> None:
    store = _period_store(tmp_path)

    with pytest.raises(StoreError, match=re.escape(f"driver-a usage field {message}")):
        scan_session_usage(store, fetch=_period_fetch(**documents))
    assert _snapshots(store) == []


def test_period_snapshot_accepts_an_empty_product_list_and_integer_percentages() -> None:
    billing = {**_billing(), "used": 12, "products": []}

    snapshot = period_snapshot(
        PERIOD_DRIVER,
        _usage(PERIOD_DRIVER),
        {"billing": billing, "plan": PLAN},
        "2026-08-20T12:00:00Z",
        "user@example.com",
    )

    assert snapshot["used_percent"] == 12.0
    assert isinstance(snapshot["used_percent"], float)
    assert snapshot["products"] == []


@pytest.mark.parametrize(
    ("credential", "error", "message"),
    [
        ({"expires_at": "2020-01-01T00:00:00Z"}, AuthStale, "driver-a credential expired"),
        ({"email": ""}, StoreError, "driver-a credential entry field email is missing or invalid"),
        ({"expires_at": "2099-01-01T00:00:00"}, StoreError, "field expires_at is invalid"),
    ],
)
def test_scan_session_usage_credential_errors(
    tmp_path: Path, credential: dict[str, str], error: type[StoreError], message: str
) -> None:
    store = _period_store(tmp_path, **credential)

    with pytest.raises(error, match=re.escape(message)):
        scan_session_usage(store, fetch=_period_fetch())
    assert _snapshots(store) == []


def test_scan_session_usage_missing_or_foreign_credential_is_stale(tmp_path: Path) -> None:
    directories = _configure(tmp_path)
    store = Store(tmp_path)
    _owned_session(store)

    with pytest.raises(AuthStale, match="driver-a credential file not found"):
        scan_session_usage(store, fetch=_period_fetch())

    (directories[PERIOD_DRIVER] / "auth.json").write_text(
        json.dumps({"https://other.example.test::client": {"key": "x"}}), encoding="utf-8"
    )
    with pytest.raises(AuthStale, match=re.escape(f"no entry that starts with {ENTRY_PREFIX}")):
        scan_session_usage(store, fetch=_period_fetch())

    (directories[PERIOD_DRIVER] / "auth.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError, match="driver-a credential file is not valid JSON"):
        scan_session_usage(store, fetch=_period_fetch())
    assert _snapshots(store) == []


def test_scan_session_usage_requires_an_owned_active_usage_session(tmp_path: Path) -> None:
    directories = _configure(tmp_path)
    _credential(directories[PERIOD_DRIVER])
    store = Store(tmp_path)

    with pytest.raises(StoreError, match="AI usage session must be owned and active"):
        scan_session_usage(store, fetch=_period_fetch())

    store.apply_replica_row(
        {
            "table": "session",
            "row_id": USAGE_SESSION,
            "origin_device_id": "other-device",
            "payload": {"id": USAGE_SESSION, "kind": "human", "status": "active"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    )
    with pytest.raises(StoreError, match="AI usage session must be owned and active"):
        scan_session_usage(store, fetch=_period_fetch())
    assert _snapshots(store) == []


def test_scan_session_usage_without_usage_session_is_stale(tmp_path: Path) -> None:
    _configure(tmp_path, usage_session=None)
    store = Store(tmp_path)

    def forbidden(*_args: object) -> dict[str, Any]:
        raise AssertionError("an unconfigured usage session must not contact a driver")

    with pytest.raises(AuthStale, match="AI usage monitoring is not configured"):
        scan_session_usage(store, fetch=forbidden)


def test_scan_session_usage_locks_on_the_driver(tmp_path: Path) -> None:
    store = _period_store(tmp_path)
    lock_keys: list[str] = []
    write = store.write_with_advisory

    def recording(*args: Any, **kwargs: Any) -> Any:
        lock_keys.append(kwargs["lock_key"])
        return write(*args, **kwargs)

    store.write_with_advisory = recording  # type: ignore[method-assign]
    scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z")

    assert lock_keys == ["usage.snapshot:driver-a"]


def test_unchanged_period_snapshot_ignores_newer_spend_snapshot(tmp_path: Path) -> None:
    store = _period_store(tmp_path)
    first = scan_session_usage(store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z")
    assert isinstance(first, str)
    store.write(
        "activity",
        "insert",
        "00000000-0000-4000-8000-000000000001",
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "session_id": USAGE_SESSION,
            "type": "usage.snapshot",
            "payload": {"vendor": SPEND_DRIVER, "account": SPEND_ACCOUNT},
            "execution_status": "done",
        },
    )

    assert scan_session_usage(
        store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:01:00Z"
    ) is None
    period_rows = [
        row for row in _snapshots(store) if row["payload"].get("vendor") == PERIOD_DRIVER
    ]
    assert len(period_rows) == 1


def test_fetch_usage_documents_sends_every_configured_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == BILLING_URL:
            return httpx.Response(200, json=_billing())
        if str(request.url) == PLAN_URL:
            return httpx.Response(200, json=PLAN)
        return httpx.Response(404, json={"error": "unexpected"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        documents = fetch_usage_documents(
            PERIOD_DRIVER, _usage(PERIOD_DRIVER), "test-token", client
        )

    assert documents == {"billing": _billing(), "plan": PLAN}
    assert [str(request.url) for request in requests] == [BILLING_URL, PLAN_URL]
    for request in requests:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/json"
    assert requests[0].headers["x-client"] == "agent-tests"
    assert "x-client" not in requests[1].headers


@pytest.mark.parametrize(
    ("response_kind", "message"),
    [
        ("non-200", "driver-a usage request billing returned HTTP 503"),
        ("invalid-json", "driver-a usage request billing returned invalid JSON"),
        ("non-object", "driver-a usage request billing response is not an object"),
        ("transport", "driver-a usage request billing failed: ConnectError"),
    ],
)
def test_fetch_usage_documents_failure_hides_token(response_kind: str, message: str) -> None:
    token = f"secret-{response_kind}"

    def handler(request: httpx.Request) -> httpx.Response:
        if response_kind == "non-200":
            return httpx.Response(503, text=f"server echoed {token}")
        if response_kind == "invalid-json":
            return httpx.Response(200, text=f"not-json-{token}")
        if response_kind == "non-object":
            return httpx.Response(200, json=[token])
        raise httpx.ConnectError(f"connection failed for {token}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(StoreError) as excinfo:
            fetch_usage_documents(PERIOD_DRIVER, _usage(PERIOD_DRIVER), token, client)

    assert str(excinfo.value) == message
    assert token not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("proxy_from_environment", [False, True])
def test_owned_usage_client_follows_the_proxy_setting(
    monkeypatch: pytest.MonkeyPatch, proxy_from_environment: bool
) -> None:
    seen: dict[str, Any] = {}
    document = driver_document()
    document["drivers"][PERIOD_DRIVER]["usage"]["proxy_from_environment"] = proxy_from_environment
    real_client = httpx.Client

    class RecordingClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("authorization", request.headers.get("Authorization"))
        return httpx.Response(200, json=PLAN)

    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    if not proxy_from_environment:
        # An intercepting proxy and a custom CA would otherwise receive the bearer.
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8123")
        monkeypatch.setenv("SSL_CERT_FILE", "/ambient/intercept-ca.pem")
    monkeypatch.setattr(httpx, "Client", RecordingClient)

    fetch_usage_documents(PERIOD_DRIVER, _usage(PERIOD_DRIVER, document), "test-token")

    assert seen["trust_env"] is proxy_from_environment
    assert seen["timeout"] == 8.0
    assert seen["follow_redirects"] is False
    assert seen["authorization"] == "Bearer test-token"


def test_scan_account_usage_snapshot_and_dedup(tmp_path: Path) -> None:
    store, _ = _spend_store(tmp_path)
    _owned_session(store, "s-spend", SPEND_DRIVER)

    def fetch(driver: str, spec: UsageSpec, token: str) -> dict[str, Any]:
        assert driver == SPEND_DRIVER
        assert spec.snapshot == "spend"
        assert token == "spend-secret"
        return {"key": _key_body()}

    created = scan_account_usage(store, fetch=fetch, now=lambda: "2026-11-04T10:00:00Z")

    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None and row["session_id"] == "s-spend"
    assert row["payload"] == {
        "vendor": SPEND_DRIVER,
        "provider": SPEND_DRIVER,
        "account": SPEND_ACCOUNT,
        "usage_usd": 1.25,
        "usage_daily_usd": 0.25,
        "usage_weekly_usd": 0.75,
        "usage_monthly_usd": 1.0,
        "limit_usd": 10.0,
        "limit_remaining_usd": 8.75,
        "limit_reset": "2026-12-01T00:00:00Z",
        "fetched_at": "2026-11-04T10:00:00Z",
    }
    assert "spend-secret" not in json.dumps(row["payload"])
    assert scan_account_usage(store, fetch=fetch, now=lambda: "2026-11-04T10:01:00Z") == []


def test_scan_account_usage_without_owned_session_names_the_driver(tmp_path: Path) -> None:
    store, _ = _spend_store(tmp_path)

    with pytest.raises(AccountUsageError, match="no owned session to record driver-d usage"):
        scan_account_usage(
            store, fetch=lambda *_args: {"key": _key_body()}, now=lambda: "2026-11-04T10:00:00Z"
        )


def test_scan_account_usage_prefers_a_session_of_the_driver(tmp_path: Path) -> None:
    store, _ = _spend_store(tmp_path)
    driver_session = _owned_session(store, "s-spend", SPEND_DRIVER)
    _owned_session(store, "s-other", "driver-b")

    created = scan_account_usage(
        store, fetch=lambda *_args: {"key": _key_body()}, now=lambda: "2026-11-04T10:00:00Z"
    )

    row = store.row("activity", created[0])
    assert row is not None and row["session_id"] == driver_session


def test_scan_account_usage_dedups_latest_snapshot_in_same_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _spend_store(tmp_path)
    _owned_session(store, "s-spend", SPEND_DRIVER)
    monkeypatch.setattr("agent_cli.store.utcnow", lambda: "2026-11-04T10:00:00Z")
    usage = {"value": 1.25}

    def fetch(*_args: object) -> dict[str, Any]:
        return {"key": _key_body(usage=usage["value"])}

    first = scan_account_usage(store, fetch=fetch, now=lambda: "2026-11-04T10:00:00Z")
    usage["value"] = 2.5
    second = scan_account_usage(store, fetch=fetch, now=lambda: "2026-11-04T10:00:00Z")

    assert len(first) == 1
    assert len(second) == 1
    assert scan_account_usage(store, fetch=fetch, now=lambda: "2026-11-04T10:00:00Z") == []
    assert len(_snapshots(store)) == 2


def test_scan_account_usage_missing_key_and_malformed_data(tmp_path: Path) -> None:
    store, directories = _spend_store(tmp_path, key=False)
    _owned_session(store, "s-spend", SPEND_DRIVER)

    with pytest.raises(AccountUsageError, match=f"{SPEND_ACCOUNT}: driver-d key missing"):
        scan_account_usage(store, fetch=lambda *_args: {"key": _key_body()})
    assert _snapshots(store) == []

    _key(directories[SPEND_DRIVER])
    with pytest.raises(
        AccountUsageError, match="driver-d usage field usage_usd at /key/data/usage is missing"
    ):
        scan_account_usage(store, fetch=lambda *_args: {"key": {"data": []}})


def test_scan_account_usage_keeps_scanning_after_a_failing_account(tmp_path: Path) -> None:
    directories = _configure(
        tmp_path,
        usage_session=None,
        usage_accounts=["spend-broken", SPEND_ACCOUNT],
        extra_spend_accounts=("spend-broken",),
    )
    _key(directories[SPEND_DRIVER])
    store = Store(tmp_path)
    _owned_session(store, "s-spend", SPEND_DRIVER)
    lock_keys: list[str] = []
    write = store.write_with_advisory

    def recording(*args: Any, **kwargs: Any) -> Any:
        lock_keys.append(kwargs["lock_key"])
        return write(*args, **kwargs)

    store.write_with_advisory = recording  # type: ignore[method-assign]

    with pytest.raises(AccountUsageError) as excinfo:
        scan_account_usage(store, fetch=lambda *_args: {"key": _key_body()})

    assert excinfo.value.errors == ("spend-broken: driver-d key missing",)
    assert len(excinfo.value.activity_ids) == 1
    assert lock_keys == [f"usage.snapshot:driver-d:{SPEND_ACCOUNT}"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"limit": None, "limit_remaining": None}, {"limit_usd": None, "limit_remaining_usd": None}),
        ({"limit": ..., "limit_remaining": ..., "limit_reset": ...}, {"limit_usd": None, "limit_remaining_usd": None, "limit_reset": None}),
        ({"usage": 2, "limit_reset": 1767225600}, {"usage_usd": 2, "limit_reset": 1767225600}),
    ],
)
def test_spend_snapshot_reads_absent_or_null_limits_as_no_limit(
    data: dict[str, Any], expected: dict[str, Any]
) -> None:
    body = _key_body()
    for key, value in data.items():
        if value is ...:
            del body["data"][key]
        else:
            body["data"][key] = value

    snapshot = spend_snapshot(
        SPEND_DRIVER, _usage(SPEND_DRIVER), SPEND_ACCOUNT, {"key": body}, "2026-11-04T10:00:00Z"
    )

    for key, value in expected.items():
        assert snapshot[key] == value
        assert type(snapshot[key]) is type(value)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"usage": ...}, "usage_usd at /key/data/usage is missing"),
        ({"usage": None}, "usage_usd at /key/data/usage must be a number"),
        ({"usage_daily": True}, "usage_daily_usd at /key/data/usage_daily must be a number"),
        ({"usage_weekly": 10**400}, "usage_weekly_usd at /key/data/usage_weekly must be a finite number"),
        ({"limit": float("inf")}, "limit_usd at /key/data/limit must be a finite number"),
        ({"limit_reset": {"at": "x"}}, "limit_reset at /key/data/limit_reset has an invalid type"),
        ({"limit_reset": False}, "limit_reset at /key/data/limit_reset has an invalid type"),
    ],
)
def test_spend_snapshot_names_the_field_and_pointer_of_a_bad_response(
    data: dict[str, Any], message: str
) -> None:
    body = _key_body()
    for key, value in data.items():
        if value is ...:
            del body["data"][key]
        else:
            body["data"][key] = value

    with pytest.raises(StoreError, match=re.escape(f"driver-d usage field {message}")):
        spend_snapshot(
            SPEND_DRIVER, _usage(SPEND_DRIVER), SPEND_ACCOUNT, {"key": body}, "2026-11-04T10:00:00Z"
        )


def test_fetch_spend_documents_via_mock_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_key_body())

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        documents = fetch_usage_documents(SPEND_DRIVER, _usage(SPEND_DRIVER), "spend-secret", client)

    assert documents == {"key": _key_body()}
    assert [str(request.url) for request in requests] == [KEY_URL]
    assert requests[0].headers["Authorization"] == "Bearer spend-secret"
    assert requests[0].headers["Accept"] == "application/json"


def test_last_usage_snapshot_via_last_own_activity_insert(tmp_path: Path) -> None:
    store = _period_store(tmp_path)
    activity_id = scan_session_usage(
        store, fetch=_period_fetch(), now=lambda: "2026-08-20T12:00:00Z"
    )
    assert isinstance(activity_id, str)

    event = store.last_own_activity_insert("usage.snapshot")
    assert event is not None
    assert event["id"] == activity_id
    snapshot = last_usage_snapshot(store, lambda payload: payload.get("vendor") == PERIOD_DRIVER)
    assert snapshot is not None
    assert snapshot["used_percent"] == 11.0
    assert last_usage_snapshot(store, lambda payload: payload.get("vendor") == "other") is None


def test_usage_helpers_accept_compare_keys_and_payload_match(tmp_path: Path) -> None:
    store = Store(tmp_path)
    session_id = _owned_session(store)
    store.write(
        "activity",
        "insert",
        "00000000-0000-4000-8000-000000000002",
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "session_id": session_id,
            "type": "usage.snapshot",
            "payload": {
                "vendor": SPEND_DRIVER,
                "account": SPEND_ACCOUNT,
                "usage_usd": 1.25,
                "fetched_at": "2026-11-04T10:00:00Z",
            },
            "execution_status": "done",
        },
    )
    snapshot = last_usage_snapshot(
        store,
        lambda payload: payload.get("vendor") == SPEND_DRIVER
        and payload.get("account") == SPEND_ACCOUNT,
    )
    assert snapshot is not None
    keys = ("vendor", "account", "usage_usd")
    assert usage_unchanged(snapshot, {**snapshot, "fetched_at": "2026-11-04T10:01:00Z"}, keys)
    assert not usage_unchanged(snapshot, {**snapshot, "usage_usd": 2.5}, keys)


def test_usage_poll_due() -> None:
    assert usage_poll_due(None, 0) is True
    assert usage_poll_due(100.0, 159.0, 60) is False
    assert usage_poll_due(100.0, 160.0, 60) is True


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/a/b", 1),
        ("/a~1b/0", "slash"),
        ("/a~0b", "tilde"),
        ("/list/1/name", "second"),
        ("/list/01/name", None),
        ("/list/2/name", None),
        ("/list/-/name", None),
        ("/a/b/c", None),
        ("/missing/x", None),
        ("/", "empty key"),
    ],
)
def test_resolve_follows_json_pointers(pointer: str, expected: Any) -> None:
    root = {
        "a": {"b": 1},
        "a/b": ["slash"],
        "a~b": "tilde",
        "list": [{"name": "first"}, {"name": "second"}],
        "": "empty key",
    }
    value = _resolve(root, pointer)
    if expected is None:
        assert value is _MISSING
    else:
        assert value == expected


def test_resolve_reads_an_absent_last_segment_as_the_given_value() -> None:
    root = {"key": {"data": {"usage": 1}}}
    assert _resolve(root, "/key/data/limit", absent_leaf=None) is None
    assert _resolve(root, "/key/other/limit", absent_leaf=None) is _MISSING
    assert _resolve(root, "/key/data/usage", absent_leaf=None) == 1


def test_spend_snapshots_are_told_apart_by_their_account_field() -> None:
    assert is_spend_snapshot({"vendor": SPEND_DRIVER, "account": SPEND_ACCOUNT})
    assert is_spend_snapshot({"account": None})
    assert not is_spend_snapshot({"vendor": PERIOD_DRIVER, "account_email": "user@example.com"})
    assert not is_spend_snapshot({})
