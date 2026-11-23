"""Internal signals between hub areas (ADR-027 section 4).

An area announces that something happened with `emit(name, **ids)` **after its transaction committed**; the values are
identifiers only (UUIDs, integers or short identifier text), never content. Subscribers are discovered as
`<area>/signals.py:SUBSCRIBERS`, a mapping from a signal name to a sequence of callables `subscriber(name, ids)`. They
run synchronously in the emitter's thread, in sorted area order; a failing subscriber is logged and never affects the
emitter or the other subscribers.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any
from uuid import UUID

from agent_hub.registry import SIGNAL_SUBSCRIBERS, RegistryError, discover

LOG = logging.getLogger("agent_hub.signals")
Ids = Mapping[str, UUID | int | str]
Subscriber = Callable[[str, Ids], object]

_NAME_RE = re.compile(r"[a-z][a-z_]*(?:\.[a-z][a-z_]*)+")
_ID_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ID_TEXT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_lock = threading.Lock()
_state: dict[str, dict[str, tuple[Subscriber, ...]]] = {}


def collect(found: Iterable[tuple[str, object]]) -> dict[str, tuple[Subscriber, ...]]:
    """Merge the `SUBSCRIBERS` mappings of the areas (lists of the same name are concatenated in area order)."""
    merged: dict[str, list[Subscriber]] = {}
    for area, value in found:
        if not isinstance(value, Mapping):
            raise RegistryError(f"{area}/signals.py SUBSCRIBERS must be a mapping")
        for name, subscribers in value.items():
            if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
                raise RegistryError(f"{area}/signals.py: a signal name is lower-case dotted words")
            items = list(subscribers)
            if not all(callable(item) for item in items):
                raise RegistryError(f"{area}/signals.py: subscribers of {name} must be callable")
            merged.setdefault(name, []).extend(items)
    return {name: tuple(items) for name, items in merged.items()}


def configure(subscribers: Mapping[str, Iterable[Subscriber]] | None = None) -> dict[str, tuple[Subscriber, ...]]:
    """Set the subscribers (None discovers them) and return them."""
    found = collect(discover(SIGNAL_SUBSCRIBERS)) if subscribers is None else collect([("configured", subscribers)])
    with _lock:
        _state["subscribers"] = found
    return found


def subscribers() -> Mapping[str, tuple[Subscriber, ...]]:
    """The current subscribers (discovered on first use)."""
    current = _state.get("subscribers")
    return MappingProxyType(current if current is not None else configure())


def _check_ids(ids: Mapping[str, Any]) -> None:
    for key, value in ids.items():
        if _ID_KEY_RE.fullmatch(key) is None:
            raise ValueError("a signal ID key is a lower-case identifier")
        if isinstance(value, bool) or not isinstance(value, UUID | int | str):
            raise ValueError(f"signal ID {key} must be a UUID, an integer or identifier text")
        if isinstance(value, str) and _ID_TEXT_RE.fullmatch(value) is None:
            raise ValueError(f"signal ID {key} must be identifier text, not content")


def emit(name: str, **ids: UUID | int | str) -> int:
    """Deliver the signal to every subscriber; the number of subscribers that failed (each logged)."""
    if _NAME_RE.fullmatch(name) is None:
        raise ValueError("a signal name is lower-case dotted words")
    _check_ids(ids)
    frozen = MappingProxyType(dict(ids))
    failed = 0
    for subscriber in subscribers().get(name, ()):
        try:
            subscriber(name, frozen)
        except Exception:
            failed += 1
            LOG.error("signal %s: subscriber %s failed", name, getattr(subscriber, "__qualname__", "?"), exc_info=True)
    return failed
