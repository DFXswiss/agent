"""Doubles: `GET /doubles/{name}/state`, `POST /doubles/{name}/scenario` (switch scenario), `POST
/doubles/{name}/reset` and `POST /doubles/{name}/request` (`{method, path, body}`: one request to the double, for
seeding and probes). The double is called on its own device, so this works in T1 and T2 alike."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import Double


def _double(ctx: Context, params: dict[str, str]) -> Double:
    double = ctx.doubles.get(params["name"])
    if double is None:
        raise RouteError(404, "not_found", f"no double {params['name']}")
    return double


def _forward(double: Double, method: str, path: str, body: Any) -> tuple[int, Any]:
    answer = double.node.http(method, f"{double.raw_url}{path}", body)
    return int(answer["status"]), answer["body"]


def get_state(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return _forward(_double(ctx, params), "GET", "/_double/state", None)


def post_scenario(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", 'body must be {"scenario": {...}} or {"packaged": "<name>"}')
    return _forward(_double(ctx, params), "POST", "/_double/scenario", body)


def post_reset(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return _forward(_double(ctx, params), "POST", "/_double/reset", {})


def post_request(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict) or body.get("method") not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise RouteError(400, "invalid", 'body must be {"method", "path", "body"?}')
    path = body.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RouteError(400, "invalid", "path must start with /")
    status, payload = _forward(_double(ctx, params), body["method"], path, body.get("body"))
    return 200, {"status": status, "body": payload}


ROUTES = [
    Route("GET", "/doubles/{name}/state", get_state),
    Route("POST", "/doubles/{name}/scenario", post_scenario),
    Route("POST", "/doubles/{name}/reset", post_reset),
    Route("POST", "/doubles/{name}/request", post_request),
]
