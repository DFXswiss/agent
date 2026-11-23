"""`python -m agent_hub` / console script `agent-hub` (see `agent_hub.cli`)."""

from __future__ import annotations

import sys

from agent_hub.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())
