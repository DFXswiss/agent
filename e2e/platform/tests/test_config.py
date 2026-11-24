"""Options, environment, ports and the allow-list environment."""

from __future__ import annotations

from pathlib import Path

import pytest
from orchestrator.config import DEAD_PROXY, ConfigError, RunConfig, build_config, clean_env, home_relative, port
from support import CORE, World


def build(world: World, **changes: object) -> RunConfig:
    environ = dict(world.environ)
    for key in [k for k, v in changes.items() if v is None and k.isupper()]:
        environ.pop(key)
        changes.pop(key)
    environ.update({k: str(v) for k, v in changes.items() if k.isupper()})
    options = {k: v for k, v in changes.items() if not k.isupper()}
    options.setdefault("topology", "t1")
    return build_config(environ=environ, core_src=CORE, **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"topology": "t3"}, "--topology must be one of t1, t2"),
        ({"topology": "t2"}, "--runner <ssh target> is required for t2"),
        ({"runner": "a@b"}, "not allowed for t1"),
        ({"topology": "t2", "runner": "bad target!"}, "--runner must be an SSH target"),
        ({"project": "tablet"}, "--project must be one of"),
        ({"repeat": 0}, "--repeat must be 1..20"),
        ({"only": "E2E-06,smoke"}, "--only takes test IDs like E2E-06 (got smoke)"),
        ({"WP_PORT_BASE": "80"}, "WP_PORT_BASE must be a port number"),
        ({"WP_PORT_BASE": "x"}, "WP_PORT_BASE must be a port number"),
        ({"AGENT_RUNNER_VM_PREFIX": None}, "AGENT_RUNNER_VM_PREFIX (or WP_VM_PREFIX) must be set"),
        ({"AGENT_RUNNER_VM_PREFIX": "Bad-"}, "must be lower case"),
        ({"AGENT_RUNNER_VM_PREFIX": "nodash"}, "end with '-'"),
        ({"AGENT_RUNNER_SRC": "/nonexistent"}, "is not an agent checkout"),
        ({"E2E_POOL_SIZE": "-1"}, "E2E_POOL_SIZE must be a non-negative integer"),
        ({"E2E_RUNNERS": "5"}, "E2E_RUNNERS must be 1..4"),
        ({"E2E_VM_MEMORY_BUDGET_MIB": "lots"}, "E2E_VM_MEMORY_BUDGET_MIB must be a number"),
        ({"E2E_WEB_MINIFY": "yes"}, "E2E_WEB_MINIFY must be 0 or 1"),
    ],
)
def test_invalid_options(world: World, changes: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)").replace("|", r"\|")):
        build(world, **changes)


def test_runner_source_defaults_to_this_checkout(world: World) -> None:
    assert build(world, AGENT_RUNNER_SRC=None).agent_src == CORE


def test_valid_configuration(world: World) -> None:
    config = build(
        world,
        topology="t2",
        runner="runner@192.0.2.10",
        only="E2E-06, E2E-08",
        repeat=3,
        live=True,
        project="mobile",
        AGENT_RUNNER_VM_PREFIX=None,
        WP_VM_PREFIX="wp-abc-",
        AGENT_RUNNER_SRC="/nonexistent/agent",
        E2E_ARTIFACTS_DIR=str(world.root / "art"),
        E2E_RUNNERS="2",
        E2E_POOL_SIZE="1",
        E2E_VM_MEMORY_MIB="4096",
        E2E_REMOTE_PATH_PREFIX="/usr/local/bin",
    )
    assert config.only == ("E2E-06", "E2E-08")
    assert config.run_prefix == "wp-abc-e2e-"
    assert config.runner_prefix(2) == "wp-abc-e2e-r2-"
    assert config.artifacts == world.root / "art"
    assert config.suite == CORE / "e2e" / "platform"
    assert config.public_origin == f"http://localhost:{config.port_base + 7}"
    assert (config.runners, config.pool_size, config.vm_memory_mib) == (2, 1, 4096)
    assert config.memory_budget_mib == 65536
    assert config.path_prefix == "/usr/local/bin"
    assert config.remote_agent_src == "/nonexistent/agent"
    assert config.web_minify is False
    assert build(world, E2E_WEB_MINIFY="1").web_minify is True
    assert build(world, E2E_WEB_MINIFY="0").web_minify is False
    default = build(world, E2E_VM_MEMORY_BUDGET_MIB=None)
    assert default.memory_budget_mib is None
    assert default.artifacts == CORE / "e2e" / "platform" / "artifacts"
    assert default.remote_agent_src == str(world.agent)


def test_home_relative_and_ports(tmp_path: Path) -> None:
    assert home_relative(tmp_path / "wp" / "x" / "agent", tmp_path) == "~/wp/x/agent"
    assert home_relative(Path("/elsewhere"), tmp_path) == "/elsewhere"
    assert port(20000, "public") == 20007


def test_clean_env_is_an_allow_list() -> None:
    env = clean_env(
        {"PATH": "/bin", "HOME": "/h", "SECRET_TOKEN": "x", "AGENT_HUB_X": "y"}, {"EXTRA": "1"}, ("192.0.2.5",)
    )
    assert env["PATH"] == "/bin"
    assert env["HOME"] == "/h"
    assert env["LANG"] == "C.UTF-8"
    assert env["EXTRA"] == "1"
    assert "SECRET_TOKEN" not in env
    assert "AGENT_HUB_X" not in env
    assert env["HTTPS_PROXY"] == env["http_proxy"] == env["ALL_PROXY"] == DEAD_PROXY
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1,192.0.2.5"
