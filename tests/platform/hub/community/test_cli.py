"""Operator CLI `agent-hub community moderator …` against the test hub (14-community.md section 7)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_hub import cli as main_cli
from agent_hub.community import cli
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import ed25519_seed
from agent_hub.platform import cli as platform_cli
from tests.platform.hub.community.conftest import Community
from tests.platform.hub.conftest import HubHarness, root_private_key

Runner = Callable[..., tuple[int, str, str]]


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "root.json"
    path.write_text(json.dumps({"v": 1, "root": b64u_encode(ed25519_seed(root_private_key()))}), encoding="ascii")
    path.chmod(0o600)
    return path


@pytest.fixture
def run(hub: HubHarness, key_file: Path) -> Runner:
    def call(*argv: str, http: httpx.Client | None = None, key: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = main_cli.build_parser().parse_args(
            [
                "community",
                "moderator",
                argv[0],
                "--key-file",
                str(key or key_file),
                "--hub",
                "http://localhost:8788",
                *argv[1:],
            ]
        )
        code = cli.run(args, http=http or hub.base, clock=hub.clock, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    return call


def test_grant_list_revoke(community: Community, run: Runner) -> None:
    member = community.member()
    assert run("list") == (0, "no moderators\n", "")
    assert run("grant", "--user", member.id) == (0, f"moderator {member.id} granted\n", "")
    code, out, _ = run("list")
    assert code == 0
    assert out.startswith(f"moderator {member.id} since 20")
    assert member.state()["moderator"] is True
    assert run("revoke", "--user", member.id) == (0, f"moderator {member.id} revoked\n", "")
    assert member.state()["moderator"] is False


def test_refusals_and_errors(community: Community, run: Runner, tmp_path: Path) -> None:
    unknown = "0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
    assert run("grant", "--user", unknown) == (1, "", "agent-hub community: refused: not_found\n")
    missing = tmp_path / "missing.json"
    code, _, err = run("list", key=missing)
    assert code == 2
    assert err.startswith("agent-hub community: ")

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    offline = httpx.Client(transport=httpx.MockTransport(unreachable), base_url="http://localhost:8788")
    assert run("list", http=offline) == (1, "", "agent-hub community: the hub cannot be reached\n")


def test_a_malformed_answer_is_refused(hub: HubHarness, run: Runner, monkeypatch: Any) -> None:
    def broken(self: Any, method: str, target: str, body: Any = None) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(platform_cli.Operator, "request", broken)
    assert run("list") == (1, "", "agent-hub community: refused: unavailable (the hub's answer is malformed)\n")


def test_the_parser_registers_the_handler() -> None:
    args = main_cli.build_parser().parse_args(
        ["community", "moderator", "list", "--key-file", "k", "--hub", "http://localhost:8788"]
    )
    assert args.handler is cli.run
