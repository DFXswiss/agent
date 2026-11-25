"""The installation block of the guide for AI agents runs as written (KOR-67, owner rules E-26, E-27, E-19).

`docs/platform/agents.md` section 0 is executed line by line with `sh -e` in an empty environment: a fresh home
directory, `PATH` of the operating system only (no Homebrew, no Python of the test run). The one substitution is the
package source: the published archive of the integration branch becomes an archive of this checkout with the same
layout, so the gate tests the package it is about to ship. The block must end within a minute and leave a working
`~/.local/bin/agent-platform`.

- On the gate device (macOS with Apple's `python3`, or any other system) as the test user.
- In the Linux part of the gates (`scripts/guest-ci.sh`, a fresh Ubuntu VM) as a new user without administrator
  rights (marker `linux`).

The block downloads `uv`, a Python build and the dependencies, so these tests need the network of the device.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "platform" / "agents.md"
PUBLISHED = "https://github.com/DFXswiss/agent/archive/refs/heads/ad5f2026-live.tar.gz"
# The paths a source archive of the distribution needs (pyproject.toml: readme, license, packages below src).
ARCHIVED = ("pyproject.toml", "README.md", "LICENSE", "src")
BOUND_SECONDS = 60
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def install_block() -> str:
    """The `sh` block of section 0 of the guide."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("## 0. Install the tool\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.S)
    return str(blocks[0])


def _no_build_files(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = info.name.split("/")
    return None if "__pycache__" in parts or any(part.endswith(".egg-info") for part in parts) else info


def source_archive(directory: Path) -> Path:
    """An archive of this checkout laid out like GitHub's (one top directory)."""
    archive = directory / "agent-checkout.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        for name in ARCHIVED:
            out.add(REPO_ROOT / name, arcname=f"agent-checkout/{name}", filter=_no_build_files)
    archive.chmod(0o644)
    return archive


def clean(home: Path) -> list[str]:
    """An empty environment: this home, the system's `PATH` only."""
    return ["env", "-i", f"HOME={home}", f"PATH={SYSTEM_PATH}", "LANG=C.UTF-8"]


def run_block(
    home: Path, archive: Path, user: str | None = None, *, wget: bool = False
) -> tuple[float, subprocess.CompletedProcess[str]]:
    block = install_block()
    assert PUBLISHED in block
    script = block.replace(PUBLISHED, archive.as_uri())
    if wget:  # the variant the guide gives for a system without curl
        script = script.replace("curl -LsSf ", "wget -qO- ", 1)
    command = [*clean(home), "/bin/sh", "-e", "-c", script]
    if user is not None:
        command = ["runuser", "-u", user, "--", *command]
    started = time.monotonic()
    done = subprocess.run(command, capture_output=True, text=True, timeout=BOUND_SECONDS * 3, check=False, cwd=home)
    return time.monotonic() - started, done


def check(home: Path, elapsed: float, done: subprocess.CompletedProcess[str]) -> None:
    # the block downloads uv, Python and the dependencies: a failure here may also be the device's network
    assert done.returncode == 0, "installation failed (network?): " + done.stdout[-2000:] + done.stderr[-4000:]
    assert elapsed < BOUND_SECONDS, f"the installation took {elapsed:.0f} s"
    assert '"guide":' in done.stdout
    tool = home / ".local" / "bin" / "agent-platform"
    version = subprocess.run(
        [*clean(home), str(tool), "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    assert version.returncode == 0, version.stdout + version.stderr
    assert '"ok": true' in version.stdout


def test_the_block_is_what_the_guide_shows() -> None:
    lines = install_block().splitlines()
    assert lines[0].startswith("curl -LsSf https://astral.sh/uv/")
    assert "UV_UNMANAGED_INSTALL" in lines[0]
    assert all("sudo" not in line and "python3" not in line and "brew" not in line for line in lines)
    assert lines[-1].startswith("~/.local/bin/agent-platform guide")


@pytest.mark.timing
def test_the_block_installs_in_a_fresh_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elapsed, done = run_block(home, source_archive(tmp_path))
    check(home, elapsed, done)


@pytest.fixture
def fresh_user() -> Iterator[str]:
    name = f"install-{uuid.uuid4().hex[:8]}"
    subprocess.run(["useradd", "--create-home", "--shell", "/bin/sh", name], check=True)
    try:
        yield name
    finally:
        subprocess.run(["userdel", "--remove", name], check=False, capture_output=True)


@pytest.fixture
def readable() -> Iterator[Path]:
    """A directory every user can read (the parents of `tmp_path` are the test user's only)."""
    directory = Path(tempfile.mkdtemp(prefix="install-", dir="/tmp"))
    directory.chmod(0o755)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@pytest.mark.linux
@pytest.mark.timing
@pytest.mark.parametrize("wget", [False, True], ids=["curl", "wget"])
def test_the_block_installs_for_a_new_user_without_sudo(fresh_user: str, readable: Path, wget: bool) -> None:
    home = Path(pwd.getpwnam(fresh_user).pw_dir)
    assert not (home / ".local").exists()
    assert os.geteuid() == 0
    assert shutil.which("python3", path=SYSTEM_PATH) is not None
    elapsed, done = run_block(home, source_archive(readable), user=fresh_user, wget=wget)
    check(home, elapsed, done)
    groups = subprocess.run(["id", "-nG", fresh_user], capture_output=True, text=True, check=True).stdout.split()
    assert "sudo" not in groups
