"""Disaster recovery of E2E-33 (07-testing.md section 5.6, DR-01): export/restore of the hub and a runner store wipe.

`POST /hub/export-restore` (`{"canaries": [...]}`, optional; refused with 409 while `agent_hub_before` of an unfinished
earlier call exists, so its data is never dropped) runs the whole recovery on the hub device and answers with what
the spec asserts:

1. the hub is stopped (its live channels close; browsers and runners keep their cursors and verified heads);
2. `before`: the fingerprint of the hub database - every org's log head, the platform-log head, per scope the event
   count, the last `scope_seq`, the scope head and an MD5 over `id:sig` of its events in order, the audit head, the
   newest checkpoint and each device's `last_origin_seq`;
3. `agent-hub export --org <id> --out <scratch>/backup/<id>.jsonl` for every org; `plaintext`: the given canaries
   found in the export files (`grep -F`, expected: none);
4. a fresh database: the old one is renamed to `agent_hub_before` (its sessions ended first), `agent_hub` is created
   again for `hub_owner` (whose password is replaced by a new random one, passed like the passwords of the start: the
   owner URL of the start lives only in the run), `agent-hub migrate`, the reader grant;
5. `agent-hub restore --in <file> ...` as the owner (`AGENT_HUB_OWNER_DATABASE`);
6. `after`: the fingerprint of the restored database; `rebuild`: `agent-hub rebuild-projections --check`; `audit`:
   `agent-hub audit verify` (chain and checkpoint);
7. the hub is started again on the restored database; `agent_hub_before` is dropped.

A failing step answers 500 with the step and the command's last output line (`failed <step>` of the hub CLI, never a
secret). `POST /runners/{name}/wipe-store` (`{}`) stops the runner (its daemon stops its own PostgreSQL cluster; the
route waits until no `postmaster.pid` is left), checks the identity (`device.json`, `keys/`, 02-data-model.md section
4), reads the hub's `last_origin_seq` of the device (now stable: the runner is stopped), deletes
`$AGENT_RUNNER_HOME/pg` - the store -, registers the run's golden image again (`image adopt`: the golden table was
part of the store) and starts the runner; it answers `{"name", "state", "golden", "device", "last_origin_seq"}`.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import HUB_DATABASE, Cluster, Hub, ServiceError

BEFORE_DATABASE = "agent_hub_before"
STEP_TIMEOUT_S = 600.0
GOLDEN_NOTE = "golden image "

FINGERPRINT = """SELECT json_build_object(
 'orgs', (SELECT coalesce(json_agg(json_build_object('id', id, 'seq', log_head_seq, 'hash', log_head_hash)
   ORDER BY id), '[]'::json) FROM hub.org),
 'platform', (SELECT json_build_object('seq', seq, 'hash', entry_hash) FROM hub.platform_entry
   ORDER BY seq DESC LIMIT 1),
 'scopes', (SELECT coalesce(json_agg(s ORDER BY s.scope_id), '[]'::json) FROM (
   SELECT e.scope_id, count(*) AS events, max(e.scope_seq) AS last_seq, h.seq AS head,
     md5(string_agg(e.id::text || ':' || encode(e.sig, 'hex'), ',' ORDER BY e.scope_seq)) AS digest
   FROM hub.event e JOIN hub.scope_head h ON h.scope_id = e.scope_id GROUP BY e.scope_id, h.seq) s),
 'audit', (SELECT json_build_object('seq', seq, 'hash', hash) FROM hub.audit ORDER BY seq DESC LIMIT 1),
 'checkpoint', (SELECT json_build_object('key', hub_key_id, 'seq', seq, 'hash', hash) FROM hub.audit_checkpoint
   ORDER BY seq DESC LIMIT 1),
 'devices', (SELECT coalesce(json_agg(json_build_object('id', device_id, 'last_origin_seq', last_origin_seq)
   ORDER BY device_id), '[]'::json) FROM hub.device_stream))"""


def _services(ctx: Context) -> tuple[Cluster, Hub]:
    if ctx.cluster is None or ctx.hub is None:
        raise RouteError(409, "invalid", "the hub and its database are not running")
    return ctx.cluster, ctx.hub


def _json(text: str, step: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        raise RouteError(500, "failed", f"{step}: no JSON answer") from None


def fingerprint(cluster: Cluster) -> Any:
    """The fingerprint of the hub database (module docstring)."""
    return _json(cluster.psql(" ".join(FINGERPRINT.split()), HUB_DATABASE), "fingerprint")


def _last_line(done: dict[str, Any]) -> str:
    lines = (str(done["stdout"]) + "\n" + str(done["stderr"])).strip().splitlines()
    return lines[-1] if lines else ""


def _hub_command(hub: Hub, step: str, args: list[str], env: dict[str, str] | None = None) -> str:
    """`python -m agent_hub <args>` with the hub's environment; its stdout, or 500 with its last line."""
    done = hub.node.run([hub.python, "-m", "agent_hub", *args], env=env or hub.env, timeout=STEP_TIMEOUT_S)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"{step}: {_last_line(done)}")
    return str(done["stdout"])


def _fresh_database(cluster: Cluster) -> str:
    """Rename the hub database, create an empty one for `hub_owner` with a new password; the owner URL."""
    password = secrets.token_hex(32)  # the strict connection-string form: 64 hex characters
    cluster.psql(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
        f" WHERE datname = '{HUB_DATABASE}' AND pid <> pg_backend_pid()"
    )
    cluster.psql(f"ALTER DATABASE {HUB_DATABASE} RENAME TO {BEFORE_DATABASE}")
    cluster.psql(f"CREATE DATABASE {HUB_DATABASE} OWNER hub_owner")
    cluster.psql(f"ALTER ROLE hub_owner PASSWORD '{password}'")
    return cluster.url("hub_owner", password)


def _canaries(body: Any) -> list[str]:
    found = body.get("canaries", []) if isinstance(body, dict) else []
    if not isinstance(found, list) or not all(isinstance(item, str) and item for item in found):
        raise RouteError(400, "invalid", 'body must be {"canaries": ["<text>", ...]} (optional)')
    return found


def _scan(hub: Hub, canaries: list[str], paths: list[str]) -> list[str]:
    """The canaries found in the export files (`grep -F`: 0 found, 1 none, anything else fails)."""
    if not canaries or not paths:
        return []
    patterns = [part for canary in canaries for part in ("-e", canary)]
    done = hub.node.run(["grep", "-F", "-o", "-h", *patterns, "--", *paths])
    if done["rc"] not in {0, 1}:
        raise RouteError(500, "failed", f"scan: {_last_line(done)}")
    return sorted(set(str(done["stdout"]).split()))


def post_export_restore(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    canaries = _canaries(body)
    cluster, hub = _services(ctx)
    exists = cluster.psql(f"SELECT count(*) FROM pg_database WHERE datname = '{BEFORE_DATABASE}'").strip()
    if exists != "0":
        raise RouteError(409, "invalid", f"{BEFORE_DATABASE} exists: a previous recovery did not finish")
    hub.node.stop("hub")
    before = fingerprint(cluster)
    orgs = [
        str(org)
        for org in _json(
            cluster.psql("SELECT coalesce(json_agg(id ORDER BY id), '[]') FROM hub.org", HUB_DATABASE), "orgs"
        )
    ]
    directory = f"{ctx.scratch}/backup"
    if hub.node.run(["mkdir", "-p", directory])["rc"] != 0:
        raise RouteError(500, "failed", "export: the backup directory cannot be created")
    files, exported = [], {}
    for org in orgs:
        path = f"{directory}/{org}.jsonl"
        hub.node.run(["rm", "-f", path])
        exported[org] = _hub_command(hub, "export", ["export", "--org", org, "--out", path]).strip()
        files += ["--in", path]
    plaintext = _scan(hub, canaries, files[1::2])
    owner_url = _fresh_database(cluster)
    try:
        migrated = hub.migrate(owner_url)
    except ServiceError as error:
        raise RouteError(500, "failed", f"migrate: {error}") from None
    cluster.grant_reader()
    restored = _hub_command(hub, "restore", ["restore", *files], dict(hub.env, AGENT_HUB_OWNER_DATABASE=owner_url))
    after = fingerprint(cluster)
    rebuild = _json(_hub_command(hub, "rebuild", ["rebuild-projections", "--check"]).strip(), "rebuild")
    audit = _json(_hub_command(hub, "audit", ["audit", "verify"]).strip().splitlines()[-1], "audit")
    try:
        hub.start()
    except ServiceError as error:
        raise RouteError(500, "failed", f"start: {error}") from None
    cluster.psql(f"DROP DATABASE IF EXISTS {BEFORE_DATABASE} WITH (FORCE)")
    return 200, {
        "orgs": orgs,
        "exported": exported,
        "plaintext": plaintext,
        "migrated": migrated,
        "restored": restored.strip(),
        "before": before,
        "after": after,
        "rebuild": rebuild,
        "audit": audit,
    }


def _golden(ctx: Context) -> str | None:
    notes = [note[len(GOLDEN_NOTE) :] for note in ctx.notes if note.startswith(GOLDEN_NOTE)]
    return notes[-1] if notes else None


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
WIPE_SCRIPT = (
    'cd "$1" && test -f device.json && test -d keys || exit 3; '
    'i=0; while [ -n "$(find pg -name postmaster.pid 2>/dev/null)" ]; do '
    '[ "$i" -lt 300 ] || exit 4; i=$((i + 1)); sleep 0.1; done; cat device.json'
)


def _position(ctx: Context, device: object) -> int:
    """The hub's `last_origin_seq` of the device (0 before its first item)."""
    if ctx.cluster is None or not isinstance(device, str) or _UUID_RE.fullmatch(device) is None:
        raise RouteError(500, "failed", "wipe: the runner's device.json names no device")
    query = f"SELECT coalesce(max(last_origin_seq), 0) FROM hub.device_stream WHERE device_id = '{device}'"
    return int(ctx.cluster.psql(query, HUB_DATABASE).strip() or 0)


def post_wipe_store(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    runner.stop()
    home = runner.env["AGENT_RUNNER_HOME"]
    # Identity present, the runner's own cluster stopped (no postmaster.pid left), then the hub's position.
    done = runner.node.run(["/bin/sh", "-c", WIPE_SCRIPT, "sh", home], timeout=60)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"wipe: exit {done['rc']} {_last_line(done)}".strip())
    try:
        identity = json.loads(str(done["stdout"]))
    except ValueError:
        identity = {}
    device = identity.get("device_id") if isinstance(identity, dict) else None
    position = _position(ctx, device)
    removed = runner.node.run(["rm", "-rf", f"{home}/pg"])
    if removed["rc"] != 0:
        raise RouteError(500, "failed", f"wipe: {_last_line(removed)}")
    golden = _golden(ctx)
    try:
        if golden is not None:
            runner.adopt(golden)
        runner.start()
    except ServiceError as error:
        raise RouteError(500, "failed", f"restart: {error}") from None
    return 200, {
        "name": runner.name,
        "state": runner.state(),
        "golden": golden,
        "device": device,
        "last_origin_seq": position,
    }


ROUTES = [
    Route("POST", "/hub/export-restore", post_export_restore),
    Route("POST", "/runners/{name}/wipe-store", post_wipe_store),
]
