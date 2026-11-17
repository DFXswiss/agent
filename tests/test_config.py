from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.config import Config, ConfigError, public_url_host


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
        ("https://bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_host_line_of_the_public_url(url: str, host: str) -> None:
    assert public_url_host(url) == host
