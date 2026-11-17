from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli import token_keepalive
from agent_cli.ai_accounts import AIAccount, AIAccounts
from agent_cli.drivers import DriverConfiguration, DriverSpec, parse_driver_document
from agent_cli.main import _token_keepalive_tick, main
from agent_cli.store import StoreError
from agent_cli.token_keepalive import (
    KEEPALIVE_CHECK_SECONDS,
    KeepaliveResult,
    describe,
    driver_env,
    keep_alive,
    keepalive_due,
    token_expiry,
)
from agent_cli.usage import AccountUsageError, keychain_service_name
from driver_fixtures import (
    WINDOWS_CREDENTIAL_FILE,
    WINDOWS_DRIVER_ID,
    WINDOWS_OBJECT,
    WINDOWS_TOKEN_FIELD,
    driver_document,
    windows_driver,
    write_drivers,
)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
MARGIN = 30 * 60
TOKEN = "login-test-token"
REFRESH = "must-not-be-used"
HOME_ENV = "LOGIN_CONFIG_DIR"
KEEPALIVE_ARGV = ["login-tool", "--print", "Reply with the single word: ok", "--model", "small-model"]
NO_LOGIN = "no login; run agent account-login"


def opt_bin(name: str) -> str:
    return f"/opt/bin/{name}"


def no_keychain(_service: str, _user: str) -> str | None:
    return None


def document(*, keepalive: bool = True, keychain: bool = True) -> dict[str, Any]:
    """The placeholder drivers plus the windows driver ``driver-w``."""
    driver = windows_driver()
    if keepalive:
        driver["usage"]["keepalive"] = {"argv": list(KEEPALIVE_ARGV)}
    if not keychain:
        del driver["usage"]["credential"]["keychain"]
    doc = driver_document()
    doc["drivers"][WINDOWS_DRIVER_ID] = driver
    return doc


def configuration(**changes: Any) -> DriverConfiguration:
    return parse_driver_document(document(**changes))


def spec(**changes: Any) -> DriverSpec:
    return configuration(**changes).drivers[WINDOWS_DRIVER_ID]


def credentials(expires_at: Any) -> dict[str, Any]:
    return {
        WINDOWS_OBJECT: {
            WINDOWS_TOKEN_FIELD: TOKEN,
            "refreshToken": REFRESH,
            "expiresAt": expires_at,
            "subscriptionType": "max",
        }
    }


def write_credentials(config_dir: Path, expires_seconds: float) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / WINDOWS_CREDENTIAL_FILE).write_text(
        json.dumps(credentials(int(expires_seconds * 1000))), encoding="utf-8"
    )


def accounts_for(
    dirs: dict[str, Path | str],
    *,
    seconds: int | None = MARGIN,
    extra: dict[str, AIAccount] | None = None,
    **changes: Any,
) -> AIAccounts:
    accounts = {
        name: AIAccount(name, WINDOWS_DRIVER_ID, str(path)) for name, path in dirs.items()
    }
    if extra:
        accounts.update(extra)
    return AIAccounts(
        accounts=accounts,
        roles={},
        sessions={},
        _models=None,
        _implementer=None,
        usage_accounts=tuple(accounts),
        driver_configuration=configuration(**changes),
        token_keepalive_seconds=seconds,
    )


class FakeRun:
    """Stands in for ``subprocess.run`` of the keep-alive command."""

    def __init__(self, renew: dict[str, float] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.cwds: list[Path] = []
        self.renew = renew or {}
        self.returncode = 0
        self.stderr = ""
        self.raises: dict[str, BaseException] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir()
        self.cwds.append(cwd)
        config_dir = kwargs["env"][HOME_ENV]
        if config_dir in self.raises:
            raise self.raises[config_dir]
        if config_dir in self.renew:
            write_credentials(Path(config_dir), self.renew[config_dir])
        return subprocess.CompletedProcess(argv, self.returncode, "MODEL-OUTPUT", self.stderr)


def forbidden_run(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the keep-alive command must not run")


# --- token_expiry --------------------------------------------------------------


@pytest.mark.no_pg
def test_token_expiry_prefers_the_keychain_item_of_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USER", "operator")
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + 100)
    asked: list[tuple[str, str]] = []

    def reader(service: str, user: str) -> str | None:
        asked.append((service, user))
        return json.dumps(credentials(int((NOW + 999) * 1000)))

    driver = spec()
    assert token_expiry(driver, str(seat), read_keychain=reader) == NOW + 999
    assert asked == [(keychain_service_name(driver, str(seat)), "operator")]


@pytest.mark.no_pg
def test_token_expiry_falls_back_to_the_file(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + 100)
    assert token_expiry(spec(), str(seat), read_keychain=no_keychain) == NOW + 100


@pytest.mark.no_pg
def test_token_expiry_keychain_error_falls_back_to_the_file(tmp_path: Path) -> None:
    def failing(_service: str, _user: str) -> str | None:
        raise StoreError("login keychain read failed (exit 51)")

    seat = tmp_path / "seat"
    seat.mkdir()
    with pytest.raises(StoreError, match="exit 51"):
        token_expiry(spec(), str(seat), read_keychain=failing)
    write_credentials(seat, NOW + 100)
    assert token_expiry(spec(), str(seat), read_keychain=failing) == NOW + 100


@pytest.mark.no_pg
def test_token_expiry_without_a_configured_keychain_reads_only_the_file(
    tmp_path: Path,
) -> None:
    def reader(_service: str, _user: str) -> str | None:
        raise AssertionError("no keychain is configured")

    seat = tmp_path / "seat"
    write_credentials(seat, NOW + 100)
    assert token_expiry(spec(keychain=False), str(seat), read_keychain=reader) == NOW + 100


@pytest.mark.no_pg
def test_token_expiry_without_login_is_none(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    assert token_expiry(spec(), str(seat), read_keychain=no_keychain) is None
    (seat / WINDOWS_CREDENTIAL_FILE).write_text(json.dumps({"other": {}}), encoding="utf-8")
    assert token_expiry(spec(), str(seat), read_keychain=no_keychain) is None


@pytest.mark.no_pg
def test_token_expiry_returns_a_past_expiry(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW - 3600)
    assert token_expiry(spec(), str(seat), read_keychain=no_keychain) == NOW - 3600


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{" + TOKEN, "not valid JSON"),
        ("[]", "not an object"),
        (json.dumps({WINDOWS_OBJECT: []}), f"{WINDOWS_OBJECT} must be an object"),
        (json.dumps(credentials(None)), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials("soon")), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(True)), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(10**400)), "expiresAt must be epoch milliseconds"),
        (json.dumps(credentials(10**30)), "expiresAt must be epoch milliseconds"),
    ],
)
def test_token_expiry_malformed_is_an_error_without_the_token(
    tmp_path: Path, raw: str, message: str
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / WINDOWS_CREDENTIAL_FILE).write_text(raw, encoding="utf-8")
    with pytest.raises(StoreError, match=message) as info:
        token_expiry(spec(), str(seat), read_keychain=no_keychain)
    assert TOKEN not in str(info.value)
    assert info.value.__context__ is None


# --- driver_env ----------------------------------------------------------------

AMBIENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/operator",
    HOME_ENV: "/elsewhere",
    "TOOL_A_HOME": "/elsewhere-a",
    "TOOL_A_API_KEY": "sk-ambient",
    "TOOL_C_TOKEN": "ambient-token",
    "TOOL_D_API_KEY": "ambient-key",
    "BROWSER": "/usr/bin/browser",
    "HTTPS_PROXY": "http://127.0.0.1:8123",
    "SSL_CERT_FILE": "/tmp/ca.pem",
}


@pytest.mark.no_pg
def test_driver_env_for_the_default_profile_sets_no_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = configuration()
    env = driver_env(
        config.drivers[WINDOWS_DRIVER_ID], config.drivers, str(tmp_path / ".login-tool"), AMBIENT
    )
    assert HOME_ENV not in env
    assert env["PATH"] == "/usr/bin"


@pytest.mark.no_pg
def test_driver_env_for_a_named_profile_sets_the_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    seat = str(tmp_path / "seat")
    base = dict(AMBIENT)
    config = configuration()
    env = driver_env(config.drivers[WINDOWS_DRIVER_ID], config.drivers, seat, base)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/operator", HOME_ENV: seat}
    assert base == AMBIENT


@pytest.mark.no_pg
def test_driver_env_requires_home_env() -> None:
    with pytest.raises(StoreError, match="has no home_env"):
        driver_env(DriverSpec("bare"), {}, "/operator/seat", AMBIENT)


# --- keep_alive ----------------------------------------------------------------


@pytest.mark.no_pg
def test_keep_alive_off_does_no_work(tmp_path: Path) -> None:
    def reader(_service: str, _user: str) -> str | None:
        raise AssertionError("no keychain read when off")

    accounts = accounts_for({"seat": tmp_path / "seat"}, seconds=None)
    assert keep_alive(accounts, run=forbidden_run, read_keychain=reader, which=opt_bin) == []


@pytest.mark.no_pg
def test_keep_alive_skips_the_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    default_dir = tmp_path / ".login-tool"
    write_credentials(default_dir, NOW - 10)
    results = keep_alive(
        accounts_for({"main": default_dir}),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert results == [
        KeepaliveResult("main", "skipped", None, "default profile is not kept alive")
    ]


@pytest.mark.no_pg
def test_keep_alive_skips_a_driver_without_keepalive_section(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW - 10)
    results = keep_alive(
        accounts_for({"seat": seat}, keepalive=False),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert results == [
        KeepaliveResult(
            "seat",
            "skipped",
            None,
            "driver driver-w has no usage.keepalive in drivers.json; it cannot be kept alive",
        )
    ]
    assert describe(results[0]).startswith("token.keepalive skipped seat: driver driver-w")


@pytest.mark.no_pg
def test_keep_alive_fresh_token_runs_nothing(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + MARGIN + 60)
    results = keep_alive(
        accounts_for({"seat": seat}),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert results == [KeepaliveResult("seat", "fresh", NOW + MARGIN + 60, "")]
    assert describe(results[0]) == "token.keepalive fresh seat until 2027-01-15T08:31Z"


@pytest.mark.no_pg
@pytest.mark.parametrize("remaining", [MARGIN, MARGIN - 1, 0, -3600])
def test_keep_alive_near_expiry_and_expired_run_the_command_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining: float
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + remaining)
    fake = FakeRun({str(seat): NOW + 8 * 3600})
    environ = {
        "PATH": "/usr/bin",
        "TOOL_A_API_KEY": "sk-ambient",
        "TOOL_C_TOKEN": "ambient-token",
        "BROWSER": "/usr/bin/browser",
        HOME_ENV: "/elsewhere",
    }
    results = keep_alive(
        accounts_for({"seat": seat}),
        run=fake,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
        environ=environ,
    )
    assert results == [KeepaliveResult("seat", "refreshed", NOW + 8 * 3600, "")]
    assert len(fake.calls) == 1
    argv, kwargs = fake.calls[0]
    assert argv == ["/opt/bin/login-tool", *KEEPALIVE_ARGV[1:]]
    assert kwargs["env"] == {"PATH": "/usr/bin", HOME_ENV: str(seat)}
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 180
    assert kwargs.get("shell") in (None, False)
    # The working directory is a fresh temporary directory, removed afterwards.
    assert fake.cwds[0] != seat and not fake.cwds[0].exists()
    line = describe(results[0])
    assert line == "token.keepalive refreshed seat until 2027-01-15T16:00Z"
    assert TOKEN not in line and REFRESH not in line


@pytest.mark.no_pg
def test_keep_alive_refresh_that_does_not_extend_is_an_error(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + 60)
    fake = FakeRun()
    fake.returncode = 1
    fake.stderr = "boom\n" + "word " * 100 + "\n" + "MODEL-OUTPUT-NEVER"
    results = keep_alive(
        accounts_for({"seat": seat}),
        run=fake,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert len(fake.calls) == 1
    (result,) = results
    assert result.action == "error"
    assert result.expires_at == NOW + 60
    assert result.message.startswith("token not renewed (exit 1): boom word word")
    assert len(result.message) <= len("token not renewed (exit 1): ") + 200
    assert "MODEL-OUTPUT" not in result.message
    assert "\n" not in result.message
    assert describe(result).startswith("token.keepalive error: seat: token not renewed")


@pytest.mark.no_pg
def test_keep_alive_without_login_is_skipped(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    results = keep_alive(
        accounts_for({"seat": seat}),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert results == [KeepaliveResult("seat", "skipped", None, NO_LOGIN)]
    assert describe(results[0]) == f"token.keepalive skipped seat: {NO_LOGIN}"


@pytest.mark.no_pg
def test_keep_alive_one_failing_account_does_not_stop_the_others(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / WINDOWS_CREDENTIAL_FILE).write_text("{" + TOKEN, encoding="utf-8")
    slow = tmp_path / "slow"
    write_credentials(slow, NOW + 10)
    crash = tmp_path / "crash"
    write_credentials(crash, NOW + 10)
    good = tmp_path / "good"
    write_credentials(good, NOW + 10)
    fake = FakeRun({str(good): NOW + 8 * 3600})
    fake.raises[str(slow)] = subprocess.TimeoutExpired(["login-tool"], 180, output="MODEL-OUTPUT")
    fake.raises[str(crash)] = PermissionError(13, "Permission denied")
    results = keep_alive(
        accounts_for({"broken": broken, "slow": slow, "crash": crash, "good": good}),
        run=fake,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert [(r.account, r.action) for r in results] == [
        ("broken", "error"),
        ("slow", "error"),
        ("crash", "error"),
        ("good", "refreshed"),
    ]
    assert "not valid JSON" in results[0].message
    assert TOKEN not in results[0].message
    assert "timed out" in results[1].message
    assert "MODEL-OUTPUT" not in results[1].message
    assert "Permission denied" in results[2].message
    assert all(not cwd.exists() for cwd in fake.cwds)


@pytest.mark.no_pg
def test_keep_alive_reads_only_window_usage_accounts(tmp_path: Path) -> None:
    seat = tmp_path / "seat"
    write_credentials(seat, NOW + MARGIN + 60)
    spend = AIAccount("spend", "driver-d", str(tmp_path / "spend"))
    results = keep_alive(
        accounts_for({"seat": seat}, extra={"spend": spend}),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=opt_bin,
    )
    assert [r.account for r in results] == ["seat"]


@pytest.mark.no_pg
def test_keep_alive_without_the_executable_reports_due_accounts(tmp_path: Path) -> None:
    due = tmp_path / "due"
    write_credentials(due, NOW + 10)
    fresh = tmp_path / "fresh"
    write_credentials(fresh, NOW + MARGIN + 60)
    looked_up: list[str] = []

    def which(name: str) -> str | None:
        looked_up.append(name)
        return None

    results = keep_alive(
        accounts_for({"due": due, "fresh": fresh}),
        run=forbidden_run,
        clock=lambda: NOW,
        read_keychain=no_keychain,
        which=which,
    )
    assert [(r.account, r.action) for r in results] == [("due", "error"), ("fresh", "fresh")]
    assert results[0].message == "login-tool executable not found on PATH"
    assert looked_up == ["login-tool"]


# --- Daemon tick and watch -----------------------------------------------------


def write_manifest(home: Path, seat: Path, keepalive: object = "on") -> None:
    write_drivers(home, document())
    data: dict[str, Any] = {
        "accounts": {"seat": {"provider": WINDOWS_DRIVER_ID, "config_dir": str(seat)}},
        "usage_accounts": ["seat"],
    }
    if keepalive == "on":
        data["token_keepalive"] = {"refresh_before_minutes": 30}
    elif keepalive != "absent":
        data["token_keepalive"] = keepalive
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


RESULTS = [
    KeepaliveResult("fresh-seat", "fresh", NOW + 3 * 3600, ""),
    KeepaliveResult("renewed-seat", "refreshed", NOW + 8 * 3600, ""),
    KeepaliveResult("no-login", "skipped", None, NO_LOGIN),
    KeepaliveResult("broken-seat", "error", None, "token not renewed (exit 1)"),
]


@pytest.mark.no_pg
def test_daemon_tick_prints_refreshed_errors_and_skips_but_not_fresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path, tmp_path / "seat")
    seen: list[AIAccounts] = []

    def fake_keep_alive(accounts: AIAccounts) -> list[KeepaliveResult]:
        seen.append(accounts)
        return RESULTS

    monkeypatch.setattr(token_keepalive, "keep_alive", fake_keep_alive)
    _token_keepalive_tick(tmp_path)
    assert seen[0].token_keepalive_seconds == 30 * 60
    assert seen[0].drivers[WINDOWS_DRIVER_ID].usage.keepalive is not None  # type: ignore[union-attr]
    captured = capsys.readouterr()
    assert captured.out == "token.keepalive refreshed renewed-seat until 2027-01-15T16:00Z\n"
    assert captured.err == (
        f"token.keepalive skipped no-login: {NO_LOGIN}\n"
        "token.keepalive error: broken-seat: token not renewed (exit 1)\n"
    )


@pytest.mark.no_pg
@pytest.mark.parametrize("keepalive", ["absent", None])
def test_daemon_tick_when_off_does_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    keepalive: object,
) -> None:
    write_manifest(tmp_path, tmp_path / "seat", keepalive)

    def fake_keep_alive(_accounts: AIAccounts) -> list[KeepaliveResult]:
        raise AssertionError("keep-alive is off")

    monkeypatch.setattr(token_keepalive, "keep_alive", fake_keep_alive)
    _token_keepalive_tick(tmp_path)
    assert capsys.readouterr() == ("", "")


@pytest.mark.no_pg
def test_daemon_tick_survives_a_broken_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ai-accounts.json").write_text("{", encoding="utf-8")
    _token_keepalive_tick(tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("token.keepalive error: ")


def run_cli(home: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HOME", str(home))
    main(argv)


@pytest.mark.no_pg
def test_watch_token_keepalive_prints_every_account_and_fails_on_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path, tmp_path / "seat")
    monkeypatch.setattr(token_keepalive, "keep_alive", lambda _accounts: RESULTS)
    with pytest.raises(SystemExit) as info:
        run_cli(tmp_path, ["watch", "token-keepalive"], monkeypatch)
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "token.keepalive fresh fresh-seat until 2027-01-15T11:00Z\n"
        "token.keepalive refreshed renewed-seat until 2027-01-15T16:00Z\n"
    )
    assert "token.keepalive error: broken-seat: token not renewed (exit 1)" in captured.err

    monkeypatch.setattr(token_keepalive, "keep_alive", lambda _accounts: RESULTS[:3])
    run_cli(tmp_path, ["watch", "token-keepalive"], monkeypatch)
    captured = capsys.readouterr()
    assert "fresh-seat" in captured.out
    assert "token.keepalive skipped no-login" in captured.err

    monkeypatch.setattr(token_keepalive, "keep_alive", lambda _accounts: [])
    run_cli(tmp_path, ["watch", "token-keepalive"], monkeypatch)
    assert capsys.readouterr().out == "token.keepalive none\n"


@pytest.mark.no_pg
@pytest.mark.parametrize("keepalive", ["absent", None])
def test_watch_token_keepalive_not_configured_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keepalive: object
) -> None:
    write_manifest(tmp_path, tmp_path / "seat", keepalive)

    def fake_keep_alive(_accounts: AIAccounts) -> list[KeepaliveResult]:
        raise AssertionError("keep-alive is off")

    monkeypatch.setattr(token_keepalive, "keep_alive", fake_keep_alive)
    with pytest.raises(SystemExit) as info:
        run_cli(tmp_path, ["watch", "token-keepalive"], monkeypatch)
    assert info.value.code == "agent: token_keepalive is not configured"


@pytest.mark.no_pg
def test_watch_token_keepalive_rejects_extra_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path, tmp_path / "seat")
    with pytest.raises(SystemExit) as info:
        run_cli(tmp_path, ["watch", "token-keepalive", "--follow"], monkeypatch)
    assert info.value.code == "agent: Usage: agent watch token-keepalive"


def test_knock_daemon_runs_the_keepalive_before_the_account_scanners(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cli(tmp_path, ["init"], monkeypatch)
    capsys.readouterr()
    order: list[str] = []
    keepalive_checks: list[float | None] = []
    usage_checks: list[object] = []
    listens: list[int] = []

    class Accounts:
        usage_accounts = ("spend-a", "seat")

    def fake_keepalive_due(last: float | None, _now: float) -> bool:
        keepalive_checks.append(last)
        # Five minutes have not passed since the first pass.
        return last is None

    def fake_poll_due(*args: object, **_kwargs: object) -> bool:
        usage_checks.append(args)
        return True

    def fake_account(_store: object) -> list[str]:
        order.append("account")
        raise AccountUsageError(["spend-a: unavailable"], [])

    def fake_window(_store: object, *, pace: dict[str, float]) -> list[str]:
        order.append("window")
        raise AccountUsageError(["seat: Login usage returned HTTP 500"], [])

    def stop_listen(*_args: object, **_kwargs: object) -> None:
        listens.append(1)
        if len(listens) == 2:
            raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.keepalive_due", fake_keepalive_due)
    monkeypatch.setattr(
        "agent_cli.main._token_keepalive_tick", lambda _home: order.append("keepalive")
    )
    monkeypatch.setattr("agent_cli.main.scan_session_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.main.load_ai_accounts", lambda _home, **_kwargs: Accounts())
    monkeypatch.setattr("agent_cli.main.scan_account_usage", fake_account)
    monkeypatch.setattr("agent_cli.main.scan_window_usage", fake_window)
    monkeypatch.setattr("agent_cli.main.scan_merged", lambda _store, _runner: ([], 0))
    monkeypatch.setattr("agent_cli.github_act.scan_github", lambda _store, _runner: [])
    monkeypatch.setattr("agent_cli.mail_act.scan_mail", lambda _store, _runner: [])
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", lambda _store, _runner: [])
    monkeypatch.setattr("agent_cli.main.knock_listen", stop_listen)

    with pytest.raises(SystemExit, match="stop"):
        run_cli(tmp_path, ["knock"], monkeypatch)

    assert order == ["keepalive", "account", "window", "account", "window"]
    assert len(keepalive_checks) == 2
    assert keepalive_checks[0] is None and isinstance(keepalive_checks[1], float)
    # The keep-alive has its own clock; the usage tick is checked once per loop.
    assert len(usage_checks) == 2


@pytest.mark.no_pg
def test_keepalive_due_every_five_minutes() -> None:
    assert keepalive_due(None, 100.0)
    assert not keepalive_due(100.0, 100.0 + KEEPALIVE_CHECK_SECONDS - 1)
    assert keepalive_due(100.0, 100.0 + KEEPALIVE_CHECK_SECONDS)
    assert KEEPALIVE_CHECK_SECONDS == 300
