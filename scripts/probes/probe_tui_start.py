"""Probe: Claude Code TUI start under Landlock, compared without Landlock (PRB-01, WP-M0-14; 05-runner.md §6.1).

Run by hand on the runner device from the repository root (not part of `make platform-ci`):

    python scripts/probes/probe_tui_start.py --golden <stopped golden> --prefix <vm prefix> [--runs 10] [--out DIR]

It clones one probe VM `<prefix>tui-<hex>` from the golden (4 CPU / 8 GiB, token parameter like a pool clone) with
a one-time Lima system provisioning step that runs `tui_guest.py` as root once the guest API is up. The guest half
measures `--runs` starts of each variant (`landlock`: the real session unit with the Landlock launcher; `plain`:
the same unit without the launcher) and leaves its JSON result in `/run/prb-tui/result.json`, which this script
reads with `limactl shell`. Output: `<out>/tui-start.csv` (one row per start), `<out>/tui-start.json` (summary) and
JSON lines on standard output. The VM is deleted at the end, also on failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from probelib.report import markdown_table, summarize_series, write_csv

from agent_runner.vm import lima
from agent_runner.vm.pool import TOKEN_PARAM

RESULT = "/run/prb-tui/result.json"
GUEST = Path(__file__).resolve().with_name("tui_guest.py")
COLUMNS = ("variant", "run", "paint_ms", "transcript_ms")


def provision_set(runs: int, timeout: float) -> str:
    """The yq expression that adds the guest half as a one-time system provisioning step."""
    program = base64.b64encode(GUEST.read_bytes()).decode("ascii")
    inner = (
        "i=0; until curl -fsS --max-time 2 -o /dev/null http://127.0.0.1:7700/v1/health; do"
        ' i=$((i+1)); [ "$i" -ge 300 ] && break; sleep 0.2; done;'
        " cd /opt/agent-guest && python3 -E -s /run/prb-tui/tui_guest.py"
        f" {int(runs)} {float(timeout)} >/run/prb-tui/result.part 2>/run/prb-tui/stderr;"
        " mv -f /run/prb-tui/result.part /run/prb-tui/result.json; chmod 0644 /run/prb-tui/result.json"
        " /run/prb-tui/stderr"
    )
    script = (
        "#!/bin/sh\nset -u\n"
        "[ -e /var/lib/prb-tui.started ] && exit 0\ntouch /var/lib/prb-tui.started\n"
        "install -d -m 0755 /run/prb-tui\n"
        f"echo {program} | base64 -d >/run/prb-tui/tui_guest.py\n"
        "systemd-run --no-block --quiet --unit=prb-tui-probe --property=Type=oneshot"
        f" --property=After=agent-guest-api.service /bin/sh -c '{inner}'\n"
    )
    return ".provision += [" + json.dumps({"mode": "system", "script": script}) + "]"


def emit(kind: str, **fields: Any) -> None:
    print(json.dumps({"kind": kind, **fields}, sort_keys=True), flush=True)


def wait_result(machine: lima.Lima, name: str, limit_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    while True:
        done = machine.shell(name, ["cat", RESULT], check=False)
        if done.returncode == 0 and done.stdout.strip():
            value = json.loads(done.stdout)
            if not isinstance(value, dict):
                raise SystemExit("guest result is not an object")
            return value
        if time.monotonic() >= deadline:
            err = machine.shell(name, ["cat", "/run/prb-tui/stderr"], check=False)
            raise SystemExit(f"no TUI probe result within {limit_s} s: {err.stdout[-2000:]}")
        time.sleep(2.0)


def summarize(result: dict[str, Any]) -> list[Any]:
    rows = []
    for variant in ("landlock", "plain"):
        for metric in ("paint_ms", "transcript_ms"):
            values = [float(r[metric]) for r in result["runs"] if r["variant"] == variant and r[metric] is not None]
            if values:
                rows.append(summarize_series(f"{variant}.{metric}", values, budget_s=None, reference="ms"))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0, help="per start, seconds")
    parser.add_argument("--out", type=Path, default=Path("probe-out"))
    args = parser.parse_args()
    machine = lima.Lima(lima.run_limactl, prefix=args.prefix)
    name = machine.name(f"tui-{secrets.token_hex(4)}")
    sets = [".cpus = 4", '.memory = "8GiB"', provision_set(args.runs, args.timeout)]
    try:
        machine.clone(args.golden, name, sets=sets)
        machine.start(name, params={TOKEN_PARAM: secrets.token_urlsafe(32)})
        emit("vm", name=name, golden=args.golden)
        result = wait_result(machine, name, limit_s=60 + 2 * args.runs * (args.timeout + 10))
    finally:
        machine.delete(name)
    rows = [{key: run.get(key) for key in COLUMNS} for run in result["runs"]]
    write_csv(args.out / "tui-start.csv", COLUMNS, rows)
    table = summarize(result)
    summary = {
        "claude_version": result.get("claude_version"),
        "landlock_abi": result.get("landlock_abi"),
        "kernel": result.get("kernel"),
        "missing": [row for row in rows if row["paint_ms"] is None or row["transcript_ms"] is None],
        "rows": {row.name: row.distribution.row(1) for row in table},
    }
    (args.out / "tui-start.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    emit("summary", **summary)
    print(markdown_table(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
