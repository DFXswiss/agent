"""Route groups of the hub fake beyond `hubfake.py`: each module `fakes/<name>.py` has `install(hub: HubFake) ->
None`, which adds its routes with `HubFake.add_route` and its state as attributes; `HubFake.start` imports the modules
named in `INSTALLERS` (by name, so a module may import `hubfake` at its top). A new group adds one name."""

from __future__ import annotations

INSTALLERS: tuple[str, ...] = ("membership", "credkey", "boards", "sessions", "account_views", "relay", "admission")
