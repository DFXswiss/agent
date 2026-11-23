"""Run the GitHub double from the command line.

    python -m agent_doubles.github --seed seed.json --port 0 --port-file /tmp/port --journal /tmp/journal.jsonl

``--seed`` (repeatable) loads seed files in order (format in `agent_doubles.github.repos`; ``faults``,
``expectations`` and ``device_flow`` may be part of them). ``--public-url`` names the base URL clients use when it
differs from the bound address; ``--html-url`` the base of the web links in answers (``html_url`` and the like). The
double binds only to ``--host`` (default ``127.0.0.1``), prints its base URL on the first line of standard output,
serves until SIGTERM or SIGINT, then prints its state as JSON on standard error and exits 0 when every armed fault
fired, every expectation was met and nothing unexpected arrived, 1 otherwise, 2 for invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from threading import Event

from agent_doubles.common.cli import EXIT_USAGE, add_server_arguments, poll_wait, serve_until_signal
from agent_doubles.github.server import GitHubDouble


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_doubles.github", description="GitHub double (REST subset)")
    add_server_arguments(parser)
    parser.add_argument("--seed", action="append", default=[], help="seed file (JSON); repeatable")
    parser.add_argument("--public-url", help="base URL written into answers instead of the bound address")
    parser.add_argument("--html-url", help="base URL of the web links in answers (an https site never called)")
    return parser


def main(argv: Sequence[str] | None = None, *, wait: Callable[[Event], None] = poll_wait) -> int:
    args = build_parser().parse_args(argv)
    try:
        double = GitHubDouble(
            host=args.host,
            port=args.port,
            journal_path=args.journal,
            public_url=args.public_url,
            html_url=args.html_url,
        )
    except (OSError, ValueError) as exc:
        print(f"github double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        for path in args.seed:
            with open(path, encoding="utf-8") as handle:
                double.seed(json.load(handle))
    except (OSError, ValueError) as exc:
        double.stop()
        print(f"github double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return serve_until_signal(double, args.port_file, wait)


if __name__ == "__main__":
    sys.exit(main())
