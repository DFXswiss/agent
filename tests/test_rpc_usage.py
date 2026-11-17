from __future__ import annotations

import base64
import itertools
import json
import queue
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli import rpc_usage
from agent_cli.drivers import DriverSpec, role_clear_env
from agent_cli.rpc_usage import (
    load_rpc_account,
    read_rpc_result,
    rpc_child_env,
    rpc_snapshot,
    scan_rpc_usage,
)
from agent_cli.store import Store, StoreError
from agent_cli.usage import AccountUsageError, AuthStale
from driver_fixtures import (
    RPC_ACCOUNT_FILE,
    RPC_DRIVER_ID,
    RPC_HOME_DIR,
    RPC_HOME_ENV,
    RPC_LABEL,
    RPC_METHOD,
    RPC_POLL_SECONDS,
    driver_document,
    driver_specs,
    rpc_driver,
    rpc_driver_spec,
    write_drivers,
)

ACCESS = "rpc-access-secret"
REFRESH = "rpc-refresh-secret"
CREDIT_ID = "credit-secret-id"
RESETS_AT = 1795019580
SPEC = rpc_driver_spec()
ADAPTER = rpc_usage.rpc_spec(SPEC)
CLEAR = role_clear_env({**driver_specs(), RPC_DRIVER_ID: SPEC})


def _window(used: Any, minutes: Any, resets_at: Any = RESETS_AT) -> dict[str, Any]:
    return {"usedPercent": used, "windowMinutes": minutes, "resetsAt": resets_at}


def limits(
    *,
    primary: Any = None,
    secondary: Any = None,
    plan: Any = "pro",
    reached: Any = "limit_reached",
) -> dict[str, Any]:
    return {
        "limitId": "seat",
        "primary": primary,
        "secondary": secondary,
        "credits": {"hasCredits": False, "balance": "0"},
        "plan": plan,
        "reachedType": reached,
    }


def result(**kwargs: Any) -> dict[str, Any]:
    """A placeholder answer of the configured method, with overrides."""
    current = limits(**{"primary": _window(100, 10080), **kwargs})
    return {
        "limits": current,
        "limitsById": {"seat": current},
        "resetCredits": {
            "availableCount": 1,
            "credits": [
                {
                    "id": CREDIT_ID,
                    "status": "available",
                    "title": "Full reset",
                    "description": "Resets every limit once.",
                }
            ],
        },
    }


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def snapshot(raw: dict[str, Any], email: str = "seat-1@example.com", now: str = "t") -> dict[str, Any]:
    return rpc_snapshot(RPC_DRIVER_ID, ADAPTER, raw, email, now)


# --- Fake server ---------------------------------------------------------------


class FakeStdout:
    """A pipe the reader thread blocks on until a line or the end arrives."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        self.closed = False

    def feed(self, line: str) -> None:
        self._lines.put(line if line.endswith("\n") else line + "\n")

    def end(self) -> None:
        self._lines.put("")

    def readline(self) -> str:
        return self._lines.get()

    def close(self) -> None:
        self.closed = True


Responder = Callable[[dict[str, Any]], list[str]]


class FakeStdin:
    """Hands every flushed request to the responder and feeds its lines."""

    def __init__(self, process: FakeProcess) -> None:
        self._process = process
        self._buffer = ""
        self.closed = False

    def write(self, text: str) -> int:
        self._buffer += text
        return len(text)

    def flush(self) -> None:
        *complete, self._buffer = self._buffer.split("\n")
        for line in complete:
            message = json.loads(line)
            self._process.received.append(message)
            for answer in self._process.respond(message):
                self._process.stdout.feed(answer)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, respond: Responder) -> None:
        self.respond = respond
        self.received: list[dict[str, Any]] = []
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.end()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def exit(self, code: int) -> None:
        """The process ends on its own: output closes, exit status is set."""
        self.returncode = code
        self.stdout.end()


class FakePopen:
    def __init__(self, respond: Responder) -> None:
        self.respond = respond
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.process: FakeProcess | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        self.process = FakeProcess(self.respond)
        return self.process


def rpc_server(
    answer: dict[str, Any] | None = None, *, before: tuple[str, ...] = ()
) -> FakePopen:
    """A fake that answers ``initialize`` and then the request with ``answer``.

    ``answer`` is the complete message for id 2 without the id; ``None``
    leaves the request unanswered.
    """

    def respond(message: dict[str, Any]) -> list[str]:
        if message.get("id") == 1:
            return [json.dumps({"id": 1, "result": {"userAgent": "tool-r/1.0"}})]
        if message.get("id") == 2:
            lines = list(before)
            if answer is not None:
                lines.append(json.dumps({"id": 2, **answer}))
            return lines
        return []

    return FakePopen(respond)


def read(config_dir: Path | str, popen: Any, **kwargs: Any) -> dict[str, Any]:
    return read_rpc_result(SPEC, str(config_dir), clear_env=CLEAR, popen=popen, **kwargs)


# --- Exchange with the server ---------------------------------------------------


def test_read_sends_the_handshake_and_returns_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOOL_R_API_KEY", "ambient-key")
    monkeypatch.setenv("TOOL_A_API_KEY", "other-driver-key")
    monkeypatch.setenv(RPC_HOME_ENV, "/ambient/tool-r")
    monkeypatch.setenv("TOOL_A_HOME", "/ambient/tool-a")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    monkeypatch.setenv("SSL_CERT_FILE", "/ambient/ca.pem")
    monkeypatch.setenv("AGENT_TEST_UNRELATED", "kept")
    config_dir = tmp_path / "seat"
    popen = rpc_server({"result": result()})

    assert read(config_dir, popen) == result()

    argv, kwargs = popen.calls[0]
    assert argv == ["tool-r", "serve-rpc"]
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs.get("shell") in (None, False)
    env = kwargs["env"]
    assert env[RPC_HOME_ENV] == str(config_dir)
    for name in ("TOOL_R_API_KEY", "TOOL_A_API_KEY", "TOOL_A_HOME", "HTTPS_PROXY", "SSL_CERT_FILE"):
        assert name not in env
    assert env["AGENT_TEST_UNRELATED"] == "kept"
    process = popen.process
    assert process is not None
    assert [message.get("method") for message in process.received] == [
        "initialize",
        "initialized",
        RPC_METHOD,
    ]
    initialize, initialized, request = process.received
    assert initialize["id"] == 1 and initialize["jsonrpc"] == "2.0"
    assert initialize["params"] == {"clientInfo": {"name": "agent-tests", "version": "1"}}
    assert "id" not in initialized
    assert request["id"] == 2 and request["params"] == {}
    assert process.terminated


def test_read_runs_the_configured_argv(tmp_path: Path) -> None:
    document = rpc_driver()
    document["usage"]["argv"] = ["/opt/tool-r/bin/tool-r", "serve-rpc", "--stdio"]
    from agent_cli.drivers import parse_driver_document

    driver = parse_driver_document({"drivers": {RPC_DRIVER_ID: document}}).drivers[RPC_DRIVER_ID]
    popen = rpc_server({"result": result()})
    read_rpc_result(driver, str(tmp_path / "seat"), clear_env=CLEAR, popen=popen)
    assert popen.calls[0][0] == ["/opt/tool-r/bin/tool-r", "serve-rpc", "--stdio"]


def test_default_home_leaves_the_variable_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(RPC_HOME_ENV, "/ambient/tool-r")
    default_dir = tmp_path / RPC_HOME_DIR.removeprefix("~/")
    default_dir.mkdir()
    popen = rpc_server({"result": result()})
    read(default_dir, popen)
    assert RPC_HOME_ENV not in popen.calls[0][1]["env"]
    assert rpc_child_env(SPEC, str(tmp_path / "named"), CLEAR, {})[RPC_HOME_ENV] == str(
        tmp_path / "named"
    )


def test_read_ignores_notifications_and_other_answers(tmp_path: Path) -> None:
    before = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "limits/updated",
                "params": {"limits": limits(primary=_window(1, 300))},
            }
        ),
        "",
        json.dumps({"id": 7, "result": {"limits": None}}),
        json.dumps({"id": "2", "error": {"code": 1, "message": "a string id is not ours"}}),
        json.dumps({"id": True, "error": {"code": 1, "message": "a boolean id is not ours"}}),
    )
    popen = rpc_server({"result": result()}, before=before)
    assert read(tmp_path / "seat", popen) == result()


def test_error_answer_is_reported(tmp_path: Path) -> None:
    popen = rpc_server(
        {"error": {"code": -32600, "message": "not logged in\n\x1b[31mrun tool-r login"}}
    )
    with pytest.raises(StoreError) as info:
        read(tmp_path / "seat", popen)
    assert str(info.value) == (
        f"{RPC_LABEL} {RPC_METHOD} failed: -32600 not logged in [31mrun tool-r login"
    )
    assert popen.process is not None and popen.process.terminated


def test_whole_exchange_times_out(tmp_path: Path) -> None:
    ticks = itertools.chain([0.0], itertools.repeat(31.0))
    popen = rpc_server(None)
    with pytest.raises(StoreError, match=f"{RPC_LABEL} timed out after 30 s"):
        read(tmp_path / "seat", popen, clock=lambda: next(ticks))
    assert popen.process is not None and popen.process.terminated


def test_early_exit_is_reported_with_the_exit_status(tmp_path: Path) -> None:
    def respond(message: dict[str, Any]) -> list[str]:
        if message.get("id") == 2:
            assert popen.process is not None
            popen.process.exit(1)
        return []

    popen = FakePopen(respond)
    with pytest.raises(StoreError, match=r"ended before answering \(exit 1\)"):
        read(tmp_path / "seat", popen)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not json " + ACCESS, "a line that is not JSON"),
        ("[2]", "message that is not an object"),
        (json.dumps({"id": 2, "result": []}), "result is not an object"),
    ],
)
def test_malformed_output_is_an_error(tmp_path: Path, line: str, message: str) -> None:
    def respond(request: dict[str, Any]) -> list[str]:
        return [line] if request.get("id") == 2 else []

    with pytest.raises(StoreError, match=message) as info:
        read(tmp_path / "seat", FakePopen(respond))
    assert ACCESS not in str(info.value)
    assert info.value.__context__ is None


def test_missing_program_is_an_error(tmp_path: Path) -> None:
    def missing(argv: list[str], **_kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    with pytest.raises(StoreError, match=f"{RPC_LABEL} program not found: tool-r") as info:
        read(tmp_path / "seat", missing)
    assert info.value.__context__ is None


def test_a_driver_without_an_rpc_adapter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="driver-a has no rpc usage section"):
        read_rpc_result(
            driver_specs()["driver-a"], str(tmp_path), clear_env=CLEAR, popen=rpc_server()
        )


# --- Identity -------------------------------------------------------------------


def _b64url(data: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode()


def id_token(claims: dict[str, Any]) -> str:
    """An unsigned JWT; the reader never checks the signature."""
    return f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(claims)}.c2lnbmF0dXJl"


def write_auth(
    config_dir: Path,
    *,
    email: str = "seat-1@example.com",
    tokens: Any = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    if tokens is None:
        tokens = {
            "id_token": id_token({"email": email, "plan": "pro"}),
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "account_id": "account-1",
        }
    (config_dir / RPC_ACCOUNT_FILE).write_text(
        json.dumps({"API_KEY": None, "tokens": tokens, "last_refresh": "2026-11-17T08:00:00Z"}),
        encoding="utf-8",
    )


def test_email_comes_from_the_unsigned_id_token(tmp_path: Path) -> None:
    for email in ("seat-1@example.com", "s@example.com", "seat-10@example.com"):
        write_auth(tmp_path / "seat", email=email)
        assert load_rpc_account(SPEC, str(tmp_path / "seat")) == email


def test_missing_login_is_stale(tmp_path: Path) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    with pytest.raises(AuthStale, match="account file not found"):
        load_rpc_account(SPEC, str(config_dir))
    (config_dir / RPC_ACCOUNT_FILE).write_text(
        json.dumps({"API_KEY": "sk-not-a-login", "tokens": None}), encoding="utf-8"
    )
    with pytest.raises(AuthStale, match="account file has no tokens; log in with tool-r") as info:
        load_rpc_account(SPEC, str(config_dir))
    assert "sk-not" not in str(info.value)
    write_auth(config_dir, tokens={"access_token": ACCESS})
    with pytest.raises(AuthStale, match="holds no tokens.id_token") as info:
        load_rpc_account(SPEC, str(config_dir))
    assert ACCESS not in str(info.value)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{" + ACCESS, "not valid JSON"),
        ("[]", "not an object"),
        (json.dumps({"tokens": []}), "tokens must be an object"),
        (json.dumps({"tokens": {"id_token": 5}}), "id_token must be a string"),
        (json.dumps({"tokens": {"id_token": "a." + ACCESS}}), "not a JWT"),
        (json.dumps({"tokens": {"id_token": "a.b+c.d"}}), "not a JWT"),
        (json.dumps({"tokens": {"id_token": "a.bm90LWpzb24.c"}}), "payload is not valid JSON"),
        (json.dumps({"tokens": {"id_token": "a." + _b64url([1]) + ".c"}}), "payload is not an object"),
        (json.dumps({"tokens": {"id_token": id_token({"sub": "x"})}}), "no email claim"),
        (json.dumps({"tokens": {"id_token": id_token({"email": " "})}}), "no email claim"),
    ],
)
def test_malformed_account_file_is_an_error_without_its_content(
    tmp_path: Path, raw: str, message: str
) -> None:
    config_dir = tmp_path / "seat"
    config_dir.mkdir()
    (config_dir / RPC_ACCOUNT_FILE).write_text(raw, encoding="utf-8")
    with pytest.raises(StoreError, match=message) as info:
        load_rpc_account(SPEC, str(config_dir))
    assert not isinstance(info.value, AuthStale)
    assert ACCESS not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


# --- Snapshot -------------------------------------------------------------------


def test_snapshot_maps_the_answer() -> None:
    mapped = snapshot(result(), now="2026-11-17T12:00:00Z")
    assert mapped == {
        "vendor": RPC_DRIVER_ID,
        "provider": RPC_DRIVER_ID,
        "account_email": "seat-1@example.com",
        "tier": "pro",
        "windows": {
            "five_hour": None,
            "weekly": {"used_percent": 100.0, "resets_at": _iso(RESETS_AT)},
            "weekly_models": [],
            "other": [],
        },
        "limit_reached": True,
        "reset_credits_available": 1,
        "fetched_at": "2026-11-17T12:00:00Z",
    }
    assert isinstance(mapped["windows"]["weekly"]["used_percent"], float)
    stored = json.dumps(mapped)
    assert CREDIT_ID not in stored
    assert "Full reset" not in stored


def test_snapshot_maps_windows_by_length() -> None:
    raw = result(primary=_window(12.5, 300), secondary=_window(40, 10080), reached=None)
    windows = snapshot(raw)["windows"]
    assert windows["five_hour"] == {"used_percent": 12.5, "resets_at": _iso(RESETS_AT)}
    assert windows["weekly"] == {"used_percent": 40.0, "resets_at": _iso(RESETS_AT)}
    assert windows["other"] == []

    raw = result(primary=_window(5, 1440, None), secondary=_window(6, 300))
    windows = snapshot(raw)["windows"]
    assert windows["five_hour"] == {"used_percent": 6.0, "resets_at": _iso(RESETS_AT)}
    assert windows["weekly"] is None
    assert windows["other"] == [{"window_minutes": 1440, "used_percent": 5.0, "resets_at": None}]

    raw = result(primary=_window(5, 300), secondary=_window(6, 300))
    windows = snapshot(raw)["windows"]
    assert windows["five_hour"]["used_percent"] == 5.0
    assert windows["other"] == [
        {"window_minutes": 300, "used_percent": 6.0, "resets_at": _iso(RESETS_AT)}
    ]


def test_snapshot_keeps_null_windows_null() -> None:
    raw: dict[str, Any] = {"limits": limits(primary=None, secondary=None, plan=None, reached=None)}
    mapped = snapshot(raw)
    assert mapped["windows"] == {
        "five_hour": None,
        "weekly": None,
        "weekly_models": [],
        "other": [],
    }
    assert mapped["tier"] is None
    # Not reported is null, never "no limit reached" or "0 free resets".
    assert mapped["limit_reached"] is None
    assert mapped["reset_credits_available"] is None
    raw["resetCredits"] = {"credits": []}
    assert snapshot(raw)["reset_credits_available"] is None
    raw["resetCredits"] = None
    assert snapshot(raw)["reset_credits_available"] is None
    raw["resetCredits"] = {"availableCount": 0}
    assert snapshot(raw)["reset_credits_available"] == 0
    raw["limits"]["plan"] = " "
    raw["limits"]["reachedType"] = False
    assert snapshot(raw)["tier"] is None
    assert snapshot(raw)["limit_reached"] is False
    del raw["limits"]["reachedType"]
    assert snapshot(raw)["limit_reached"] is None
    del raw["limits"]["primary"]
    assert snapshot(raw)["windows"]["five_hour"] is None


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, RESETS_AT), (29, RESETS_AT), (30, RESETS_AT + 60), (-31, RESETS_AT - 60)],
)
def test_reset_is_rounded_to_the_nearest_minute(offset: int, expected: int) -> None:
    raw = result(primary=_window(1, 10080, RESETS_AT + offset))
    assert snapshot(raw)["windows"]["weekly"]["resets_at"] == _iso(expected)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "result.limits must be an object"),
        ({"limits": []}, "result.limits must be an object"),
        ({"limits": limits(primary=[])}, "result.limits.primary must be an object or null"),
        ({"limits": limits(primary=_window("12", 300))}, "primary.usedPercent must be a number"),
        ({"limits": limits(primary=_window(True, 300))}, "primary.usedPercent must be a number"),
        ({"limits": limits(primary=_window(None, 300))}, "primary.usedPercent must be a number"),
        ({"limits": limits(secondary=_window(1, 0))}, "secondary.windowMinutes"),
        ({"limits": limits(secondary=_window(1, 300.0))}, "secondary.windowMinutes"),
        ({"limits": limits(primary=_window(1, 300, "soon"))}, "primary.resetsAt must be a number"),
        ({"limits": limits(primary=_window(1, 300, 10**20))}, "primary.resetsAt is not a valid time"),
        ({"limits": limits(plan=5)}, "result.limits.plan must be a string or null"),
        ({"limits": limits(), "resetCredits": []}, "result.resetCredits must be an object or null"),
        (
            {"limits": limits(), "resetCredits": {"availableCount": -1}},
            "availableCount must be a non-negative integer or null",
        ),
        (
            {"limits": limits(), "resetCredits": {"availableCount": True}},
            "availableCount must be a non-negative integer or null",
        ),
    ],
)
def test_snapshot_rejects_malformed_values(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(StoreError, match=message) as info:
        snapshot(raw)
    assert str(info.value).startswith(RPC_LABEL)


# --- Scan -----------------------------------------------------------------------


def _session(store: Store, session_id: str, provider: str) -> None:
    store.write(
        "session",
        "insert",
        session_id,
        {"id": session_id, "kind": "human", "status": "active", "runtime": {"provider": provider}},
    )


def _config(store: Store, accounts: dict[str, dict[str, str]], selected: list[str]) -> None:
    document = driver_document()
    document["drivers"][RPC_DRIVER_ID] = rpc_driver()
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


def _rpc_snapshots(store: Store) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows("activity")
        if row.get("type") == "usage.snapshot"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("vendor") == RPC_DRIVER_ID
    ]


class _Reads:
    """Answers per account directory and records which directories were read."""

    def __init__(self) -> None:
        self.dirs: list[str] = []
        self.drivers: list[str] = []
        self.used: dict[str, float] = {}
        self.fail: dict[str, Exception] = {}

    def __call__(self, driver: DriverSpec, config_dir: str) -> dict[str, Any]:
        self.dirs.append(config_dir)
        self.drivers.append(driver.id)
        if config_dir in self.fail:
            raise self.fail[config_dir]
        return result(primary=_window(self.used.get(config_dir, 100.0), 10080))


def _store(tmp_path: Path, names: list[str], *, logged_out: tuple[str, ...] = ()) -> Store:
    store = Store(tmp_path)
    _session(store, "s-other", "driver-a")
    _session(store, "s-rpc", RPC_DRIVER_ID)
    accounts = {}
    for name in names:
        config_dir = tmp_path / name
        if name in logged_out:
            config_dir.mkdir()
        else:
            write_auth(config_dir, email=f"{name.capitalize()}@Example.com")
        accounts[name] = {"provider": RPC_DRIVER_ID, "config_dir": str(config_dir)}
    _config(store, accounts, names)
    return store


def test_scan_records_changed_snapshots_only(tmp_path: Path) -> None:
    store = _store(tmp_path, ["seat"])
    try:
        reads = _Reads()
        seat = str(tmp_path / "seat")
        created = scan_rpc_usage(store, read=reads, now=lambda: "2026-11-17T12:00:00Z")
        assert len(created) == 1
        row = store.row("activity", created[0])
        assert row is not None and row["session_id"] == "s-rpc"
        payload = row["payload"]
        assert payload["provider"] == RPC_DRIVER_ID
        assert payload["account_email"] == "Seat@Example.com"
        assert payload["tier"] == "pro"
        assert payload["windows"]["weekly"]["used_percent"] == 100.0
        assert payload["limit_reached"] is True
        stored = json.dumps(payload)
        for secret in (ACCESS, REFRESH, CREDIT_ID):
            assert secret not in stored
        assert reads.dirs == [seat]
        assert reads.drivers == [RPC_DRIVER_ID]

        assert scan_rpc_usage(store, read=reads, now=lambda: "2026-11-17T12:15:00Z") == []
        reads.used[seat] = 99.0
        assert len(scan_rpc_usage(store, read=reads, now=lambda: "2026-11-17T12:30:00Z")) == 1
        assert len(_rpc_snapshots(store)) == 2
    finally:
        store.close()


def _check(store: Store, account_name: str) -> dict[str, str] | None:
    value = store.meta(f"usage.checked.{RPC_DRIVER_ID}.{account_name}")
    return None if value is None else json.loads(value)


def test_scan_records_the_check_time_of_every_successful_read(tmp_path: Path) -> None:
    store = _store(tmp_path, ["seat"])
    try:
        reads = _Reads()
        assert _check(store, "seat") is None
        assert len(scan_rpc_usage(store, read=reads, now=lambda: "2026-11-17T12:00:00Z")) == 1
        # The value names the case-folded login that was read; the key does not.
        assert _check(store, "seat") == {
            "checked_at": "2026-11-17T12:00:00Z",
            "identity": "seat@example.com",
        }

        # Unchanged usage stores no snapshot, but the check still counts.
        assert scan_rpc_usage(store, read=reads, now=lambda: "2026-11-17T12:15:00Z") == []
        assert _check(store, "seat") == {
            "checked_at": "2026-11-17T12:15:00Z",
            "identity": "seat@example.com",
        }
        assert [row["payload"]["fetched_at"] for row in _rpc_snapshots(store)] == [
            "2026-11-17T12:00:00Z"
        ]
        assert not any("@" in key for key in store.meta_prefix("usage.checked."))
    finally:
        store.close()


def test_scan_records_no_check_time_for_skipped_failed_or_malformed_reads(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, ["gone", "broken", "odd", "good"], logged_out=("gone",))
    try:
        reads = _Reads()
        reads.fail[str(tmp_path / "broken")] = StoreError(f"{RPC_LABEL} timed out after 30 s")
        # A malformed answer fails in the mapping, after the read itself.
        reads.used[str(tmp_path / "odd")] = "much"
        skipped: list[str] = []
        with pytest.raises(AccountUsageError) as info:
            scan_rpc_usage(
                store, read=reads, skipped=skipped, now=lambda: "2026-11-17T12:00:00Z"
            )
        assert [line.split(":", 1)[0] for line in skipped] == ["gone"]
        assert [line.split(":", 1)[0] for line in info.value.errors] == ["broken", "odd"]
        assert _check(store, "gone") is None
        assert _check(store, "broken") is None
        assert _check(store, "odd") is None
        assert _check(store, "good") == {
            "checked_at": "2026-11-17T12:00:00Z",
            "identity": "good@example.com",
        }
    finally:
        store.close()


def test_scan_skips_missing_logins_quietly_and_reports_errors(tmp_path: Path) -> None:
    store = _store(tmp_path, ["gone", "broken", "good"], logged_out=("gone",))
    try:
        reads = _Reads()
        reads.fail[str(tmp_path / "broken")] = StoreError(f"{RPC_LABEL} timed out after 30 s")
        skipped: list[str] = []
        with pytest.raises(AccountUsageError) as info:
            scan_rpc_usage(store, read=reads, skipped=skipped)
        assert info.value.errors == (f"broken: {RPC_LABEL} timed out after 30 s",)
        assert len(info.value.activity_ids) == 1
        assert [line.split(":", 1)[0] for line in skipped] == ["gone"]
        assert reads.dirs == [str(tmp_path / "broken"), str(tmp_path / "good")]
        assert [row["payload"]["account_email"] for row in _rpc_snapshots(store)] == [
            "Good@Example.com"
        ]
    finally:
        store.close()


def test_scan_reads_only_rpc_accounts(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        _session(store, "s-rpc", RPC_DRIVER_ID)
        write_auth(tmp_path / "seat")
        router = tmp_path / "router"
        router.mkdir()
        _config(
            store,
            {
                "seat": {"provider": RPC_DRIVER_ID, "config_dir": str(tmp_path / "seat")},
                "router": {"provider": "driver-d", "config_dir": str(router)},
            },
            ["router", "seat"],
        )
        reads = _Reads()
        assert len(scan_rpc_usage(store, read=reads)) == 1
        assert reads.dirs == [str(tmp_path / "seat")]
    finally:
        store.close()


def test_unconfigured_device_reads_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        reads = _Reads()
        _config(store, {}, [])
        assert scan_rpc_usage(store, read=reads) == []
        assert reads.dirs == []
    finally:
        store.close()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pace_reads_an_account_once_per_interval(tmp_path: Path) -> None:
    store = _store(tmp_path, ["seat", "gone"], logged_out=("gone",))
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        scan_rpc_usage(store, read=reads, pace=pace, clock=clock)
        assert reads.dirs == [str(tmp_path / "seat")]
        assert pace == {
            "seat": 1000.0 + RPC_POLL_SECONDS,
            "gone": 1000.0 + RPC_POLL_SECONDS,
        }

        clock.now += RPC_POLL_SECONDS - 1
        skipped: list[str] = []
        scan_rpc_usage(store, read=reads, pace=pace, clock=clock, skipped=skipped)
        assert len(reads.dirs) == 1 and skipped == []

        clock.now = 1000.0 + RPC_POLL_SECONDS
        scan_rpc_usage(store, read=reads, pace=pace, clock=clock, skipped=skipped)
        assert len(reads.dirs) == 2
        assert [line.split(":", 1)[0] for line in skipped] == ["gone"]
    finally:
        store.close()


def test_errors_are_paced_and_without_pace_every_call_reads(tmp_path: Path) -> None:
    store = _store(tmp_path, ["seat"])
    try:
        reads, clock, pace = _Reads(), _Clock(), {}
        reads.fail[str(tmp_path / "seat")] = StoreError(f"{RPC_LABEL} program not found: tool-r")
        with pytest.raises(AccountUsageError):
            scan_rpc_usage(store, read=reads, pace=pace, clock=clock)
        clock.now += 60
        assert scan_rpc_usage(store, read=reads, pace=pace, clock=clock) == []
        assert len(reads.dirs) == 1

        del reads.fail[str(tmp_path / "seat")]
        scan_rpc_usage(store, read=reads, clock=clock)
        scan_rpc_usage(store, read=reads, clock=clock)
        assert len(reads.dirs) == 3
    finally:
        store.close()


def test_scan_reads_through_the_local_server_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, ["seat"])
    try:
        popen = rpc_server({"result": result()})
        clears: list[tuple[str, ...]] = []
        original = rpc_usage.read_rpc_result

        def through_fake(driver: DriverSpec, config_dir: str, *, clear_env: Any) -> dict[str, Any]:
            clears.append(tuple(clear_env))
            return original(driver, config_dir, clear_env=clear_env, popen=popen)

        # The scan resolves the reader at call time; route it to the fake.
        monkeypatch.setattr(rpc_usage, "read_rpc_result", through_fake)
        assert len(scan_rpc_usage(store)) == 1
        assert popen.calls[0][1]["env"][RPC_HOME_ENV] == str(tmp_path / "seat")
        # The server loses every configured credential and home variable.
        assert {"TOOL_R_API_KEY", "TOOL_A_API_KEY", RPC_HOME_ENV, "TOOL_A_HOME"} <= set(clears[0])
    finally:
        store.close()
