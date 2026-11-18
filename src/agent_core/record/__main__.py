"""`python -m agent_core.record <command>` (RECORD-PROTOCOL.md sections 11.4 and 12.3).

Change 1 of section 16 builds the command `migrate`; `export`, `verify` and `restore` follow with
change 3.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

from .migrate import STEP_CONFIG, run_migrate

# The usage line of section 11.4 after change 1 (and change 2), decision 82.
USAGE = "usage: python -m agent_core.record {migrate}"


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "migrate":
        # No command or an unknown command: one line to stderr, nothing to stdout, no variable
        # read, no connection, exit 2 (section 11.4).
        print(USAGE, file=sys.stderr, flush=True)
        return 2
    if len(args) > 1:
        # `migrate` takes no argument; an argument is a configuration fault (step `config`).
        print(f"failed {STEP_CONFIG}", flush=True)
        return 1
    env = os.environ if environ is None else environ
    line, code = run_migrate(env)
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
