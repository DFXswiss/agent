"""Manual integration run of the work VM manager against real Lima VMs (docs/platform/spec/13-work-vm.md section 11).

Not collected by pytest (no `test_` prefix). Needs a stopped work image instance in the current `LIMA_HOME` (built
from `images/workvm.yaml` and `images/workvm/provision.sh`), a PostgreSQL server for a scratch database and runs in
self mode (`TEST_MODE`: the runner's own account, no host filter, internet through Lima's user network):

    AGENT_RUNNER_TEST_PG=postgresql://... LIMA_HOME=... python tests/platform/runner/workvm/real_workvm.py IMAGE PREFIX
    LIMA_HOME=... python tests/platform/runner/workvm/real_workvm.py --build NAME PREFIX   # builds the image first

After the scenario it measures the warm pool: one pool entry is started (`pool_pass`, the device's cold start time)
and a second task's request is answered from it (`pooled_request_s`, the assignment time).

It requests a work VM, runs commands as root with internet, a failing and a passing test run over the mirrored
workspace, a background service, the browser (read and screenshot of a page served inside the VM), brings a file
back, releases the VM and prints the measured times and the recorded events as JSON.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_runner.github import snapshot
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.vm.lima import Lima
from agent_runner.workvm.manager import Deps, Settings, WorkVms
from agent_runner.workvm.shell import SlotCommand, VmShell

sys.path.insert(0, str(Path(__file__).parent))
_kit: Any = importlib.import_module("kit")
ORG, START, World, session = _kit.ORG, _kit.START, _kit.World, _kit.session

WORKSPACE = {
    "proj/calc.py": b"def add(a, b):\n    return a - b\n",
    "proj/test_calc.py": b"import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
    b"    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\nunittest.main()\n",
}


async def main(image: str, prefix: str) -> dict[str, Any]:  # noqa: PLR0915 - one linear manual scenario
    admin = os.environ["AGENT_RUNNER_TEST_PG"]
    name = "real_workvm_" + uuid.uuid4().hex[:8]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    dsn = admin.rsplit("/", 1)[0] + "/" + name
    migrate(dsn)
    tmp = Path(tempfile.mkdtemp(prefix="kor29-real-"))
    world = World(dsn=dsn, tmp=tmp)
    world.workspace = dict(WORKSPACE)

    async def guest_for(env: Any, local: Any) -> Any:
        return world.guest()

    snapshot.guest_for = guest_for  # type: ignore[assignment]
    events: list[tuple[str, dict[str, Any]]] = []
    command = SlotCommand(())
    db = await open_pool(dsn)
    async with db.connection() as aconn:
        await aconn.execute(
            "INSERT INTO runner.workvm_image (name, state, built_at) VALUES (%s, 'ready', %s)", (image, START)
        )

    async def emit(identity: Any, kind: str, body: Any) -> None:
        events.append((kind, dict(body)))

    async def yes() -> bool:
        return True

    async def plenty() -> int:
        return 1_000_000

    async def base_for(slot: int, img: str) -> str:
        return img

    deps = Deps(
        db=db,
        settings=Settings(
            slots=2,
            max_size="large",
            idle_seconds=1800,
            queue_seconds=60,
            max_per_task=4,
            max_per_org=16,
            max_per_owner=8,
            ssh_port_base=47290,
            test_mode=True,
        ),
        now=lambda: datetime.now(UTC),
        lima_for=lambda slot: Lima(command.sync_runner(slot), prefix=prefix),
        shell_for=lambda vm: VmShell(command, vm.slot, vm.lima_name),
        base_for=base_for,
        boundary_ok=yes,
        free_memory_mib=plenty,
        log=lambda line: print(line, file=sys.stderr),
        emit=emit,
        scratch=tmp,
    )
    vms = WorkVms(deps)
    measured: dict[str, Any] = {}
    local = session()
    try:
        started = time.monotonic()
        result = await vms.request(local, {})
        measured["request_s"] = round(time.monotonic() - started, 2)
        measured["request"] = result

        async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            begin = time.monotonic()
            answer = await vms.call(None, local, tool, arguments)
            measured.setdefault("calls", []).append({"tool": tool, "s": round(time.monotonic() - begin, 2)})
            return answer

        root = await call(
            "workvm_exec", {"command": "id -u; curl -s -o /dev/null -w '%{http_code}' https://example.com"}
        )
        measured["root_and_internet"] = root["output"]
        failing = await call("workvm_exec", {"command": "cd proj && python3 test_calc.py"})
        measured["failing_exit"] = failing["exit_code"]
        world.workspace["proj/calc.py"] = b"def add(a, b):\n    return a + b\n"
        passing = await call("workvm_exec", {"command": "cd proj && python3 test_calc.py"})
        measured["passing_exit"] = passing["exit_code"]
        measured["passing_synced"] = passing.get("synced")
        await call(
            "workvm_exec",
            {
                "command": "mkdir -p /srv/site && echo '<title>Work VM</title><h1>Hello from the work VM</h1>'"
                " > /srv/site/index.html && systemd-run --unit=site python3 -m http.server 8080 --directory /srv/site",
                "sync": "none",
            },
        )
        await asyncio.sleep(1)
        service = await call("workvm_exec", {"command": "curl -s http://127.0.0.1:8080/ | head -c 200", "sync": "none"})
        measured["service"] = service["output"]
        read = await call("workvm_browse", {"url": "http://127.0.0.1:8080/"})
        measured["browse"] = {key: read[key] for key in ("title", "status", "text")}
        shot = await call(
            "workvm_browse", {"url": "http://127.0.0.1:8080/", "action": "screenshot", "path": "shots/site.png"}
        )
        measured["screenshot"] = shot["screenshot"]
        await call("workvm_exec", {"command": "cd proj && echo generated > out.txt", "sync": "none"})
        back = await call("workvm_sync", {"direction": "from_vm", "path": "proj"})
        measured["from_vm"] = back
        measured["imports"] = [(directory, len(data)) for directory, data, _ in world.imports]
        status = await call("workvm_status", {})
        measured["jobs"] = len(status["jobs"])
        begin = time.monotonic()
        measured["release"] = await vms.release(local, {})
        measured["release_s"] = round(time.monotonic() - begin, 2)

        pooled = WorkVms(dataclasses.replace(deps, settings=dataclasses.replace(deps.settings, pool_size=1)))
        begin = time.monotonic()
        measured["pool_started"] = await pooled.pool_pass()
        measured["pool_fill_s"] = round(time.monotonic() - begin, 2)
        other = session(_kit.CHILD, task_id=_kit.TASK_B)
        begin = time.monotonic()
        warm = await pooled.request(other, {})
        measured["pooled_request_s"] = round(time.monotonic() - begin, 3)
        measured["pooled_request"] = warm
        ran = await pooled.call(None, other, "workvm_exec", {"command": "uname -sr; id -u", "sync": "none"})
        measured["pooled_exec"] = ran["output"]
        await pooled.release(other, {})
    finally:
        for vm in await __import__("agent_runner.workvm.records", fromlist=["live"]).live(db):
            await vms.delete(vm, reason="failed")
        await db.close()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {name}")
    measured["events"] = [(kind, body.get("state"), body.get("kind"), body.get("exit_code")) for kind, body in events]
    measured["runtime_ms"] = [
        body["runtime_ms"] for kind, body in events if kind == "workvm.state" and body["state"] == "deleted"
    ]
    assert ORG
    return measured


def build(name: str, prefix: str) -> str:
    """Build the work image `<prefix>wimg-<name>` in the current `LIMA_HOME` (self mode: no export)."""
    from agent_runner.workvm.image import Builder  # noqa: PLC0415 - only the build mode needs it

    root = Path(__file__).resolve().parents[4]
    lima = Lima(SlotCommand(()).sync_runner(0), prefix=prefix)
    with tempfile.TemporaryDirectory(prefix="kor29-image-") as scratch:
        built = Builder(
            lima=lima,
            template=root / "images" / "workvm.yaml",
            scratch=Path(scratch),
            register=lambda _name, _at: None,
            announce=lambda kind, fields: print(kind, fields, file=sys.stderr, flush=True),
            log=lambda text: print(text, file=sys.stderr, flush=True),
        ).build(name)
    print(f"built {built.name} in {built.seconds:.1f} s", file=sys.stderr)
    return built.name


if __name__ == "__main__":
    if sys.argv[1] == "--build":
        image_name = build(sys.argv[2], sys.argv[3])
        print(json.dumps(asyncio.run(main(image_name, sys.argv[3])), indent=1, default=str))
    else:
        print(json.dumps(asyncio.run(main(sys.argv[1], sys.argv[2])), indent=1, default=str))
