"""`POST /hub-db/forge-event` (`{"event": {"header", "ct", "sig"}, "author_kind": "user" | "device"}`): a tampered hub
stores an event that the hub's own checks refuse (E2E-18: an event signed by a member without the right, which the hub
answers with `author_not_writer`) directly in `hub.event` under the next `scope_seq` of its scope, as the database
superuser. The real hub then serves it like any stored event (exactly as stored: header text, `ct`, `sig`), so the
test can prove that reader browsers verify author rights themselves (04-crypto.md section 6.4, zero-knowledge hub,
R-8.6). No audit row is written (a tampering hub would not either); the audit chain stays intact. Answers
`{scope_seq}`.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import HUB_DATABASE

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_KIND = re.compile(r"^[a-z][a-z_.]{0,63}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_B64U = re.compile(r"^[A-Za-z0-9_-]+$")
_OPTIONAL_IDS = ("wg", "task", "session")
AUTHOR_KINDS = ("user", "device")


def _invalid(detail: str) -> RouteError:
    return RouteError(400, "invalid", detail)


def _uuid(header: dict[str, Any], key: str) -> str:
    value = header.get(key)
    if not isinstance(value, str) or not _UUID.match(value):
        raise _invalid(f"header.{key} must be a UUID")
    return value


def _optional(header: dict[str, Any], key: str) -> str | None:
    value = header.get(key, "")
    if value == "":
        return None
    return _uuid(header, key)


def _integer(header: dict[str, Any], key: str) -> int:
    value = header.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"header.{key} must be a non-negative integer")
    return value


def _bytes(event: dict[str, Any], key: str) -> bytes:
    value = event.get(key)
    if not isinstance(value, str) or not _B64U.match(value):
        raise _invalid(f"event.{key} must be base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _invalid(f"event.{key} must be base64url") from exc


def _text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _id(value: str | None) -> str:
    return "NULL" if value is None else _text(value)


def forge_sql(event: Any, author_kind: Any) -> str:
    """The one statement that stores `event` (validated) under the next sequence number of its scope."""
    if not isinstance(event, dict) or not isinstance(event.get("header"), dict):
        raise _invalid('body must be {"event": {"header", "ct", "sig"}, "author_kind": "user"|"device"}')
    if author_kind not in AUTHOR_KINDS:
        raise _invalid("author_kind must be user or device")
    header: dict[str, Any] = event["header"]
    ids = {key: _uuid(header, key) for key in ("id", "org", "scope_id", "author")}
    optional = {key: _optional(header, key) for key in _OPTIONAL_IDS}
    scope, kind, ts, ref = header.get("scope"), header.get("kind"), header.get("ts"), header.get("ref", "")
    if scope not in ("org", "wg"):
        raise _invalid("header.scope must be org or wg")
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise _invalid("header.kind must be an event kind")
    if not isinstance(ts, str) or not _TIME.match(ts):
        raise _invalid("header.ts must be a UTC time")
    if not isinstance(ref, str) or len(ref) > 200:
        raise _invalid("header.ref must be text")
    epoch, lh = _integer(header, "epoch"), _integer(header, "lh")
    ct, sig = _bytes(event, "ct"), _bytes(event, "sig")
    wg = ids["scope_id"] if scope == "wg" else optional["wg"]
    header_text = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    scope_id = _text(ids["scope_id"])
    return (
        f"WITH h AS (INSERT INTO hub.scope_head (scope_id, scope, seq) VALUES ({scope_id}, {_text(scope)}, 1)"
        " ON CONFLICT (scope_id) DO UPDATE SET seq = hub.scope_head.seq + 1 RETURNING seq)"
        " INSERT INTO hub.event (scope_id, scope_seq, id, scope, org_id, wg_id, task_id, session_id, author_id,"
        " author_kind, kind, ref, epoch, ts, lh, header_text, ct, sig, size, received_at)"
        f" SELECT {scope_id}, h.seq, {_text(ids['id'])}, {_text(scope)}, {_text(ids['org'])}, {_id(wg)},"
        f" {_id(optional['task'])}, {_id(optional['session'])}, {_text(ids['author'])}, {_text(author_kind)},"
        f" {_text(kind)}, {_text(ref)}, {epoch}, {_text(ts)}, {lh}, {_text(header_text)},"
        f" decode('{ct.hex()}', 'hex'), decode('{sig.hex()}', 'hex'), {len(ct)}, now() FROM h RETURNING scope_seq"
    )


def post_forge(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    payload = body if isinstance(body, dict) else {}
    sql = forge_sql(payload.get("event"), payload.get("author_kind", "user"))
    if ctx.cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    out = ctx.cluster.psql(sql, HUB_DATABASE).strip().splitlines()
    return 200, {"scope_seq": int(out[0])}


ROUTES = [Route("POST", "/hub-db/forge-event", post_forge)]
