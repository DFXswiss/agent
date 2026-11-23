"""`POST /hub-db/query` (`{"sql": "SELECT …"}`): rows of a read-only query on the hub database as JSON, for the
zero-knowledge checks (E2E-09) and state assertions. The session is read-only (`default_transaction_read_only`)."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import HUB_DATABASE


def post_query(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    sql = body.get("sql") if isinstance(body, dict) else None
    if not isinstance(sql, str) or not sql.strip() or ";" in sql.strip().rstrip(";"):
        raise RouteError(400, "invalid", 'body must be {"sql": "<one SELECT statement>"}')
    if ctx.cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    wrapped = f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql.strip().rstrip(';')}) AS q"
    out = ctx.cluster.psql(wrapped, HUB_DATABASE, read_only=True)
    return 200, {"rows": json.loads(out)}


ROUTES = [Route("POST", "/hub-db/query", post_query)]
