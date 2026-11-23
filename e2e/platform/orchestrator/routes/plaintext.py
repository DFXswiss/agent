"""`POST /checks/plaintext` (`{"canaries": ["…", …]}`): the plaintext scan of the hub side (09-milestones.md M1;
`checks/plaintext_scan.py`). Sources: a fresh `pg_dump` of the hub database, the hub and PostgreSQL logs of the hub
device, and every byte the hub sent on the browser link (the tap of the `browser-hub` relay, one file per connection,
HTTP-decoded). Answers `{findings: [{source, canary, encoding}], sources: {label: bytes}, sse_frames: n}`; the
spec asserts no finding and that the sources are not empty (`sse_frames` > 0: the browser's stream was captured).
The canary values are neither logged nor stored."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from checks.plaintext_scan import check_canaries, http_source, scan, sse_frames

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.relay import BROWSER_TAP

DUMP_NAME = "plaintext-scan.sql"
LOG_LINES = 10_000_000
LOGS = ("hub", "postgres")


def post_plaintext(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    values = body.get("canaries") if isinstance(body, dict) else None
    try:
        canaries = check_canaries(values if isinstance(values, list) and values else [None])
    except ValueError as exc:
        raise RouteError(400, "invalid", f'body must be {{"canaries": [...]}}: {exc}') from None
    cluster = ctx.cluster
    if cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    dump = Path(ctx.scratch) / DUMP_NAME
    cluster.dump(str(dump))
    try:
        sources = {"hub-db-dump": dump.read_bytes()}
    finally:
        dump.unlink(missing_ok=True)
    for name in LOGS:
        sources[f"{name}-log"] = ctx.hub_node.log_tail(name, LOG_LINES).encode("utf-8")
    frames = 0
    tap = Path(ctx.scratch) / BROWSER_TAP
    for path in sorted(tap.glob("*.down")):
        data = path.read_bytes()
        frames += sse_frames(data)
        sources[f"browser-link/{path.name}"] = http_source(data)
    found = scan(sources, canaries)
    sizes = {label: len(data) for label, data in sources.items()}
    return 200, {"findings": [asdict(item) for item in found], "sources": sizes, "sse_frames": frames}


ROUTES = [Route("POST", "/checks/plaintext", post_plaintext)]
