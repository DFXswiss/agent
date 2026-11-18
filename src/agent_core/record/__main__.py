"""`python -m agent_core.record <command>` (RECORD-PROTOCOL.md sections 11.4 and 12.3).

The commands are `migrate` (change 1 of section 16) and `export`, `verify` and `restore` (change 3).
No command or an unknown command prints the usage line to stderr, prints nothing to stdout, reads no
environment variable, does not connect and exits 2. A known command prints its one line to stdout.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Mapping, Sequence

from .commands import run_export, run_restore, run_verify
from .migrate import STEP_CONFIG, run_migrate

# The usage line of section 11.4 after change 3, decision 82.
USAGE = "usage: python -m agent_core.record {migrate,export,verify,restore}"


def _migrate(args: Sequence[str], environ: Mapping[str, str]) -> tuple[str, int]:
    if args:
        # `migrate` takes no argument; an argument is a configuration fault (step `config`),
        # decided before any variable is read and before any connection.
        return f"failed {STEP_CONFIG}", 1
    return run_migrate(environ)


# The commands of section 11.4 after change 3, compared byte for byte with the first argument.
COMMANDS: dict[str, Callable[[Sequence[str], Mapping[str, str]], tuple[str, int]]] = {
    "migrate": _migrate,
    "export": run_export,
    "verify": run_verify,
    "restore": run_restore,
}


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = COMMANDS.get(args[0]) if args else None
    if command is None:
        # No command or an unknown command: one line to stderr, nothing to stdout, no variable
        # read, no connection, exit 2 (section 11.4).
        print(USAGE, file=sys.stderr, flush=True)
        return 2
    # The environment is chosen only for a known command; the command checks its arguments
    # before it reads a variable (section 11.4 for `migrate`, section 12.3 for the others).
    env = os.environ if environ is None else environ
    line, code = command(args[1:], env)
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
