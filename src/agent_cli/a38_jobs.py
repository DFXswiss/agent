"""CLI entry for reusable A38 local job adapters.

Invoked as ``agent a38 job ADAPTER --config JSON``. The central policy loader
normalizes structured executor configuration into this canonical CLI command; the
legacy report wire format remains unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Mapping

from .a38_job_adapters import ADAPTERS
from .a38_job_adapters.commands import run_commands
from .a38_job_adapters.common import JobError
from .a38_job_adapters.compose import run_compose
from .a38_job_adapters.http_smoke import run_http_smoke
from .a38_job_adapters.immutable import run_immutable

_RUNNERS = {
    "commands": run_commands,
    "compose": run_compose,
    "http-smoke": run_http_smoke,
    "immutable": run_immutable,
}


def run_job(
    adapter: str,
    config_text: str,
    *,
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Validate nested config then execute the named adapter. Returns exit status."""
    if adapter not in ADAPTERS:
        raise JobError(f"unknown adapter: {adapter}")
    runner = _RUNNERS[adapter]
    return int(
        runner(
            config_text,
            cwd=cwd,
            lock_root=lock_root,
            environ=environ,
        )
    )


def add_job_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``job`` under an existing ``a38`` parser."""
    job_p = subparsers.add_parser(
        "job",
        help="Run a reusable local job adapter with inline JSON config",
    )
    job_p.add_argument(
        "adapter",
        choices=sorted(ADAPTERS),
        help="Adapter name: commands, compose, http-smoke, or immutable",
    )
    job_p.add_argument(
        "--config",
        required=True,
        help="Inline JSON object configuring the adapter (no shell expansion)",
    )
    job_p.set_defaults(func=_cmd_job)


def _cmd_job(args: argparse.Namespace) -> int:
    try:
        return run_job(str(args.adapter), str(args.config))
    except JobError as exc:
        print(f"a38: {exc}", file=__import__("sys").stderr)
        return 1
