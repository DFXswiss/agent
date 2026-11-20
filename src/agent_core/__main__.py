"""Run the hub."""

from __future__ import annotations

import logging.config
import os
import time
from functools import partial

import uvicorn

from .app import create_app
from .config import Config
from .log_redact import log_config
from .record.runtime import NO_HOOKS, start_record

# The connections of the writer role that one hub process holds at most (RECORD-PROTOCOL.md
# section 11.6), and how often its background thread looks for a due periodic verification or
# checkpoint attempt (sections 12.1 and 12.2).
RECORD_POOL_SIZE = 4
RECORD_POLL_SECONDS = 1.0


def main() -> None:
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


if __name__ == "__main__":
    main()
