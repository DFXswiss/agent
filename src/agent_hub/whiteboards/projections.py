"""Projection rebuilders of the whiteboards area (ADR-027 section 4: `<area>/projections.py:REBUILDERS`).

`whiteboards` recomputes `whiteboard_entry` from the stored whiteboard events (`agent_hub.whiteboards.projection`)
inside the caller's transaction and returns the number of entries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from agent_hub.config import HubConfig
from agent_hub.whiteboards import projection

Rebuilder = Callable[[psycopg.Connection[Any], HubConfig], int]


def rebuild_whiteboards(connection: psycopg.Connection[Any], _config: HubConfig) -> int:
    """Rebuild every whiteboard entry; the number of entries."""
    return projection.rebuild(connection)


REBUILDERS: Mapping[str, Rebuilder] = {"whiteboards": rebuild_whiteboards}
