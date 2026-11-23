"""Run the model double from the command line.

    python -m agent_doubles.model --scenario atk-01 --port 0 --port-file /tmp/port --journal /tmp/journal.jsonl
    python -m agent_doubles.model --scenario path/to/scenario.json --access api_key --token-env MODEL_DOUBLE_TOKEN

``--scenario`` names a scenario file or, when no such file exists, a packaged scenario (``scenarios/<name>.json``).
``--access KIND --token-env NAME`` makes the double require the credential of that access kind with the token held
in the environment variable, so the token never appears in argv. ``--var NAME=VALUE`` overrides a template
variable of the scenario. The double binds only to ``--host`` (default ``127.0.0.1``), prints its base URL on the
first line of standard output, serves until SIGTERM or SIGINT, then prints its state as JSON on standard error and
exits 0 when every step was consumed without a failure, 1 otherwise, 2 for invalid arguments.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event

from agent_doubles.common.cli import EXIT_USAGE, add_server_arguments, poll_wait, serve_until_signal
from agent_doubles.model.access import KINDS, Access, AccessError
from agent_doubles.model.scenario import Scenario, ScenarioError, load_scenario, packaged_scenario
from agent_doubles.model.server import ModelDouble


def resolve_scenario(value: str) -> Scenario:
    """The scenario file `value`, else the packaged scenario of that name."""
    path = Path(value)
    if path.is_file():
        return load_scenario(path)
    return packaged_scenario(value)


def parse_variables(values: Sequence[str]) -> dict[str, str]:
    """``NAME=VALUE`` arguments as a mapping."""
    out: dict[str, str] = {}
    for value in values:
        name, sep, text = value.partition("=")
        if not sep or not name:
            raise ScenarioError(f"--var needs NAME=VALUE, got {value!r}")
        out[name] = text
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_doubles.model", description="Model double (Anthropic Messages API)"
    )
    parser.add_argument("--scenario", required=True, help="scenario file or packaged scenario name")
    add_server_arguments(parser)
    parser.add_argument("--access", choices=KINDS, help="required credential kind")
    parser.add_argument("--token-env", help="environment variable holding the required token")
    parser.add_argument("--key-token-env", help="environment variable holding the platform key GET /v1/key requires")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="override a template variable")
    return parser


def main(argv: Sequence[str] | None = None, *, wait: Callable[[Event], None] = poll_wait) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario = resolve_scenario(args.scenario)
        variables = parse_variables(args.var)
        access = None
        if args.access is not None or args.token_env is not None:
            if args.access is None or args.token_env is None:
                raise AccessError("--access and --token-env go together")
            access = Access(args.access, os.environ.get(args.token_env, ""))
        key_token = None
        if args.key_token_env is not None:
            key_token = os.environ.get(args.key_token_env, "")
            if not key_token:
                raise AccessError("--key-token-env names a variable that must not be empty")
        double = ModelDouble(
            scenario,
            host=args.host,
            port=args.port,
            journal_path=args.journal,
            access=access,
            variables=variables,
            key_token=key_token,
        )
    except (OSError, ValueError) as exc:
        print(f"model double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return serve_until_signal(double, args.port_file, wait)


if __name__ == "__main__":
    sys.exit(main())
