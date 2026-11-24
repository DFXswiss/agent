"""`GET /state`: origin, control URL, double URLs, runner states and link names of the run."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route


def get_state(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return 200, ctx.state()


ROUTES = [Route("GET", "/state", get_state)]
