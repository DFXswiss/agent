from __future__ import annotations

import hashlib
import json
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_cli import usage
from agent_cli.store import Store, StoreError
from agent_cli.usage import (
    CLAUDE_USAGE_URL,
    AuthStale,
    ClaudeUsageError,
    _fetch_claude_usage,
    claude_keychain_service,
    claude_keychain_user,
    claude_snapshot,
    load_claude_account_email,
    load_claude_credentials,
    read_claude_keychain,
    scan_claude_usage,
    scan_openrouter_usage,
)

FUTURE_MS = 4_102_444_800_000  # 2100-01-01
PAST_MS = 1_000_000_000_000  # 2001-09-09
TOKEN = "claude-test-token"


def no_keychain(_service: str, _user: str) -> str | None:
    return None


def credentials(
    *,
    token: Any = TOKEN,
    expires_at: Any = FUTURE_MS,
    subscription: Any = "max",
    tier: Any = "default_claude_max_20x",
) -> dict[str, Any]:
    oauth: dict[str, Any] = {
        "accessToken": token,
        "refreshToken": "must-not-be-used",
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": subscription,
        "rateLimitTier": tier,
    }
    return {"claudeAiOauth": oauth}


def write_login(
    config_dir: Path,
    *,
    email: str = "seat-1@example.com",
    creds: dict[str, Any] | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email, "organizationUuid": "x"}}),
        encoding="utf-8",
    )
    (config_dir / ".credentials.json").write_text(
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
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": True,
            },
        ],
        "extra_usage": {"is_enabled": False},
        "spend": None,
    }


# --- Keychain naming ---------------------------------------------------------


def test_keychain_service_for_default_and_named_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_keychain_service(str(tmp_path / ".claude")) == "Claude Code-credentials"

    named = "/operator/claude-seat-1"
    expected = hashlib.sha256(named.encode("utf-8")).hexdigest()[:8]
    assert claude_keychain_service(named) == f"Claude Code-credentials-{expected}"

    composed = "/operator/café"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    assert claude_keychain_service(decomposed) == claude_keychain_service(composed)
    digest = hashlib.sha256(composed.encode("utf-8")).hexdigest()[:8]
    assert claude_keychain_service(composed) == f"Claude Code-credentials-{digest}"


def test_keychain_user_uses_plain_user_name_only() -> None:
    assert claude_keychain_user({"USER": "operator.name-1_x"}) == "operator.name-1_x"
    assert claude_keychain_user({"USER": "seat@example.com"}) == "claude-code-user"
    assert claude_keychain_user({"USER": ""}) == "claude-code-user"
    assert claude_keychain_user({}) == "claude-code-user"


# --- Keychain read -----------------------------------------------------------


def test_read_keychain_runs_security_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    result = {"code": 0, "out": json.dumps(credentials()) + "\n"}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, result["code"], result["out"], "")

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    assert read_claude_keychain("Claude Code-credentials", "operator") == json.dumps(credentials())
    argv, kwargs = calls[0]
    assert argv == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "Claude Code-credentials",
        "-a",
        "operator",
        "-w",
    ]
    assert kwargs.get("shell") in (None, False)
    assert kwargs["timeout"] > 0
    assert kwargs["check"] is False

    result.update(code=44, out="")
    assert read_claude_keychain("Claude Code-credentials", "operator") is None

    result.update(code=51, out=TOKEN)
    with pytest.raises(StoreError, match=r"keychain read failed \(exit 51\)") as info:
        read_claude_keychain("Claude Code-credentials", "operator")
    assert TOKEN not in str(info.value)


def test_read_keychain_timeout_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(usage.subprocess, "run", slow)
    with pytest.raises(StoreError, match="timed out"):
        read_claude_keychain("Claude Code-credentials", "operator")


# --- Credentials -------------------------------------------------------------


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

    assert load_claude_credentials(str(config_dir), read_keychain=reader) == (
        "keychain-token",
        "max_20x",
    )
    assert asked == [(claude_keychain_service(str(config_dir)), "operator")]


def test_credentials_fall_back_to_the_file_when_the_keychain_has_no_item(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(subscription="pro", tier="default_claude_pro"))
    assert load_claude_credentials(str(config_dir), read_keychain=no_keychain) == (
        TOKEN,
        "pro",
    )


def test_credentials_off_macos_read_only_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_service: str, _user: str) -> str | None:
        raise AssertionError("the keychain is macOS only")

    monkeypatch.setattr(usage, "_keychain_supported", lambda: False)
    monkeypatch.setattr(usage, "read_claude_keychain", forbidden)
    config_dir = tmp_path / "seat"
    write_login(config_dir)
    assert load_claude_credentials(str(config_dir)) == (TOKEN, "max_20x")


def test_credentials_keychain_failure_is_reported_without_a_file(tmp_path: Path) -> None:
    def failing(_service: str, _user: str) -> str | None:
        raise StoreError("claude keychain read failed (exit 51)")

    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(StoreError, match="exit 51") as info:
        load_claude_credentials(str(config_dir), read_keychain=failing)
    assert not isinstance(info.value, AuthStale)

    write_login(config_dir)
    assert load_claude_credentials(str(config_dir), read_keychain=failing)[0] == TOKEN


def test_credentials_missing_login_and_expired_token_are_stale(tmp_path: Path) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(AuthStale, match="login not found"):
        load_claude_credentials(str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds={"mcpOAuth": {}})
    with pytest.raises(AuthStale, match="no Claude login"):
        load_claude_credentials(str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds={"claudeAiOauth": {"expiresAt": FUTURE_MS}})
    with pytest.raises(AuthStale, match="no access token"):
        load_claude_credentials(str(config_dir), read_keychain=no_keychain)

    write_login(config_dir, creds=credentials(expires_at=PAST_MS))
    with pytest.raises(AuthStale, match="expired"):
        load_claude_credentials(str(config_dir), read_keychain=no_keychain)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{" + TOKEN, "not valid JSON"),
        ("[]", "not an object"),
        (json.dumps({"claudeAiOauth": []}), "claudeAiOauth must be an object"),
        (json.dumps(credentials(token=7)), "accessToken must be a non-empty string"),
        (json.dumps(credentials(token="")), "accessToken must be a non-empty string"),
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
    (config_dir / ".credentials.json").write_text(raw, encoding="utf-8")
    with pytest.raises(StoreError, match=message) as info:
        load_claude_credentials(str(config_dir), read_keychain=no_keychain)
    assert not isinstance(info.value, AuthStale)
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize(
    ("subscription", "tier", "expected"),
    [
        ("max", "default_claude_max_20x", "max_20x"),
        ("max", "default_claude_max_5x", "max_5x"),
        ("max", None, "max"),
        ("pro", "default_claude_max_20x", "pro"),
        ("team", "something_else", "team"),
        (None, "default_claude_max_20x", None),
        ("", None, None),
    ],
)
def test_tier_adds_the_max_multiplier(
    tmp_path: Path, subscription: Any, tier: Any, expected: str | None
) -> None:
    config_dir = tmp_path / "seat"
    write_login(config_dir, creds=credentials(subscription=subscription, tier=tier))
    assert load_claude_credentials(str(config_dir), read_keychain=no_keychain)[1] == expected


# --- Identity ----------------------------------------------------------------


def test_account_email_location_for_default_and_named_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    default_dir = tmp_path / ".claude"
    default_dir.mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "default@example.com"}}),
        encoding="utf-8",
    )
    (default_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "wrong@example.com"}}),
        encoding="utf-8",
    )
    assert load_claude_account_email(str(default_dir)) == "default@example.com"

    named = tmp_path / "seat"
    write_login(named, email="seat-1@example.com")
    assert load_claude_account_email(str(named)) == "seat-1@example.com"


def test_account_email_errors(tmp_path: Path) -> None:
    named = tmp_path / "seat"
    named.mkdir()
    with pytest.raises(AuthStale, match="not found"):
        load_claude_account_email(str(named))
    path = named / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3}), encoding="utf-8")
    with pytest.raises(AuthStale, match="no oauthAccount"):
        load_claude_account_email(str(named))
    path.write_text("{", encoding="utf-8")
    with pytest.raises(StoreError, match="not valid JSON"):
        load_claude_account_email(str(named))
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": ""}}), encoding="utf-8")
    with pytest.raises(StoreError, match="emailAddress"):
        load_claude_account_email(str(named))
    path.write_text(json.dumps({"oauthAccount": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must be an object"):
        load_claude_account_email(str(named))


# --- Snapshot mapping --------------------------------------------------------


def test_snapshot_maps_windows_and_keeps_missing_windows_null() -> None:
    snapshot = claude_snapshot(
        "seat-1@example.com", "max_20x", usage_body(), "2026-11-16T12:00:00Z"
    )
    assert snapshot == {
        "vendor": "claude",
        "provider": "claude",
        "account_email": "seat-1@example.com",
        "tier": "max_20x",
        "windows": {
            "five_hour": None,
            "weekly": {"used_percent": 14.0, "resets_at": "2026-11-23T10:00:00+00:00"},
            "weekly_models": [
                {
                    "model": "Fable",
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
    snapshot = claude_snapshot("seat-1@example.com", None, body, "t")
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
    windows = claude_snapshot("seat-1@example.com", "pro", body, "t")["windows"]
    assert windows == {"five_hour": None, "weekly": None, "weekly_models": []}

    body = {"five_hour": None, "seven_day": None, "limits": usage_body()["limits"]}
    windows = claude_snapshot("seat-1@example.com", "pro", body, "t")["windows"]
    assert windows["weekly"] == {
        "used_percent": 14.0,
        "resets_at": "2026-11-23T10:00:00+00:00",
    }

    body = {"limits": [{"kind": "weekly_scoped", "percent": 5, "resets_at": None, "scope": None}]}
    windows = claude_snapshot("seat-1@example.com", "pro", body, "t")["windows"]
    assert windows["weekly_models"] == [{"model": None, "used_percent": 5.0, "resets_at": None}]


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
        claude_snapshot("seat-1@example.com", None, body, "t")


# --- Request -----------------------------------------------------------------


def test_fetch_sends_the_oauth_usage_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=usage_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert _fetch_claude_usage(TOKEN, client=client) == usage_body()
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == CLAUDE_USAGE_URL
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == "dfx-agent"


def test_owned_claude_client_ignores_proxy_environment_and_redirects(
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
    assert _fetch_claude_usage(TOKEN) == usage_body()
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert seen["timeout"] == 8.0


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401, json={"error": TOKEN}), httpx.Response(200, text=TOKEN), httpx.Response(200, json=[])],
)
def test_fetch_errors_hide_the_token(response: httpx.Response) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client:
        with pytest.raises(StoreError, match="Claude usage") as info:
            _fetch_claude_usage(TOKEN, client=client)
    assert TOKEN not in str(info.value)


def test_fetch_network_error_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StoreError, match="Claude usage request failed") as info:
            _fetch_claude_usage(TOKEN, client=client)
    assert TOKEN not in str(info.value)


# --- Scan --------------------------------------------------------------------


def _session(store: Store, session_id: str, provider: str) -> None:
    store.write(
        "session",
        "insert",
        session_id,
        {"id": session_id, "kind": "human", "status": "active", "runtime": {"provider": provider}},
    )


def _config(store: Store, accounts: dict[str, dict[str, str]], selected: list[str]) -> None:
    (store.home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "claude", "second": "codex"},
                "accounts": accounts,
                "usage_accounts": selected,
            }
        ),
        encoding="utf-8",
    )


def _claude_snapshots(store: Store) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows("activity")
        if row.get("type") == "usage.snapshot"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("vendor") == "claude"
    ]


def test_scan_records_changed_snapshots_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-grok", "grok")
        _session(store, "s-claude", "claude")
        seat = tmp_path / "seat"
        write_login(seat, email="Seat-1@Example.com")
        _config(store, {"seat": {"provider": "claude", "config_dir": str(seat)}}, ["seat"])
        weekly = {"value": 14.0}
        tokens: list[str] = []

        def fetch(token: str) -> dict[str, Any]:
            tokens.append(token)
            return usage_body(weekly=weekly["value"])

        created = scan_claude_usage(
            store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:00:00Z"
        )
        assert len(created) == 1
        row = store.row("activity", created[0])
        assert row is not None and row["session_id"] == "s-claude"
        assert row["payload"]["account_email"] == "Seat-1@Example.com"
        assert row["payload"]["tier"] == "max_20x"
        assert row["payload"]["windows"]["weekly"]["used_percent"] == 14.0
        assert TOKEN not in json.dumps(row["payload"])
        assert "must-not-be-used" not in json.dumps(row["payload"])
        assert tokens == [TOKEN]

        assert scan_claude_usage(
            store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:01:00Z"
        ) == []
        weekly["value"] = 15.0
        assert len(
            scan_claude_usage(
                store, fetch=fetch, read_keychain=no_keychain, now=lambda: "2026-11-16T12:02:00Z"
            )
        ) == 1
        assert len(_claude_snapshots(store)) == 2
    finally:
        store.close()


def test_scan_skips_stale_logins_quietly_and_reports_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-claude", "claude")
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
                "stale": {"provider": "claude", "config_dir": str(stale)},
                "missing": {"provider": "claude", "config_dir": str(missing)},
                "broken": {"provider": "claude", "config_dir": str(broken)},
                "good": {"provider": "claude", "config_dir": str(good)},
            },
            ["stale", "missing", "broken", "good"],
        )
        calls: list[str] = []

        def fetch(token: str) -> dict[str, Any]:
            calls.append(token)
            if len(calls) == 1:
                raise StoreError("Claude usage returned HTTP 429")
            return usage_body()

        skipped: list[str] = []
        with pytest.raises(ClaudeUsageError) as info:
            scan_claude_usage(store, fetch=fetch, read_keychain=no_keychain, skipped=skipped)
        assert info.value.errors == ("broken: Claude usage returned HTTP 429",)
        assert len(info.value.activity_ids) == 1
        assert [line.split(":", 1)[0] for line in skipped] == ["stale", "missing"]
        assert all(TOKEN not in line for line in skipped)
        assert len(_claude_snapshots(store)) == 1
    finally:
        store.close()


def test_each_scanner_reads_only_its_own_provider(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-claude", "claude")
        seat = tmp_path / "seat"
        write_login(seat)
        router = tmp_path / "router"
        router.mkdir()
        (router / "openrouter-api-key").write_text("router-secret", encoding="utf-8")
        _config(
            store,
            {
                "seat": {"provider": "claude", "config_dir": str(seat)},
                "router": {"provider": "openrouter", "config_dir": str(router)},
            },
            ["seat", "router"],
        )
        claude_tokens: list[str] = []
        router_tokens: list[str] = []

        def claude_fetch(token: str) -> dict[str, Any]:
            claude_tokens.append(token)
            return usage_body()

        def router_fetch(token: str) -> dict[str, Any]:
            router_tokens.append(token)
            return {"data": {"usage": 1, "usage_daily": 0, "usage_weekly": 0, "usage_monthly": 1}}

        scan_claude_usage(store, fetch=claude_fetch, read_keychain=no_keychain)
        scan_openrouter_usage(store, fetch=router_fetch)
        assert claude_tokens == [TOKEN]
        assert router_tokens == ["router-secret"]
    finally:
        store.close()
