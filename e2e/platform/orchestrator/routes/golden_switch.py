"""`POST /runners/{name}/vm-outdated` (`{"vm_id": "vm-<8 hex>"}`): the test hook of the golden switch scenario (E2E-69,
KOR-70, ADR-093) - the workgroup VM counts as cloned from an older golden (`runner.vm.golden_version` =
`OUTDATED`), so the runner's switch moves it to the device's newest ready golden (the real one of the run). Runs the
agent checkout's Python on the runner device against the runner's own store, like `runner_store.py`; never a product
path."""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

OUTDATED = "e2e-outdated"
TIMEOUT_S = 60.0
_VM_ID = re.compile(r"^vm-[0-9a-f]{8}$")
SCRIPT = """import json, sys
from pathlib import Path
import psycopg
port = (Path(sys.argv[1]).expanduser() / "pg" / "port").read_text(encoding="utf-8").strip()
with psycopg.connect(f"host=127.0.0.1 port={port} user=agent dbname=agent_runner") as conn:
    rows = conn.execute(
        "UPDATE runner.vm SET golden_version = %s WHERE id = %s AND state <> 'deleted' AND wg_id IS NOT NULL"
        " RETURNING id", (sys.argv[3], sys.argv[2])).fetchall()
print(json.dumps([row[0] for row in rows]))
"""


def post_vm_outdated(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    vm = body.get("vm_id") if isinstance(body, dict) else None
    if not isinstance(vm, str) or not _VM_ID.fullmatch(vm):
        raise RouteError(400, "invalid", 'body must be {"vm_id": "vm-<8 hex>"}')
    done = runner.node.run(
        [ctx.agent_python, "-c", SCRIPT, runner.env["AGENT_RUNNER_HOME"], vm, OUTDATED],
        env=runner.env,
        timeout=TIMEOUT_S,
    )
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"store update exited with {done['rc']}: {done['stderr'].strip()[-300:]}")
    if json.loads(done["stdout"]) != [vm]:
        raise RouteError(404, "not_found", f"{runner.name} has no live workgroup VM {vm}")
    return 200, {"vm_id": vm, "golden_version": OUTDATED}


ROUTES = [Route("POST", "/runners/{name}/vm-outdated", post_vm_outdated)]
