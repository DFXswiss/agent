"""Entry point `agent-runner` / `python -m agent_runner` (docs/platform/spec/05-runner.md section 1.2)."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from agent_runner.cli import run_cli
from agent_runner.host import Host


def main(argv: Sequence[str] | None = None, host: Host | None = None) -> int:
    return run_cli(sys.argv[1:] if argv is None else argv, host or Host())


if __name__ == "__main__":
    sys.exit(main())
