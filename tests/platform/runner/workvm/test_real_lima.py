"""The work image path against real Lima (HOT-17): build with an export directory, the exported disk, a slot's base
created from it, a clone of that base started, a command inside, stopped and removed.

It runs wherever a real `limactl` >= 2.3.0 is installed on macOS - the test devices of the gates - and is skipped
elsewhere (CI runners, Linux). The instances are named with `AGENT_RUNNER_VM_PREFIX` (set by the gate tools; a missing
variable fails the test) and are deleted at the end, also on failure.

The runner's own account plays the only slot (self command, no root; the separation of the slot accounts and the host
filter are tested elsewhere). The template is `images/workvm.yaml` with its pinned Ubuntu image and every Lima setting
of production, only smaller (2 CPUs, 2 GiB, 10 GiB disk); the clone takes the clone settings of production
(`WorkVms._sets`: 50 GiB disk, pinned SSH port, resolvers) with small CPUs and memory. The provisioning script is a
stand-in that installs the three commands of the smoke step and a marker file, so the test needs no internet beyond
the (cached) image and checks what production builds on: the Lima instance directory of the current Lima version.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_runner.vm.lima import Lima
from agent_runner.workvm import image, service, shell
from agent_runner.workvm.manager import POOL_SIZE, SIZES, Deps, Settings, WorkVms
from agent_runner.workvm.shell import SlotCommand, VmShell

ROOT = Path(__file__).resolve().parents[4]
PREFIX_ENV = "AGENT_RUNNER_VM_PREFIX"
MARKER = "/var/lib/agent-workvm-image-test"
PROVISION = f"""#!/bin/bash
set -euo pipefail
printf '#!/bin/sh\\necho v0-test\\n' > /usr/local/bin/node
printf '#!/bin/sh\\n[ "$1" = selftest ]\\n' > /usr/local/sbin/workvm-job
printf '#!/bin/sh\\n[ "$1" = selftest ]\\n' > /usr/local/bin/workvm-browser
chmod 0755 /usr/local/bin/node /usr/local/sbin/workvm-job /usr/local/bin/workvm-browser
echo built > {MARKER}
"""
SMALL = {r"^cpus: \d+$": "cpus: 2", r"^memory: \S+$": "memory: 2GiB", r"^disk: \S+$": "disk: 10GiB"}
GROWN_GIB = SIZES[POOL_SIZE][2]

pytestmark = pytest.mark.timeout(1200)


def real_limactl() -> str:
    limactl = shutil.which("limactl")
    if sys.platform != "darwin" or limactl is None:
        pytest.skip("no limactl on macOS here; the test devices run the work image path with real Lima")
    version = subprocess.run([limactl, "--version"], check=True, capture_output=True, text=True).stdout.split()[-1]
    if tuple(int(part) for part in version.split(".")[:2]) < (2, 3):
        pytest.skip(f"limactl {version} is older than the required 2.3.0")
    return limactl


def small_images(root: Path) -> Path:
    """`images/workvm.yaml` made small, beside a stand-in `workvm/provision.sh`; returns the template."""
    text = (ROOT / "images" / "workvm.yaml").read_text(encoding="utf-8")
    for pattern, line in SMALL.items():
        text, count = re.subn(pattern, line, text, count=1, flags=re.MULTILINE)
        assert count == 1, pattern
    (root / "workvm").mkdir(parents=True)
    (root / "workvm" / "provision.sh").write_text(PROVISION, encoding="utf-8")
    template = root / "workvm.yaml"
    template.write_text(text, encoding="utf-8")
    return template


@pytest.fixture
def lima() -> Iterator[Lima]:
    real_limactl()
    prefix = os.environ.get(PREFIX_ENV)
    if not prefix:
        pytest.fail(f"{PREFIX_ENV} is not set (the gate tools set it; every test instance carries it)")
    lima = Lima(SlotCommand(()).sync_runner(0), prefix=prefix)
    remove_test_instances(lima)  # an earlier run that died hard
    yield lima
    remove_test_instances(lima)


def remove_test_instances(lima: Lima) -> None:
    """The instances of this test only: build and final image, the base and the VM (the prefix is the package's)."""
    kinds = ("wimg-build-", "wimg-hot", "wbase-", "wvm-hot")
    for instance in lima.list():
        if any(instance.name.startswith(lima.prefix + kind) for kind in kinds):
            lima.delete(instance.name)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def clone_sets(slot: int) -> list[str]:
    """The `limactl clone --set` expressions of production (`WorkVms._sets`, pool size `standard`: 4 CPUs, 8 GiB,
    50 GiB disk, pinned SSH port, resolvers), then CPUs and memory made small again (a later `--set` wins)."""
    settings = Settings(
        slots=1,
        max_size=POOL_SIZE,
        idle_seconds=60,
        queue_seconds=60,
        max_per_task=1,
        max_per_org=1,
        max_per_owner=1,
        ssh_port_base=free_port() - slot,
        test_mode=True,
    )
    unused: Any = None
    deps = Deps(unused, settings, unused, unused, unused, unused, unused, unused, unused)
    return [*WorkVms(deps)._sets(POOL_SIZE, slot), ".cpus=2", '.memory="2GiB"']


def test_work_image_export_to_a_running_slot_vm(lima: Lima, tmp_path: Path) -> None:
    template = small_images(tmp_path / "images")
    exports, scratch = tmp_path / "exports", tmp_path / "scratch"
    exports.mkdir()
    scratch.mkdir()
    facts: list[tuple[str, dict[str, str | int | bool]]] = []
    registered: list[str] = []

    def register(name: str, _at: datetime) -> None:
        registered.append(name)

    builder = image.Builder(
        lima=lima,
        template=template,
        scratch=scratch,
        register=register,
        export_dir=exports,
        announce=lambda name, fields: facts.append((name, fields)),
        log=lambda text: facts.append(("log", {"text": text})),
    )
    suffix = "hot" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:6]
    built = builder.build(suffix)

    steps = [fields["step"] for name, fields in facts if name == "workvm.image"]
    assert steps == ["create", "start", "bundle", "provision", "smoke", "seal", "stop", "rename", "export", "register"]
    assert registered == [built.name]
    instance = lima.get(built.name)
    assert instance is not None
    assert instance.status == "Stopped"
    assert built.export == exports / f"{built.name}.img"
    disk = Path(shell.disk_path(instance.dir))
    assert built.export.stat().st_size == disk.stat().st_size == 10 << 30
    assert built.export.stat().st_mode & 0o777 == 0o644
    assert built.export.stat().st_blocks * 512 < built.export.stat().st_size  # copied sparsely

    # the slot: its base from the exported disk (no download), a clone of it grown and started
    lines: list[str] = []
    bases = service.Bases(SlotCommand(()), lima.prefix, template, exports, lines.append)
    base = asyncio.run(bases(0, built.name))
    assert lines == []
    assert base == lima.name("wbase-" + hashlib.sha256(built.name.encode()).hexdigest()[:8])
    found = lima.get(base)
    assert found is not None
    assert Path(shell.disk_path(found.dir)).stat().st_size == 10 << 30
    assert (exports / f"{built.name}.yaml").is_file()

    vm = lima.name(f"wvm-{suffix}")
    lima.clone(base, vm, sets=clone_sets(0))
    lima.start(vm)
    run = VmShell(SlotCommand(()), 0, vm)
    result = asyncio.run(
        run(["sudo", "sh", "-c", f"cat {MARKER}; lsblk -bdno SIZE /dev/vda"], stdin=None, timeout=120, limit=4096)
    )
    assert result.returncode == 0
    assert result.stdout.decode().split() == ["built", str(GROWN_GIB << 30)]
    lima.stop(vm)
    stopped = lima.get(vm)
    assert stopped is not None
    assert stopped.status == "Stopped"
    assert lima.delete(vm) is True
    assert lima.get(vm) is None
    assert lima.delete(base) is True
    assert lima.delete(built.name) is True
    assert {i.name for i in lima.list()} & {built.name, base, vm} == set()
