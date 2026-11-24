"""`agent-runner status [--json]` and `agent-runner doctor [--clear NAME]` (05-runner.md section 1.2).

`status` reads the identity files and the runner store (a stopped cluster is started for the command and stopped
afterwards, `vm.cli.existing_store`); it makes no hub request. Facts, one per line (`--json`: one document):

    runner.status.identity enrolled=yes|no [device_id=… kind=org|platform hub_url=… enrolled_at=…]
    runner.status.hub last_connect=<time|never> sync=ok|<sync_fatal code> revoked=no|<reason> stopped_orgs=<n>
        platform_log=ok|<code>
    runner.status.outbox position=known|unknown last_origin_seq=<n> next_origin_seq=<n> pushed_origin_seq=<n>
        backlog=<n>
    runner.status.keys orgs=<verified org logs> scope_keys=<n>
    runner.status.vm vm_id=… lima_name=… state=… wg_id=<id|none> golden_version=…      (per VM, not deleted)
    runner.status.session session_id=… state=… wg_id=… vm_id=<id|none> depth=<n>        (per session not ended)
    runner.status.pool size=<POOL_SIZE> ready=<n>
    runner.capacity budget_mib=… used_mib=… slots_total=… slots_free=… pool_ready=… capacity_low=yes|no

`doctor` runs the checks of `doctor.checks` and prints `runner.doctor.check name=… result=ok|warn|fail detail=…
[value=…]` per check and `runner.doctor result=ok|warn|fail` last; the exit code is 1 when a check failed. With
`--clear NAME` it removes one operator marker (`sync_fatal`, `platform_stopped`, `org_stopped_<32 hex>`) from the
store instead (`runner.doctor.cleared name=… removed=yes|no`); it takes effect at the next daemon start.
Never printed: keys, tokens, codes, decrypted content.

`last_origin_seq` is the highest number the device assigned: the highest stored item, or the hub position adopted by a
wiped store that has not written an item since (DR-01; the outbox is empty then, KOR-10); `next_origin_seq` is the
number the next item takes (`sync.outbox.enqueue`). `position=unknown`: the store has not learned the hub's position
yet (fresh or wiped, before the first reconnect); `pushed_origin_seq` is shown as 0 then and the numbers are not final
(the items are renumbered to follow the hub's position when it is adopted).
"""

from __future__ import annotations

import argparse
import functools
from typing import Any

import psycopg

from agent_runner import service
from agent_runner.config import Config
from agent_runner.doctor import checks
from agent_runner.errors import RunnerError
from agent_runner.sessions.records import FINAL
from agent_runner.vm import capacity as capacity_module
from agent_runner.vm import cli as store_cli
from agent_runner.vm import lima

EXIT_OK = 0
EXIT_FAILURE = 1
WS_LAST_CONNECT = "hub_ws_last_connect"
PUSHED = "pushed_origin_seq"
PLATFORM_STOPPED = "platform_stopped"


def register(subparsers: Any) -> None:
    status = subparsers.add_parser("status", help="identity, hub connection, outbox, VMs, sessions, pool, capacity")
    status.set_defaults(handler=cmd_status)
    doctor = subparsers.add_parser("doctor", help="check the host, the store, the image and the hub")
    doctor.add_argument("--clear", metavar="NAME", help="remove an operator marker (sync_fatal, platform_stopped, ...)")
    doctor.set_defaults(handler=cmd_doctor)


def default_options() -> checks.DoctorOptions:
    """The options of a real run (tests replace this function)."""
    return checks.DoctorOptions()


def _rows(dsn: str | None) -> dict[str, Any]:
    empty: dict[str, Any] = {"state": {}, "last": 0, "backlog": 0, "orgs": 0, "keys": 0, "vms": [], "sessions": []}
    if dsn is None:
        return empty
    try:
        with psycopg.connect(dsn) as conn:
            state = {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM runner.sync_state")}
            pushed = int(state.get(PUSHED, "0") or 0)
            row = conn.execute(
                "SELECT GREATEST(COALESCE(MAX(origin_seq), 0), %s), COUNT(*) FILTER (WHERE origin_seq > %s)"
                " FROM runner.outbox",
                (pushed, pushed),
            ).fetchone()
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM runner.log_head), (SELECT COUNT(*) FROM runner.scope_key)"
            ).fetchone()
            vms = conn.execute(
                "SELECT id, lima_name, state, wg_id::text, golden_version, memory_mib FROM runner.vm"
                " WHERE state <> 'deleted' ORDER BY created_at, id"
            ).fetchall()
            sessions = conn.execute(
                "SELECT id::text, state, wg_id::text, vm_id, depth FROM runner.session WHERE state <> ALL(%s)"
                " ORDER BY created_at, id",
                (sorted(FINAL),),
            ).fetchall()
    except psycopg.Error:
        raise RunnerError("store_unavailable") from None
    assert row is not None
    assert counts is not None
    return {
        "state": state,
        "last": int(row[0]),
        "backlog": int(row[1]),
        "orgs": int(counts[0]),
        "keys": int(counts[1]),
        "vms": vms,
        "sessions": sessions,
    }


def cmd_status(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    identity = ctx.identity(config)
    with store_cli.existing_store(config, ctx) as dsn:
        found = _rows(dsn)
    out = ctx.out
    if identity is None:
        out.fact("status.identity", {"enrolled": False})
    else:
        device = identity.device
        out.fact(
            "status.identity",
            {
                "enrolled": True,
                "device_id": device.device_id,
                "kind": device.kind,
                "hub_url": device.hub_url,
                "enrolled_at": device.enrolled_at,
            },
        )
    state: dict[str, str] = found["state"]
    out.fact(
        "status.hub",
        {
            "last_connect": state.get(WS_LAST_CONNECT, "never"),
            "sync": state.get("sync_fatal", "ok"),
            "revoked": state.get(checks.REVOKED, "no"),
            "removed": state.get(checks.REMOVED, "no"),
            "stopped_orgs": sum(1 for key in state if key.startswith(checks.ORG_STOPPED)),
            "platform_log": state.get(PLATFORM_STOPPED, "ok"),
        },
    )
    out.fact(
        "status.outbox",
        {
            "position": "known" if PUSHED in state else "unknown",
            "last_origin_seq": found["last"],
            "next_origin_seq": found["last"] + 1,
            "pushed_origin_seq": int(state.get(PUSHED, "0") or 0),
            "backlog": found["backlog"],
        },
    )
    out.fact("status.keys", {"orgs": found["orgs"], "scope_keys": found["keys"]})
    for vm_id, name, vm_state, wg, golden, _memory in found["vms"]:
        out.fact(
            "status.vm",
            {"vm_id": vm_id, "lima_name": name, "state": vm_state, "wg_id": wg or "none", "golden_version": golden},
        )
    for sid, session_state, wg, vm_id, depth in found["sessions"]:
        out.fact(
            "status.session",
            {"session_id": sid, "state": session_state, "wg_id": wg, "vm_id": vm_id or "none", "depth": depth},
        )
    slots = [(str(vm[2]), int(vm[5])) for vm in found["vms"]]
    capacity = capacity_module.compute(
        physical_mib=capacity_module.physical_memory_mib(),
        reserve_mib=config.memory_reserve_mib,
        vm_memory_mib=config.vm_memory_mib,
        vms=slots,
    )
    out.fact("status.pool", {"size": config.pool_size, "ready": capacity.pool_ready})
    out.fact(
        "capacity",
        {
            "budget_mib": capacity.budget_mib,
            "used_mib": capacity.used_mib,
            "slots_total": capacity.slots_total,
            "slots_free": capacity.slots_free,
            "pool_ready": capacity.pool_ready,
            "capacity_low": capacity.capacity_low,
        },
    )
    return EXIT_OK


def _clear(config: Config, ctx: Any, name: str) -> int:
    if not checks.clearable(name):
        raise RunnerError("marker_invalid")
    with store_cli.existing_store(config, ctx) as dsn:
        if dsn is None:
            raise RunnerError("store_unavailable")
        removed = checks.clear_marker(dsn, name)
    ctx.out.fact("doctor.cleared", {"name": name, "removed": removed})
    return EXIT_OK


def _report(ctx: Any, found: list[checks.Check]) -> int:
    for item in found:
        fields: dict[str, str | int | bool] = {"name": item.name, "result": item.result, "detail": item.detail}
        if item.value is not None:
            fields["value"] = item.value
        ctx.out.fact("doctor.check", fields)
    results = {item.result for item in found}
    overall = checks.FAIL if checks.FAIL in results else checks.WARN if checks.WARN in results else checks.OK
    ctx.out.fact("doctor", {"result": overall})
    return EXIT_FAILURE if overall == checks.FAIL else EXIT_OK


def daemon_path(config: Config, ctx: Any) -> str:
    """The `PATH` the daemon (and so the login) searches: the one of the installed plist, else the caller's."""
    plist = service.read_plist(service.plist_path(config, ctx.host))
    env = plist.get("EnvironmentVariables") if plist is not None else None
    found = env.get("PATH") if isinstance(env, dict) else None
    return found if isinstance(found, str) else str(ctx.host.env.get("PATH", ""))


def cmd_doctor(args: argparse.Namespace, ctx: Any) -> int:
    system = checks.check_os(ctx.host.platform)
    if system.result == checks.FAIL:
        # the configuration refuses other systems (`unsupported_os`): nothing else can be checked
        return _report(ctx, [system])
    config: Config = ctx.config(require=frozenset())
    if args.clear is not None:
        return _clear(config, ctx, args.clear)
    options = default_options()
    limactl = options.limactl or functools.partial(lima.run_limactl, env=dict(ctx.host.env))
    identity = ctx.identity(config)
    found: list[checks.Check] = [system, checks.check_lima(limactl), checks.check_vz(limactl)]
    try:
        with store_cli.existing_store(config, ctx) as dsn:
            facts = checks.read_store(dsn)
    except RunnerError:
        facts = checks.StoreFacts()
    found += [
        checks.check_store(facts),
        checks.check_golden(facts),
        checks.check_landlock(facts, config.home / "golden"),
        checks.check_login_cli(config, daemon_path(config, ctx), options.login_timeout),
        *checks.check_hub(config, identity, options),
        checks.check_textfile(config.textfile_dir),
        checks.check_operator_subscriptions(config),
        checks.check_operator_github(config),
        checks.check_test_mode(config.test_mode),
        *checks.check_sync(facts),
    ]
    return _report(ctx, found)
