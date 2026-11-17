"""`python -m agent_core.record <command>` (RECORD-PROTOCOL.md sections 11.4 and 12.3).

Change 1 of section 16 builds the command `migrate`; `export`, `verify` and `restore` follow with
change 3.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

from .migrate import STEP_CONFIG, run_migrate

USAGE = "usage: python -m agent_core.record migrate"


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    if not args or args[0] != "migrate":
        print(USAGE, file=sys.stderr)
        return 2
    if len(args) > 1:
        # `migrate` takes no argument; an argument is a configuration fault.
        print(f"failed {STEP_CONFIG}", flush=True)
        return 1
    line, code = run_migrate(env)
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
