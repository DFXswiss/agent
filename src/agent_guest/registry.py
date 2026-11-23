"""Discovery of the guest's extension points (ADR-027 section 4, guest row).

A work package never edits a central list: it adds a module to one of the area packages and the guest finds it
here. Modules are visited in sorted order; sub-packages are not visited.

| Module and attribute | Value |
|---|---|
| ``control_routes/<name>.py:ROUTES`` | ``routing.Route`` sequence on the control socket (context ``ControlContext``) |
| ``control_routes/<name>.py:API_OPS`` | mapping op name -> ``op(context, args) -> dict`` served on ``api.sock`` |
| ``api_routes/<name>.py:ROUTES`` | sequence of ``routing.Route`` on port 7700 (context ``ApiContext``) |
| ``mcp_tools/<tool>.py:TOOL`` | the MCP tool object (type defined by GST-04), keyed by module name |
| ``hook_addons/<name>.py:ADDON`` | the hook-response decorator (type defined by GST-03), keyed by module name |

A control-route module needs at least one of ``ROUTES`` and ``API_OPS``; the other areas need their attribute.
A missing attribute, a value of the wrong type or a name registered twice raises ``RegistryError``
(``registry_invalid`` / ``registry_duplicate``), so a process never starts with half a registry.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any

from agent_guest.routing import Route

CONTROL_ROUTES = "agent_guest.control_routes"
API_ROUTES = "agent_guest.api_routes"
MCP_TOOLS = "agent_guest.mcp_tools"
HOOK_ADDONS = "agent_guest.hook_addons"

ApiOp = Callable[[Any, dict[str, Any]], dict[str, Any]]


class RegistryError(Exception):
    def __init__(self, code: str, name: str) -> None:
        super().__init__(f"{code} {name}")
        self.code = code
        self.name = name


def modules(package: str) -> list[ModuleType]:
    """The plain modules of ``package``, imported, sorted by name."""
    root = importlib.import_module(package)
    names = sorted(info.name for info in pkgutil.iter_modules(root.__path__) if not info.ispkg)
    return [importlib.import_module(f"{package}.{name}") for name in names]


def _routes(module: ModuleType) -> list[Route]:
    value = getattr(module, "ROUTES", ())
    if not isinstance(value, list | tuple) or not all(isinstance(route, Route) for route in value):
        raise RegistryError("registry_invalid", f"{module.__name__}.ROUTES")
    return list(value)


def _unique_routes(found: list[Route]) -> list[Route]:
    seen: set[tuple[str, str]] = set()
    for route in found:
        key = (route.method, route.path)
        if key in seen:
            raise RegistryError("registry_duplicate", f"{route.method} {route.path}")
        seen.add(key)
    return found


def control_routes(package: str = CONTROL_ROUTES) -> list[Route]:
    found: list[Route] = []
    for module in modules(package):
        if not hasattr(module, "ROUTES") and not hasattr(module, "API_OPS"):
            raise RegistryError("registry_invalid", module.__name__)
        found.extend(_routes(module))
    return _unique_routes(found)


def api_ops(package: str = CONTROL_ROUTES) -> dict[str, ApiOp]:
    found: dict[str, ApiOp] = {}
    for module in modules(package):
        value = getattr(module, "API_OPS", {})
        if not isinstance(value, Mapping) or not all(
            isinstance(name, str) and callable(op) for name, op in value.items()
        ):
            raise RegistryError("registry_invalid", f"{module.__name__}.API_OPS")
        for name, op in value.items():
            if name in found:
                raise RegistryError("registry_duplicate", name)
            found[name] = op
    return found


def api_routes(package: str = API_ROUTES) -> list[Route]:
    found: list[Route] = []
    for module in modules(package):
        if not hasattr(module, "ROUTES"):
            raise RegistryError("registry_invalid", module.__name__)
        found.extend(_routes(module))
    return _unique_routes(found)


def named(package: str, attribute: str) -> dict[str, object]:
    """``{module name: module.<attribute>}`` for every module of ``package`` (MCP tools, hook addons)."""
    found: dict[str, object] = {}
    for module in modules(package):
        if not hasattr(module, attribute):
            raise RegistryError("registry_invalid", f"{module.__name__}.{attribute}")
        found[module.__name__.rpartition(".")[2]] = getattr(module, attribute)
    return found


def mcp_tools(package: str = MCP_TOOLS) -> dict[str, object]:
    return named(package, "TOOL")


def hook_addons(package: str = HOOK_ADDONS) -> dict[str, object]:
    return named(package, "ADDON")
