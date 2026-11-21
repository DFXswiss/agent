"""Placeholder entry point `agent-runner` / `python -m agent_runner`; RUN-01 replaces it with the real CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agent_runner import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-runner", description="agent.dfx.swiss platform runner")
    parser.add_argument("--version", action="version", version=f"agent-runner {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
