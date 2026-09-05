"""Reusable A38 local job adapters (commands, compose, http-smoke, immutable)."""

from __future__ import annotations

from .commands import run_commands
from .compose import run_compose
from .http_smoke import run_http_smoke
from .immutable import run_immutable

ADAPTERS = frozenset({"commands", "compose", "http-smoke", "immutable"})

__all__ = [
    "ADAPTERS",
    "run_commands",
    "run_compose",
    "run_http_smoke",
    "run_immutable",
]
