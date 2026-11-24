"""Control route of E2E-60: the operator's GitHub account on a platform runner (`routes/operator_github.py`). The
shell steps run for real in a temporary directory, so the modes the runner requires are checked on disk."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.node import Node
from orchestrator.services import Runner
from support import CORE

ORG = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
TOKEN = "kz53-operator-tok-q4w8e2r6"


class ShellNode(Node):
    """`run` executes argv (the route's `/bin/sh -c` steps); processes by name for stop and start."""

    def __init__(self) -> None:
        self.name = "shell"
        self.runs: list[tuple[list[str], dict[str, str] | None]] = []
        self.alive_names: set[str] = set()
        self.spawned: list[dict[str, str]] = []
        self.fail = False

    def call(self, op: str, /, **args: Any) -> Any:  # noqa: PLR0911 - one answer per device method
        if op == "run":
            self.runs.append((args["argv"], args["env"]))
            if self.fail:
                return {"rc": 1, "stdout": "", "stderr": "mkdir: permission denied"}
            done = subprocess.run(args["argv"], env=args["env"], capture_output=True, text=True, check=False)
            return {"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
        if op == "alive":
            return args["name"] in self.alive_names
        if op == "log_tail":
            return "runner.run enrolled=yes"
        if op == "stop":
            self.alive_names.discard(args["name"])
            return 0
        if op == "spawn":
            self.alive_names.add(args["name"])
            self.spawned.append(dict(args["env"]))
            return 1
        if op == "wait_line":
            return "runner.run enrolled=yes"
        raise AssertionError(op)


ROUTES = discover()


def setup(tmp_path: Path) -> tuple[Context, ShellNode, Runner]:
    node = ShellNode()
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    ctx = Context(config, node, node, "/agent/python", scratch=str(tmp_path))
    runner = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": str(tmp_path / "runner-1")})
    ctx.runners["runner-1"] = runner
    node.alive_names.add("runner-1")
    return ctx, node, runner


def call(ctx: Context, body: Any, name: str = "runner-1") -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, "POST", f"/runners/{name}/operator-github", body)


def test_refusals(tmp_path: Path) -> None:
    ctx, node, _ = setup(tmp_path)
    assert call(ctx, {"token": None}, "runner-9")[0] == 404
    for bad in (None, [TOKEN], {"org": ORG}, {"token": "with space", "org": ORG}, {"token": 7, "org": ORG}):
        assert call(ctx, bad)[0] == 400
    for org in (None, "not-an-id", ORG.upper()):
        assert call(ctx, {"token": TOKEN, "org": org})[0] == 400
    assert node.runs == []


def test_token_file_and_restart(tmp_path: Path) -> None:
    ctx, node, runner = setup(tmp_path)
    status, answer = call(ctx, {"token": TOKEN, "org": ORG})
    path = tmp_path / "runner-1-operator-github" / "token"
    assert (status, answer) == (
        200,
        {"name": "runner-1", "token_file": str(path), "org": ORG, "restarted": True},
    )
    assert path.read_text(encoding="ascii") == TOKEN + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.stat().st_uid == os.geteuid()
    assert node.spawned[-1]["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] == str(path)
    assert node.spawned[-1]["AGENT_RUNNER_OPERATOR_GITHUB_ORG"] == ORG
    # the token travels in the environment of the write only, never in argv or the answer
    assert all(TOKEN not in " ".join(argv) for argv, _ in node.runs)
    assert TOKEN not in str(answer)

    # a second token replaces the first; a stopped runner is not started
    node.alive_names.clear()
    assert call(ctx, {"token": TOKEN + "-2", "org": ORG})[1]["restarted"] is False
    assert path.read_text(encoding="ascii") == TOKEN + "-2\n"
    assert sorted(item.name for item in path.parent.iterdir()) == ["token"]

    # removal: directory gone, variables unset, restart of a running runner
    node.alive_names.add("runner-1")
    assert call(ctx, {"token": None}) == (
        200,
        {"name": "runner-1", "token_file": None, "org": None, "restarted": True},
    )
    assert not path.parent.exists()
    assert "AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE" not in runner.env
    assert "AGENT_RUNNER_OPERATOR_GITHUB_ORG" not in runner.env


def test_failed_write(tmp_path: Path) -> None:
    ctx, node, _ = setup(tmp_path)
    node.fail = True
    status, answer = call(ctx, {"token": TOKEN, "org": ORG})
    assert (status, answer["error"]) == (500, "failed")
    assert "permission denied" in answer["detail"]
