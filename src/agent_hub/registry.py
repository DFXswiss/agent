"""Discovery of the hub's extension points (ADR-027 section 4): a package adds files, never edits a shared list.

An area is a direct sub-package of `agent_hub` (`agent_hub/<area>/`). For an extension point `<module>:<attribute>`
the registry imports `agent_hub.<area>.<module>` for every area that has such a module, in sorted area order, and
reads the attribute. A module of that name without the attribute is not an extension (e.g. the primitive module
`agent_hub/crypto/keys.py` is no key-resolver module) and is skipped; a missing registration therefore fails closed
(no route, no resolver), and every area's tests prove its registration. Mappings from several areas are merged; a
key defined by two areas is refused, so no area silently overrides another.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ROOT_PACKAGE = "agent_hub"


class RegistryError(RuntimeError):
    """An extension module breaks the discovery rules (missing attribute, wrong type, a key defined twice)."""


@dataclass(frozen=True)
class ExtensionPoint:
    """One discovered extension point: the module name inside an area and the attribute it must define."""

    module: str
    attribute: str


ROUTERS = ExtensionPoint("routes", "router")
JOBS = ExtensionPoint("jobs", "JOBS")
CLI = ExtensionPoint("cli", "register")
OUTBOX_HANDLERS = ExtensionPoint("outbox", "HANDLERS")
DEVICE_STATE_SECTIONS = ExtensionPoint("device_state", "SECTION")
PROJECTION_REBUILDERS = ExtensionPoint("projections", "REBUILDERS")
SIGNAL_SUBSCRIBERS = ExtensionPoint("signals", "SUBSCRIBERS")
KEY_RESOLVERS = ExtensionPoint("keys", "RESOLVERS")


def areas(package: str = ROOT_PACKAGE) -> list[str]:
    """The names of the direct sub-packages of `package`, sorted."""
    root = importlib.import_module(package)
    return sorted(info.name for info in pkgutil.iter_modules(root.__path__) if info.ispkg)


def discover(point: ExtensionPoint, package: str = ROOT_PACKAGE) -> list[tuple[str, Any]]:
    """`(area, value)` for every area of `package` that has the module of `point`, in sorted area order."""
    found: list[tuple[str, Any]] = []
    for area in areas(package):
        name = f"{package}.{area}.{point.module}"
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        if hasattr(module, point.attribute):
            found.append((area, getattr(module, point.attribute)))
    return found


def merge_mappings(point: ExtensionPoint, package: str = ROOT_PACKAGE) -> dict[str, Any]:
    """The union of the mappings of every area; a key defined by two areas is refused."""
    merged: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for area, value in discover(point, package):
        if not isinstance(value, Mapping):
            raise RegistryError(f"{package}.{area}.{point.module}.{point.attribute} must be a mapping")
        for key, item in value.items():
            if key in merged:
                raise RegistryError(
                    f"{point.module}.{point.attribute} key {key!r} is defined by {owners[key]} and {area}"
                )
            merged[key] = item
            owners[key] = area
    return merged


def concat_sequences(point: ExtensionPoint, package: str = ROOT_PACKAGE) -> list[Any]:
    """The items of the sequences of every area, area by area in sorted order."""
    items: list[Any] = []
    for area, value in discover(point, package):
        if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
            raise RegistryError(f"{package}.{area}.{point.module}.{point.attribute} must be a sequence")
        items.extend(value)
    return items


def callables(point: ExtensionPoint, package: str = ROOT_PACKAGE) -> list[tuple[str, Callable[..., Any]]]:
    """`(area, function)` for an extension point whose attribute is a function (CLI `register`)."""
    found = discover(point, package)
    for area, value in found:
        if not callable(value):
            raise RegistryError(f"{package}.{area}.{point.module}.{point.attribute} must be callable")
    return found
