"""Probe: session start end to end on the clone path and the pool path (PRB-01, WP-M0-14; 05-runner.md §3.2, §6.1).

Run by hand on the runner device from the repository root (not part of `make platform-ci`):

    python scripts/probes/probe_clone_path.py --golden <stopped golden> --prefix <vm prefix> [--runs 10] [--out DIR]

With the runner's own VM code (`VmManager` of VM-02 over real `limactl`, own PostgreSQL cluster) and the real guest
of the golden, every run times (host monotonic clock, marks of `probelib.timeline`):

* clone path (pool size 0): `ensure_vm` = clone -> start with the control-token parameter (boot + harden unit) ->
  guest health `unassigned` -> `POST /v1/assign`; then, after the excluded test setup (model double started inside
  the VM, journal head read), `POST /v1/sessions` (initial prompt on the command line) -> `PUT .../credential`
  (platform access to the double) -> the guest starts the unit (`agent_starting`) -> first hook (TUI up) ->
  `prompt_accepted` -> first answered model request;
* pool path (pool size 1): one pool VM is filled and its double started before `begin` (not hot path); then
  `ensure_vm` takes it (`POST /v1/assign`) and the same session steps follow.

After each run the session is stopped and the workgroup VM deleted (outside the timing). Output:
`<out>/clone-path.csv` (raw marks, one row per run, seconds since `begin`, plus the guest's own durations),
`<out>/clone-path.json` and `<out>/clone-path.md` (step distributions against the budgets, go/no-go).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from probehost import (
    DOUBLE_MODEL,
    ORG,
    Runner,
    Watcher,
    credential,
    double_session,
    doubles_archive,
    emit,
    runner_vms,
    setup_double,
    uuid7,
)
from probelib.report import markdown_table, summarize_path, verdict, write_csv
from probelib.timeline import (
    CLONE_MARKS,
    CLONE_STEPS,
    POOL_MARKS,
    POOL_STEPS,
    TOTAL_BUDGET_S,
    check_order,
    end_to_end,
)

from agent_runner.guestlink.client import GuestClient
from agent_runner.vm import records
from agent_runner.vm.records import GoldenRef, VmRow

PROMPT = "What is 2+2?"
SESSION_TIMEOUT_S = 60.0
# Hooks after the answer (Stop, ...) are collected this long, outside the timing.
TAIL_S = 3.0
EXTRA = ("hook_event", "hook_events", "guest_tui_start_s", "guest_prompt_accept_s", "failure")
COLUMNS = ("path", "run", *CLONE_MARKS, *EXTRA)


def _seconds(first: str | None, last: str | None) -> float | None:
    if first is None or last is None:
        return None
    return round((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds(), 4)


async def journal_head(client: GuestClient) -> int:
    return (await client.journal(0, limit=1, wait=0)).head


async def session_run(runner: Runner, row: VmRow, port: int, after: int) -> dict[str, Any]:
    """Session steps on an assigned VM; returns the extra columns."""
    sid = uuid7()
    async with await runner.manager.client(row) as client, await runner.manager.client(row) as follower:
        watcher = Watcher(follower, sid, runner.marks, after)
        task = asyncio.create_task(watcher.run())
        runner.marks.set("session_begin")
        await client.create_session(double_session(sid, PROMPT))
        runner.marks.set("session_created")
        body = credential(
            f"http://127.0.0.1:{port}", "Bearer " + secrets.token_urlsafe(24), access="platform",
            models=[(DOUBLE_MODEL, 200_000)],
        )  # fmt: skip
        await client.put_credential(sid, body)
        runner.marks.set("credential_put")
        try:
            await asyncio.wait_for(watcher.done.wait(), SESSION_TIMEOUT_S)
        except TimeoutError:
            watcher.failure = watcher.failure or "timeout"
        finally:
            runner.marks.stop()
            await asyncio.sleep(TAIL_S)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.control(sid, "stop")
    at = watcher.guest_at
    return {
        "hook_event": watcher.hook_event,
        "hook_events": "+".join(dict.fromkeys(watcher.hooks)),
        "guest_tui_start_s": _seconds(at.get("agent_starting"), at.get("first_hook")),
        "guest_prompt_accept_s": _seconds(at.get("first_hook"), at.get("prompt_accepted")),
        "failure": watcher.failure,
    }


async def clone_runs(runner: Runner, archive: Path, runs: int) -> list[dict[str, Any]]:
    out = []
    runner.env.settings = replace(runner.env.settings, pool_size=0)
    for run in range(1, runs + 1):
        wg = uuid7()
        runner.marks.begin()
        row = await runner.manager.ensure_vm(ORG, wg)
        runner.marks.set("assigned")
        port = await asyncio.to_thread(setup_double, runner.machine, row.lima_name, archive)
        async with await runner.manager.client(row) as client:
            after = await journal_head(client)
        extra = await session_run(runner, row, port, after)
        out.append({"path": "clone", "run": run, **runner.marks.values, **extra})
        emit("run", **out[-1])
        await runner.manager.delete_workgroup(wg)
    return out


async def pool_runs(runner: Runner, archive: Path, runs: int) -> list[dict[str, Any]]:
    out = []
    runner.env.settings = replace(runner.env.settings, pool_size=1)
    golden = await _golden(runner)
    for run in range(1, runs + 1):
        pooled = await runner.manager.pool.fill_one(golden)
        port = await asyncio.to_thread(setup_double, runner.machine, pooled.lima_name, archive)
        async with await runner.manager.client(pooled) as client:
            after = await journal_head(client)
        wg = uuid7()
        runner.marks.begin()
        row = await runner.manager.ensure_vm(ORG, wg)
        runner.marks.set("assigned")
        if row.id != pooled.id:
            raise SystemExit("ensure_vm did not take the pool VM")
        extra = await session_run(runner, row, port, after)
        out.append({"path": "pool", "run": run, **runner.marks.values, **extra})
        emit("run", **out[-1])
        await runner.manager.delete_workgroup(wg)
    return out


async def _golden(runner: Runner) -> GoldenRef:
    found = await records.ready_golden(runner.env.db)
    if found is None:
        raise SystemExit("no ready golden")
    return found


def report(rows: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    write_csv(out / "clone-path.csv", COLUMNS, rows)
    summary: dict[str, Any] = {"failed": [row for row in rows if row.get("failure")]}
    text = []
    for path, steps, marks in (("clone", CLONE_STEPS, CLONE_MARKS), ("pool", POOL_STEPS, POOL_MARKS)):
        good = [
            {key: float(value) for key, value in row.items() if key in marks and value is not None}
            for row in rows
            if row["path"] == path and not row.get("failure")
        ]
        failed = sum(1 for row in rows if row["path"] == path and row.get("failure"))
        if not good:
            if failed and path == "clone":
                summary["verdict"] = f"No-Go: all {failed} clone run(s) failed"
                text.append(f"**{summary['verdict']}**\n")
            continue
        for run in good:
            check_order(run, marks)
        table = summarize_path(good, steps, total_budget_s=TOTAL_BUDGET_S[path])
        summary[path] = {row.name: {**row.distribution.row(3), "within": row.within} for row in table}
        text.append(f"### {path} path ({len(good)} runs)\n\n{markdown_table(table)}")
        if path == "clone":
            found = verdict([end_to_end(run) for run in good], failed=failed)
            summary["verdict"] = found.text
            text.append(f"**{found.text}**\n")
    (out / "clone-path.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "clone-path.md").write_text("\n".join(text), encoding="utf-8")
    return summary


async def scenario(args: argparse.Namespace) -> list[dict[str, Any]]:
    src = Path(__file__).resolve().parents[2] / "src"
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="prb01-doubles-") as scratch:
        archive = doubles_archive(src, Path(scratch) / "doubles.tgz")
        async with runner_vms(args.golden, args.prefix, pool_size=0) as runner:
            if "clone" in args.paths:
                rows += await clone_runs(runner, archive, args.runs)
            if "pool" in args.paths:
                rows += await pool_runs(runner, archive, args.runs)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--paths", default="clone,pool")
    parser.add_argument("--out", type=Path, default=Path("probe-out"))
    args = parser.parse_args()
    args.paths = args.paths.split(",")
    rows = asyncio.run(scenario(args))
    summary = report(rows, args.out)
    emit("summary", **summary)
    print((args.out / "clone-path.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
