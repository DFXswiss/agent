"""Live runs (07-testing.md section 6, SCN-05): the operator's `E2E_LIVE_*` secrets reach Playwright and the artefact
scan, and HTTPS to the live providers is the only egress beside the doubles."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from orchestrator import config, run
from orchestrator.node import LocalNode
from orchestrator.run import Run
from support import World
from test_run import config_of, summary, tools_of

SECRET = "live-secret-0123456789abcdef"


def capture_playwright_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []
    clean_env = config.clean_env

    def recording(environ: Any, extra: Any, *args: Any) -> dict[str, str]:
        if "E2E_CONTROL_URL" in extra:
            seen.append(dict(extra))
        return clean_env(environ, extra, *args)

    monkeypatch.setattr(run, "clean_env", recording)
    return seen


def test_live_run_hands_over_the_secrets_and_allows_the_providers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {**world.environ, "E2E_LIVE_OPENROUTER_KEY": SECRET, "E2E_LIVE_EMPTY": ""}
    seen = capture_playwright_env(monkeypatch)
    bridge = {"pid": 4242, "command": "Python"}
    records = [
        {**bridge, "remote": "198.51.100.7:443", "host": "198.51.100.7", "port": 443},
        {**bridge, "remote": "198.51.100.7:80", "host": "198.51.100.7", "port": 80},
        {**bridge, "remote": "198.51.100.9:443", "host": "198.51.100.9", "port": 443},
        {"pid": 77, "command": "limactl", "remote": "198.51.100.7:443", "host": "198.51.100.7", "port": 443},
    ]
    monkeypatch.setattr(run, "violations", lambda _records: [dict(r, device="hub-device") for r in records])
    monkeypatch.setattr(LocalNode, "processes", lambda _self: {"double-usage": {"pid": 4242}, "hub": {"pid": 1}})
    tools = tools_of(world, live_addresses=lambda: {"198.51.100.7"})
    live = Run(config_of(world, live=True), environ, tools)
    assert SECRET in live.secrets.values
    assert "" not in live.secrets.values
    assert live.execute() == run.EXIT_FAILED
    failures = summary(world)["failures"]
    assert "egress to 198.51.100.9:443 by Python on hub-device" in failures
    assert "egress to 198.51.100.7:80 by Python on hub-device" in failures
    assert "egress to 198.51.100.7:443 by limactl on hub-device" in failures
    assert not any("198.51.100.7:443 by Python" in failure for failure in failures)
    assert seen[-1]["E2E_LIVE_OPENROUTER_KEY"] == SECRET


def test_hermetic_run_keeps_live_variables_away(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = {**world.environ, "E2E_LIVE_OPENROUTER_KEY": SECRET}
    seen = capture_playwright_env(monkeypatch)
    world.set(pw_model="1", hub_commands="audit verify")
    hermetic = Run(config_of(world), environ, tools_of(world, live_addresses=lambda: pytest.fail("not live")))
    assert SECRET not in hermetic.secrets.values
    assert hermetic.execute() == run.EXIT_OK
    assert "E2E_LIVE_OPENROUTER_KEY" not in seen[-1]


def test_live_addresses_resolve_the_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str, port: int, **_kw: Any) -> list[Any]:
        if host == "gone.example":
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert run.live_addresses(("openrouter.ai", "gone.example")) == {"203.0.113.5"}
    assert run.live_addresses() == {"203.0.113.5"}


def test_live_specs_that_need_vms_get_the_golden_image(tmp_path: Any) -> None:
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "live-01-openrouter.spec.ts").write_text("test('@LIVE-01 @live @needs-vm smoke', () => {});\n")
    assert run.needs_vm(tmp_path, ("LIVE-01",)) is True
    assert run.needs_vm(tmp_path, ("LIVE-03",)) is False
