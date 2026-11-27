"""The local socket between the CI controller and the runner (`agent_runner.ci.link`) and the listener bridge
(`agent_runner.ci.bridge`) against the sidecar double (`agent_doubles.ci.listener`)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runner.ci.bridge import Bridge, BridgeError
from agent_runner.ci.link import LinkDown, LinkRefused, RunnerLink, serve


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory with a short path (a Unix socket path is limited to about 100 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="ci-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _handler(op: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    if op == "echo":
        return {"echo": body.get("value")}
    if op == "refuse":
        raise LinkRefused("not_found")
    raise RuntimeError("secret content must not travel")


def test_a_request_is_answered_and_refusals_carry_their_code(short_dir: Path) -> None:
    path = short_dir / "ci.sock"
    failures: list[tuple[str, str]] = []

    async def main() -> None:
        (short_dir / "ci.sock").write_text("stale", encoding="utf-8")
        server = await serve(
            path, _handler, gid=os.getgid(), failed=lambda op, e: failures.append((op, type(e).__name__))
        )
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            link = RunnerLink(path)
            assert (await link.call("echo", value=3))["echo"] == 3
            with pytest.raises(LinkRefused) as refused:
                await link.call("refuse")
            assert refused.value.code == "not_found"
            with pytest.raises(LinkRefused) as internal:
                await link.call("boom")
            assert internal.value.code == "internal"
            reader, writer = await asyncio.open_unix_connection(str(path))
            for raw in (b"not json\n", b'{"op": "echo"}\n', b"[1]\n"):
                writer.write(raw)
                await writer.drain()
                assert json.loads(await reader.readline()) == {"id": 0, "ok": False, "error": "request_invalid"}
            writer.write(b"x" * 70_000 + b"\n")  # above the line limit: the connection ends
            await writer.drain()
            assert await reader.readline() == b""
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(main())
    assert failures == [("boom", "RuntimeError")]


def test_a_missing_socket_or_a_broken_answer_is_link_down(short_dir: Path) -> None:
    async def main() -> None:
        with pytest.raises(LinkDown):
            await RunnerLink(short_dir / "missing.sock").call("echo")

        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            line = json.loads(await reader.readline())
            replies = {"echo": b"not json\n", "wrong": b'{"id": 999, "ok": true}\n', "silent": b""}
            reply = replies[line["op"]]
            if reply:
                writer.write(reply)
                await writer.drain()
            else:
                await asyncio.sleep(1)
            writer.close()

        path = short_dir / "odd.sock"
        server = await asyncio.start_unix_server(answer, path=str(path))
        try:
            with pytest.raises(LinkDown, match="answer_invalid"):
                await RunnerLink(path).call("echo")
            with pytest.raises(LinkDown, match="answer_invalid"):
                await RunnerLink(path).call("wrong")
            with pytest.raises(LinkDown, match="TimeoutError"):
                await RunnerLink(path, timeout=0.05).call("silent")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(main())


def test_serve_without_a_group_keeps_the_owner_group(short_dir: Path) -> None:
    async def main() -> None:
        server = await serve(short_dir / "plain.sock", _handler, gid=None, failed=lambda _op, _e: None)
        server.close()
        await server.wait_closed()

    asyncio.run(main())


# -- the listener bridge ---------------------------------------------------------------------------------------------


def _scenario(short_dir: Path, **changes: Any) -> list[str]:
    scenario: dict[str, Any] = {
        "scale_set_id": 4,
        "batches": [
            {"available": [{"request_id": 1}, {"request_id": 2}], "statistics": {}},
            {"started": [], "message_id": 9},
        ],
        "give": [2],
        "noise": True,
        "then": "serve",
        "record": str(short_dir / "record.jsonl"),
    }
    scenario.update(changes)
    path = short_dir / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    return [sys.executable, "-m", "agent_doubles.ci.listener", str(path)]


def _recorded(short_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (short_dir / "record.jsonl").read_text(encoding="utf-8").splitlines()]


def test_the_bridge_decides_batches_and_asks_for_runners(short_dir: Path) -> None:
    logs: list[str] = []
    acquired: list[list[int]] = []
    answers: dict[str, Any] = {}

    async def main() -> int:
        bridge = Bridge(_scenario(short_dir), env=dict(os.environ), log=logs.append)

        async def on_batch(batch: Mapping[str, Any]) -> Sequence[int]:
            if batch["message_id"] == 9:
                raise RuntimeError("a failing decision still answers the message")
            return [item["request_id"] for item in batch["available"]]

        async def ask() -> None:
            answers["jit"] = await bridge.jit("agent-ci-wvm-00000001")
            await bridge.remove(5)
            await bridge.capacity(-3)
            assert bridge.process is not None
            assert bridge.process.stdin is not None
            bridge.process.stdin.close()

        async def on_acquired(ids: Sequence[int]) -> None:
            acquired.append(list(ids))
            if len(acquired) == 2:  # jit and remove wait for the reader: they run beside it, as in the controller
                answers["task"] = asyncio.ensure_future(ask())

        code = await bridge.run(on_batch, on_acquired)
        await answers["task"]
        return code

    assert asyncio.run(main()) == 0
    assert acquired == [[2], []]
    assert answers["jit"] == "jit-agent-ci-wvm-00000001"
    assert "ci.listener ready scale_set_id=4" in logs
    assert any(line.startswith("ci.listener batch_failed error=RuntimeError") for line in logs)
    lines = _recorded(short_dir)
    assert {"type": "capacity", "max": 0} in lines
    assert lines[0] == {"type": "acquire", "message_id": 0, "request_ids": [1, 2]}


def test_requests_fail_when_the_sidecar_refuses_or_is_gone(short_dir: Path) -> None:
    async def main() -> None:
        bridge = Bridge(_scenario(short_dir, batches=[], jit_fail=True), answer_timeout=5)
        errors: list[str] = []

        async def on_batch(_batch: Mapping[str, Any]) -> Sequence[int]:
            return []

        async def on_acquired(_ids: Sequence[int]) -> None:
            return None

        run = asyncio.ensure_future(bridge.run(on_batch, on_acquired))
        await bridge.ready.wait()
        try:
            await bridge.jit("x")
        except BridgeError as error:
            errors.append(error.code)
        assert bridge.process is not None
        assert bridge.process.stdin is not None
        bridge.process.stdin.close()
        assert await run == 0
        for call in (bridge.jit("y"), bridge.remove(1), bridge.capacity(1)):
            try:
                await call
            except BridgeError as error:
                errors.append(error.code)
        assert errors == ["github_error", "listener_down", "listener_down", "listener_down"]

    asyncio.run(main())


def test_unanswered_and_odd_answers(short_dir: Path) -> None:
    script = short_dir / "odd.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'ready', 'scale_set_id': 1}), flush=True)\n"
        "for raw in sys.stdin:\n"
        "    line = json.loads(raw)\n"
        "    if line.get('name') == 'silent':\n"
        "        continue\n"
        "    if line['type'] == 'jit':\n"
        "        print(json.dumps({'type': 'removed', 'ref': line['ref']}), flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'type': 'jit', 'ref': line['ref'], 'encoded': 'x'}), flush=True)\n"
        "        print(json.dumps({'type': 'acquired', 'request_ids': 'no list'}), flush=True)\n"
        "        print(json.dumps({'type': 'jit', 'ref': 'unknown'}), flush=True)\n",
        encoding="utf-8",
    )

    async def main() -> None:
        bridge = Bridge([sys.executable, str(script)], answer_timeout=0.2)

        async def nothing(_value: Any) -> Any:
            return []

        run = asyncio.ensure_future(bridge.run(nothing, nothing))
        await bridge.ready.wait()
        with pytest.raises(BridgeError, match="answer_invalid"):
            await bridge.jit("odd")
        with pytest.raises(BridgeError, match="answer_invalid"):
            await bridge.remove(1)
        with pytest.raises(BridgeError, match="timeout"):
            await bridge.jit("silent")
        assert bridge.process is not None
        assert bridge.process.stdin is not None
        bridge.process.stdin.close()
        await run

    asyncio.run(main())


def test_a_sidecar_that_exits_by_itself_is_reported(short_dir: Path) -> None:
    async def main() -> int:
        bridge = Bridge(_scenario(short_dir, batches=[{"available": []}], then="exit", exit_code=3, noise=False))

        async def on_batch(_batch: Mapping[str, Any]) -> Sequence[int]:
            return []

        async def on_acquired(_ids: Sequence[int]) -> None:
            return None

        return await bridge.run(on_batch, on_acquired)

    assert asyncio.run(main()) == 3


def _script(short_dir: Path, body: str) -> list[str]:
    path = short_dir / "sidecar.py"
    path.write_text(
        "import json, os, sys, time\nprint(json.dumps({'type': 'ready', 'scale_set_id': 1}), flush=True)\n" + body,
        encoding="utf-8",
    )
    return [sys.executable, str(path)]


async def _nothing(_value: Any) -> Any:
    return []


def test_a_sidecar_that_stopped_reading_is_listener_down(short_dir: Path) -> None:
    argv = _script(short_dir, "os.close(0)\ntime.sleep(0.5)\n")

    async def main() -> None:
        bridge = Bridge(argv)
        run = asyncio.ensure_future(bridge.run(_nothing, _nothing))
        await bridge.ready.wait()
        await asyncio.sleep(0.1)
        codes: list[str] = []
        for _ in range(50):  # the first writes may land in the pipe before the closed end shows
            try:
                await bridge.capacity(1)
            except BridgeError as error:
                codes.append(error.code)
                break
            await asyncio.sleep(0.01)
        assert codes == ["listener_down"]
        await run

    asyncio.run(main())


def test_a_question_open_when_the_sidecar_exits_fails(short_dir: Path) -> None:
    argv = _script(short_dir, "sys.stdin.readline()\n")

    async def main() -> None:
        bridge = Bridge(argv, answer_timeout=10)
        run = asyncio.ensure_future(bridge.run(_nothing, _nothing))
        await bridge.ready.wait()
        with pytest.raises(BridgeError, match="listener_down"):
            await bridge.jit("never-answered")
        await run

    asyncio.run(main())


def test_a_sidecar_that_closes_its_output_is_terminated(short_dir: Path) -> None:
    argv = _script(short_dir, "os.close(1)\ntime.sleep(30)\n")

    async def main() -> int:
        return await Bridge(argv).run(_nothing, _nothing)

    assert asyncio.run(main()) != 0


def test_the_double_in_process(short_dir: Path) -> None:
    from io import StringIO  # noqa: PLC0415

    from agent_doubles.ci import listener  # noqa: PLC0415

    out = StringIO()
    assert listener.serve({"batches": [{"available": []}], "exit_code": 4}, StringIO(""), out) == 4
    assert json.loads(out.getvalue().splitlines()[-1])["type"] == "batch"
    out = StringIO()
    source = StringIO('[1]\n{"type": "acquire", "message_id": 0, "request_ids": [1]}\n')
    assert listener.serve({"batches": [{}], "then": "serve"}, source, out) == 0
    path = short_dir / "s.json"
    path.write_text(json.dumps({"batches": []}), encoding="utf-8")
    import sys as system  # noqa: PLC0415

    saved = system.stdin
    system.stdin = StringIO("")
    try:
        assert listener.main([str(path)]) == 0
    finally:
        system.stdin = saved


def test_the_double_ends_when_the_controller_closes_during_a_batch(short_dir: Path) -> None:
    argv = _scenario(short_dir, batches=[{"available": []}], exit_code=0, record=None)
    import subprocess  # noqa: PLC0415 - a plain child: the controller side closes at once

    finished = subprocess.run(argv, input="", capture_output=True, text=True, check=False, timeout=30)
    assert finished.returncode == 0
    assert json.loads(finished.stdout.splitlines()[0])["type"] == "ready"
