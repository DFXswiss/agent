"""`python -m agent_hub.db migrate` (entry point until the main CLI of HUB-02 discovers `db/cli.py`)."""

from __future__ import annotations

import sys

from agent_hub.db.cli import main

if __name__ == "__main__":
    sys.exit(main())
