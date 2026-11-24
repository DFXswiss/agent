"""`python -m orchestrator` (called by `e2e/platform/run.sh`)."""

import sys

from orchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
