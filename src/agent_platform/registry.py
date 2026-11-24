"""The command modules of `agent-platform` beyond the ones in `cli` (11-agent-access.md section 8.4).

Each module has `register(tree: commands.Tree) -> None`; `cli.build_parser` calls them in this order after its own
groups. A new module adds one line here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_platform import (
    alone,
    billing,
    community,
    connector,
    credkey,
    creds,
    devices,
    identities,
    inbox,
    keywraps,
    membership,
    ownmac,
    security,
    sessions,
    wgsettings,
    whiteboards,
    workvm,
)

MODULES: tuple[Callable[[Any], None], ...] = (
    membership.register,
    devices.register,
    credkey.register,
    whiteboards.register,
    sessions.register,
    wgsettings.register,
    inbox.register,
    security.register,
    creds.register,
    identities.register,
    keywraps.register,
    workvm.register,
    billing.register,
    community.register,
    alone.register,
    ownmac.register,
    connector.register,
)
