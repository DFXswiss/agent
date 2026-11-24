"""U-RUN-config: fail-closed configuration (05-runner.md section 1.3, R-3.6, ADR-027 section 7)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_runner import config as config_module
from agent_runner.config import (
    DEFAULT_LOGIN_ARGV,
    HUB_NAMES,
    NAMES,
    PREFIX,
    Config,
    check_platform,
    environment,
    load_config,
)
from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.errors import RunnerError

SECRETISH = "s3cr3t-VALUE-must-not-leak"


def load(env: dict[str, str], **kwargs: object) -> Config:
    return load_config(env, platform="darwin", **kwargs)  # type: ignore[arg-type]


def refused(env: dict[str, str], name: str, **kwargs: object) -> None:
    with pytest.raises(RunnerError) as caught:
        load(env, **kwargs)
    assert caught.value.code == "config_invalid"
    assert caught.value.name == name
    for value in env.values():
        if len(value) > 3:
            assert value not in str(caught.value)


@pytest.fixture
def base(runner_home: Path, env_for: Callable[..., dict[str, str]]) -> dict[str, str]:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    return env


def test_defaults(base: dict[str, str], runner_home: Path, root_key: str) -> None:
    cfg = load(base)
    assert cfg.home == runner_home
    assert cfg.hub_url == "http://127.0.0.1:9"
    assert cfg.platform_root_key == root_key
    assert (cfg.vm_cpus, cfg.vm_memory_mib, cfg.vm_disk_gib) == (4, 8192, 20)
    assert cfg.pool_size == 2
    assert cfg.memory_reserve_mib == 32768
    assert (cfg.session_idle_seconds, cfg.vm_idle_seconds) == (1800, 1800)
    assert cfg.textfile_dir is None
    assert cfg.model_catalog is None
    assert cfg.openrouter_key_file is None
    assert cfg.github_client_id is None
    assert cfg.github_api_url == "https://api.github.com"
    assert cfg.github_web_url == "https://github.com"
    assert cfg.model_upstream_anthropic == "https://api.anthropic.com"
    assert cfg.model_upstream_openrouter == "https://openrouter.ai/api"
    assert cfg.anthropic_usage_url == "https://api.anthropic.com/api/oauth/usage"
    assert cfg.login_argv == DEFAULT_LOGIN_ARGV == ("claude", "setup-token")
    assert cfg.login_automation is False
    assert cfg.golden_template is None
    assert cfg.test_mode is True
    assert cfg.vm_prefix == ""
    assert cfg.pg_dir == runner_home / "pg"
    assert cfg.log_dir == runner_home / "log"
    assert cfg.tmp_dir == runner_home / "tmp"


def test_default_home_expands_user(base: dict[str, str]) -> None:
    del base["AGENT_RUNNER_HOME"]
    cfg = load(base, expand_user=lambda text: text.replace("~", "/nonexistent-test-root"))
    assert cfg.home == Path("/nonexistent-test-root/.local/share/agent-runner")


def test_every_name_is_listed_once() -> None:
    assert len(NAMES) == len(set(NAMES)) == 40
    assert "VM_PREFIX" in NAMES
    assert "TEST_MODE" in NAMES
    assert frozenset({"HUB_URL", "PLATFORM_ROOT_KEY"}) == HUB_NAMES


@pytest.mark.parametrize("platform", ["linux", "win32", "freebsd", ""])
def test_non_darwin_is_refused(platform: str, base: dict[str, str]) -> None:
    with pytest.raises(RunnerError) as caught:
        load_config(base, platform=platform)
    assert caught.value.code == "unsupported_os"
    assert caught.value.name is None
    with pytest.raises(RunnerError):
        check_platform(platform)


def test_darwin_is_supported() -> None:
    check_platform("darwin")


def test_unknown_prefixed_variable_is_refused(base: dict[str, str]) -> None:
    base["AGENT_RUNNER_HUB_UR"] = SECRETISH
    refused(base, "HUB_UR")


def test_unrelated_variables_are_ignored(base: dict[str, str]) -> None:
    base["AGENT_HOME"] = "/elsewhere"
    base["OTHER"] = SECRETISH
    assert load(base).home != Path("/elsewhere")


@pytest.mark.parametrize("name", ["HUB_URL", "PLATFORM_ROOT_KEY"])
def test_required_names(name: str, base: dict[str, str]) -> None:
    del base[PREFIX + name]
    refused(base, name)
    base[PREFIX + name] = ""
    refused(base, name)
    cfg = load(base, require=frozenset())
    assert getattr(cfg, name.lower()) is None


@pytest.mark.parametrize(
    "value",
    [
        "https://hub.example.org",
        "https://hub.example.org/",
        "https://hub.example.org:8443/base",
        "https://[2001:db8::1]:443",
    ],
)
def test_hub_url_valid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_HUB_URL"] = value
    base["AGENT_RUNNER_TEST_MODE"] = "0"
    assert load(base).hub_url == value.rstrip("/")


@pytest.mark.parametrize("value", ["http://127.0.0.1:8788", "http://localhost:1", "http://[::1]:2", "http://127.9.9.9"])
def test_loopback_http_only_in_test_mode(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_HUB_URL"] = value
    assert load(base).hub_url == value
    base["AGENT_RUNNER_TEST_MODE"] = "0"
    refused(base, "HUB_URL")


@pytest.mark.parametrize(
    "value",
    [
        "http://hub.example.org",
        "ftp://hub.example.org",
        "hub.example.org",
        "https://",
        "https://user:pw@hub.example.org",
        "https://user@hub.example.org",
        "https://hub.example.org/?x=1",
        "https://hub.example.org/#f",
        "https://hub.example.org:99999",
        "https://hub.example.org:0",
        "https://hub.example .org",
        "https://[::1",
        "http://192.0.2.1",
        "http://hub.localhost.example",
    ],
)
def test_hub_url_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_HUB_URL"] = value
    refused(base, "HUB_URL")


@pytest.mark.parametrize(
    "name",
    [
        "GITHUB_API_URL",
        "GITHUB_WEB_URL",
        "MODEL_UPSTREAM_ANTHROPIC",
        "MODEL_UPSTREAM_OPENROUTER",
        "ANTHROPIC_USAGE_URL",
    ],
)
def test_service_urls(name: str, base: dict[str, str]) -> None:
    base[PREFIX + name] = "http://127.0.0.1:20055/double/"
    assert getattr(load(base), name.lower()) == "http://127.0.0.1:20055/double"
    base["AGENT_RUNNER_TEST_MODE"] = "0"
    base["AGENT_RUNNER_HUB_URL"] = "https://hub.example.org"
    refused(base, name)
    base[PREFIX + name] = "https://upstream.example.org/v1"
    assert getattr(load(base), name.lower()) == "https://upstream.example.org/v1"


def test_root_key_valid(base: dict[str, str]) -> None:
    key = b64u_encode(b"\x07" * 32)
    base["AGENT_RUNNER_PLATFORM_ROOT_KEY"] = key
    assert load(base).platform_root_key == key


@pytest.mark.parametrize(
    "value",
    [
        b64u_encode(b"\x07" * 31),
        b64u_encode(b"\x07" * 33),
        b64u_encode(b"\x07" * 32) + "=",
        "not base64!",
        b64u_encode(b"\x07" * 32)[:-1] + "B",
    ],
)
def test_root_key_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_PLATFORM_ROOT_KEY"] = value
    refused(base, "PLATFORM_ROOT_KEY")


INTEGER_CASES = {
    "VM_CPUS": (1, 256),
    "VM_MEMORY_MIB": (1024, 4_194_304),
    "VM_DISK_GIB": (5, 65_536),
    "POOL_SIZE": (0, 64),
    "MEMORY_RESERVE_MIB": (0, 4_194_304),
    "SESSION_IDLE_SECONDS": (60, 604_800),
    "VM_IDLE_SECONDS": (60, 604_800),
}


@pytest.mark.parametrize("name", list(INTEGER_CASES))
def test_integers(name: str, base: dict[str, str]) -> None:
    low, high = INTEGER_CASES[name]
    for value in (low, high):
        base[PREFIX + name] = str(value)
        assert getattr(load(base), name.lower()) == value
    for bad in (str(low - 1) if low > 0 else "-1", str(high + 1), "1.5", "abc", " 5", "0x10", "1234567890"):
        base[PREFIX + name] = bad
        refused(base, name)


def test_pool_zero_disables_pool(base: dict[str, str]) -> None:
    base["AGENT_RUNNER_POOL_SIZE"] = "0"
    assert load(base).pool_size == 0


@pytest.mark.parametrize("value", ["0", "1"])
def test_test_mode_values(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_TEST_MODE"] = value
    base["AGENT_RUNNER_HUB_URL"] = "https://hub.example.org"
    assert load(base).test_mode is (value == "1")


@pytest.mark.parametrize("value", ["yes", "true", "2", " 1"])
def test_test_mode_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_TEST_MODE"] = value
    refused(base, "TEST_MODE")


def test_home_relative_is_refused(base: dict[str, str]) -> None:
    base["AGENT_RUNNER_HOME"] = "relative/home"
    refused(base, "HOME")


def test_home_existing_private_dir(base: dict[str, str], runner_home: Path) -> None:
    runner_home.mkdir(mode=0o700)
    assert load(base).home == runner_home


def test_home_wide_mode_is_refused(base: dict[str, str], runner_home: Path) -> None:
    runner_home.mkdir()
    os.chmod(runner_home, 0o755)
    refused(base, "HOME")


def test_home_file_or_symlink_is_refused(base: dict[str, str], runner_home: Path, tmp_path: Path) -> None:
    runner_home.write_text("x", encoding="utf-8")
    os.chmod(runner_home, 0o600)
    refused(base, "HOME")
    runner_home.unlink()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    runner_home.symlink_to(target)
    refused(base, "HOME")


def test_textfile_dir(base: dict[str, str], tmp_path: Path) -> None:
    base["AGENT_RUNNER_TEXTFILE_DIR"] = str(tmp_path / "textfile")
    assert load(base).textfile_dir == tmp_path / "textfile"
    base["AGENT_RUNNER_TEXTFILE_DIR"] = "textfile"
    refused(base, "TEXTFILE_DIR")


@pytest.mark.parametrize("name", ["MODEL_CATALOG", "GOLDEN_TEMPLATE"])
def test_files(name: str, base: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / f"{name.lower()}.json"
    path.write_text("{}", encoding="utf-8")
    base[PREFIX + name] = str(path)
    assert getattr(load(base), name.lower()) == path
    base[PREFIX + name] = str(tmp_path / "missing.json")
    refused(base, name)
    base[PREFIX + name] = str(tmp_path)
    refused(base, name)
    base[PREFIX + name] = "relative.json"
    refused(base, name)


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "openrouter.key"
    path.write_text(SECRETISH, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize("kind", [None, "platform"])
def test_openrouter_key_file_allowed(kind: str | None, base: dict[str, str], key_file: Path) -> None:
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(key_file)
    assert load(base, device_kind=kind).openrouter_key_file == key_file


def test_openrouter_key_file_refused_on_org_devices(base: dict[str, str], key_file: Path) -> None:
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(key_file)
    refused(base, "OPENROUTER_KEY_FILE", device_kind="org")
    del base["AGENT_RUNNER_OPENROUTER_KEY_FILE"]
    assert load(base, device_kind="org").openrouter_key_file is None


def test_openrouter_key_file_checks(base: dict[str, str], key_file: Path, tmp_path: Path) -> None:
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(key_file)
    os.chmod(key_file, 0o640)
    refused(base, "OPENROUTER_KEY_FILE")
    os.chmod(key_file, 0o400)
    assert load(base).openrouter_key_file == key_file
    link = tmp_path / "link.key"
    link.symlink_to(key_file)
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(link)
    refused(base, "OPENROUTER_KEY_FILE")
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(tmp_path / "missing.key")
    refused(base, "OPENROUTER_KEY_FILE")
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = "relative.key"
    refused(base, "OPENROUTER_KEY_FILE")
    directory = tmp_path / "dir.key"
    directory.mkdir(mode=0o700)
    base["AGENT_RUNNER_OPENROUTER_KEY_FILE"] = str(directory)
    refused(base, "OPENROUTER_KEY_FILE")


USER_A = "01a1ee56-5e00-7359-8ec3-2c90dd7ae358"
USER_B = "01a1ee56-5e00-73da-a86c-5c56722cfde3"


@pytest.mark.parametrize("kind", [None, "platform"])
def test_operator_github_settings_on_platform_devices(kind: str | None, base: dict[str, str], tmp_path: Path) -> None:
    base["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = str(tmp_path / "not-yet-there.token")
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = f" {USER_A.upper()} , {USER_B},{USER_A}"
    found = load(base, device_kind=kind)
    # the file is checked at each read, not here: the operator may place it after the start
    assert found.operator_github_token_file == tmp_path / "not-yet-there.token"
    assert found.operator_github_users == frozenset({USER_A, USER_B})


def test_operator_github_settings_default_off(base: dict[str, str]) -> None:
    found = load(base)
    assert (found.operator_github_token_file, found.operator_github_users) == (None, frozenset())
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = " "
    assert load(base).operator_github_users == frozenset()


def test_operator_github_settings_are_refused(base: dict[str, str], tmp_path: Path) -> None:
    token = str(tmp_path / "github.token")
    base["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = token
    refused(base, "OPERATOR_GITHUB_USERS")
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = "alice"
    refused(base, "OPERATOR_GITHUB_USERS")
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = f"{USER_A},"
    refused(base, "OPERATOR_GITHUB_USERS")
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = USER_A
    refused(base, "OPERATOR_GITHUB_TOKEN_FILE", device_kind="org")
    base["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = "relative.token"
    refused(base, "OPERATOR_GITHUB_TOKEN_FILE")
    del base["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"]
    refused(base, "OPERATOR_GITHUB_TOKEN_FILE")
    refused(base, "OPERATOR_GITHUB_USERS", device_kind="org")


@pytest.mark.parametrize("value", ["Iv1.0123456789abcdef", "Iv23liAbCdEf", "client_id-1"])
def test_github_client_id(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_GITHUB_CLIENT_ID"] = value
    assert load(base).github_client_id == value


@pytest.mark.parametrize("value", ["has space", "x" * 65, "semi;colon", "ünicode"])
def test_github_client_id_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_GITHUB_CLIENT_ID"] = value
    refused(base, "GITHUB_CLIENT_ID")


def test_login_argv(base: dict[str, str]) -> None:
    base["AGENT_RUNNER_LOGIN_ARGV"] = json.dumps(["/opt/double/login", "--fast"])
    assert load(base).login_argv == ("/opt/double/login", "--fast")


@pytest.mark.parametrize("value", ["[]", '"claude"', "[1]", '[""]', "not json", '["a\\u0000b"]', '{"a": 1}'])
def test_login_argv_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_LOGIN_ARGV"] = value
    refused(base, "LOGIN_ARGV")


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        (None, "", False),
        ("org", "", False),
        ("platform", "", True),
        ("org", "on", True),
        ("platform", "off", False),
    ],
)
def test_login_automation(kind: str | None, value: str, expected: bool, base: dict[str, str]) -> None:
    if value:
        base["AGENT_RUNNER_LOGIN_AUTOMATION"] = value
    assert load(base, device_kind=kind).login_automation is expected


@pytest.mark.parametrize("value", ["yes", "ON", "1"])
def test_login_automation_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_LOGIN_AUTOMATION"] = value
    refused(base, "LOGIN_AUTOMATION")


@pytest.mark.parametrize("value", ["", "wp-run01-", "arp", "a" * 32])
def test_vm_prefix(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_VM_PREFIX"] = value
    assert load(base).vm_prefix == value


@pytest.mark.parametrize("value", ["WP-", "1wp-", "-wp", "wp_", "wp.", "a" * 33, "wp run"])
def test_vm_prefix_invalid(value: str, base: dict[str, str]) -> None:
    base["AGENT_RUNNER_VM_PREFIX"] = value
    refused(base, "VM_PREFIX")


def test_environment_round_trip_defaults(base: dict[str, str], runner_home: Path) -> None:
    cfg = load(base)
    env = environment(cfg)
    assert env == {
        "AGENT_RUNNER_HOME": str(runner_home),
        "AGENT_RUNNER_HUB_URL": "http://127.0.0.1:9",
        "AGENT_RUNNER_PLATFORM_ROOT_KEY": cfg.platform_root_key,
        "AGENT_RUNNER_TEST_MODE": "1",
    }
    assert load(env) == cfg


def test_environment_round_trip_everything(base: dict[str, str], tmp_path: Path, key_file: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    base.update(
        {
            "AGENT_RUNNER_VM_CPUS": "8",
            "AGENT_RUNNER_VM_MEMORY_MIB": "16384",
            "AGENT_RUNNER_VM_DISK_GIB": "40",
            "AGENT_RUNNER_POOL_SIZE": "0",
            "AGENT_RUNNER_MEMORY_RESERVE_MIB": "1024",
            "AGENT_RUNNER_SESSION_IDLE_SECONDS": "600",
            "AGENT_RUNNER_VM_IDLE_SECONDS": "900",
            "AGENT_RUNNER_TEXTFILE_DIR": str(tmp_path / "tf"),
            "AGENT_RUNNER_MODEL_CATALOG": str(catalog),
            "AGENT_RUNNER_GOLDEN_TEMPLATE": str(catalog),
            "AGENT_RUNNER_OPENROUTER_KEY_FILE": str(key_file),
            "AGENT_RUNNER_GITHUB_CLIENT_ID": "Iv1.abc",
            "AGENT_RUNNER_GITHUB_API_URL": "http://127.0.0.1:20050",
            "AGENT_RUNNER_GITHUB_WEB_URL": "http://127.0.0.1:20051",
            "AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC": "http://127.0.0.1:20052",
            "AGENT_RUNNER_MODEL_UPSTREAM_OPENROUTER": "http://127.0.0.1:20053",
            "AGENT_RUNNER_ANTHROPIC_USAGE_URL": "http://127.0.0.1:20054/usage",
            "AGENT_RUNNER_LOGIN_ARGV": '["login-double"]',
            "AGENT_RUNNER_LOGIN_AUTOMATION": "on",
            "AGENT_RUNNER_VM_PREFIX": "wp-run01-",
            "AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE": str(tmp_path / "github.token"),
            "AGENT_RUNNER_OPERATOR_GITHUB_USERS": f"{USER_B},{USER_A}",
        }
    )
    cfg = load(base)
    env = environment(cfg)
    assert SECRETISH not in json.dumps(env)
    assert env["AGENT_RUNNER_LOGIN_ARGV"] == '["login-double"]'
    assert load(env) == cfg


def test_environment_without_test_mode(base: dict[str, str]) -> None:
    base["AGENT_RUNNER_TEST_MODE"] = "0"
    base["AGENT_RUNNER_HUB_URL"] = "https://hub.example.org"
    env = environment(load(base))
    assert "AGENT_RUNNER_TEST_MODE" not in env


def test_b64u_round_trip() -> None:
    for size in range(5):
        data = bytes(range(size))
        assert b64u_decode(b64u_encode(data)) == data
    with pytest.raises(ValueError, match="padding"):
        b64u_decode("A")


def test_module_constants() -> None:
    assert config_module.SUPPORTED_PLATFORM == "darwin"
    assert config_module.DEFAULT_HOME == "~/.local/share/agent-runner"


def test_environment_leaves_the_login_automation_default_to_the_device_kind(base: dict[str, str]) -> None:
    """Installed before enrollment: the plist must not pin `off`, so a platform device gets `on` later."""
    env = environment(load(base))
    assert "AGENT_RUNNER_LOGIN_AUTOMATION" not in env
    assert load(env, device_kind="platform").login_automation is True
    base["AGENT_RUNNER_LOGIN_AUTOMATION"] = "off"
    pinned = environment(load(base))
    assert pinned["AGENT_RUNNER_LOGIN_AUTOMATION"] == "off"
    assert load(pinned, device_kind="platform").login_automation is False


@pytest.mark.parametrize("name", ["VM_CPUS", "GITHUB_CLIENT_ID", "VM_PREFIX", "PLATFORM_ROOT_KEY"])
def test_trailing_newline_is_refused(name: str, base: dict[str, str]) -> None:
    values = {
        "VM_CPUS": "4",
        "GITHUB_CLIENT_ID": "Iv1.abc",
        "VM_PREFIX": "wp-",
        "PLATFORM_ROOT_KEY": base[PREFIX + "PLATFORM_ROOT_KEY"],
    }
    base[PREFIX + name] = values[name] + "\n"
    refused(base, name)


def test_home_that_cannot_be_inspected_is_refused(base: dict[str, str], tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    base["AGENT_RUNNER_HOME"] = str(locked / "home")
    os.chmod(locked, 0o000)
    try:
        refused(base, "HOME")
    finally:
        os.chmod(locked, 0o700)


# -- operator subscriptions (KOR-23, ADR-052) ----------------------------------------------------------------------

ORG_A = "0b9c6a52-4a35-4c1e-9d8e-8f3a0e8d5b11"
ORG_B = "7f1d2c3b-0000-4000-8000-00000000000b"


@pytest.fixture
def operator_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "operator"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


@pytest.mark.parametrize("kind", [None, "platform"])
def test_operator_subscriptions_take_a_directory_and_organizations(
    kind: str | None, base: dict[str, str], operator_dir: Path
) -> None:
    assert (load(base).operator_subscription_dir, load(base).operator_subscription_orgs) == (None, frozenset())
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = f" {ORG_A.upper()} ,{ORG_B},{ORG_A}"
    cfg = load(base, device_kind=kind)
    assert cfg.operator_subscription_dir == operator_dir
    assert cfg.operator_subscription_orgs == frozenset({ORG_A, ORG_B})
    env = environment(cfg)
    assert env["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] == str(operator_dir)
    assert env["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] == ",".join(sorted({ORG_A, ORG_B}))
    assert load(env, device_kind=kind) == cfg


def test_operator_subscriptions_and_operator_github_together(
    base: dict[str, str], operator_dir: Path, tmp_path: Path
) -> None:
    """KOR-23 and KOR-27 side by side: both pairs load, round-trip through the service environment and are refused
    together on org devices."""
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = ORG_A
    base["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = str(tmp_path / "github.token")
    base["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = USER_A
    cfg = load(base, device_kind="platform")
    assert (cfg.operator_subscription_dir, cfg.operator_subscription_orgs) == (operator_dir, frozenset({ORG_A}))
    assert (cfg.operator_github_token_file, cfg.operator_github_users) == (
        tmp_path / "github.token",
        frozenset({USER_A}),
    )
    assert load(environment(cfg), device_kind="platform") == cfg
    refused(base, "OPERATOR_SUBSCRIPTION_DIR", device_kind="org")
    del base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"], base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"]
    refused(base, "OPERATOR_GITHUB_TOKEN_FILE", device_kind="org")


def test_operator_subscriptions_need_both_settings(base: dict[str, str], operator_dir: Path) -> None:
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    refused(base, "OPERATOR_SUBSCRIPTION_ORGS")
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = "  "  # empty is nobody, and the directory alone is not valid
    refused(base, "OPERATOR_SUBSCRIPTION_ORGS")
    del base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"]
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = ORG_A
    refused(base, "OPERATOR_SUBSCRIPTION_DIR")
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = ""
    assert load(base).operator_subscription_orgs == frozenset()


def test_operator_subscriptions_are_refused_on_org_devices(base: dict[str, str], operator_dir: Path) -> None:
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = ORG_A
    refused(base, "OPERATOR_SUBSCRIPTION_DIR", device_kind="org")


@pytest.mark.parametrize("value", ["not-a-uuid", f"{ORG_A},", f"{ORG_A},,{ORG_B}", "1234"])
def test_operator_organizations_must_be_ids(value: str, base: dict[str, str], operator_dir: Path) -> None:
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = value
    refused(base, "OPERATOR_SUBSCRIPTION_ORGS")


def test_operator_directory_checks(base: dict[str, str], operator_dir: Path, tmp_path: Path) -> None:
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = ORG_A
    for value in ("relative/dir", str(tmp_path / "missing")):
        base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = value
        refused(base, "OPERATOR_SUBSCRIPTION_DIR")
    link = tmp_path / "link"
    link.symlink_to(operator_dir, target_is_directory=True)
    plain = tmp_path / "plain"
    plain.write_text("x")
    plain.chmod(0o600)
    for path in (link, plain):
        base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(path)
        refused(base, "OPERATOR_SUBSCRIPTION_DIR")
    operator_dir.chmod(0o750)
    base["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(operator_dir)
    refused(base, "OPERATOR_SUBSCRIPTION_DIR")


# -- work VMs (KOR-29, ADR-059, spec 11) ---------------------------------------------------------------------------


def test_workvm_defaults_offer_no_work_vm(base: dict[str, str]) -> None:
    cfg = load(base)
    assert (cfg.workvm_slots, cfg.workvm_slots_self, cfg.workvm_max_size) == (0, False, "large")
    assert (cfg.workvm_idle_seconds, cfg.workvm_queue_seconds, cfg.workvm_pool_size) == (1800, 900, 1)
    assert cfg.workvm_disk_quota_gib == 0
    assert (cfg.workvm_max_per_task, cfg.workvm_max_per_org, cfg.workvm_max_per_owner) == (4, 16, 8)
    assert (cfg.workvm_ssh_port_base, cfg.workvm_template) == (47200, None)
    assert not any(name.startswith("AGENT_RUNNER_WORKVM_") for name in environment(cfg))


def test_workvm_settings_round_trip(base: dict[str, str], tmp_path: Path) -> None:
    template = tmp_path / "workvm.yaml"
    template.write_text("vmType: vz\n", encoding="utf-8")
    base.update(
        {
            "AGENT_RUNNER_TEST_MODE": "1",
            "AGENT_RUNNER_WORKVM_SLOTS": "3",
            "AGENT_RUNNER_WORKVM_SLOTS_SELF": "1",
            "AGENT_RUNNER_WORKVM_MAX_SIZE": "xlarge",
            "AGENT_RUNNER_WORKVM_IDLE_SECONDS": "600",
            "AGENT_RUNNER_WORKVM_POOL_SIZE": "0",
            "AGENT_RUNNER_WORKVM_DISK_QUOTA_GIB": "500",
            "AGENT_RUNNER_WORKVM_TEMPLATE": str(template),
        }
    )
    cfg = load(base)
    assert (cfg.workvm_slots, cfg.workvm_slots_self, cfg.workvm_max_size) == (3, True, "xlarge")
    assert (cfg.workvm_idle_seconds, cfg.workvm_template, cfg.workvm_pool_size) == (600, template, 0)
    assert cfg.workvm_disk_quota_gib == 500
    env = environment(cfg)
    assert env["AGENT_RUNNER_WORKVM_SLOTS_SELF"] == "1"
    assert env["AGENT_RUNNER_WORKVM_SLOTS"] == "3"
    assert load(env) == cfg


def test_workvm_settings_are_checked(base: dict[str, str]) -> None:
    production = {key: value for key, value in base.items() if key != "AGENT_RUNNER_TEST_MODE"}
    production["AGENT_RUNNER_HUB_URL"] = "https://hub.example"
    refused({**production, "AGENT_RUNNER_WORKVM_SLOTS_SELF": "1"}, "WORKVM_SLOTS_SELF")
    refused({**base, "AGENT_RUNNER_WORKVM_MAX_SIZE": "huge"}, "WORKVM_MAX_SIZE")
    refused({**base, "AGENT_RUNNER_WORKVM_SLOTS": "101"}, "WORKVM_SLOTS")
    refused({**base, "AGENT_RUNNER_WORKVM_POOL_SIZE": "17"}, "WORKVM_POOL_SIZE")
