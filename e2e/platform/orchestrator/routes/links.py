"""Connections (07-testing.md section 9 fault switches): `GET /links`, `POST /links/{name}/cut`, `POST
/links/{name}/restore`. Links: `runner-hub` (runners → hub), `browser-hub` (browser → hub) and one per double
(`model`, …); cutting closes every open connection of the link and refuses new ones until it is restored."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.node import Node


def _node(ctx: Context, name: str) -> Node:
    node = ctx.links.get(name)
    if node is None:
        raise RouteError(404, "not_found", f"no link {name} (links: {', '.join(sorted(ctx.links))})")
    return node


def get_links(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    out: dict[str, Any] = {}
    for node in dict.fromkeys(ctx.links.values()):
        out.update({name: state for name, state in node.relays().items() if name in ctx.links})
    return 200, out


def post_cut(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return 200, _node(ctx, params["name"]).relay_cut(params["name"])


def post_restore(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return 200, _node(ctx, params["name"]).relay_restore(params["name"])


ROUTES = [
    Route("GET", "/links", get_links),
    Route("POST", "/links/{name}/cut", post_cut),
    Route("POST", "/links/{name}/restore", post_restore),
]
