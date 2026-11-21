"""Placeholder entry point `agent-guest` / `python -m agent_guest`; GST-01 replaces it with the real CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agent_guest import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-guest", description="agent.dfx.swiss guest agent")
    parser.add_argument("--version", action="version", version=f"agent-guest {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
