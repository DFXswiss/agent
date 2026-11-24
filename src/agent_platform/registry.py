"""The command modules of `agent-platform` beyond the ones in `cli` (11-agent-access.md section 8.4).

Each module has `register(tree: commands.Tree) -> None`; `cli.build_parser` calls them in this order after its own
groups. A new module adds one line here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MODULES: tuple[Callable[[Any], None], ...] = ()
