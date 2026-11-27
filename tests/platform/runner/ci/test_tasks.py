"""Daemon task `ci` (`agent_runner.ci.tasks`): switched on by `<HOME>/ci/area.json`, serves the CI area's socket."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.ci import tasks
from agent_runner.ci.link import LinkRefused, RunnerLink


@pytest.fixture
def home() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="cih-", dir="/tmp"))
    (path / "ci").mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(home: Path, data: Any) -> None:
    (home / "ci" / "area.json").write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def test_settings(home: Path) -> None:
    assert tasks.settings(home) is None
    bad: Any
    for bad in (
        "{",
        [],
        {"socket": "relative", "gid": 1},
        {"socket": "/x\u0000", "gid": 1},
        {"socket": "/x", "gid": True},
        {"socket": "/x", "gid": -1},
        {"socket": 5, "gid": 1},
    ):
        _write(home, bad)
        assert tasks.settings(home) is None
    _write(home, {"socket": "/run/ci.sock", "gid": 20})
    assert tasks.settings(home) == (Path("/run/ci.sock"), 20)


def _context(home: Path) -> Any:
    return SimpleNamespace(config=SimpleNamespace(home=home), log=io.StringIO(), stopping=asyncio.Event())


def test_the_task_ends_without_settings_or_work_vms(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(home)
    asyncio.run(tasks.run_ci(context))
    _write(home, {"socket": str(home / "ci.sock"), "gid": os.getgid()})
    monkeypatch.setattr(tasks, "workvms_for", lambda _context: None)
    asyncio.run(tasks.run_ci(context))
    assert "runner.ci off reason=no_workvms" in context.log.getvalue()
    monkeypatch.setattr(tasks, "workvms_for", lambda _context: object())
    from agent_runner.sessions import service  # noqa: PLC0415

    monkeypatch.setattr(service, "service_for", lambda _context: None)
    asyncio.run(tasks.run_ci(context))


def test_the_task_serves_the_area_until_the_daemon_stops(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket = home / "ci.sock"
    _write(home, {"socket": str(socket), "gid": os.getgid()})
    context = _context(home)
    enqueued: list[tuple[str, dict[str, Any]]] = []

    class Outbox:
        async def enqueue(self, _conn: Any, kind: str, body: dict[str, Any]) -> None:
            enqueued.append((kind, body))

    class Env:
        outbox = Outbox()

        @asynccontextmanager
        async def transaction(self) -> Any:
            yield None

    monkeypatch.setattr(tasks, "workvms_for", lambda _context: SimpleNamespace(deps=SimpleNamespace(db=None)))
    monkeypatch.setattr(tasks, "browsers_of", lambda _context, _workvms: None)
    from agent_runner.sessions import service  # noqa: PLC0415

    monkeypatch.setattr(service, "service_for", lambda _context: SimpleNamespace(env=Env()))
    detail = {
        "stage": "refused",
        "event": "push",
        "basis": None,
        "reason": "push_unattributable",
        "result": None,
        "size": None,
        "duration_ms": None,
        "workvm_id": None,
    }

    async def main() -> None:
        running = asyncio.ensure_future(tasks.run_ci(context))
        while not socket.exists():
            await asyncio.sleep(0.01)
        link = RunnerLink(socket)
        await link.call(
            "audit", task_id="0193000c-0000-7000-8000-0000000000c3", target_id="run:1:1", result="denied", detail=detail
        )
        with pytest.raises(LinkRefused, match="internal"):  # the stand-in work VMs cannot answer
            await link.call("capacity", size="standard")
        context.stopping.set()
        await running

    asyncio.run(main())
    assert enqueued
    assert enqueued[0][0] == "audit"
    log = context.log.getvalue()
    assert "runner.ci listening" in log
    assert "runner.ci request_failed op=capacity error=AttributeError" in log
    assert tasks.TASKS[0].name == "ci"
