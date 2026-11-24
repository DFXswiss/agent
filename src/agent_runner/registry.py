"""Discovery of the runner's extension points (ADR-027 section 4; decisions/RUN-01.md).

A work package never edits a central list: it adds a module to its area package `agent_runner/<area>/` and the
runner finds it here. Areas are the sub-packages of the root package, visited in sorted order.

| Module and attribute | Value |
|---|---|
| `<area>/cli.py:register` | `register(subparsers)` adds sub-commands (`set_defaults(handler=...)`) |
| `<area>/tasks.py:TASKS` | sequence of `TaskSpec`, daemon tasks run by the supervisor |
| `<area>/shutdown.py:HOOKS` | sequence of `ShutdownHook`, awaited on SIGTERM before task cancellation |
| `<area>/reconcile.py:STEPS` | sequence of `ReconcileStep`, ordered by `order`, then `name` |
| `<area>/journal.py:HANDLERS` | mapping journal kind -> handler |
| `<area>/executors.py:EXECUTORS` | mapping operation type -> executor |
| `<area>/requests.py:HANDLERS` | mapping device-request kind -> handler |
| `<area>/hints.py:HANDLERS` | mapping WebSocket hint kind -> handler |
| `<area>/uninstall.py:CLEANERS` | sequence of `Cleaner`, called by `uninstall` before the store is deleted |
| `<area>/capacity.py:USAGE` | `async USAGE(context) -> vm.capacity.Usage`: VMs and memory of the area (ADR-075) |

`USAGE` is counted against the VM budget and reported as `work_vms_running` (KOR-41, decisions/KOR-41.md).

A module without its attribute, a value of the wrong type or a name registered twice raises
`RunnerError("registry_invalid" | "registry_duplicate", ...)`, so the daemon never starts with half a registry.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runner.errors import RunnerError

ROOT_PACKAGE = "agent_runner"
_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class _Named(Protocol):
    @property
    def name(self) -> str: ...


@dataclass(frozen=True)
class TaskSpec:
    """A named daemon task; `run(context)` is restarted by the supervisor when it raises."""

    name: str
    run: Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class ShutdownHook:
    name: str
    run: Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class ReconcileStep:
    order: int
    name: str
    run: Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class Cleaner:
    name: str
    run: Callable[[Any], None]


def areas(package: str = ROOT_PACKAGE) -> list[str]:
    """Names of the area sub-packages of `package`, sorted."""
    root = importlib.import_module(package)
    return sorted(info.name for info in pkgutil.iter_modules(root.__path__) if info.ispkg)


def discover(module: str, attribute: str, package: str = ROOT_PACKAGE) -> Iterator[tuple[str, object]]:
    """Yield `(area, value)` of `<package>.<area>.<module>.<attribute>` for every area that has the module.

    The module is looked up with `find_spec` first, so an import error inside an existing module propagates
    instead of being mistaken for "no registration".
    """
    for area in areas(package):
        qualified = f"{package}.{area}.{module}"
        if importlib.util.find_spec(qualified) is None:
            continue
        loaded = importlib.import_module(qualified)
        if not hasattr(loaded, attribute):
            raise RunnerError("registry_invalid", f"{area}.{module}")
        yield area, getattr(loaded, attribute)


def _sequence[T](module: str, attribute: str, kind: type[T], package: str) -> list[tuple[str, T]]:
    found: list[tuple[str, T]] = []
    for area, value in discover(module, attribute, package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.{module}")
        for item in value:
            if not isinstance(item, kind):
                raise RunnerError("registry_invalid", f"{area}.{module}")
            found.append((area, item))
    return found


def _unique_names[N: _Named](items: list[tuple[str, N]], module: str) -> list[N]:
    seen: set[str] = set()
    out: list[N] = []
    for area, item in items:
        if not _NAME_RE.fullmatch(item.name):
            raise RunnerError("registry_invalid", f"{area}.{module}")
        if item.name in seen:
            raise RunnerError("registry_duplicate", item.name)
        seen.add(item.name)
        out.append(item)
    return out


def tasks(package: str = ROOT_PACKAGE) -> list[TaskSpec]:
    return _unique_names(_sequence("tasks", "TASKS", TaskSpec, package), "tasks")


def shutdown_hooks(package: str = ROOT_PACKAGE) -> list[ShutdownHook]:
    return _unique_names(_sequence("shutdown", "HOOKS", ShutdownHook, package), "shutdown")


def reconcile_steps(package: str = ROOT_PACKAGE) -> list[ReconcileStep]:
    steps = _unique_names(_sequence("reconcile", "STEPS", ReconcileStep, package), "reconcile")
    return sorted(steps, key=lambda step: (step.order, step.name))


def cleaners(package: str = ROOT_PACKAGE) -> list[Cleaner]:
    return _unique_names(_sequence("uninstall", "CLEANERS", Cleaner, package), "uninstall")


def _handlers(module: str, attribute: str, package: str) -> dict[str, Callable[..., Any]]:
    merged: dict[str, Callable[..., Any]] = {}
    for area, value in discover(module, attribute, package):
        if not isinstance(value, Mapping):
            raise RunnerError("registry_invalid", f"{area}.{module}")
        for key, handler in value.items():
            if not isinstance(key, str) or not _NAME_RE.fullmatch(key) or not callable(handler):
                raise RunnerError("registry_invalid", f"{area}.{module}")
            if key in merged:
                raise RunnerError("registry_duplicate", key)
            merged[key] = handler
    return merged


def journal_handlers(package: str = ROOT_PACKAGE) -> dict[str, Callable[..., Any]]:
    return _handlers("journal", "HANDLERS", package)


def executors(package: str = ROOT_PACKAGE) -> dict[str, Callable[..., Any]]:
    return _handlers("executors", "EXECUTORS", package)


def request_handlers(package: str = ROOT_PACKAGE) -> dict[str, Callable[..., Any]]:
    return _handlers("requests", "HANDLERS", package)


def hint_handlers(package: str = ROOT_PACKAGE) -> dict[str, Callable[..., Any]]:
    return _handlers("hints", "HANDLERS", package)


def capacity_usages(package: str = ROOT_PACKAGE) -> list[Callable[[Any], Awaitable[Any]]]:
    """The `USAGE` providers of the areas, in area order."""
    found: list[Callable[[Any], Awaitable[Any]]] = []
    for area, value in discover("capacity", "USAGE", package):
        if not callable(value):
            raise RunnerError("registry_invalid", f"{area}.capacity")
        found.append(value)
    return found


def cli_registrars(package: str = ROOT_PACKAGE) -> list[tuple[str, Callable[[Any], None]]]:
    found: list[tuple[str, Callable[[Any], None]]] = []
    for area, value in discover("cli", "register", package):
        if not callable(value):
            raise RunnerError("registry_invalid", f"{area}.cli")
        found.append((area, value))
    return found
