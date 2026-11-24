"""U-RUN-config: the settings of the DFX key issuer (KOR-50, ADR-081 section 7) - `OPENROUTER_MANAGEMENT_KEY_FILE`
(absolute, 0600, platform devices only, never echoed) and `DFX_KEY_MAX_HEADROOM_USD` (default 5 USD), both carried
into the service environment."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_runner.config import (
    DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD,
    NAMES,
    Config,
    environment,
    load_config,
    usd_text,
)
from agent_runner.errors import RunnerError

SECRETISH = "mgmt-s3cr3t-VALUE-must-not-leak"
NAME = "AGENT_RUNNER_OPENROUTER_MANAGEMENT_KEY_FILE"
CAP = "AGENT_RUNNER_DFX_KEY_MAX_HEADROOM_USD"


def load(env: dict[str, str], **kwargs: object) -> Config:
    return load_config(env, platform="darwin", **kwargs)  # type: ignore[arg-type]


def refused(env: dict[str, str], name: str, **kwargs: object) -> None:
    with pytest.raises(RunnerError) as caught:
        load(env, **kwargs)
    assert (caught.value.code, caught.value.name) == ("config_invalid", name)
    assert SECRETISH not in str(caught.value)


@pytest.fixture
def base(runner_home: Path, env_for: Callable[..., dict[str, str]]) -> dict[str, str]:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    return env


@pytest.fixture
def management_file(tmp_path: Path) -> Path:
    path = tmp_path / "management.key"
    path.write_text(SECRETISH, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_the_names_are_known() -> None:
    assert {"OPENROUTER_MANAGEMENT_KEY_FILE", "DFX_KEY_MAX_HEADROOM_USD"} <= set(NAMES)


def test_defaults(base: dict[str, str]) -> None:
    cfg = load(base)
    assert cfg.openrouter_management_key_file is None
    assert cfg.dfx_key_max_headroom_micro_usd == DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD == 5_000_000
    assert CAP not in environment(cfg)
    assert NAME not in environment(cfg)


@pytest.mark.parametrize("kind", [None, "platform"])
def test_the_management_key_file_on_a_platform_device(
    kind: str | None, base: dict[str, str], management_file: Path
) -> None:
    base[NAME] = str(management_file)
    cfg = load(base, device_kind=kind)
    assert cfg.openrouter_management_key_file == management_file
    env = environment(cfg)
    assert env[NAME] == str(management_file)
    assert SECRETISH not in str(env)
    assert load(env, device_kind=kind) == cfg


def test_the_management_key_file_is_refused_on_org_devices(base: dict[str, str], management_file: Path) -> None:
    base[NAME] = str(management_file)
    refused(base, "OPENROUTER_MANAGEMENT_KEY_FILE", device_kind="org")


def test_the_management_key_file_checks(base: dict[str, str], management_file: Path, tmp_path: Path) -> None:
    os.chmod(management_file, 0o640)
    base[NAME] = str(management_file)
    refused(base, "OPENROUTER_MANAGEMENT_KEY_FILE")
    os.chmod(management_file, 0o600)
    link = tmp_path / "link.key"
    link.symlink_to(management_file)
    for value in (str(link), str(tmp_path / "missing.key"), "relative.key", str(tmp_path)):
        base[NAME] = value
        refused(base, "OPENROUTER_MANAGEMENT_KEY_FILE")
    base[NAME] = ""
    refused(base, "OPENROUTER_MANAGEMENT_KEY_FILE", require=frozenset({"OPENROUTER_MANAGEMENT_KEY_FILE"}))


@pytest.mark.parametrize(
    ("text", "micro"),
    [
        ("5", 5_000_000),
        ("0", 0),
        ("2.5", 2_500_000),
        ("0.000001", 1),
        ("10000", 10_000_000_000),
        ("12.340", 12_340_000),
    ],
)
def test_the_headroom_cap(base: dict[str, str], text: str, micro: int) -> None:
    base[CAP] = text
    cfg = load(base)
    assert cfg.dfx_key_max_headroom_micro_usd == micro
    assert load(environment(cfg)) == cfg


@pytest.mark.parametrize("text", ["-1", "1e3", "1.", ".5", "1.0000001", "10000.000001", "100000", "five", " 5"])
def test_an_invalid_headroom_cap_is_refused(base: dict[str, str], text: str) -> None:
    base[CAP] = text
    refused(base, "DFX_KEY_MAX_HEADROOM_USD")


def test_usd_text() -> None:
    assert [usd_text(m) for m in (5_000_000, 2_500_000, 1, 0, 12_340_000)] == ["5", "2.5", "0.000001", "0", "12.34"]
