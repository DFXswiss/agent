"""`e2e/platform/run.sh` options (07-testing.md section 7, `tools/wp/e2e.sh`):

    run.sh --topology t1|t2 [--runner <ssh target>] [--only E2E-06,…] [--repeat N] [--live [--live-stdin]]
           [--project desktop|mobile]

`--live-stdin` (live runs only, LVT-01): the operator's live values arrive on standard input instead of the command
line or a file - one `NAME=value` line each, `E2E_LIVE_*` (secrets: Playwright gets them, the artefact scan looks for
them) or `AGENT_LIVE_*` (settings), read up to the end of the input or an empty line (`e2e/platform/live/driver.py`
writes them). They override the environment.

Exit codes: 0 green, 1 a failure (details above the last line and in the artefacts), 2 invalid options or
environment, 3 a pre-check failed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from orchestrator.config import PROJECTS, TOPOLOGIES, ConfigError, build_config
from orchestrator.run import Run, Tools, install_sigterm

EXIT_USAGE = 2
CORE_SRC = Path(__file__).resolve().parents[3]
LIVE_NAME = re.compile(r"^(?:E2E_LIVE|AGENT_LIVE)_[A-Z0-9_]{1,64}$")
LIVE_VALUE = re.compile(r"^[ -~]{1,8192}$")
MAX_LIVE_LINES = 64


class LiveInputError(ValueError):
    """A line of `--live-stdin` is not `NAME=value` with an allowed name (the value is never shown)."""


def read_live_values(lines: Iterable[str]) -> dict[str, str]:
    """`NAME=value` lines up to the end or an empty line; names `E2E_LIVE_*`/`AGENT_LIVE_*`, printable values."""
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line:
            break
        name, separator, value = line.partition("=")
        if number > MAX_LIVE_LINES or not separator or not LIVE_NAME.fullmatch(name) or not LIVE_VALUE.fullmatch(value):
            raise LiveInputError(f"--live-stdin: line {number} is not NAME=value with an E2E_LIVE_/AGENT_LIVE_ name")
        values[name] = value
    return values


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="e2e/platform/run.sh", description="platform E2E suite (orchestrator + Playwright)"
    )
    p.add_argument("--topology", required=True, choices=TOPOLOGIES)
    p.add_argument("--runner", help="SSH target of the runner device (t2 only, LAN)")
    p.add_argument("--only", help="comma-separated test IDs, e.g. E2E-06,E2E-08")
    p.add_argument("--repeat", type=int, default=1, help="consecutive complete runs (fresh services each time)")
    p.add_argument("--live", action="store_true", help="include the @live tests (real providers)")
    p.add_argument("--live-stdin", action="store_true", help="read the live NAME=value lines from standard input")
    p.add_argument("--project", default="desktop", choices=PROJECTS)
    p.add_argument("--artifacts", help="artefact directory (default E2E_ARTIFACTS_DIR or e2e/platform/artifacts)")
    return p


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    tools: Tools | None = None,
    stdin: Iterable[str] | None = None,
) -> int:
    args = parser().parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    if args.live_stdin:
        if not args.live:
            print("e2e: --live-stdin needs --live", file=sys.stderr)
            return EXIT_USAGE
        try:
            env.update(read_live_values(stdin if stdin is not None else sys.stdin))
        except LiveInputError as exc:
            print(f"e2e: {exc}", file=sys.stderr)
            return EXIT_USAGE
    try:
        config = build_config(
            topology=args.topology,
            environ=env,
            core_src=Path(env.get("AGENT_CORE_SRC") or CORE_SRC).expanduser(),
            runner=args.runner,
            only=args.only,
            repeat=args.repeat,
            live=args.live,
            project=args.project,
            artifacts=args.artifacts,
        )
    except ConfigError as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return EXIT_USAGE
    install_sigterm()
    return Run(config, env, tools).execute()
