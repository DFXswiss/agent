"""`POST /sessions/{session}/guest-journal` (`{"kinds": ["session.hook", "ping.delivered"]}`, required: a spec names
only the kinds it checks, so no session content lands in its artefacts by accident): the guest's own records of a
session, read as root from the guest journal in the session's VM (SCN-06, E2E-26). Only for specs, never a product
path.

Answers `{vm, records: [{seq, ts, kind, body}], pings: [{id, kind, level, ref, created_at, noticed_at,
delivered_at}]}`: the journal records of the session in `seq` order (03-api.md section 7.2; acknowledged records stay
24 h) and the rows of the guest's `ping` table (05-runner.md section 9). E2E-26 reads the order of `session.hook` and
`ping.delivered` (the notice rides only on PreToolUse, PostToolUse and UserPromptSubmit answers) and checks that no
ping carries content. The database is opened read-only (`mode=ro`).
"""

from __future__ import annotations

import json
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

JOURNAL = "/var/lib/agent-guest/journal.db"
KINDS = (
    "session.state",
    "session.output",
    "session.tool",
    "session.hook",
    "session.file",
    "session.terminal",
    "usage",
    "operation.request",
    "platform.query",
    "ping.delivered",
    "metrics",
    "guest.error",
)
SCRIPT = """import json, sqlite3, sys
path, session, kinds = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
marks = ",".join("?" * len(kinds))
records = [
    {"seq": seq, "ts": ts, "kind": kind, "body": json.loads(body)}
    for seq, ts, kind, body in db.execute(
        f"SELECT seq, ts, kind, body FROM journal WHERE session_id = ? AND kind IN ({marks}) ORDER BY seq",
        [session, *kinds],
    )
]
columns = ("id", "kind", "level", "ref", "created_at", "noticed_at", "delivered_at")
pings = [
    dict(zip(columns, row))
    for row in db.execute(
        f"SELECT {', '.join(columns)} FROM ping WHERE session_id = ? ORDER BY created_at, id", [session]
    )
]
print(json.dumps({"records": records, "pings": pings}))
"""


def post_guest_journal(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    kinds = body.get("kinds") if isinstance(body, dict) else None
    if not isinstance(kinds, list) or not kinds or not all(kind in KINDS for kind in kinds):
        raise RouteError(400, "invalid", f'body must be {{"kinds": [one or more of {", ".join(KINDS)}]}}')
    session = params["session"]
    try:
        vm = guestvm.session_vm(ctx, session)
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None
    done = guestvm.shell(ctx, vm, ["python3", "-I", "-c", SCRIPT, JOURNAL, session, json.dumps(kinds)])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"guest journal of {session} in {vm}: {done['stderr'].strip()[-300:]}")
    found = json.loads(done["stdout"])
    return 200, {"vm": vm, "records": found["records"], "pings": found["pings"]}


ROUTES = [Route("POST", "/sessions/{session}/guest-journal", post_guest_journal)]
