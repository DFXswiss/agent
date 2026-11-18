from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agent_core.config import Config, ConfigError, public_url_host
from agent_core.record.connection import migrate_config, parse_connection_string


REQUIRED = (
    "AGENT_CORE_PUBLIC_URL",
    "AGENT_CORE_SESSION_SECRET",
    "AGENT_CORE_GITHUB_CLIENT_ID",
    "AGENT_CORE_GITHUB_CLIENT_SECRET",
    "AGENT_CORE_GITHUB_AUTHORIZE_URL",
    "AGENT_CORE_GITHUB_TOKEN_URL",
    "AGENT_CORE_GITHUB_USER_URL",
    "AGENT_CORE_DATABASE",
    "AGENT_CORE_TEAMS",
    "AGENT_CORE_KEYS",
    "AGENT_CORE_SIGNING_KEY",
    "AGENT_CORE_HUB_KEY_ID",
    "AGENT_CORE_HOST",
    "AGENT_CORE_PORT",
)
SEED = "ab" * 32


def _full_env(tmp_path: Path) -> dict[str, str]:
    teams = tmp_path / "teams.yaml"
    teams.write_text("teams:\n  dfx:\n    members: []\n", encoding="utf-8")
    keys = tmp_path / "keys.yaml"
    keys.write_text("keys: []\n", encoding="utf-8")
    return {
        "AGENT_CORE_PUBLIC_URL": "http://127.0.0.1:8787",
        "AGENT_CORE_SESSION_SECRET": "x" * 32,
        "AGENT_CORE_GITHUB_CLIENT_ID": "id",
        "AGENT_CORE_GITHUB_CLIENT_SECRET": "secret",
        "AGENT_CORE_GITHUB_AUTHORIZE_URL": "https://github.com/login/oauth/authorize",
        "AGENT_CORE_GITHUB_TOKEN_URL": "https://github.com/login/oauth/access_token",
        "AGENT_CORE_GITHUB_USER_URL": "https://api.github.com/user",
        "AGENT_CORE_DATABASE": str(tmp_path / "db.sqlite"),
        "AGENT_CORE_TEAMS": str(teams),
        "AGENT_CORE_KEYS": str(keys),
        "AGENT_CORE_SIGNING_KEY": SEED,
        "AGENT_CORE_HUB_KEY_ID": "hub-test-1",
        "AGENT_CORE_HOST": "127.0.0.1",
        "AGENT_CORE_PORT": "8787",
    }


@pytest.fixture
def full_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = _full_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_from_env_requires_every_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    env = _full_env(tmp_path)
    with pytest.raises(ConfigError, match="is not set"):
        Config.from_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()
    assert cfg.port == 8787
    monkeypatch.setenv("AGENT_CORE_SESSION_SECRET", "short")
    with pytest.raises(ConfigError, match="32"):
        Config.from_env()
    monkeypatch.setenv("AGENT_CORE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("AGENT_CORE_TEAMS", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError, match="not a file"):
        Config.from_env()
    assert os.environ["AGENT_CORE_HOST"] == "127.0.0.1"


def test_every_required_variable_is_in_the_full_env(tmp_path: Path) -> None:
    assert set(_full_env(tmp_path)) == set(REQUIRED)


@pytest.mark.parametrize("name", REQUIRED)
def test_each_variable_missing_or_empty_stops_the_start(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ConfigError, match=f"{name} is not set"):
        Config.from_env()
    monkeypatch.setenv(name, "")
    with pytest.raises(ConfigError, match=f"{name} is not set"):
        Config.from_env()


def test_new_variables_are_read(full_env: dict[str, str]) -> None:
    cfg = Config.from_env()
    assert cfg.keys_path == Path(full_env["AGENT_CORE_KEYS"])
    assert cfg.signing_key == SEED
    assert cfg.hub_key_id == "hub-test-1"


def test_keys_path_must_be_a_file(
    full_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CORE_KEYS", str(tmp_path / "missing-keys.yaml"))
    with pytest.raises(ConfigError, match="AGENT_CORE_KEYS is not a file"):
        Config.from_env()


@pytest.mark.parametrize(
    "seed",
    [
        "AB" * 32,
        "Ab" * 32,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        " " + "a" * 63,
        "a" * 64 + "\n",
        "0x" + "a" * 62,
    ],
)
def test_signing_seed_must_be_64_lowercase_hex(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, seed: str
) -> None:
    monkeypatch.setenv("AGENT_CORE_SIGNING_KEY", seed)
    with pytest.raises(ConfigError, match="64 lowercase hex") as caught:
        Config.from_env()
    assert seed.strip() not in str(caught.value)


@pytest.mark.parametrize(
    "key_id",
    ["Hub-1", "h1", "-hub-1", "hub-1-", "hub_1", "hub.1", "a" * 64, "hub 1", "hub-1\n"],
)
def test_hub_key_id_must_follow_the_id_rule(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, key_id: str
) -> None:
    monkeypatch.setenv("AGENT_CORE_HUB_KEY_ID", key_id)
    with pytest.raises(ConfigError, match="AGENT_CORE_HUB_KEY_ID"):
        Config.from_env()


def test_seed_appears_in_no_representation(full_env: dict[str, str]) -> None:
    cfg = Config.from_env()
    assert SEED not in repr(cfg)
    assert SEED not in str(cfg)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://127.0.0.1:8787/hub", "path"),
        ("http://127.0.0.1:8787//", "path"),
        ("http://127.0.0.1:8787/hub/", "path"),
        ("http://user@127.0.0.1:8787", "user information"),
        ("http://user:pass@127.0.0.1:8787", "user information"),
        ("http://127.0.0.1:8787?a=1", "query"),
        ("http://127.0.0.1:8787/?", "query"),
        ("http://127.0.0.1:8787#top", "fragment"),
        ("http://127.0.0.1:8787/#", "fragment"),
        ("ftp://127.0.0.1", "http or https"),
        ("http://127.0.0.1:notaport", "not a valid URL"),
    ],
)
def test_public_url_with_user_path_query_or_fragment_stops_the_start(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, url: str, message: str
) -> None:
    monkeypatch.setenv("AGENT_CORE_PUBLIC_URL", url)
    with pytest.raises(ConfigError, match=message):
        Config.from_env()


def test_public_url_single_trailing_slash_is_not_a_path(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CORE_PUBLIC_URL", "https://hub.example.com/")
    assert Config.from_env().public_url == "https://hub.example.com"


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://Hub.Example.COM", "hub.example.com"),
        ("HTTPS://HUB.EXAMPLE.COM/", "hub.example.com"),
        ("https://hub.example.com:443", "hub.example.com"),
        ("http://hub.example.com:80", "hub.example.com"),
        ("http://Hub.Example.com:8787", "hub.example.com:8787"),
        ("https://hub.example.com:80", "hub.example.com:80"),
        ("http://hub.example.com:443", "hub.example.com:443"),
        ("http://[::1]:8787", "[::1]:8787"),
        ("https://[2001:DB8::1]:443", "[2001:db8::1]"),
        ("https://xn--bcher-kva.example", "xn--bcher-kva.example"),
    ],
)
def test_host_line_of_the_public_url(url: str, host: str) -> None:
    assert public_url_host(url) == host


def test_international_host_name_must_be_configured_in_ascii_form() -> None:
    with pytest.raises(ConfigError) as caught:
        public_url_host("https://bücher.example")
    assert str(caught.value) == "AGENT_CORE_PUBLIC_URL must carry an international host name in its ASCII form"


@pytest.mark.parametrize("url", ["https://", "https://:8787"])
def test_public_url_without_a_host_is_refused(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        public_url_host(url)
    assert str(caught.value) == "AGENT_CORE_PUBLIC_URL has no host"


@pytest.mark.parametrize("url", ["https://hub..example", "https://" + "a" * 64 + ".example"])
def test_public_url_with_an_invalid_host_name_is_refused(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        public_url_host(url)
    assert str(caught.value) == "AGENT_CORE_PUBLIC_URL has an invalid host name"


# RECORD-PROTOCOL.md sections 14 and 15: the compose file of this repository.

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yaml"
COMPOSE_PASSWORD = "0" * 64


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _resolved(value: str) -> str:
    """A connection string of the compose file with its password variable filled in."""
    for name in ("AGENT_CORE_RECORD_OWNER_PASSWORD", "AGENT_CORE_RECORD_WRITER_PASSWORD"):
        value = value.replace("${" + name + "}", COMPOSE_PASSWORD)
    return value


def test_the_hub_service_never_holds_the_owner_connection_string() -> None:
    hub = _compose()["services"]["hub"]
    assert "AGENT_CORE_RECORD_OWNER_DATABASE" not in hub["environment"]
    assert "AGENT_CORE_RECORD_DATABASE" in hub["environment"]


def test_the_hub_service_starts_only_after_the_migration_completed() -> None:
    services = _compose()["services"]
    assert services["hub"]["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}
    assert services["migrate"]["command"] == ["python", "-m", "agent_core.record", "migrate"]
    assert set(services["migrate"]["environment"]) == {
        "AGENT_CORE_RECORD_OWNER_DATABASE",
        "AGENT_CORE_RECORD_DATABASE",
    }


def test_every_connection_string_of_the_compose_file_uses_tls_and_has_the_form_of_section_15() -> None:
    services = _compose()["services"]
    strings = [
        services["hub"]["environment"]["AGENT_CORE_RECORD_DATABASE"],
        services["migrate"]["environment"]["AGENT_CORE_RECORD_DATABASE"],
        services["migrate"]["environment"]["AGENT_CORE_RECORD_OWNER_DATABASE"],
    ]
    for value in strings:
        assert value.endswith("?sslmode=require") or value.endswith("?sslmode=verify-full")
        parse_connection_string(_resolved(value))
    owner, writer = migrate_config(
        {
            "AGENT_CORE_RECORD_OWNER_DATABASE": _resolved(strings[2]),
            "AGENT_CORE_RECORD_DATABASE": _resolved(strings[1]),
        }
    )
    assert (owner.role, writer.role) == ("record_owner", "record_writer")
    assert strings[0] == strings[1]


def test_the_record_database_service_is_postgres_16_with_a_volume_and_tls() -> None:
    database = _compose()["services"]["record-db"]
    assert database["image"] == "postgres:16"
    assert "ssl=on" in database["command"]
    assert any(volume.endswith(":/var/lib/postgresql/data") for volume in database["volumes"])
    init = Path(__file__).resolve().parent.parent / "deploy" / "record-init.sh"
    text = init.read_text(encoding="utf-8")
    assert "CREATE DATABASE record OWNER record_owner;" in text
    for role in ("record_owner", "record_writer"):
        assert f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in text
