"""Control route of E2E-56 (`POST /own-device/prepare`): the signed test release in the hub's download area, the
stopped runner instance, and the stand-in of the administrator step that the agent tool runs (`SEAM`), against a
stand-in for the runner's Python (`-m agent_runner --json enroll`)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.node import Node
from orchestrator.routes import own_device
from orchestrator.services import Runner
from support import CORE, write_exec

VERSION = own_device.VERSION
PACKAGE = f"DFX-Agent-Runner-{VERSION}.pkg"
CODE = "E51-CODE-0001"

# The runner's Python: records what `enroll` got (argv, the code file and its mode, the runner environment) and
# answers like `agent_runner --json` as the file `mode` next to it says.
RUNNER = r"""#!@PYTHON@
import json, os, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
code_file = Path(args[args.index("--code-file") + 1])
record = {"args": args, "code": code_file.read_text(), "mode": code_file.stat().st_mode & 0o777,
          "mark": os.environ.get("RUNNER_MARK", "")}
(here / "enroll.json").write_text(json.dumps(record))
mode = (here / "mode").read_text() if (here / "mode").exists() else "ok"
if mode == "ok":
    print("progress")
    facts = [{"fact": "runner.migrate"}, {"fact": "runner.enroll", "device_id": "dev-1"}]
    print(json.dumps({"ok": True, "facts": facts}))
elif mode == "refused":
    print(json.dumps({"ok": False, "facts": [], "error": {"code": "invite_unknown", "name": None}}))
    sys.exit(1)
elif mode == "no-code":
    print(json.dumps({"ok": False, "facts": [], "error": None}))
    sys.exit(1)
else:
    sys.exit(1)
"""

# Verifies the published feed with the release code of the checkout and the key the route answered.
VERIFY = r"""
import json, sys
from datetime import UTC, datetime
from pathlib import Path
from agent_runner.install import release
folder, key = Path(sys.argv[1]), sys.argv[2]
document = release.verify((folder / "latest.json").read_bytes(), (folder / "latest.json.sig").read_text(), [key])
feed = release.feed_from(document)
print(json.dumps({**feed.document(), "expired": feed.expired(datetime.now(UTC))}))
"""

ROUTES = discover()


class StopNode(Node):
    """The runner device: only stopping a runner instance is expected."""

    def __init__(self) -> None:
        self.name = "runner-device"
        self.stopped: list[str] = []

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "stop":
            self.stopped.append(args["name"])
            return -15
        raise AssertionError(f"unexpected node call {op}")


@dataclass
class Setup:
    ctx: Context
    node: StopNode
    tmp: Path

    @property
    def runner_dir(self) -> Path:
        return self.tmp / "runner"

    @property
    def downloads(self) -> Path:
        return self.tmp / "scratch" / "downloads" / "runner"

    @property
    def computer(self) -> Path:
        return self.tmp / "scratch" / "agent-tool" / "mac"

    @property
    def state(self) -> Path:
        return self.computer / "device-root" / "Library/Application Support/DFXAgentRunner/state.json"


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    node = StopNode()
    ctx = Context(config, node, node, sys.executable, agent_src=str(CORE), scratch=str(tmp_path / "scratch"))
    python = write_exec(tmp_path / "runner" / "python", RUNNER.replace("@PYTHON@", sys.executable))
    ctx.runners["runner-2"] = Runner(node, "runner-2", str(python), {"RUNNER_MARK": "runner-2-env"})
    return Setup(ctx, node, tmp_path)


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, method, path, body)


def prepare(setup: Setup) -> dict[str, Any]:
    status, answer = call(setup.ctx, "POST", "/own-device/prepare", {"home": "mac", "runner": "runner-2"})
    assert status == 200, answer
    return dict(answer)


def seam(setup: Setup, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(setup.computer / "tools" / "device-admin"), *args], capture_output=True, text=True, timeout=60, check=False
    )


def install(setup: Setup, digest: str | None = None) -> subprocess.CompletedProcess[str]:
    request = setup.tmp / "enroll.request"
    request.write_text(CODE + "\n")
    package = setup.downloads / PACKAGE
    found = hashlib.sha256(package.read_bytes()).hexdigest()
    return seam(setup, "install", str(package), digest or found, str(request))


def test_prepare_publishes_a_signed_release_and_gives_the_computer_the_seam(setup: Setup) -> None:
    ctx = setup.ctx
    answer = prepare(setup)
    assert set(answer) == {"key", "version", "package"}
    assert (answer["version"], answer["package"]) == (VERSION, PACKAGE)
    data = b"E2E-56 stand-in package " + VERSION.encode()
    published = sorted(path.name for path in setup.downloads.iterdir())
    assert published == sorted([PACKAGE, "latest.json", "latest.json.sig"])
    assert (setup.downloads / PACKAGE).read_bytes() == data
    checked = subprocess.run(
        [sys.executable, "-c", VERIFY, str(setup.downloads), answer["key"]],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CORE / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    feed = json.loads(checked.stdout)
    assert feed["version"] == VERSION
    assert feed["package"] == PACKAGE
    assert feed["package_sha256"] == hashlib.sha256(data).hexdigest()
    assert (feed["notarized"], feed["min_macos"], feed["expired"]) == (True, "14.0", False)
    # the runner instance is stopped: the seam enrolls it, the spec starts it
    assert setup.node.stopped == ["runner-2"]
    assert ctx.runners["runner-2"].stopped_on_request is True
    root = setup.computer
    config = root / "device-seam.json"
    assert config.stat().st_mode & 0o777 == 0o600
    assert json.loads(config.read_text()) == {
        "python": str(setup.runner_dir / "python"),
        "env": {"RUNNER_MARK": "runner-2-env"},
        "hub": ctx.config.public_origin,
        "root": str(root / "device-root"),
        "version": VERSION,
    }
    script = root / "tools" / "device-admin"
    assert script.stat().st_mode & 0o777 == 0o755
    assert script.read_text().splitlines()[0] == f"#!{sys.executable}"
    assert (root / "device-root").is_dir()
    assert json.loads((root / "own-device.json").read_text()) == {
        "AGENT_PLATFORM_DEVICE_ADMIN": str(script),
        "AGENT_PLATFORM_DEVICE_ROOT": str(root / "device-root"),
        "AGENT_PLATFORM_DEVICE_KEYS": answer["key"],
    }
    # a second preparation publishes a fresh release over the first one (the computer keeps its directories)
    again = prepare(setup)
    assert again["key"] != answer["key"]


def test_the_seam_enrolls_the_runner_with_the_code_file_and_writes_the_state(setup: Setup) -> None:
    prepare(setup)
    done = install(setup)
    assert (done.returncode, done.stderr) == (0, "")
    state = json.loads(setup.state.read_text())
    updated = state.pop("updated_at")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated)
    assert state == {
        "v": 1,
        "version": VERSION,
        "owner": os.environ.get("USER", ""),
        "enrolled": True,
        "device_id": "dev-1",
        "enrollment": "enrolled",
        "daemon": "stopped",
        "golden": "unknown",
        "disabled_in_login_items": False,
    }
    record = json.loads((setup.runner_dir / "enroll.json").read_text())
    args = record["args"]
    hub = setup.ctx.config.public_origin
    assert args[:7] == ["-m", "agent_runner", "--json", "enroll", "--hub", hub, "--code-file"]
    assert len(args) == 8
    assert CODE not in " ".join(args)  # the code never appears in an argv
    assert (record["code"], record["mode"], record["mark"]) == (CODE, 0o600, "runner-2-env")
    assert not Path(args[7]).parent.exists()  # the code file and its directory are gone
    # removal: the helper's answer with empty leftovers, the state removed; a second removal answers the same
    for _ in range(2):
        removed = seam(setup, "uninstall", "", "", "")
        assert removed.returncode == 0
        answer = {"ok": True, "removed": ["stand-in"], "leftovers": [], "notes": []}
        assert json.loads(removed.stdout) == answer
        assert not setup.state.exists()


def test_the_seam_refuses_a_package_with_another_digest(setup: Setup) -> None:
    prepare(setup)
    done = install(setup, digest="0" * 64)
    assert done.returncode != 0
    assert "digest mismatch" in done.stderr
    assert not setup.state.exists()
    assert not (setup.runner_dir / "enroll.json").exists()


@pytest.mark.parametrize(
    ("mode", "error"), [("refused", "invite_unknown"), ("no-code", "enroll"), ("silent", "enroll")]
)
def test_a_failed_enrollment_is_written_to_the_state(setup: Setup, mode: str, error: str) -> None:
    prepare(setup)
    (setup.runner_dir / "mode").write_text(mode)
    done = install(setup)
    assert done.returncode == 0, done.stderr
    state = json.loads(setup.state.read_text())
    assert (state["enrolled"], state["enrollment"], state["device_id"]) == (False, "failed", "")
    assert state["enrollment_error"] == error


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"home": "mac"},
        {"home": "mac", "runner": 7},
        {"home": "mac", "runner": "runner-9"},
        {"home": "../mac", "runner": "runner-2"},
    ],
)
def test_prepare_refuses_bad_bodies(setup: Setup, body: Any) -> None:
    status, answer = call(setup.ctx, "POST", "/own-device/prepare", body)
    assert (status, answer["error"]) == (400, "invalid")
    assert setup.node.stopped == []


def test_a_failed_release_build_is_an_error_and_keeps_the_runner(setup: Setup) -> None:
    broken = "#!/bin/sh\necho 'Traceback: agent_runner.install.release is broken' >&2\nexit 1\n"
    setup.ctx.agent_python = str(write_exec(setup.tmp / "broken" / "python", broken))
    status, answer = call(setup.ctx, "POST", "/own-device/prepare", {"home": "mac", "runner": "runner-2"})
    assert (status, answer["error"]) == (500, "failed")
    assert answer["detail"].startswith("release: ")
    assert answer["detail"] == "release: Traceback: agent_runner.install.release is broken"
    assert setup.node.stopped == []
    assert not (setup.computer / "own-device.json").exists()
