"""HTTP adapter of the order operations for the loopback dashboard.

The routes are generated from :data:`order_ops.OPERATIONS`; this module holds
no rule about orders.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import order_ops
from .cli_bridge import MAX_REQUEST_BYTES
from .coordinator_config import load_coordinator_config
from .preconditions import SKIPPABLE
from .store import Store, StoreConnectionError


PREFIX = "/api/orders/"
_LOOPBACK_HOST = re.compile(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}")


def error_text(exc: SystemExit) -> str:
    """The message of a refused operation without the command-line prefix."""
    text = str(exc.code if exc.code is not None else "")
    return text.removeprefix("agent: ")


def same_origin(headers: Mapping[str, str]) -> bool:
    """True for a request addressed to a loopback name whose Origin,
    when sent, is that same address.
    """
    host = headers.get("Host") or ""
    if _LOOPBACK_HOST.fullmatch(host) is None:
        return False
    origin = headers.get("Origin") or ""
    return origin == "" or origin == "http://" + host


def schema(home: Path) -> dict[str, Any]:
    return {
        "enabled": any(
            worker.orders for worker in load_coordinator_config(home).values()
        ),
        "skippable": sorted(SKIPPABLE),
        "operations": order_ops.view_operations(),
    }


def _failure(status: int, text: str) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error": text}


def handle_order_request(
    store: Store,
    home: Path,
    *,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    body: bytes,
) -> tuple[int, dict[str, Any]]:
    if not same_origin(headers):
        return _failure(403, "cross-origin request refused")

    name = path[len(PREFIX):]
    if name == "schema":
        if method != "GET":
            return _failure(405, "method not allowed")
        try:
            result = schema(home)
        except StoreConnectionError:
            raise
        except SystemExit as exc:
            return _failure(400, error_text(exc))
        return 200, {"ok": True, "result": result}

    operation = next(
        (candidate for candidate in order_ops.OPERATIONS if candidate.name == name),
        None,
    )
    if operation is None or operation.cli_only:
        return _failure(404, "unknown order operation")

    expected_method = "GET" if operation.mode == "read" else "POST"
    if method != expected_method:
        return _failure(405, "method not allowed")

    if method == "GET":
        raw: dict[str, Any] = {
            key: values[-1]
            for key, values in parse_qs(query, keep_blank_values=True).items()
        }
    else:
        media_type = (headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            return _failure(415, "content type must be application/json")
        if len(body) > MAX_REQUEST_BYTES:
            return _failure(413, "request body too large")
        try:
            raw = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, RecursionError):
            return _failure(400, "invalid JSON")
        if not isinstance(raw, dict):
            return _failure(400, "body must be an object")

    declared = {param.name: param for param in operation.params}
    params: dict[str, Any] = {}
    for key, value in raw.items():
        param = declared.get(key)
        if param is None:
            params[key] = value
            continue
        if value is None:
            continue
        if param.kind == "bool":
            if isinstance(value, bool):
                params[key] = value
            elif value in ("1", "true"):
                params[key] = True
            elif value in ("0", "false", ""):
                params[key] = False
            else:
                return _failure(400, f"{param.name} must be true or false")
            continue
        if param.kind == "skips":
            params[key] = value
            continue
        if isinstance(value, str):
            params[key] = value
            continue
        if (
            param.kind != "body"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            params[key] = str(value)
            continue
        return _failure(400, f"{param.name} must be a string")

    try:
        result = order_ops.run(operation.name, store, home, params)
    except StoreConnectionError:
        raise
    except SystemExit as exc:
        return _failure(400, error_text(exc))
    return 200, {"ok": True, "result": result}
