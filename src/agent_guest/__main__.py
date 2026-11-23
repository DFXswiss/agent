"""Console script ``agent-guest``: ``agent-guest control`` / ``agent-guest api`` start the two guest processes.

The systemd units start the modules directly (``python3 -m agent_guest.control`` and ``python3 -m
agent_guest.server``, 05-runner.md section 5.1); this entry point is the same for manual use.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

from agent_guest import __version__

MODULES = {"control": "agent_guest.control", "api": "agent_guest.server"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-guest", description="agent.dfx.swiss guest agent")
    parser.add_argument("--version", action="version", version=f"agent-guest {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("control", help="run agent-guest-control (root; control socket, api.sock, journal)")
    commands.add_parser("api", help="run agent-guest-api (user platform; 127.0.0.1:7700)")
    arguments = parser.parse_args(argv)
    # Imported per command: the API process must never load the journal modules of the control process.
    if arguments.command in MODULES:
        module = importlib.import_module(MODULES[arguments.command])
        return int(module.main([]))
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
