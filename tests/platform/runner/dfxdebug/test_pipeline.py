"""U-RUN-dfxdebug-pipeline (ADR-102 sections 3 to 6): `dfx.debug` through the real operation pipeline - journaled
request, formal checks, pre-check, registration with the sealed `operation.params` event, execution, the sealed
`operation.result` event, the audit item and the answer to the guest.

- the owner's session in a released workgroup gets the rows;
- a session whose task owner did not release the sign-in to the workgroup is rejected (`rights_denied`) and the DFX
  API sees no request;
- the signature, the address and the token are in no event, outbox item, answer or log line of the device;
- there is no write type: `dfx.poll` is `unknown_type`, a `poll` parameter is `params_invalid`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agent_runner.dfxdebug import client
from agent_runner.dfxdebug.executors import EXECUTORS
from agent_runner.dfxdebug.op_prechecks import PRECHECKS
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.kit import ck, make_record
from ..operations.kit import OWNER, WG, OKit, setup
from ..reconcile.rig import Rig, standard_world
from .kit import ADDRESS, KEYS, ROWS, SIGNATURE, TOKEN, DfxApi, sign_in_body

QUERY: dict[str, Any] = {
    "table": "buy_crypto",
    "select": [{"kind": "column", "column": "id"}, {"kind": "column", "column": "created"}],
    "where": {"kind": "leaf", "column": "amlCheck", "op": "=", "value": "Pass"},
    "limit": 2,
}


def with_kit(
    dsn: str, monkeypatch: pytest.MonkeyPatch, body: Callable[[OKit, DfxApi, dict[str, Any]], Awaitable[None]]
) -> None:
    """A pipeline with the executor and pre-check of `dfx.debug`, the owner's `CK` on the device and the hub serving
    the owner's `dfx_debug` record put into the returned mapping (`404` while absent)."""
    api = DfxApi()
    monkeypatch.setattr(client, "transport", api.transport())

    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS)
            async with session(env.pool) as conn:
                await store_scope_key(conn, env.sync.storage_key, ("cred", OWNER, 1), ck(OWNER), source="wrap")
            served: dict[str, Any] = {}
            original = env.hub.route

            def route(method: str, target: str, data: Any) -> tuple[int, dict[str, Any]]:
                if (method, target) == ("GET", f"/v1/device/credentials/{OWNER}/dfx_debug"):
                    return (200, {"record": served["record"]}) if served else (404, {"error": "not_found"})
                return original(method, target, data)

            env.hub.route = route  # type: ignore[method-assign,assignment]
            await body(found, api, served)

    asyncio.run(main())


def release(served: dict[str, Any], wgs: list[str]) -> None:
    served["record"] = make_record(sign_in_body(wgs=wgs), owner=OWNER, kind="dfx_debug", author="alice")


async def _everything(kit: OKit) -> str:
    """Every trace of the device: outbox items (events opened), registrations, answers and log lines."""
    events = [(item["body"] if kind == "event" else item) for kind, item in await kit.outbox()]
    registrations = [
        kit.hub.params_event(index) for index, sent in enumerate(kit.hub.requests) if "params_event" in sent
    ]
    return json.dumps([events, registrations, kit.guest.answers(), kit.hub.requests]) + "\n".join(kit.lines)


def test_the_owners_session_gets_the_rows_and_the_secret_stays_on_the_device(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(kit: OKit, api: DfxApi, served: dict[str, Any]) -> None:
        release(served, [WG])
        sid = await kit.session()
        op = await kit.run(sid, "dfx.debug", QUERY)
        assert op.state == "succeeded", (op.error_code, op.answer)
        assert op.answer is not None
        assert op.answer["result"] == {
            "table": "buy_crypto",
            "keys": KEYS,
            "rows": ROWS,
            "rows_total": 2,
            "truncated": False,
        }
        _header, params = kit.hub.params_event()
        assert (params["type"], params["params"]) == ("dfx.debug", QUERY)
        audits = [item for kind, item in await kit.outbox() if kind == "audit"]
        assert [(row["action"], row["detail"]["filter"], row["detail"]["rows"]) for row in audits] == [
            ("dfx.read", "amlCheck =", 2)
        ]
        trace = await _everything(kit)
        for secret in (SIGNATURE, ADDRESS, TOKEN):
            assert secret not in trace
        assert api.paths() == ["/v1/auth/signIn", "/v1/support/debug"]

    with_kit(store_dsn, monkeypatch, body)


def test_a_task_without_release_is_rejected_and_nothing_is_read(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(kit: OKit, api: DfxApi, served: dict[str, Any]) -> None:
        release(served, [])
        sid = await kit.session()
        kit.hub.decisions.append({"state": "rejected", "error_code": "rights_denied"})
        op = await kit.run(sid, "dfx.debug", QUERY)
        assert op.state == "rejected"
        assert op.answer is not None
        assert op.answer["error"]["code"] == "rights_denied"
        assert "not released to this workgroup" in op.answer["error"]["message"]
        assert api.requests == []
        assert [item for kind, item in await kit.outbox() if kind == "audit"] == []

    with_kit(store_dsn, monkeypatch, body)


def test_poll_is_not_possible(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(kit: OKit, api: DfxApi, served: dict[str, Any]) -> None:
        release(served, [WG])
        sid = await kit.session()
        poll = {"address": "deposit", "blockchain": "Ethereum", "fromBlock": 1, "toBlock": 2}
        kit.hub.decisions.append({"state": "rejected", "error_code": "unknown_type"})
        unknown = await kit.run(sid, "dfx.poll", poll)
        assert (unknown.state, unknown.answer and unknown.answer["error"]["code"]) == ("rejected", "unknown_type")
        kit.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        smuggled = await kit.run(sid, "dfx.debug", {**QUERY, "poll": poll})
        assert smuggled.answer is not None
        assert (smuggled.state, smuggled.answer["error"]["code"]) == ("rejected", "params_invalid")
        assert smuggled.answer["error"]["pointers"] == ["/params/poll"]
        assert api.requests == []

    with_kit(store_dsn, monkeypatch, body)
