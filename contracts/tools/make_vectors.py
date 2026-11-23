"""Write or check the shared crypto test vectors in contracts/vectors/ (docs/platform/spec/04-crypto.md section 10).

Usage: `python contracts/tools/make_vectors.py [--check]`. The generator logic lives in `agent_hub.crypto.vectors`
(ADR-027 section 2); further sections register themselves as `contracts/tools/vectors_*.py` or
`agent_hub/<area>/vectors.py` with a `SECTIONS` list.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_hub.crypto.vectors import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], Path(__file__).resolve().parent.parent))
