"""`POST /runners/{name}/store-query` (`{"sql": "SELECT …"}`): rows of a read-only query on the runner's own store
(database `agent_runner` of the cluster under `$AGENT_RUNNER_HOME/pg`, 05-runner.md section 1.4), for the outbox
assertions of the reconnect scenario (E2E-14). Runs the agent checkout's Python on the runner device in a read-only
transaction; the store listens on 127.0.0.1 with its own port file."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

QUERY_TIMEOUT_S = 60.0
SCRIPT = """import json, sys
from pathlib import Path
import psycopg
port = (Path(sys.argv[1]).expanduser() / "pg" / "port").read_text(encoding="utf-8").strip()
with psycopg.connect(f"host=127.0.0.1 port={port} user=agent dbname=agent_runner") as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    row = conn.execute("SELECT coalesce(json_agg(q), '[]'::json) FROM (" + sys.argv[2] + ") AS q").fetchone()
print(json.dumps(row[0]))
"""


def post_store_query(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    sql = body.get("sql") if isinstance(body, dict) else None
    if not isinstance(sql, str) or not sql.strip() or ";" in sql.strip().rstrip(";"):
        raise RouteError(400, "invalid", 'body must be {"sql": "<one SELECT statement>"}')
    home = runner.env["AGENT_RUNNER_HOME"]
    done = runner.node.run(
        [ctx.agent_python, "-c", SCRIPT, home, sql.strip().rstrip(";")], env=runner.env, timeout=QUERY_TIMEOUT_S
    )
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"store query exited with {done['rc']}: {done['stderr'].strip()[-300:]}")
    return 200, {"rows": json.loads(done["stdout"])}


ROUTES = [Route("POST", "/runners/{name}/store-query", post_store_query)]
