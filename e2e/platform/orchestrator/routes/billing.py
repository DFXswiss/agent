"""Billing routes of the orchestrator (KOR-31, ADR-062, `docs/platform/runbooks/credit.md`):

- `POST /hub/billing` (`{"args": ["credit", "--org", "<id>", "--set", "0", "--reason", "e2e"]}`): runs the operator
  command `agent-hub billing <args>` with the hub's environment, as the operator does on the hub device. With
  `"price_list": {...}` the document is written to the run's scratch directory and the argument `@price_list` stands
  for its path. Answers `{rc, lines, stderr}` (`lines`: the JSON lines printed on stdout).
- `POST /hub/start-credit` (`{"usd": "<amount>"}`): restarts the hub with `AGENT_HUB_BILLING_START_CREDIT_USD`; the
  database, the relays and the runners stay (they reconnect).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

START_CREDIT = "AGENT_HUB_BILLING_START_CREDIT_USD"
_ARG_RE = re.compile(r"^[A-Za-z0-9 ._:/+=@-]{1,200}$")
"""One argument: printable words only (no shell is involved; the limit keeps the route a test tool)."""
_USD_RE = re.compile(r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$")


def post_billing(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    args = body.get("args") if isinstance(body, dict) else None
    if not isinstance(args, list) or not args or not all(isinstance(a, str) and _ARG_RE.fullmatch(a) for a in args):
        raise RouteError(400, "invalid", 'body must be {"args": ["<word>", ...]}')
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    document = body.get("price_list")
    if document is not None:
        if not isinstance(document, dict):
            raise RouteError(400, "invalid", "price_list must be an object")
        path = Path(ctx.scratch) / f"price-list-{len(list(Path(ctx.scratch).glob('price-list-*.json')))}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        args = [str(path) if arg == "@price_list" else arg for arg in args]
    done = hub.node.run([hub.python, "-m", "agent_hub", "billing", *args], env=hub.env)
    out = str(done.get("stdout") or "")
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return 200, {"rc": done["rc"], "lines": lines, "stderr": str(done.get("stderr") or "")[-2000:]}


def post_start_credit(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    usd = body.get("usd") if isinstance(body, dict) else None
    if not isinstance(usd, str) or not _USD_RE.fullmatch(usd):
        raise RouteError(400, "invalid", 'body must be {"usd": "<amount with at most 6 decimals>"}')
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    hub.env[START_CREDIT] = usd
    hub.node.stop("hub")
    hub.start()
    return 200, {"usd": usd}


ROUTES = [
    Route("POST", "/hub/billing", post_billing),
    Route("POST", "/hub/start-credit", post_start_credit),
]
