"""Work image cache `cache-wimg-<sha12>` (KOR-29): inputs, name, ensure under the lock, command line."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from orchestrator import workvm_image


def test_tree_sha_and_name(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    with pytest.raises(workvm_image.WorkImageError, match=r"images/workvm\.yaml, images/workvm, images/versions\.json"):
        workvm_image.tree_sha(agent)
    files = {
        "images/workvm.yaml": "a",
        "images/workvm/provision.sh": "b",
        "images/workvm/__pycache__/x.pyc": "c",
        "images/workvm/.DS_Store": "d",
        "images/versions.json": "e",
        "images/golden.yaml": "not an input",
    }
    for rel, text in files.items():
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text(text)
    counted = {"images/workvm.yaml": "a", "images/workvm/provision.sh": "b", "images/versions.json": "e"}

    def digest(entries: dict[str, str]) -> str:
        lines = "".join(f"{hashlib.sha256(t.encode()).hexdigest()}  {r}\n" for r, t in sorted(entries.items()))
        return hashlib.sha256(lines.encode()).hexdigest()

    assert workvm_image.tree_sha(agent) == digest(counted)
    (agent / "src/agent_runner/workvm").mkdir(parents=True)
    (agent / "src/agent_runner/workvm/image.py").write_text("f")
    assert workvm_image.tree_sha(agent) == digest({**counted, "src/agent_runner/workvm/image.py": "f"})
    assert workvm_image.cache_name(agent) == f"cache-wimg-{workvm_image.tree_sha(agent)[:12]}"


def test_ensure_builds_once_and_main(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    for rel in ("images/workvm.yaml", "images/workvm/provision.sh", "images/versions.json"):
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text(rel)
    name = workvm_image.cache_name(agent)
    listing = {"names": ""}
    fail = {"build": False}
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake(argv: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        if argv[:2] == ["limactl", "list"]:
            return subprocess.CompletedProcess(argv, 0, listing["names"], "")
        if not fail["build"]:
            listing["names"] = f"other\n{name}\n"
        return subprocess.CompletedProcess(argv, 1 if fail["build"] else 0, "", "build log")

    locks = tmp_path / "locks"
    assert workvm_image.ensure(agent, "py", tmp_path / "h", locks, run=fake) == name
    assert workvm_image.ensure(agent, "py", tmp_path / "h", locks, run=fake) == name
    builds = [(argv, env) for argv, env in calls if "build" in argv]
    assert len(builds) == 1
    argv, env = builds[0]
    assert argv == ["py", "-m", "agent_runner", "workvm", "image", "build", "--name", name.rsplit("-", 1)[1]]
    assert env is not None
    assert (env["AGENT_RUNNER_VM_PREFIX"], env["AGENT_RUNNER_WORKVM_SLOTS_SELF"], env["AGENT_RUNNER_TEST_MODE"]) == (
        "cache-",
        "1",
        "1",
    )
    assert not list(locks.iterdir())
    args = ["--agent", str(agent), "--python", "py", "--home", str(tmp_path), "--lock-dir", str(locks)]
    assert workvm_image.main(args, run=fake) == 0
    listing["names"] = ""
    fail["build"] = True
    assert workvm_image.main(args, run=fake) == 1
    fail["build"] = False

    def silent(argv: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(workvm_image.WorkImageError, match="build failed"):
        workvm_image.ensure(agent, "py", tmp_path / "h", locks, run=silent)
