"""Canaries of the zero-knowledge check E2E-09 (07-testing.md section 2.1; 04-crypto.md section 7; ADR-003).

- `POST /canary/new` (`{"labels": ["prompt", …], "values"?: {"invite-code": "<value>", …}}`): a fresh pair of canaries
  per label, random per run - `ascii` (`zk-<label>-<hex>`) and `unicode` (Greek, umlaut, CJK and an astral character
  around the same random part) - and, under `values`, values the spec met on its way (invitation and recovery codes)
  that must not reach the hub either. All are kept in the iteration's scratch directory (never in the artefacts) and
  answered as `{label: {"ascii", "unicode"} | {"value"}}`; a later call adds labels.
- `POST /canary/scan`: every canary must be absent on the hub side - a fresh `pg_dump` of the hub database, every log
  of the hub and of its PostgreSQL (also those of earlier hub processes), every byte that crossed the browser link and
  the runner link in both directions (the relay taps: requests, responses, SSE frames, WebSocket frames; the runner
  link is scanned on the runner device) and the hub's organization exports (`agent-hub export --org <id>`, once the
  command exists). Every pair is also searched by its shared random part alone (`<label>/token`: a truncated or
  re-cased copy). Before the scan a fresh probe value goes through every source on purpose (`_probe`), so the answer
  also proves the scan sees them: `probe` lists the sources it was found in. Answers `{findings: [{source, label,
  encoding}], sources: {label: bytes}, sse_frames, ws_messages, ws_failures, probe: [source, …], notes}`: labels
  instead of canary values, so the answer can go into the artefacts.
- `POST /canary/runner`: the positive control on the runner device (the execution device sees plaintext, 00-overview.md
  section 3): the double journals, the runner logs and a dump of every runner's store. Answers `{present: {label:
  [source, …]}, sources: {label: bytes}}`.
- `GET /canary/columns`: every column of the hub database against the map of 04-crypto.md section 7
  (`checks/hub_columns.py`): `{columns: [...], categories: {...}, problems: [...]}`.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any

from checks import hub_columns
from checks.plaintext_scan import check_canaries, http_source, scan, sse_frames, ws_failures, ws_messages

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.node import LocalNode, Node
from orchestrator.relay import BROWSER_TAP, RUNNER_TAP
from orchestrator.services import HUB_DATABASE, Cluster, ServiceError, find_pg_bin

STORE = "canaries.json"
PROBE_LABEL = "probe"
DUMP_NAME = "canary-scan.sql"
EXPORT_PREFIX = "canary-export-"
HUB_LOGS = re.compile(r"^(hub|postgres)(\.\d+)?\.log$")
RUNNER_LOGS = re.compile(r"^(runner-\d+|double-[a-z]+)(\.\d+)?\.log$")
JOURNALS = ("model", "github")
LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SCAN_TIMEOUT_S = 600.0
DUMP_TIMEOUT_S = 120.0
# Characters of the non-ASCII canary: Greek, Latin with umlaut, CJK and an emoji outside the basic plane (surrogate
# pairs in JSON escapes).
UNICODE_PARTS = ("Zκ", "Ärger", "日本語", "😀")
TOKEN_CHARS = 12
PROBE_SCHEMA = "e2e_probe"


def _store(ctx: Context) -> Path:
    return Path(ctx.scratch) / STORE


def _load(ctx: Context) -> dict[str, dict[str, str]]:
    path = _store(ctx)
    return dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}


def _values(ctx: Context) -> dict[str, str]:
    """canary value → label: `<label>` for given values; `<label>/ascii`, `<label>/unicode` and `<label>/token` (the
    random part both share, so a truncated or re-cased copy of either is found too) for pairs."""
    out = {}
    for label, entry in _load(ctx).items():
        for kind, value in entry.items():
            out[value] = label if kind == "value" else f"{label}/{kind}"
        if "ascii" in entry:
            token = entry["ascii"][-TOKEN_CHARS:]
            out[token] = out[token.upper()] = f"{label}/token"
    if not out:
        raise RouteError(409, "invalid", "no canaries yet: POST /canary/new first")
    return out


def post_new(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    labels = body.get("labels", []) if isinstance(body, dict) else None
    given = body.get("values", {}) if isinstance(body, dict) else None
    if not isinstance(labels, list) or not isinstance(given, dict) or not (labels or given):
        raise RouteError(400, "invalid", 'body must be {"labels": [...], "values"?: {...}}')
    names = [*labels, *given]
    if not all(isinstance(name, str) and LABEL.match(name) for name in names) or len(set(names)) != len(names):
        raise RouteError(400, "invalid", "labels must be distinct, lower case, digits and dashes")
    try:
        check_canaries(given.values())
    except ValueError as exc:
        raise RouteError(400, "invalid", str(exc)) from None
    stored = _load(ctx)
    taken = sorted(set(names) & set(stored))
    if taken:
        raise RouteError(409, "invalid", f"labels exist: {', '.join(taken)}")
    fresh: dict[str, dict[str, str]] = {}
    for label in labels:
        token = secrets.token_hex(TOKEN_CHARS // 2)
        fresh[label] = {"ascii": f"zk-{label}-{token}", "unicode": "-".join((*UNICODE_PARTS[:3], label, token))}
        fresh[label]["unicode"] = f"{fresh[label]['unicode']}-{UNICODE_PARTS[3]}"
    fresh.update({label: {"value": value} for label, value in given.items()})
    _store(ctx).write_text(json.dumps({**stored, **fresh}, ensure_ascii=False), encoding="utf-8")
    return 200, fresh


def _runner_scan(node: Node, ctx: Context, values: list[str], sources: list[str]) -> dict[str, Any]:
    """`checks.plaintext_scan --report` on `node` (its own checkout), sources as CLI options."""
    argv = [ctx.agent_python, "-m", "checks.plaintext_scan", "--report", *sources]
    for value in values:
        argv.append(f"--canary={value}")  # a value may start with "-" (base64url)
    done = node.run(
        argv, env={"PATH": "/usr/bin:/bin"}, cwd=f"{ctx.core_on_runner}/e2e/platform", timeout=SCAN_TIMEOUT_S
    )
    if done["rc"] not in (0, 1):
        raise RouteError(500, "failed", f"scan on {node.name} exited with {done['rc']}: {done['stderr'][-300:]}")
    return dict(json.loads(done["stdout"]))


def _hub_logs(node: LocalNode) -> dict[str, bytes]:
    logs = node.device.logs
    return {f"logs/{path.name}": path.read_bytes() for path in sorted(logs.iterdir()) if HUB_LOGS.match(path.name)}


def _exports(ctx: Context, notes: list[str]) -> dict[str, bytes]:
    hub = ctx.hub
    assert hub is not None
    assert ctx.cluster is not None
    if not hub.has_command("export"):
        notes.append("exports: agent-hub export is not available yet (DR-01); nothing to scan")
        return {}
    rows = json.loads(
        ctx.cluster.psql("SELECT coalesce(json_agg(id ORDER BY id), '[]'::json) FROM hub.org", HUB_DATABASE)
    )
    out = {}
    for org in rows:
        path = Path(ctx.scratch) / f"{EXPORT_PREFIX}{org}.jsonl"
        done = hub.node.run([hub.python, "-m", "agent_hub", "export", "--org", org, "--out", str(path)], env=hub.env)
        if done["rc"] != 0:
            raise RouteError(500, "failed", f"agent-hub export --org {org} exited with {done['rc']}")
        try:
            out[f"export/{org}"] = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
    notes.append(f"exports: {len(out)} organization(s)")
    return out


def _probe(ctx: Context, cluster: Cluster) -> str:
    """A fresh value put through every hub-side source the scan reads, so the scan proves it can see them: a request
    path through the browser link and through the runner link (both relays' `.up` taps, the hub's access log), a row
    in a probe schema of the hub database (the dump; the hub's own schema stays untouched) and a failing statement
    (the PostgreSQL log)."""
    probe = f"zk-probe-{secrets.token_hex(TOKEN_CHARS // 2)}"
    ctx.hub_node.http("GET", f"http://{ctx.browser_address}/v1/{probe}")
    ctx.runner_node.http("GET", f"http://127.0.0.1:{ctx.config.port('public')}/v1/{probe}")
    cluster.psql(
        f"CREATE SCHEMA IF NOT EXISTS {PROBE_SCHEMA}; CREATE TABLE IF NOT EXISTS {PROBE_SCHEMA}.marker (value text);"
        f"INSERT INTO {PROBE_SCHEMA}.marker VALUES ('{probe}')",
        HUB_DATABASE,
    )
    with contextlib.suppress(ServiceError):
        cluster.psql(f'SELECT 1 FROM "{probe}"', HUB_DATABASE)
    return probe


def post_scan(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    values = _values(ctx)
    cluster, hub_node = ctx.cluster, ctx.hub_node
    if cluster is None or ctx.hub is None or not isinstance(hub_node, LocalNode):
        raise RouteError(409, "invalid", "the hub and its database are not running on this device")
    probe = _probe(ctx, cluster)
    values[probe] = PROBE_LABEL
    dump = Path(ctx.scratch) / DUMP_NAME
    cluster.dump(str(dump))
    try:
        sources = {"hub-db-dump": dump.read_bytes()}
    finally:
        dump.unlink(missing_ok=True)
    sources.update(_hub_logs(hub_node))
    notes: list[str] = []
    sources.update(_exports(ctx, notes))
    frames = {"sse_frames": 0, "ws_messages": 0, "ws_failures": 0}
    tap = Path(ctx.scratch) / BROWSER_TAP
    for path in sorted(tap.iterdir()) if tap.is_dir() else []:
        data = path.read_bytes()
        frames["sse_frames"] += sse_frames(data)
        frames["ws_messages"] += ws_messages(data)
        frames["ws_failures"] += ws_failures(data)
        sources[f"browser-link/{path.name}"] = http_source(data)
    findings = [{**asdict(item), "label": values[item.canary]} for item in scan(sources, list(values))]
    sizes = {label: len(data) for label, data in sources.items()}
    runner = _runner_scan(
        ctx.runner_node, ctx, list(values), ["--http-dir", f"runner-link={ctx.runner_scratch}/{RUNNER_TAP}"]
    )
    findings += [{**item, "label": values[item["canary"]]} for item in runner["findings"]]
    sizes.update(runner["sources"])
    frames = {name: count + int(runner[name]) for name, count in frames.items()}
    out = [{key: item[key] for key in ("source", "label", "encoding")} for item in findings]
    probed = sorted({item["source"] for item in out if item["label"] == PROBE_LABEL})
    out = [item for item in out if item["label"] != PROBE_LABEL]
    return 200, {"findings": out, "sources": sizes, **frames, "probe": probed, "notes": notes}


def _store_dump(ctx: Context, name: str, env: dict[str, str], pg_bin: str) -> tuple[str, str]:
    """A plain dump of runner `name`'s store into the runner scratch: (CLI option, path)."""
    path = f"{ctx.runner_scratch}/{name}-store.sql"
    port_file = f"{env['AGENT_RUNNER_HOME']}/pg/port"
    port = ctx.runner_node.read_text(port_file).strip()
    argv = [f"{pg_bin}/pg_dump", "-h", "127.0.0.1", "-p", port, "-U", "agent", "-d", "agent_runner", "-f", path]
    done = ctx.runner_node.run(argv, env={"PATH": "/usr/bin:/bin"}, timeout=DUMP_TIMEOUT_S)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"{name} store dump exited with {done['rc']}: {done['stderr'][-300:]}")
    return f"store/{name}={path}", path


def post_runner(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    values = _values(ctx)
    node = ctx.runner_node
    files = [f"journal/{name}={ctx.runner_run_dir}/{name}-journal.jsonl" for name in JOURNALS]
    listing = node.run(["/bin/ls", "-1", f"{ctx.runner_run_dir}/logs"], env={"PATH": "/usr/bin:/bin"})
    files += [
        f"logs/{name}={ctx.runner_run_dir}/logs/{name}"
        for name in str(listing["stdout"]).split()
        if RUNNER_LOGS.match(name)
    ]
    dumps = []
    if ctx.runners:
        pg_bin = find_pg_bin(node)
        for name, runner in sorted(ctx.runners.items()):
            option, path = _store_dump(ctx, name, runner.env, pg_bin)
            files.append(option)
            dumps.append(path)
    try:
        report = _runner_scan(node, ctx, list(values), [arg for item in files for arg in ("--file", item)])
    finally:
        if dumps:
            node.run(["/bin/rm", "-f", *dumps], env={"PATH": "/usr/bin:/bin"})
    present: dict[str, list[str]] = {}
    for item in report["findings"]:
        sources = present.setdefault(values[item["canary"]], [])
        if item["source"] not in sources:
            sources.append(item["source"])
    return 200, {"present": present, "sources": report["sources"]}


def get_columns(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    cluster = ctx.cluster
    if cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")

    def rows(sql: str) -> list[dict[str, Any]]:
        wrapped = f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) AS q"
        return list(json.loads(cluster.psql(wrapped, HUB_DATABASE, read_only=True)))

    columns = rows(hub_columns.COLUMNS_SQL)
    query = hub_columns.values_sql(hub_columns.checked(columns))
    return 200, hub_columns.report(columns, rows(query) if query else [])


ROUTES = [
    Route("POST", "/canary/new", post_new),
    Route("POST", "/canary/scan", post_scan),
    Route("POST", "/canary/runner", post_runner),
    Route("GET", "/canary/columns", get_columns),
]
