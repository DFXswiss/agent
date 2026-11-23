"""Discovery of the outbox handler chains (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030).

Every area may define `HANDLERS`, a mapping from an item kind of `contracts/enums/outbox-item-kinds.json` to a handler
(`agent_hub.outbox.base.Handler`). The outbox area defines the base handler of every kind, except the kinds another area
claims with `<area>/outbox.py:BASE_KINDS` (a collection of kinds; each kind by one area at most, ADR-037 - the usage
area claims `usage` and `quota`): for those, that area's handler is the base. A handler of any other area for the same
kind is appended to that kind's chain, in sorted area order, and runs after the base handler in the same savepoint. A
mapping that is not a mapping, an unknown kind, a non-callable handler, a broken or doubled claim, a base handler of a
claimed kind in the outbox area, an extension of a kind without a base handler or a kind without any handler stops
the discovery (`RegistryError`); the outbox routes module discovers the chains when the hub app is built, so a broken
registration fails closed at start, never during a push.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from agent_hub.outbox.base import Handler
from agent_hub.outbox.bodies import ITEM_KINDS
from agent_hub.registry import OUTBOX_HANDLERS, ExtensionPoint, RegistryError, discover

BASE_AREA = "outbox"
BASE_KINDS = ExtensionPoint("outbox", "BASE_KINDS")
_lock = threading.Lock()
_state: dict[str, Mapping[str, tuple[Handler, ...]]] = {}


def claimed_bases(found: Iterable[tuple[str, object]]) -> dict[str, str]:
    """`kind -> area` of the discovered `(area, BASE_KINDS)` claims (module docstring)."""
    owners: dict[str, str] = {}
    for area, kinds in found:
        if area == BASE_AREA or isinstance(kinds, str | bytes | Mapping) or not isinstance(kinds, Iterable):
            raise RegistryError(f"{area}/outbox.py BASE_KINDS must be a collection of item kinds of another area")
        for kind in kinds:
            if kind not in ITEM_KINDS or kind in owners:
                raise RegistryError(f"{area}/outbox.py BASE_KINDS claims an unknown or already claimed kind")
            owners[kind] = area
    return owners


def collect(
    found: Iterable[tuple[str, object]], claimed: Mapping[str, str] | None = None
) -> dict[str, tuple[Handler, ...]]:
    """The chain of every item kind from the discovered `(area, HANDLERS)` pairs and the claims (`None` discovers
    them; module docstring)."""
    owners = claimed_bases(discover(BASE_KINDS)) if claimed is None else claimed

    def _base_area(kind: str) -> str:
        return owners.get(kind, BASE_AREA)

    bases: dict[str, Handler] = {}
    extensions: dict[str, list[Handler]] = {}
    for area, value in sorted(found, key=lambda pair: pair[0]):
        if not isinstance(value, Mapping):
            raise RegistryError(f"{area}/outbox.py HANDLERS must be a mapping")
        for kind, handler in value.items():
            if kind not in ITEM_KINDS or not callable(handler):
                raise RegistryError(f"{area}/outbox.py HANDLERS has an unknown item kind or a non-callable handler")
            if area == _base_area(kind):
                bases[kind] = handler
            elif area == BASE_AREA:
                raise RegistryError(f"the base handler of {kind} belongs to the {_base_area(kind)} area")
            else:
                extensions.setdefault(kind, []).append(handler)
    orphans = sorted(kind for kind in extensions if kind not in bases)
    if orphans:
        raise RegistryError(f"an area extends {', '.join(orphans)}, which has no base handler")
    missing = [kind for kind in ITEM_KINDS if kind not in bases]
    if missing:
        raise RegistryError(f"no outbox handler for {', '.join(missing)}")
    return {kind: (bases[kind], *extensions.get(kind, ())) for kind in ITEM_KINDS}


def configure(found: Iterable[tuple[str, object]] | None = None) -> Mapping[str, tuple[Handler, ...]]:
    """Set the chains from `found` (None discovers them) and return them."""
    chains = MappingProxyType(collect(discover(OUTBOX_HANDLERS) if found is None else found))
    with _lock:
        _state["chains"] = chains
    return chains


def chains() -> Mapping[str, tuple[Handler, ...]]:
    """The current chains (discovered on first use)."""
    current = _state.get("chains")
    return current if current is not None else configure()
