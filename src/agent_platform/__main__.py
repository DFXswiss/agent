"""Entry point `agent-platform` / `python -m agent_platform` (11-agent-access.md section 8)."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from agent_platform.cli import run


def main(argv: Sequence[str] | None = None) -> int:
    return run(sys.argv[1:] if argv is None else list(argv))


if __name__ == "__main__":
    sys.exit(main())
