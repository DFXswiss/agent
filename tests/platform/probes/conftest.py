"""Fixtures of the early-measurement probes (PRB-01): the evaluation library `scripts/probes/probelib` is imported
from the checkout (the probes are scripts, not an installed package)."""

from __future__ import annotations

import sys
from pathlib import Path

PROBES_DIR = Path(__file__).resolve().parents[3] / "scripts" / "probes"

if str(PROBES_DIR) not in sys.path:
    sys.path.insert(0, str(PROBES_DIR))
