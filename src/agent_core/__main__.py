"""Run the hub, or one of the operator commands of PASSKEY-PROTOCOL.md section 5.3.

`python -m agent_core` without an argument starts the hub exactly as before. `invite-bootstrap` and
`invite-list` run their commands and never start the hub. Any other first argument, and an unknown
or repeated option, prints a usage line to standard error, starts nothing and exits with status 2.
"""

from __future__ import annotations

import logging.config
import os
import sys
import time
from functools import partial
from typing import Sequence

import uvicorn

from .app import create_app
from .config import Config
from .log_redact import log_config
from .passkey.operator import run_command, system_streams
from .record.runtime import NO_HOOKS, start_record
from .signing import system_clock

# The connections of the writer role that one hub process holds at most (RECORD-PROTOCOL.md
# section 11.6), and how often its background thread looks for a due periodic verification or
# checkpoint attempt (sections 12.1 and 12.2).
RECORD_POOL_SIZE = 4
RECORD_POLL_SECONDS = 1.0


def run_hub() -> None:
    cfg = Config.from_env()
    # The record start of RECORD-PROTOCOL.md section 11.4 logs before uvicorn runs; its lines go to
    # the same handlers as every later line.
    logging.config.dictConfig(log_config())
    record = partial(
        start_record,
        os.environ,
        monotonic=time.monotonic,
        pool_size=RECORD_POOL_SIZE,
        poll_seconds=RECORD_POLL_SECONDS,
        hooks=NO_HOOKS,
    )
    app = create_app(cfg, record=record)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", log_config=log_config())


def main(argv: Sequence[str] | None = None) -> None:
    """Without an argument the hub; otherwise the operator command, whose status ends the process."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        run_hub()
        return
    raise SystemExit(run_command(args, system_streams(), system_clock))


if __name__ == "__main__":
    main()
