"""`POST /hub-db/audit-tamper` (`{"seq": n}`): the tamper check of E2E-15 (07-testing.md section 2.1, 02-data-model.md
section 2.9: "tampering a row in a copy is detected").

The hub database is copied (`pg_dump` of the run's hub database into a fresh database `agent_hub_tamper` of the same
cluster, as the superuser) and the audit row `seq` of the *copy* is changed: its `result` flips between `ok` and
`denied` after the append-only triggers of the copy's `hub.audit` are disabled (they fire in every replication role;
only the superuser of the test cluster may disable them). Then `agent-hub audit verify` - the operator's check, the
same verification as `GET /v1/orgs/{org}/audit/verify` - runs once against the copy and once against the original.
The copy is dropped at the end, also on failure. The live hub database is never written. Answers
`{"seq": n, "before": result, "after": result, "copy": {"rc", "ok"}, "original": {"rc", "ok"}}`.
"""

from __future__ import annotations

import json
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import HUB_DATABASE, Cluster, Hub

COPY_DATABASE = "agent_hub_tamper"
COPY_TIMEOUT_S = 300.0
FLIP = {"ok": "denied"}


def _copy(cluster: Cluster) -> None:
    cluster.psql(f"DROP DATABASE IF EXISTS {COPY_DATABASE} WITH (FORCE)")
    cluster.psql(f"CREATE DATABASE {COPY_DATABASE}")
    script = (
        '"$1/pg_dump" -h 127.0.0.1 -p "$2" -U postgres "$3"'
        ' | "$1/psql" -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$2" -U postgres -d "$4" >/dev/null'
    )
    argv = ["/bin/bash", "-c", f"set -o pipefail; {script}", "bash", cluster.bin_dir, str(cluster.port)]
    env = dict(cluster.env, PGPASSWORD=cluster.password)
    done = cluster.node.run([*argv, HUB_DATABASE, COPY_DATABASE], env=env, timeout=COPY_TIMEOUT_S)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"copy of the hub database failed: {done['stderr'].strip()[-300:]}")


def _verify(hub: Hub, database_url: str | None) -> dict[str, Any]:
    env = dict(hub.env) if database_url is None else dict(hub.env, AGENT_HUB_DATABASE=database_url)
    done = hub.node.run([hub.python, "-m", "agent_hub", "audit", "verify"], env=env, timeout=COPY_TIMEOUT_S)
    lines = [line for line in str(done["stdout"]).splitlines() if line.startswith("{")]
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except ValueError:
        answer = {}
    return {"rc": done["rc"], "ok": answer.get("ok") if isinstance(answer, dict) else None}


def post_tamper(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    seq = body.get("seq") if isinstance(body, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise RouteError(400, "invalid", 'body must be {"seq": n} with n >= 1')
    cluster, hub = ctx.cluster, ctx.hub
    if cluster is None or hub is None:
        raise RouteError(409, "invalid", "the hub and its database are not running")
    try:
        _copy(cluster)
        rows = json.loads(
            cluster.psql(
                f"SELECT coalesce(json_agg(result), '[]'::json) FROM hub.audit WHERE seq = {seq}", COPY_DATABASE
            )
        )
        if not rows:
            raise RouteError(404, "not_found", f"no audit row {seq}")
        before = str(rows[0])
        after = FLIP.get(before, "ok")
        cluster.psql(
            f"ALTER TABLE hub.audit DISABLE TRIGGER USER; UPDATE hub.audit SET result = '{after}' WHERE seq = {seq}",
            COPY_DATABASE,
        )
        copy_url = f"postgresql://postgres:{cluster.password}@127.0.0.1:{cluster.port}/{COPY_DATABASE}?sslmode=disable"
        copy = _verify(hub, copy_url)
        original = _verify(hub, None)
    finally:
        cluster.psql(f"DROP DATABASE IF EXISTS {COPY_DATABASE} WITH (FORCE)")
    return 200, {"seq": seq, "before": before, "after": after, "copy": copy, "original": original}


ROUTES = [Route("POST", "/hub-db/audit-tamper", post_tamper)]
