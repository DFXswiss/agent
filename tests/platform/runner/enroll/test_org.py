"""U-RUN-identity / enroll: org device enrollment `agent-runner enroll --hub URL --code CODE` (05-runner.md section
2.1, 04-crypto.md section 5.8): success, used/unknown/invalid codes, manipulated packages and logs, and every failure
path without a half identity or stored data left behind."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from agent_runner.cli import run_cli
from agent_runner.config import Config, load_config
from agent_runner.crypto.invite import seal_package
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import entry_hash, sign_log_entry
from agent_runner.crypto.log.verify import verify_log
from agent_runner.crypto.storage import open_local, scope_key_context, storage_key
from agent_runner.encoding import b64u_encode
from agent_runner.enroll import org
from agent_runner.errors import RunnerError
from agent_runner.identity import key_path, load_identity, write_keys
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session
from agent_runner.store.pool import open_pool
from agent_runner.vm.capacity import host_reserve_mib

from ..assignments.orgkit import (
    HOST,
    ODEV,
    ONE,
    WALL,
    OrgHub,
    OrgWorld,
    cluster_tools,
    code_text,
    odev_keys,
    org_spec,
    package,
)
from ..reconcile.rig import ROOT, pid

URL = f"http://{HOST}"
DEVICE_ID = pid(ODEV)


@dataclass
class Case:
    config: Config
    host: Any
    dsn: str
    stops: list[Path]
    env: dict[str, str]


@pytest.fixture
def case(
    runner_home: Path, env_for: Callable[..., dict[str, str]], make_host: Callable[..., Any], store_dsn: str
) -> Case:
    env = env_for(runner_home, AGENT_RUNNER_HUB_URL=URL, AGENT_RUNNER_PLATFORM_ROOT_KEY=ROOT)
    stops: list[Path] = []
    host = make_host(env, cluster=cluster_tools(store_dsn, stops)).host
    return Case(load_config(env, platform="darwin"), host, store_dsn, stops, env)


def options(hub: OrgHub, **extra: Any) -> org.EnrollOptions:
    keys = odev_keys()
    values: dict[str, Any] = {
        "transport": httpx.ASGITransport(app=hub),  # type: ignore[arg-type]
        "wall": lambda: WALL,
        "generate": lambda: keys,
        "new_id": lambda: DEVICE_ID,
        "arch": "arm64",
        "physical_mib": lambda: 65536,
        "store_running": lambda path: False,
    }
    values.update(extra)
    return org.EnrollOptions(**values)


def enroll(config: Config, host: Any, hub: OrgHub, code: str | None = None, **extra: Any) -> org.Enrolled:
    return org.run_enroll(config, code or code_text(), host, options(hub, **extra))


def rows(dsn: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return list(conn.execute(sql).fetchall())


def nothing_left(config: Config, dsn: str) -> bool:
    stored = rows(
        dsn,
        "SELECT (SELECT count(*) FROM runner.log_entry) + (SELECT count(*) FROM runner.platform_log_entry)"
        " + (SELECT count(*) FROM runner.scope_key) + (SELECT count(*) FROM runner.log_head)",
    )[0][0]
    return load_identity(config.home) is None and not key_path(config.home).exists() and stored == 0


def targets(hub: OrgHub) -> list[str]:
    return [r.target.split("?")[0] for r in hub.requests]


# --- success ----------------------------------------------------------------------------------------------------


def test_successful_enrollment_stores_log_keys_and_identity(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    done = enroll(case.config, case.host, hub)
    assert (done.org, done.keys) == (world.spec.org, 2)
    identity = load_identity(case.config.home)
    assert identity is not None
    assert identity.device == done.device
    assert (identity.device.kind, identity.device.hub_url) == ("org", URL)
    assert identity.device.hub_key == b64u_encode(hub.public)
    assert identity.keys == odev_keys()
    assert targets(hub) == ["/v1/invites/lookup", "/v1/device/enroll", "/v1/device/platform-log"]
    body = hub.enrolled_body
    assert body is not None
    assert (body["kind"], body["os"], body["arch"], body["invite_id"]) == (
        "org",
        "darwin",
        "arm64",
        v.secrets(ODEV).invite_id,
    )
    assert body["entry"]["body"]["seq"] == 5
    assert body["entry"]["body"]["prev"] == entry_hash(world.spec.log.entries[4])
    assert body["capacity"]["vm_memory_budget_mib"] == 65536 - host_reserve_mib(case.config.memory_reserve_mib)
    # the store: platform log, org log with the accept, its head and the entitled package keys only
    assert [r[0] for r in rows(case.dsn, "SELECT seq FROM runner.log_entry ORDER BY seq")] == [0, 1, 2, 3, 4, 5]
    assert rows(case.dsn, "SELECT seq, hash FROM runner.log_head") == [(5, entry_hash(world.spec.log.entries[5]))]
    assert len(rows(case.dsn, "SELECT seq FROM runner.platform_log_entry")) == len(world.platform.entries)
    stored = rows(case.dsn, "SELECT scope, scope_id::text, epoch, key_ct, source FROM runner.scope_key ORDER BY scope")
    sealing = storage_key(identity.keys.sig)
    assert [(s, i, e, src) for s, i, e, _, src in stored] == [
        ("org", world.spec.org, 1, "package"),
        ("wg", ONE, 1, "package"),
    ]
    for scope, scope_id, epoch, blob, _ in stored:
        opened = open_local(sealing, "scope_key", scope_key_context(scope, scope_id, epoch), bytes(blob))
        assert opened == v.scope_key(scope, scope_id, epoch)
    assert case.stops == [case.config.pg_dir]
    # the book of the daemon loads the copies and acts for the org as an org device
    book = OrgBook(
        device_id=DEVICE_ID,
        kem_public=identity.keys.kem_public,
        sig_public=identity.keys.sig_public,
        root=ROOT,
        kind="org",
    )

    async def load() -> None:
        pool = await open_pool(case.dsn, max_size=1)
        try:
            async with session(pool) as conn:
                await book.ensure_loaded(conn)
        finally:
            await pool.close()

    asyncio.run(load())
    assert book.acting_orgs() == [world.spec.org]
    assert book.assignments() == [(world.spec.org, ONE)]
    assert book.status() == "enrolled"
    with pytest.raises(RunnerError, match="already_enrolled"):
        enroll(case.config, case.host, hub)


def test_a_log_with_platform_devices_is_anchored_then_verified(case: Case) -> None:
    world = OrgWorld(spec=org_spec(platform_device=True))
    hub = OrgHub(world)
    done = enroll(case.config, case.host, hub)
    assert done.keys == 2
    assert [r[0] for r in rows(case.dsn, "SELECT seq FROM runner.log_entry ORDER BY seq")] == list(range(7))


def test_the_enroll_command(case: Case, monkeypatch: pytest.MonkeyPatch, make_host: Callable[..., Any]) -> None:
    hub = OrgHub(OrgWorld())
    monkeypatch.setattr(org, "default_options", lambda: options(hub))
    captured = make_host(case.env, cluster=cluster_tools(case.dsn, []))
    assert run_cli(["enroll", "--hub", URL, "--code", code_text()], captured.host) == 0, captured.stderr.getvalue()
    line = captured.stdout.getvalue()
    assert line == (f"runner.enroll state=active kind=org device_id={DEVICE_ID} org_id={hub.world.spec.org} keys=2\n")
    assert code_text() not in line + captured.stderr.getvalue()


# --- codes and lookup -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "error"), [("ABCDE", "invite_code_length"), ("!!!!!-" * 5, "invite_code_invalid")])
def test_bad_codes_are_refused_before_any_request(case: Case, code: str, error: str) -> None:
    hub = OrgHub(OrgWorld())
    with pytest.raises(RunnerError, match=error):
        enroll(case.config, case.host, hub, code)
    assert hub.requests == []
    assert nothing_left(case.config, case.dsn)
    assert case.stops == []


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"error": "invite_unknown"}, "invite_unknown"),
        (429, {"error": "rate_limited"}, "rate_limited"),
        (404, {"error": "no_such_code"}, "response_invalid"),
        (404, {"error": 5}, "response_invalid"),
        (503, b"down", "response_invalid"),
        (200, b"not json", "response_invalid"),
        (200, [], "response_invalid"),
        (200, b" " * (org.MAX_LOOKUP_BYTES + 1), "response_invalid"),
    ],
)
def test_lookup_refusals(case: Case, status: int, body: object, error: str) -> None:
    world = OrgWorld(lookup_status=status, lookup_body=body)
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match=error):
        enroll(case.config, case.host, hub)
    assert targets(hub) == ["/v1/invites/lookup"]
    assert nothing_left(case.config, case.dsn)


def test_an_unreachable_hub(case: Case) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(RunnerError, match="hub_unreachable"):
        enroll(case.config, case.host, OrgHub(OrgWorld()), transport=httpx.MockTransport(refuse))
    assert nothing_left(case.config, case.dsn)


@pytest.mark.parametrize(
    ("changes", "error", "name"),
    [
        ({"kind": "user"}, "invite_unknown", "user"),
        ({"scope": "platform", "kind": "device"}, "invite_unknown", "platform"),
        ({"kind": "browser"}, "invite_unknown", None),
        ({"package_ct": None}, "response_invalid", None),
        ({"log": None}, "response_invalid", None),
        ({"log_head": {"seq": "1", "hash": "x"}}, "response_invalid", None),
        ({"log_head": None}, "response_invalid", None),
        ({"org_id": None}, "log_genesis", None),
    ],
)
def test_lookup_answers_of_the_wrong_kind_or_shape(
    case: Case, changes: dict[str, Any], error: str, name: str | None
) -> None:
    world = OrgWorld(lookup_changes=changes)
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match=error) as raised:
        enroll(case.config, case.host, hub)
    assert raised.value.name == name
    assert nothing_left(case.config, case.dsn)
    hub.violations.clear()


def test_a_package_of_another_code_is_invalid(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    world.sealed = hub.seal(label="other")
    with pytest.raises(RunnerError, match="invite_code_invalid"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


# --- manipulated logs ----------------------------------------------------------------------------------------------


def tamper(world: OrgWorld, seq: int, signer: str = "carol", **changes: Any) -> None:
    entries = list(world.spec.log.entries)
    body = {**entries[seq]["body"], **changes}
    entries[seq] = sign_log_entry(body, v.identity(signer).sig)
    world.served[world.spec.org] = entries


@pytest.mark.parametrize("platform_device", [False, True])
def test_a_forged_entry_is_refused(case: Case, platform_device: bool) -> None:
    world = OrgWorld(spec=org_spec(platform_device=platform_device))
    tamper(world, 2)  # re-signed by carol: a signature error, or (anchored) a broken hash chain
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match="log_chain" if platform_device else "log_signature"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_a_forged_entry_after_the_package_head_is_refused(case: Case) -> None:
    world = OrgWorld()
    world.spec.log.add("grant", "alice", subject=pid("bob"), wg=world.spec.default_wg, access="read")  # 5
    tamper(world, 5)  # signed by carol, who is no member
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match="log_signature"):
        enroll(case.config, case.host, hub)
    assert targets(hub) == ["/v1/invites/lookup"]
    assert nothing_left(case.config, case.dsn)


def test_a_log_without_the_package_head_is_a_rollback(case: Case) -> None:
    world = OrgWorld()
    world.served[world.spec.org] = world.spec.log.entries[:4]
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match="log_rollback"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


@pytest.mark.parametrize("platform_device", [False, True])
def test_a_wrong_served_head_is_a_chain_error(case: Case, platform_device: bool) -> None:
    world = OrgWorld(spec=org_spec(platform_device=platform_device))
    world.lookup_changes = {"log_head": {"seq": 3, "hash": entry_hash(world.spec.log.entries[3])}}
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match="log_chain"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_another_founder_is_refused(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    # a package pinning carol as founder: the log of alice does not verify against it
    changed = package(world.spec, hub_public=hub.public)
    changed["founder"] = v.sig_pk("carol")
    world.sealed = seal_package(v.secrets(ODEV), changed)
    with pytest.raises(RunnerError, match="log_genesis"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_anchored_logs_check_the_chain_genesis_and_invitation(case: Case) -> None:
    # founder mismatch
    world = OrgWorld(spec=org_spec(platform_device=True))
    hub = OrgHub(world)
    changed = package(world.spec, hub_public=hub.public)
    changed["founder"] = v.sig_pk("carol")
    world.sealed = seal_package(v.secrets(ODEV), changed)
    with pytest.raises(RunnerError, match="log_genesis"):
        enroll(case.config, case.host, hub)
    # org mismatch of genesis and package
    world = OrgWorld(spec=org_spec(platform_device=True))
    hub = OrgHub(world)
    changed = package(world.spec, hub_public=hub.public)
    changed["org"] = pid("x/org")
    world.sealed = seal_package(v.secrets(ODEV), changed)
    world.lookup_changes = {"org_id": pid("x/org")}
    with pytest.raises(RunnerError, match="log_genesis"):
        enroll(case.config, case.host, hub)
    # a broken link after the package head
    world = OrgWorld(spec=org_spec(platform_device=True))
    entries = list(world.spec.log.entries)
    entries[5] = sign_log_entry({**entries[5]["body"], "prev": entry_hash(entries[3])}, v.identity("alice").sig)
    world.served[world.spec.org] = entries
    world.lookup_changes = {"log_head": {"seq": 5, "hash": entry_hash(entries[5])}}
    with pytest.raises(RunnerError, match="log_chain"):
        enroll(case.config, case.host, OrgHub(world))
    # a gap in the sequence
    world = OrgWorld(spec=org_spec(platform_device=True))
    entries = list(world.spec.log.entries)
    entries[5] = sign_log_entry({**entries[5]["body"], "seq": 7}, v.identity("alice").sig)
    world.served[world.spec.org] = entries
    world.lookup_changes = {"log_head": {"seq": 5, "hash": entry_hash(entries[5])}}
    with pytest.raises(RunnerError, match="log_chain"):
        enroll(case.config, case.host, OrgHub(world))
    # a genesis that is not one
    world = OrgWorld(spec=org_spec(platform_device=True))
    first = world.spec.log.entries[0]
    world.spec.log.entries[0] = sign_log_entry({**first["body"], "pub": v.pub("carol")}, v.identity("alice").sig)
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match="log_chain"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


@pytest.mark.parametrize("platform_device", [False, True])
def test_the_invitation_must_be_in_the_log(case: Case, platform_device: bool) -> None:
    spec = org_spec(platform_device=platform_device)
    world = OrgWorld(spec=spec)
    hub = OrgHub(world)
    # a package of the code `other` whose head pins this log: no invitation of `other` in it
    world.sealed = seal_package(v.secrets("other"), package(spec, hub_public=hub.public))
    with pytest.raises(RunnerError, match="invite_not_in_log"):
        enroll(case.config, case.host, hub, code_text("other"))
    assert nothing_left(case.config, case.dsn)


def test_a_user_invitation_with_this_code_is_refused(case: Case) -> None:
    spec = org_spec(invite=False)
    spec.log.invite_user("alice", ODEV, wgs={ONE: "read"})  # 4: same code, kind user
    world = OrgWorld(spec=spec)
    with pytest.raises(RunnerError, match="invite_not_in_log"):
        enroll(case.config, case.host, OrgHub(world))
    assert nothing_left(case.config, case.dsn)


@pytest.mark.parametrize("platform_device", [False, True])
@pytest.mark.parametrize(("entry", "error"), [("revoke", "invite_revoked"), ("accept", "invite_consumed")])
def test_a_used_or_revoked_invitation_in_the_log(case: Case, entry: str, error: str, platform_device: bool) -> None:
    world = OrgWorld(spec=org_spec(platform_device=platform_device))
    if entry == "revoke":
        world.spec.log.add("invite_revoke", "alice", invite_id=v.secrets(ODEV).invite_id)
    else:
        world.spec.log.accept(ODEV, kind="device", key_label="dev2")
    with pytest.raises(RunnerError, match=error):
        enroll(case.config, case.host, OrgHub(world))
    assert nothing_left(case.config, case.dsn)


# --- registration and storing ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((404, {"error": "invite_unknown"}), "invite_unknown"),
        ((409, {"error": "seq_conflict"}), "seq_conflict"),
        ((200, {"state": "pending_certification"}), "response_invalid"),
    ],
)
def test_hub_refusals_of_the_enrollment_remove_the_keys(
    case: Case, answer: tuple[int, dict[str, Any]], error: str
) -> None:
    world = OrgWorld(enroll_answer=answer)
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match=error):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)
    assert case.stops == [case.config.pg_dir]
    hub.violations.clear()


def test_an_answer_not_signed_by_the_package_hub_key(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    world.sealed = seal_package(v.secrets(ODEV), package(world.spec, hub_public=bytes(range(32))))
    with pytest.raises(RunnerError, match="response_"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_a_platform_log_that_does_not_verify(case: Case) -> None:
    world = OrgWorld()
    entries = world.platform.entries
    world.served_platform = [*entries[:1], {**entries[1], "sig": entries[0]["sig"]}]
    hub = OrgHub(world)
    with pytest.raises(RunnerError, match=r"log_signature|log_chain"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_a_platform_device_without_certification_fails_after_the_enrollment(case: Case) -> None:
    world = OrgWorld(spec=org_spec(platform_device=True))
    hub = OrgHub(world)
    # the hub accepts, but the platform log it serves to the device lacks the certification
    original = hub.page

    def short_page(entries: list[dict[str, Any]], query: dict[str, list[str]], schema: str) -> tuple[int, Any]:
        if "platform-log" in schema:
            entries = entries[:2]
        return original(entries, query, schema)

    hub.page = short_page  # type: ignore[method-assign]
    with pytest.raises(RunnerError, match="log_rights"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)
    assert len(world.accepted) == 1


def test_the_platform_log_is_read_page_by_page(case: Case) -> None:
    world = OrgWorld(page_size=1)
    hub = OrgHub(world)
    enroll(case.config, case.host, hub)
    pages = [r.target for r in hub.requests if "platform-log" in r.target]
    assert len(pages) == len(world.platform.entries)
    assert pages[1].endswith("&after=0")


@pytest.mark.parametrize(
    "answer",
    [
        {"entries": "x", "head": {}, "next_after": None},
        {"entries": [5], "head": {}, "next_after": None},
        {"entries": [{"seq": 0}], "head": {}, "next_after": None},
    ],
)
def test_bad_platform_log_pages(case: Case, answer: dict[str, Any]) -> None:
    world = OrgWorld()
    hub = OrgHub(world)

    def page(entries: list[dict[str, Any]], query: dict[str, list[str]], schema: str) -> tuple[int, Any]:
        return 200, answer

    hub.page = page  # type: ignore[method-assign]
    with pytest.raises(RunnerError, match="response_invalid"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_a_platform_log_page_that_does_not_advance_ends_the_read(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    original = hub.page
    calls: list[int] = []

    def page(entries: list[dict[str, Any]], query: dict[str, list[str]], schema: str) -> tuple[int, Any]:
        status, answer = original(entries, query, schema)
        calls.append(1)
        if len(calls) == 2:
            answer["next_after"] = 0
        elif len(calls) == 1:
            answer["entries"] = answer["entries"][:1]
            answer["next_after"] = 0
        return status, answer

    hub.page = page  # type: ignore[method-assign]
    enroll(case.config, case.host, hub)
    assert len(calls) == 2


def test_a_store_with_data_is_refused(case: Case) -> None:
    with psycopg.connect(case.dsn) as conn:
        conn.execute("INSERT INTO runner.platform_log_entry (seq, entry_text, entry_hash) VALUES (0, '{}', 'x')")
    world = OrgWorld()
    with pytest.raises(RunnerError, match="store_not_empty"):
        enroll(case.config, case.host, OrgHub(world))
    assert load_identity(case.config.home) is None
    assert not key_path(case.config.home).exists()


def test_a_store_failure_removes_the_identity(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("server text")

    monkeypatch.setattr(org, "seal_local", broken)
    with pytest.raises(RunnerError, match="store_unavailable"):
        enroll(case.config, case.host, OrgHub(OrgWorld()))
    assert nothing_left(case.config, case.dsn)


def test_a_failed_device_write_rolls_back(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(home: Path, device: Any) -> None:
        raise RunnerError("identity_exists", "device.json")

    monkeypatch.setattr(org, "write_device", broken)
    with pytest.raises(RunnerError, match="identity_exists"):
        enroll(case.config, case.host, OrgHub(OrgWorld()))
    assert load_identity(case.config.home) is None
    assert not key_path(case.config.home).exists()


# --- configuration ---------------------------------------------------------------------------------------------------


def test_configuration_and_identity_checks(case: Case) -> None:
    hub = OrgHub(OrgWorld())
    with pytest.raises(RunnerError, match="config_invalid") as raised:
        org.run_enroll(replace(case.config, hub_url=None), code_text(), case.host, options(hub))
    assert raised.value.name == "HUB_URL"
    with pytest.raises(RunnerError, match="config_invalid") as raised:
        org.run_enroll(replace(case.config, platform_root_key=None), code_text(), case.host, options(hub))
    assert raised.value.name == "PLATFORM_ROOT_KEY"
    with pytest.raises(RunnerError, match="config_invalid"):
        org.run_enroll(replace(case.config, hub_url="ftp://x"), code_text(), case.host, options(hub))
    write_keys(case.config.home, odev_keys())
    with pytest.raises(RunnerError, match="identity_incomplete"):
        enroll(case.config, case.host, hub)
    assert hub.requests == []


def test_host_facts_in_the_body(case: Case) -> None:
    hub = OrgHub(OrgWorld())
    enroll(case.config, case.host, hub, arch="Power Mac")
    assert hub.enrolled_body is not None
    assert hub.enrolled_body["arch"] == "unknown"


def test_default_options() -> None:
    made = org.default_options()
    assert made.transport is None
    assert made.os_name == "darwin"


def test_entitled_keys_skip_unknown_epochs() -> None:
    world = OrgWorld()
    world.spec.log.accept(ODEV, kind="device")
    log = verify_log(world.spec.log.entries, v.sig_pk("alice"))
    keys = [
        {"scope": "wg", "scope_id": ONE, "epoch": 2, "key": b64u_encode(bytes(32))},
        {"scope": "wg", "scope_id": ONE, "epoch": 1, "key": b64u_encode(bytes(32))},
    ]
    assert [slot[:3] for slot in org.entitled_keys(log, DEVICE_ID, keys)] == [("wg", ONE, 1)]


def test_lookup_body_is_canonical(case: Case) -> None:
    hub = OrgHub(OrgWorld())
    enroll(case.config, case.host, hub)
    assert hub.requests[0].body == {"invite_id": v.secrets(ODEV).invite_id}
    assert hub.requests[0].raw == b"" or json.loads(hub.requests[0].raw or b"{}") is not None


def test_a_package_naming_another_org_than_the_log(case: Case) -> None:
    world = OrgWorld()
    hub = OrgHub(world)
    changed = package(world.spec, hub_public=hub.public)
    changed["org"] = pid("x/org")
    world.sealed = seal_package(v.secrets(ODEV), changed)
    world.lookup_changes = {"org_id": pid("x/org")}
    with pytest.raises(RunnerError, match="log_genesis"):
        enroll(case.config, case.host, hub)
    assert nothing_left(case.config, case.dsn)


def test_the_platform_log_read_is_bounded(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(org, "MAX_PAGES", 1)
    world = OrgWorld(page_size=1)
    hub = OrgHub(world)
    enroll(case.config, case.host, hub)
    assert len([r for r in hub.requests if "platform-log" in r.target]) == 1
    assert rows(case.dsn, "SELECT count(*) FROM runner.platform_log_entry") == [(1,)]


def test_a_store_with_leftovers_of_another_identity_is_refused(case: Case) -> None:
    with psycopg.connect(case.dsn) as conn:
        conn.execute("INSERT INTO runner.sync_state (key, value) VALUES ('device_revoked', 'key_revoked')")
    with pytest.raises(RunnerError, match="store_not_empty"):
        enroll(case.config, case.host, OrgHub(OrgWorld()))
    assert load_identity(case.config.home) is None


@pytest.mark.parametrize(
    ("body", "needed"),
    [
        ({"type": "device_assign", "platform_seq": 3}, True),
        ({"type": "device_assign_org", "platform_seq": 3}, True),
        ({"type": "device_assign_org"}, False),
        ({"type": "grant", "platform_seq": 3}, False),
    ],
)
def test_platform_assignments_of_both_kinds_need_the_platform_log(body: dict[str, object], needed: bool) -> None:
    """ADR-075: an assignment to the whole organization of a platform device is verified with the platform log too."""
    assert org._needs_platform([{"body": body}, "not an entry"]) is needed
