from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_cli import usage
from agent_cli.drivers import DriverSpec, UsageSpec
from agent_cli.store import Store, StoreError
from agent_cli.usage import (
    AccountUsageError,
    AuthStale,
    UsageRateLimited,
    _get_bearer_json,
    _retry_after_seconds,
    _window_reset,
    fetch_window_documents,
    keychain_service_name,
    keychain_user,
    load_login_account,
    load_login_credentials,
    read_keychain_item,
    scan_account_usage,
    scan_window_usage,
    windows_snapshot,
)
from driver_fixtures import (
    WINDOWS_ACCOUNT_FILE,
    WINDOWS_CREDENTIAL_FILE,
    WINDOWS_CREDENTIAL_LABEL,
    WINDOWS_DRIVER_ID,
    WINDOWS_LABEL,
    WINDOWS_OBJECT,
    WINDOWS_POLL_SECONDS,
    WINDOWS_SERVICE,
    WINDOWS_TOKEN_FIELD,
    WINDOWS_TOKEN_NAME,
    WINDOWS_URL,
    driver_document,
    driver_specs,
    rpc_driver_spec,
    windows_driver,
    windows_driver_spec,
    write_drivers,
)

FUTURE_MS = 4_102_444_800_000  # 2100-01-01
PAST_MS = 1_000_000_000_000  # 2001-09-09
TOKEN = "login-test-token"
SPEC = windows_driver_spec()
ADAPTED = SPEC.usage
assert ADAPTED is not None
KEYCHAIN = ADAPTED.credential.keychain
assert KEYCHAIN is not None


def no_keychain(_service: str, _user: str) -> str | None:
    return None


def credentials(
    *,
    token: Any = TOKEN,
    expires_at: Any = FUTURE_MS,
    subscription: Any = "max",
    tier: Any = "default_plan_max_20x",
) -> dict[str, Any]:
    oauth: dict[str, Any] = {
        "accessToken": token,
        "refreshToken": "must-not-be-used",
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": subscription,
        "rateLimitTier": tier,
    }
    return {WINDOWS_OBJECT: oauth}


def write_login(
    config_dir: Path,
    *,
    email: str = "seat-1@example.com",
    creds: dict[str, Any] | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": email, "organizationUuid": "x"}}),
        encoding="utf-8",
    )
    (config_dir / WINDOWS_CREDENTIAL_FILE).write_text(
        json.dumps(credentials() if creds is None else creds), encoding="utf-8"
    )


def usage_body(*, five_hour: Any = None, weekly: float = 14.0) -> dict[str, Any]:
    return {
        "five_hour": five_hour,
        "seven_day": {
            "utilization": weekly,
            "resets_at": "2026-11-23T10:00:00.403646+00:00",
        },
        "seven_day_oauth_apps": None,
        "limits": [
            {
                "kind": "weekly_all",
                "percent": 14,
                "resets_at": "2026-11-23T10:00:00.403646+00:00",
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "percent": 21,
                "resets_at": "2026-11-23T10:00:00+00:00",
                "scope": {"model": {"id": None, "display_name": "model-x"}, "surface": None},
                "is_active": True,
            },
        ],
        "extra_usage": {"is_enabled": False},
        "spend": None,
    }


def mapped(
    email: str,
    tier: str | None,
    body: dict[str, Any],
    fetched_at: str = "t",
) -> dict[str, Any]:
    return windows_snapshot(
        WINDOWS_DRIVER_ID, ADAPTED, email, tier, {"usage": body}, fetched_at
    )


def test_keychain_service_for_default_and_named_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert keychain_service_name(SPEC, str(tmp_path / ".login-tool")) == WINDOWS_SERVICE

    named = "/operator/login-seat-1"
    expected = hashlib.sha256(named.encode("utf-8")).hexdigest()[:8]
    assert keychain_service_name(SPEC, named) == f"{WINDOWS_SERVICE}-{expected}"

    composed = "/operator/café"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    assert keychain_service_name(SPEC, decomposed) == keychain_service_name(SPEC, composed)
    digest = hashlib.sha256(composed.encode("utf-8")).hexdigest()[:8]
    assert keychain_service_name(SPEC, composed) == f"{WINDOWS_SERVICE}-{digest}"


def test_keychain_user_uses_plain_user_name_only() -> None:
    assert keychain_user(KEYCHAIN, {"USER": "operator.name-1_x"}) == "operator.name-1_x"
    assert keychain_user(KEYCHAIN, {"USER": "seat@example.com"}) == "login-user"
    assert keychain_user(KEYCHAIN, {"USER": ""}) == "login-user"
    assert keychain_user(KEYCHAIN, {}) == "login-user"


def test_read_keychain_runs_security_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    result = {"code": 0, "out": json.dumps(credentials()) + "\n"}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, result["code"], result["out"], "")

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    assert read_keychain_item(
        KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL
    ) == json.dumps(credentials())
    argv, kwargs = calls[0]
    assert argv == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        WINDOWS_SERVICE,
        "-a",
        "operator",
        "-w",
    ]
    assert kwargs.get("shell") in (None, False)
    assert kwargs["timeout"] > 0
    assert kwargs["check"] is False

    result.update(code=44, out="")
    assert (
        read_keychain_item(KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL)
        is None
    )

    result.update(code=51, out=TOKEN)
    with pytest.raises(StoreError, match=r"keychain read failed \(exit 51\)") as info:
        read_keychain_item(KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL)
    assert TOKEN not in str(info.value)


def test_read_keychain_timeout_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(usage.subprocess, "run", slow)
    with pytest.raises(StoreError, match="timed out"):
        read_keychain_item(KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL)


def test_credentials_prefer_the_keychain_item_of_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USER", "operator")
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(token="file-token"))
    asked: list[tuple[str, str]] = []

    def reader(service: str, user: str) -> str | None:
        asked.append((service, user))
        return json.dumps(credentials(token="keychain-token"))

    assert load_login_credentials(SPEC, str(config_dir), read_keychain=reader) == (
        "keychain-token",
        "max_20x",
    )
    assert asked == [(keychain_service_name(SPEC, str(config_dir)), "operator")]


def test_credentials_fall_back_to_the_file_when_the_keychain_has_no_item(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(subscription="pro", tier="default_plan_pro"))
    assert load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain) == (
        TOKEN,
        "pro",
    )


def test_credentials_off_platform_read_only_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_keychain: object, _service: str, _user: str, *, label: str) -> str | None:
        raise AssertionError(label)

    monkeypatch.setattr(usage.sys, "platform", "never")
    monkeypatch.setattr(usage, "read_keychain_item", forbidden)
    config_dir = tmp_path / "seat"
    write_login(config_dir)
    assert load_login_credentials(SPEC, str(config_dir)) == (TOKEN, "max_20x")


def test_credentials_keychain_failure_is_reported_without_a_file(tmp_path: Path) -> None:
    def failing(_service: str, _user: str) -> str | None:
        raise StoreError(f"{WINDOWS_CREDENTIAL_LABEL} keychain read failed (exit 51)")

    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(StoreError, match="exit 51") as info:
        load_login_credentials(SPEC, str(config_dir), read_keychain=failing)
    assert not isinstance(info.value, AuthStale)

    write_login(config_dir)
    assert load_login_credentials(SPEC, str(config_dir), read_keychain=failing)[0] == TOKEN


def test_credentials_missing_login_and_expired_token_are_stale(tmp_path: Path) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(AuthStale, match="login not found"):
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds={"otherOauth": {}})
    with pytest.raises(AuthStale, match="no Login"):
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds={WINDOWS_OBJECT: {"expiresAt": FUTURE_MS}})
    with pytest.raises(AuthStale, match=f"no {WINDOWS_TOKEN_NAME}"):
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds=credentials(expires_at=PAST_MS))
    with pytest.raises(AuthStale, match="expired"):
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{" + TOKEN, "not valid JSON"),
        ("[]", "not an object"),
        (json.dumps({WINDOWS_OBJECT: []}), f"{WINDOWS_OBJECT} must be an object"),
        (json.dumps(credentials(token=7)), f"{WINDOWS_TOKEN_FIELD} must be a non-empty string"),
        (json.dumps(credentials(token="")), f"{WINDOWS_TOKEN_FIELD} must be a non-empty string"),
        (json.dumps(credentials(expires_at=None)), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(expires_at="soon")), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(expires_at=True)), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(expires_at=10**400)), "expiresAt must be epoch milliseconds"),
    ],
)
def test_malformed_credentials_are_errors_without_the_token(
    tmp_path: Path, raw: str, message: str
) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    (config_dir / WINDOWS_CREDENTIAL_FILE).write_text(raw, encoding="utf-8")
    with pytest.raises(StoreError, match=message) as info:
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)
    assert not isinstance(info.value, AuthStale)
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize(
    ("subscription", "tier", "expected"),
    [
        ("max", "default_plan_max_20x", "max_20x"),
        ("max", "default_plan_max_5x", "max_5x"),
        ("max", None, "max"),
        ("pro", "default_plan_max_20x", "pro"),
        ("team", "something_else", "team"),
        (None, "default_plan_max_20x", None),
        ("", None, None),
    ],
)
def test_tier_adds_the_max_multiplier(
    tmp_path: Path, subscription: Any, tier: Any, expected: str | None
) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(subscription=subscription, tier=tier))
    assert load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)[1] == expected


def test_account_email_location_for_default_and_named_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    default_dir = tmp_path / ".login-tool"
    default_dir.mkdir()
    (tmp_path / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": "default@example.com"}}),
        encoding="utf-8",
    )
    (default_dir / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": "wrong@example.com"}}),
        encoding="utf-8",
    )
    assert load_login_account(SPEC, str(default_dir)) == "default@example.com"

    named = tmp_path / "seat"
    write_login(named, email="seat-1@example.com")
    assert load_login_account(SPEC, str(named)) == "seat-1@example.com"


def test_account_email_errors(tmp_path: Path) -> None:
    named = tmp_path / "seat"
    named.mkdir()
    with pytest.raises(AuthStale, match="not found"):
        load_login_account(SPEC, str(named))
    path = named / WINDOWS_ACCOUNT_FILE
    path.write_text(json.dumps({"numStartups": 3}), encoding="utf-8")
    with pytest.raises(AuthStale, match="no loginAccount"):
        load_login_account(SPEC, str(named))
    path.write_text("{", encoding="utf-8")
    with pytest.raises(StoreError, match="not valid JSON"):
        load_login_account(SPEC, str(named))
    path.write_text(json.dumps({"loginAccount": {"emailAddress": ""}}), encoding="utf-8")
    with pytest.raises(StoreError, match="emailAddress"):
        load_login_account(SPEC, str(named))
    path.write_text(json.dumps({"loginAccount": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must be an object"):
        load_login_account(SPEC, str(named))


@pytest.mark.parametrize("spec", [rpc_driver_spec(), driver_specs()["driver-d"]], ids=["rpc", "spend"])
def test_login_account_needs_a_windows_usage_section(tmp_path: Path, spec: DriverSpec) -> None:
    with pytest.raises(StoreError, match=f"^{spec.id} has no windows usage section$"):
        load_login_account(spec, str(tmp_path))


def _windows_spec_with(**changes: Any) -> DriverSpec:
    assert ADAPTED is not None and isinstance(ADAPTED, UsageSpec)
    return dataclasses.replace(SPEC, usage=dataclasses.replace(ADAPTED, **changes))


def _windows_spec_with_credential(**changes: Any) -> DriverSpec:
    assert ADAPTED is not None and isinstance(ADAPTED, UsageSpec)
    return _windows_spec_with(credential=dataclasses.replace(ADAPTED.credential, **changes))


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_windows_spec_with(poll_seconds=None), "windows usage section is incomplete"),
        (_windows_spec_with(account_file=None), "windows usage section is incomplete"),
        (_windows_spec_with(window=None), "windows usage section is incomplete"),
        (_windows_spec_with(limits=None), "windows usage section is incomplete"),
        (_windows_spec_with_credential(label=None), "windows credential section is incomplete"),
        (_windows_spec_with_credential(renew=None), "windows credential section is incomplete"),
        (_windows_spec_with_credential(token=None), "windows credential section is incomplete"),
    ],
    ids=["poll_seconds", "account_file", "window", "limits", "label", "renew", "token"],
)
def test_login_account_needs_a_complete_windows_section(
    tmp_path: Path, spec: DriverSpec, message: str
) -> None:
    with pytest.raises(StoreError, match=f"^{WINDOWS_DRIVER_ID} {message}$"):
        load_login_account(spec, str(tmp_path))


def test_snapshot_maps_windows_and_keeps_missing_windows_null() -> None:
    snapshot = mapped("seat-1@example.com", "max_20x", usage_body(), "2026-11-16T12:00:00Z")
    assert snapshot == {
        "vendor": WINDOWS_DRIVER_ID,
        "provider": WINDOWS_DRIVER_ID,
        "account_email": "seat-1@example.com",
        "tier": "max_20x",
        "windows": {
            "five_hour": None,
            "weekly": {"used_percent": 14.0, "resets_at": "2026-11-23T10:00:00+00:00"},
            "weekly_models": [
                {
                    "model": "model-x",
                    "used_percent": 21.0,
                    "resets_at": "2026-11-23T10:00:00+00:00",
                }
            ],
        },
        "fetched_at": "2026-11-16T12:00:00Z",
    }
    assert isinstance(snapshot["windows"]["weekly_models"][0]["used_percent"], float)


def test_snapshot_reads_the_five_hour_window_and_ignores_unknown_keys() -> None:
    body = usage_body(five_hour={"utilization": 12, "resets_at": "2026-11-16T22:00:00Z"})
    body["unknown_future_key"] = {"anything": [1, 2]}
    snapshot = mapped("seat-1@example.com", None, body)
    assert snapshot["windows"]["five_hour"] == {
        "used_percent": 12.0,
        "resets_at": "2026-11-16T22:00:00+00:00",
    }
    assert snapshot["tier"] is None


def test_snapshot_without_reported_values_invents_no_zero() -> None:
    body = {
        "five_hour": {"utilization": None, "resets_at": None},
        "seven_day": None,
        "limits": None,
    }
    windows = mapped("seat-1@example.com", "pro", body)["windows"]
    assert windows == {"five_hour": None, "weekly": None, "weekly_models": []}

    body = {"five_hour": None, "seven_day": None, "limits": usage_body()["limits"]}
    windows = mapped("seat-1@example.com", "pro", body)["windows"]
    assert windows["weekly"] == {
        "used_percent": 14.0,
        "resets_at": "2026-11-23T10:00:00+00:00",
    }

    body = {"limits": [{"kind": "weekly_scoped", "percent": 5, "resets_at": None, "scope": None}]}
    windows = mapped("seat-1@example.com", "pro", body)["windows"]
    assert windows["weekly_models"] == [{"model": None, "used_percent": 5.0, "resets_at": None}]


JITTERED_RESETS = (
    "2026-11-23T10:00:00.406269+00:00",
    "2026-11-23T09:59:59.659417+00:00",
    "2026-11-23T09:59:59.923301+00:00",
    "2026-11-23T10:00:00.173192+00:00",
)


def test_reset_jitter_maps_to_one_minute() -> None:
    assert {_window_reset(value, "f", WINDOWS_LABEL) for value in JITTERED_RESETS} == {
        "2026-11-23T10:00:00+00:00"
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-23T10:00:29.999999+00:00", "2026-11-23T10:00:00+00:00"),
        ("2026-11-23T10:00:30+00:00", "2026-11-23T10:01:00+00:00"),
        ("2026-11-23T23:59:45Z", "2026-11-24T00:00:00+00:00"),
        ("2026-11-23T12:00:10+02:00", "2026-11-23T10:00:00+00:00"),
    ],
)
def test_reset_rounds_to_the_nearest_minute_in_utc(value: str, expected: str) -> None:
    assert _window_reset(value, "f", WINDOWS_LABEL) == expected


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"five_hour": []}, "five_hour must be an object or null"),
        ({"five_hour": {"utilization": "12"}}, "five_hour.utilization must be a number"),
        ({"five_hour": {"utilization": True}}, "five_hour.utilization must be a number"),
        ({"five_hour": {"utilization": float("nan")}}, "finite"),
        ({"seven_day": {"utilization": 1, "resets_at": 5}}, "seven_day.resets_at"),
        ({"seven_day": {"utilization": 1, "resets_at": "tomorrow"}}, "not a valid timestamp"),
        ({"seven_day": {"utilization": 1, "resets_at": "2026-11-23T10:00:00"}}, "no time zone"),
        ({"limits": {}}, "limits must be a list or null"),
        ({"limits": [3]}, r"limits\[0\] must be an object"),
        ({"limits": [{"kind": "weekly_all", "percent": "x"}]}, r"limits\[0\]\.percent"),
    ],
)
def test_snapshot_rejects_malformed_values(body: dict[str, Any], message: str) -> None:
    with pytest.raises(StoreError, match=message):
        mapped("seat-1@example.com", None, body)


def test_fetch_sends_the_oauth_usage_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=usage_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_window_documents(ADAPTED, TOKEN, client=client) == {"usage": usage_body()}
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == WINDOWS_URL
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Api-Beta"] == "oauth-2025-04-20"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == "dfx-agent"


def test_owned_window_client_ignores_proxy_environment_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class RecordingClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=usage_body())

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    monkeypatch.setattr(httpx, "Client", RecordingClient)
    assert fetch_window_documents(ADAPTED, TOKEN) == {"usage": usage_body()}
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert seen["timeout"] == 8.0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": TOKEN}),
        httpx.Response(200, text=TOKEN),
        httpx.Response(200, json=[]),
    ],
)
def test_fetch_errors_hide_the_token(response: httpx.Response) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client:
        with pytest.raises(StoreError, match=WINDOWS_LABEL) as info:
            fetch_window_documents(ADAPTED, TOKEN, client=client)
    assert TOKEN not in str(info.value)


def test_fetch_network_error_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StoreError, match=f"{WINDOWS_LABEL} request failed") as info:
            fetch_window_documents(ADAPTED, TOKEN, client=client)
    assert TOKEN not in str(info.value)


def _session(store: Store, session_id: str, provider: str) -> None:
    store.write(
        "session",
        "insert",
        session_id,
        {"id": session_id, "kind": "human", "status": "active", "runtime": {"provider": provider}},
    )


def _config(store: Store, accounts: dict[str, dict[str, str]], selected: list[str]) -> None:
    document = driver_document()
    document["drivers"][WINDOWS_DRIVER_ID] = windows_driver()
    write_drivers(store.home, document)
    (store.home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "driver-a", "second": "driver-b"},
                "accounts": accounts,
                "usage_accounts": selected,
            }
        ),
        encoding="utf-8",
    )


def _window_snapshots(store: Store) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows("activity")
        if row.get("type") == "usage.snapshot"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("vendor") == WINDOWS_DRIVER_ID
    ]


def test_scan_records_changed_snapshots_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-other", "driver-b")
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat, email="Seat-1@Example.com")
        _config(
            store,
            {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
            ["seat"],
        )
        weekly = {"value": 14.0}
        tokens: list[str] = []

        def fetch(token: str) -> dict[str, Any]:
            tokens.append(token)
            return usage_body(weekly=weekly["value"])

        created = scan_window_usage(
            store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:00:00Z"
        )
        assert len(created) == 1
        row = store.row("activity", created[0])
        assert row is not None and row["session_id"] == "s-windows"
        assert row["payload"]["account_email"] == "Seat-1@Example.com"
        assert row["payload"]["tier"] == "max_20x"
        assert row["payload"]["windows"]["weekly"]["used_percent"] == 14.0
        assert TOKEN not in json.dumps(row["payload"])
        assert "must-not-be-used" not in json.dumps(row["payload"])
        assert tokens == [TOKEN]

        assert scan_window_usage(
            store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:01:00Z"
        ) == []
        weekly["value"] = 15.0
        assert (
            len(
                scan_window_usage(
                    store,
                    fetch=fetch,
                    read_keychain=no_keychain,
                    now=lambda: "2026-11-16T12:02:00Z",
                )
            )
            == 1
        )
        assert len(_window_snapshots(store)) == 2
    finally:
        store.close()


def test_scan_writes_no_snapshot_for_reset_jitter(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat)
        _config(
            store,
            {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
            ["seat"],
        )
        answers = iter(JITTERED_RESETS)

        def fetch(_token: str) -> dict[str, Any]:
            reset = next(answers)
            body = usage_body()
            body["seven_day"]["resets_at"] = reset
            for limit in body["limits"]:
                limit["resets_at"] = reset
            return body

        created = [
            scan_window_usage(store, fetch=fetch, read_keychain=no_keychain)
            for _ in JITTERED_RESETS
        ]
        assert [len(ids) for ids in created] == [1, 0, 0, 0]
        assert len(_window_snapshots(store)) == 1
    finally:
        store.close()


def test_scan_skips_stale_logins_quietly_and_reports_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        stale = tmp_path / "stale"
        write_login(stale, email="stale@example.com", creds=credentials(expires_at=PAST_MS))
        missing = tmp_path / "missing"
        missing.mkdir()
        broken = tmp_path / "broken"
        write_login(broken, email="broken@example.com")
        good = tmp_path / "good"
        write_login(good, email="good@example.com")
        _config(
            store,
            {
                "stale": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(stale)},
                "missing": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(missing)},
                "broken": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(broken)},
                "good": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(good)},
            },
            ["stale", "missing", "broken", "good"],
        )
        calls: list[str] = []

        def fetch(token: str) -> dict[str, Any]:
            calls.append(token)
            if len(calls) == 1:
                raise StoreError(f"{WINDOWS_LABEL} returned HTTP 429")
            return usage_body()

        skipped: list[str] = []
        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(store, fetch=fetch, read_keychain=no_keychain, skipped=skipped)
        assert info.value.errors == (f"broken: {WINDOWS_LABEL} returned HTTP 429",)
        assert len(info.value.activity_ids) == 1
        assert [line.split(":", 1)[0] for line in skipped] == ["stale", "missing"]
        assert all(TOKEN not in line for line in skipped)
        assert len(_window_snapshots(store)) == 1
    finally:
        store.close()


def _check(store: Store, account_name: str) -> dict[str, str] | None:
    value = store.meta(f"usage.checked.{WINDOWS_DRIVER_ID}.{account_name}")
    return None if value is None else json.loads(value)


def _checked(store: Store, account_name: str) -> str | None:
    check = _check(store, account_name)
    return None if check is None else check["checked_at"]


def test_scan_records_the_check_time_of_every_successful_read(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat, email="Seat-1@Example.com")
        _config(
            store,
            {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
            ["seat"],
        )
        assert _checked(store, "seat") is None

        def fetch(_token: str) -> dict[str, Any]:
            return usage_body()

        assert len(
            scan_window_usage(
                store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:00:00Z"
            )
        ) == 1
        # The value names the case-folded login that was read; the key does not.
        assert _check(store, "seat") == {
            "checked_at": "2026-11-16T12:00:00Z",
            "identity": "seat-1@example.com",
        }

        # Unchanged usage stores no snapshot, but the check still counts.
        assert scan_window_usage(
            store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:15:00Z"
        ) == []
        assert _checked(store, "seat") == "2026-11-16T12:15:00Z"
        snapshots = _window_snapshots(store)
        assert [row["payload"]["fetched_at"] for row in snapshots] == ["2026-11-16T12:00:00Z"]
    finally:
        store.close()


def test_scan_records_no_check_time_for_skipped_failed_or_rate_limited_reads(
    tmp_path: Path,
) -> None:
    store = _paced_store(tmp_path, ["stale", "broken", "limited", "good"], stale=("stale",))
    try:
        reads, clock = _Reads(), _Clock()
        reads.fail["token-broken"] = StoreError(f"{WINDOWS_LABEL} returned HTTP 500")
        reads.fail["token-limited"] = UsageRateLimited(f"{WINDOWS_LABEL} returned HTTP 429", 60.0)
        skipped: list[str] = []
        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(
                store,
                fetch=reads.fetch,
                read_keychain=reads.read_keychain,
                now=lambda: "2026-11-16T12:00:00Z",
                skipped=skipped,
                clock=clock,
            )
        assert [line.split(":", 1)[0] for line in skipped] == ["stale"]
        assert [line.split(":", 1)[0] for line in info.value.errors] == ["broken", "limited"]
        assert _checked(store, "stale") is None
        assert _checked(store, "broken") is None
        assert _checked(store, "limited") is None
        assert _check(store, "good") == {
            "checked_at": "2026-11-16T12:00:00Z",
            "identity": "good@example.com",
        }
        # The key names the configured account, never the login's address.
        assert _checked(store, "good@example.com") is None
    finally:
        store.close()


def test_scan_records_no_check_time_for_a_malformed_answer(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat)
        _config(
            store,
            {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
            ["seat"],
        )
        with pytest.raises(AccountUsageError):
            scan_window_usage(
                store,
                fetch=lambda _token: {"limits": "not a list"},
                read_keychain=no_keychain,
                now=lambda: "2026-11-16T12:00:00Z",
            )
        assert _checked(store, "seat") is None
    finally:
        store.close()


def test_each_scanner_reads_only_its_own_adapter(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat)
        router = tmp_path / "router"
        router.mkdir()
        (router / "tool-d-key").write_text("router-secret", encoding="utf-8")
        _config(
            store,
            {
                "seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)},
                "router": {"provider": "driver-d", "config_dir": str(router)},
            },
            ["seat", "router"],
        )
        window_tokens: list[str] = []
        router_tokens: list[str] = []

        def window_fetch(token: str) -> dict[str, Any]:
            window_tokens.append(token)
            return usage_body()

        def router_fetch(_driver: str, _spec: object, token: str) -> dict[str, Any]:
            router_tokens.append(token)
            return {
                "key": {
                    "data": {
                        "usage": 1,
                        "usage_daily": 0,
                        "usage_weekly": 0,
                        "usage_monthly": 1,
                    }
                }
            }

        scan_window_usage(store, fetch=window_fetch, read_keychain=no_keychain)
        scan_account_usage(store, fetch=router_fetch)
        assert window_tokens == [TOKEN]
        assert router_tokens == ["router-secret"]
    finally:
        store.close()


def _answer(response: httpx.Response, name: str = WINDOWS_LABEL) -> dict[str, Any]:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client:
        return _get_bearer_json("https://usage.example.com/", {}, name, client)


def _http_date(offset: timedelta) -> str:
    return format_datetime(datetime.now(timezone.utc) + offset, usegmt=True)


def test_rate_limit_carries_the_retry_after_seconds() -> None:
    with pytest.raises(UsageRateLimited) as info:
        _answer(httpx.Response(429, headers={"Retry-After": "166"}, json={"error": {}}))
    assert str(info.value) == f"{WINDOWS_LABEL} returned HTTP 429"
    assert info.value.retry_after == 166.0

    with pytest.raises(UsageRateLimited) as info:
        _answer(httpx.Response(429, headers={"Retry-After": _http_date(timedelta(minutes=10))}))
    assert info.value.retry_after is not None and 500 < info.value.retry_after <= 600

    with pytest.raises(UsageRateLimited) as info:
        _answer(httpx.Response(429, headers={"Retry-After": _http_date(-timedelta(minutes=10))}))
    assert info.value.retry_after == 0.0

    for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": "-5"}, {"Retry-After": "1.5"}):
        with pytest.raises(UsageRateLimited) as info:
            _answer(httpx.Response(429, headers=headers))
        assert info.value.retry_after is None


def test_other_statuses_keep_the_plain_error_and_the_caller_name() -> None:
    with pytest.raises(StoreError) as info:
        _answer(httpx.Response(503, headers={"Retry-After": "166"}))
    assert not isinstance(info.value, UsageRateLimited)
    assert str(info.value) == f"{WINDOWS_LABEL} returned HTTP 503"

    with pytest.raises(UsageRateLimited) as info:
        _answer(httpx.Response(429, headers={"Retry-After": "166"}), "Spend usage")
    assert str(info.value) == "Spend usage returned HTTP 429"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Reads:
    """Counts keychain reads and requests per account directory and token."""

    def __init__(self) -> None:
        self.keychain: list[str] = []
        self.fetches: list[str] = []
        self.fail: dict[str, Exception] = {}

    def read_keychain(self, service: str, _user: str) -> str | None:
        self.keychain.append(service)
        return None

    def fetch(self, token: str) -> dict[str, Any]:
        self.fetches.append(token)
        if token in self.fail:
            raise self.fail[token]
        return usage_body()


def _paced_store(tmp_path: Path, names: list[str], *, stale: tuple[str, ...] = ()) -> Store:
    store = Store(tmp_path)
    _session(store, "s-windows", WINDOWS_DRIVER_ID)
    accounts = {}
    for name in names:
        config_dir = tmp_path / name
        expires = PAST_MS if name in stale else FUTURE_MS
        write_login(
            config_dir,
            email=f"{name}@example.com",
            creds=credentials(token=f"token-{name}", expires_at=expires),
        )
        accounts[name] = {"provider": WINDOWS_DRIVER_ID, "config_dir": str(config_dir)}
    _config(store, accounts, names)
    return store


def _scan(store: Store, reads: _Reads, pace: dict[str, float] | None, clock: _Clock) -> list[str]:
    try:
        scan_window_usage(
            store, fetch=reads.fetch, read_keychain=reads.read_keychain, pace=pace, clock=clock
        )
    except AccountUsageError as exc:
        return list(exc.errors)
    return []


def test_pace_reads_an_account_once_per_interval(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        assert _scan(store, reads, pace, clock) == []
        assert reads.fetches == ["token-seat"]
        assert pace == {"seat": 1000.0 + WINDOWS_POLL_SECONDS}

        clock.now += WINDOWS_POLL_SECONDS - 1
        assert _scan(store, reads, pace, clock) == []
        assert len(reads.keychain) == 1 and len(reads.fetches) == 1

        clock.now = 1000.0 + WINDOWS_POLL_SECONDS
        _scan(store, reads, pace, clock)
        assert len(reads.keychain) == 2 and len(reads.fetches) == 2
    finally:
        store.close()


def test_without_pace_every_call_reads(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock = _Reads(), _Clock()
        _scan(store, reads, None, clock)
        _scan(store, reads, None, clock)
        assert reads.fetches == ["token-seat", "token-seat"]
    finally:
        store.close()


def test_long_retry_after_postpones_the_next_read(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-seat"] = UsageRateLimited(f"{WINDOWS_LABEL} returned HTTP 429", 3600.0)
        assert _scan(store, reads, pace, clock) == [
            f"seat: {WINDOWS_LABEL} returned HTTP 429; retry after 3600 s"
        ]
        assert pace["seat"] == 1000.0 + 3600.0
        clock.now += 3599
        assert _scan(store, reads, pace, clock) == []
        assert len(reads.fetches) == 1
        clock.now = 1000.0 + 3600.0
        _scan(store, reads, pace, clock)
        assert len(reads.fetches) == 2
    finally:
        store.close()


def test_short_retry_after_keeps_the_interval(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-seat"] = UsageRateLimited(f"{WINDOWS_LABEL} returned HTTP 429", 165.2)
        assert _scan(store, reads, pace, clock) == [
            f"seat: {WINDOWS_LABEL} returned HTTP 429; retry after 166 s"
        ]
        assert pace["seat"] == 1000.0 + WINDOWS_POLL_SECONDS
        clock.now += 166
        assert _scan(store, reads, pace, clock) == []
        assert len(reads.fetches) == 1
    finally:
        store.close()


def test_rate_limit_without_header_value_is_still_an_error(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-seat"] = UsageRateLimited(f"{WINDOWS_LABEL} returned HTTP 429", None)
        assert _scan(store, reads, pace, clock) == [f"seat: {WINDOWS_LABEL} returned HTTP 429"]
        assert pace["seat"] == 1000.0 + WINDOWS_POLL_SECONDS
    finally:
        store.close()


def test_stale_logins_and_errors_are_paced_by_the_interval(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["stale", "broken"], stale=("stale",))
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-broken"] = StoreError(f"{WINDOWS_LABEL} returned HTTP 500")
        assert _scan(store, reads, pace, clock) == [f"broken: {WINDOWS_LABEL} returned HTTP 500"]
        assert pace == {
            "stale": 1000.0 + WINDOWS_POLL_SECONDS,
            "broken": 1000.0 + WINDOWS_POLL_SECONDS,
        }
        clock.now += 60
        assert _scan(store, reads, pace, clock) == []
        assert len(reads.keychain) == 2 and reads.fetches == ["token-broken"]
    finally:
        store.close()


def test_accounts_are_paced_independently(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["limited", "other"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-limited"] = UsageRateLimited(f"{WINDOWS_LABEL} returned HTTP 429", 3600.0)
        _scan(store, reads, pace, clock)
        clock.now += WINDOWS_POLL_SECONDS
        assert _scan(store, reads, pace, clock) == []
        assert reads.fetches == ["token-limited", "token-other", "token-other"]
    finally:
        store.close()


def test_retry_after_that_is_not_a_finite_number_is_unusable() -> None:
    assert _retry_after_seconds("9" * 400) is None
    with pytest.raises(UsageRateLimited) as info:
        _answer(httpx.Response(429, headers={"Retry-After": "9" * 400}))
    assert info.value.retry_after is None


def test_rate_limit_with_an_unusable_retry_after_keeps_the_interval(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail["token-seat"] = UsageRateLimited(
            f"{WINDOWS_LABEL} returned HTTP 429", _retry_after_seconds("9" * 400)
        )
        assert _scan(store, reads, pace, clock) == [f"seat: {WINDOWS_LABEL} returned HTTP 429"]
        assert pace["seat"] == 1000.0 + WINDOWS_POLL_SECONDS
    finally:
        store.close()


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:45+00:00"])
def test_reset_at_the_edge_of_the_date_range_is_a_store_error(value: str) -> None:
    with pytest.raises(StoreError, match="seven_day.resets_at is not a valid timestamp"):
        mapped(
            "seat-1@example.com",
            None,
            {"seven_day": {"utilization": 1, "resets_at": value}},
        )


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:45+00:00"])
def test_scan_reports_an_edge_reset_and_still_reads_the_next_account(
    tmp_path: Path, value: str
) -> None:
    store = _paced_store(tmp_path, ["edge", "other"])
    try:
        def fetch(token: str) -> dict[str, Any]:
            body = usage_body()
            if token == "token-edge":
                body["seven_day"]["resets_at"] = value
            return body

        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(store, fetch=fetch, read_keychain=no_keychain)
        assert info.value.errors == (
            f"edge: {WINDOWS_LABEL} seven_day.resets_at is not a valid timestamp",
        )
        assert len(info.value.activity_ids) == 1
        assert [row["payload"]["account_email"] for row in _window_snapshots(store)] == [
            "other@example.com"
        ]
    finally:
        store.close()


@pytest.mark.parametrize("token", ["t\u00f6ken-secret", "token secret", "token-secret\n"])
def test_access_token_must_be_visible_ascii(tmp_path: Path, token: str) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(token=token))
    with pytest.raises(StoreError, match=f"{WINDOWS_TOKEN_FIELD} must be visible ASCII") as info:
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)
    assert not isinstance(info.value, AuthStale)
    assert "secret" not in str(info.value)
    assert WINDOWS_CREDENTIAL_FILE in str(info.value)


def test_unencodable_bearer_is_a_request_failure_without_the_token() -> None:
    token = "t\u00f6ken-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("the request must not be sent")

    for name in (WINDOWS_LABEL, "Spend usage"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(StoreError) as info:
                _get_bearer_json(
                    "https://usage.example.com/",
                    {"Authorization": f"Bearer {token}"},
                    name,
                    client,
                )
        assert str(info.value) == f"{name} request failed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None
        assert "secret" not in str(info.value)


def test_transport_failure_is_a_request_failure_without_a_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}", request=request)

    for name in (WINDOWS_LABEL, "Spend usage"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(StoreError) as info:
                _get_bearer_json(
                    "https://usage.example.com/",
                    {"Authorization": f"Bearer {TOKEN}"},
                    name,
                    client,
                )
        assert str(info.value) == f"{name} request failed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


def _assert_unchained(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert TOKEN not in str(error)


def test_keychain_failures_keep_the_output_out_of_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=TOKEN)

    monkeypatch.setattr(usage.subprocess, "run", slow)
    with pytest.raises(StoreError, match=f"{WINDOWS_CREDENTIAL_LABEL} keychain read timed out") as info:
        read_keychain_item(KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL)
    _assert_unchained(info.value)

    def undecodable(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise UnicodeDecodeError("utf-8", b"\xff" + TOKEN.encode(), 0, 1, "invalid start byte")

    monkeypatch.setattr(usage.subprocess, "run", undecodable)
    with pytest.raises(StoreError, match=f"{WINDOWS_CREDENTIAL_LABEL} keychain read failed") as info:
        read_keychain_item(KEYCHAIN, WINDOWS_SERVICE, "operator", label=WINDOWS_CREDENTIAL_LABEL)
    _assert_unchained(info.value)


def test_invalid_credentials_json_keeps_the_text_out_of_the_chain(tmp_path: Path) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(StoreError, match="in the keychain are not valid JSON") as info:
        load_login_credentials(SPEC, str(config_dir), read_keychain=lambda _s, _u: "{" + TOKEN)
    _assert_unchained(info.value)

    (config_dir / WINDOWS_CREDENTIAL_FILE).write_text("{" + TOKEN, encoding="utf-8")
    with pytest.raises(StoreError, match=r"in \.credentials\.json are not valid JSON") as info:
        load_login_credentials(SPEC, str(config_dir), read_keychain=no_keychain)
    _assert_unchained(info.value)


def test_invalid_account_file_keeps_the_text_out_of_the_chain(tmp_path: Path) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    (config_dir / WINDOWS_ACCOUNT_FILE).write_text("{" + TOKEN, encoding="utf-8")
    with pytest.raises(
        StoreError, match=f"{WINDOWS_CREDENTIAL_LABEL} account file is not valid JSON"
    ) as info:
        load_login_account(SPEC, str(config_dir))
    _assert_unchained(info.value)


@pytest.mark.parametrize(
    ("blocked", "message"),
    [
        (WINDOWS_ACCOUNT_FILE, f"{WINDOWS_CREDENTIAL_LABEL} account file is not readable"),
        (WINDOWS_CREDENTIAL_FILE, f"{WINDOWS_CREDENTIAL_LABEL} credentials file is not readable"),
    ],
)
def test_unreadable_login_files_are_reported_and_the_next_account_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str, message: str
) -> None:
    store = _paced_store(tmp_path, ["locked", "other"])
    try:
        original = Path.is_file
        locked = tmp_path / "locked" / blocked

        def is_file(self: Path) -> bool:
            if self == locked:
                raise PermissionError(13, "Permission denied", str(self))
            return original(self)

        monkeypatch.setattr(Path, "is_file", is_file)
        reads = _Reads()
        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(store, fetch=reads.fetch, read_keychain=reads.read_keychain)
        assert info.value.errors == (f"locked: {message}",)
        assert reads.fetches == ["token-other"]
        assert len(info.value.activity_ids) == 1
    finally:
        store.close()


def test_account_file_read_error_is_not_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir)
    account_file = config_dir / WINDOWS_ACCOUNT_FILE
    original = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == account_file:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert account_file.is_file()
    with pytest.raises(StoreError) as info:
        load_login_account(SPEC, str(config_dir))
    assert str(info.value) == f"{WINDOWS_CREDENTIAL_LABEL} account file is not readable"
    _assert_unchained(info.value)


def test_scan_reports_a_fetched_answer_that_is_not_an_object(tmp_path: Path) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        tokens: list[str] = []

        def fetch(token: str) -> Any:
            tokens.append(token)
            return ["not", "an", "object"]

        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(store, fetch=fetch, read_keychain=no_keychain)
        assert info.value.errors == (f"seat: {WINDOWS_LABEL} response is not an object",)
        assert len(info.value.activity_ids) == 0
        assert tokens == ["token-seat"]
        assert _window_snapshots(store) == []
        assert _checked(store, "seat") is None
    finally:
        store.close()


def test_scan_with_a_fetch_refuses_an_adapter_with_two_requests(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-windows", WINDOWS_DRIVER_ID)
        seat = tmp_path / "seat"
        write_login(seat, creds=credentials(token="token-seat"))
        document = driver_document()
        driver = windows_driver()
        driver["usage"]["requests"].append(
            {"name": "extra", "url": "https://usage.example/oauth/extra"}
        )
        document["drivers"][WINDOWS_DRIVER_ID] = driver
        write_drivers(store.home, document)
        (store.home / "ai-accounts.json").write_text(
            json.dumps(
                {
                    "models": {"first": "driver-a", "second": "driver-b"},
                    "accounts": {
                        "seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}
                    },
                    "usage_accounts": ["seat"],
                }
            ),
            encoding="utf-8",
        )
        tokens: list[str] = []

        def fetch(token: str) -> dict[str, Any]:
            tokens.append(token)
            return usage_body()

        with pytest.raises(AccountUsageError) as info:
            scan_window_usage(store, fetch=fetch, read_keychain=no_keychain)
        assert info.value.errors == (
            f"seat: {WINDOWS_LABEL} fetch requires exactly one request",
        )
        assert len(info.value.activity_ids) == 0
        assert tokens == ["token-seat"]
        assert _window_snapshots(store) == []
        assert _checked(store, "seat") is None
    finally:
        store.close()


def test_scan_without_a_fetch_reads_the_usage_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _paced_store(tmp_path, ["seat"])
    try:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=usage_body(weekly=33.0))

        class MockedClient(httpx.Client):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        # The usage service is mocked; the scanner builds its own client.
        monkeypatch.setattr(httpx, "Client", MockedClient)
        created = scan_window_usage(
            store, read_keychain=no_keychain, now=lambda: "2026-11-16T12:00:00Z"
        )
        assert len(created) == 1
        assert len(requests) == 1
        assert str(requests[0].url) == WINDOWS_URL
        assert requests[0].headers["Authorization"] == "Bearer token-seat"
        row = store.row("activity", created[0])
        assert row is not None
        assert row["payload"]["account_email"] == "seat@example.com"
        assert row["payload"]["windows"]["weekly"]["used_percent"] == 33.0
        assert "token-seat" not in json.dumps(row["payload"])
        assert _checked(store, "seat") == "2026-11-16T12:00:00Z"
    finally:
        store.close()


def test_scan_refuses_a_windows_adapter_without_poll_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The driver loader and ``_windows_spec`` both require ``poll_seconds``
    # today. A windows spec that still arrives without one stops the scan
    # before any account is paced or read.
    store = _paced_store(tmp_path, ["seat"])
    try:
        real_windows_spec = usage._windows_spec

        def without_poll_seconds(spec: Any) -> Any:
            return replace(real_windows_spec(spec), poll_seconds=None)

        monkeypatch.setattr(usage, "_windows_spec", without_poll_seconds)
        reads = _Reads()
        pace: dict[str, float] = {}
        with pytest.raises(StoreError) as info:
            scan_window_usage(
                store,
                fetch=reads.fetch,
                read_keychain=reads.read_keychain,
                pace=pace,
                clock=lambda: 100.0,
            )
        assert type(info.value) is StoreError
        assert str(info.value) == (
            f"{WINDOWS_DRIVER_ID} windows usage has no poll_seconds"
        )
        assert pace == {}
        assert reads.keychain == []
        assert reads.fetches == []
        assert _window_snapshots(store) == []
        assert _checked(store, "seat") is None
    finally:
        store.close()
