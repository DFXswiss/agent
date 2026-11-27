"""U-RUN-dfxdebug (ADR-102 sections 3 to 6): `dfx.debug` on the device against a double of the DFX API.

- a session of the owner's task queries an allowed table in a released workgroup and gets the endpoint's answer;
- a task without a release (no record, released elsewhere, another person's instruction, an owner without access to
  the workgroup) is refused before anything reaches the API;
- the endpoint's refusals reach the session in plain words: a column outside the allowlist, the daily limit of
  persons whose sensitive data were read, the kill switch, a refused sign-in, an API that is down or slow;
- the audit row `dfx.read` names table, filter summary, row count and outcome, never a value;
- the signature, the address and the token appear in no answer, audit row or log line;
- there is no write: the client knows the sign-in and the debug endpoint only.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from agent_runner.dfxdebug import access, client, executors
from agent_runner.dfxdebug.executors import debug
from agent_runner.dfxdebug.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, OperationFailed, PrecheckRequest
from agent_runner.operations.settings import DEFAULTS
from tests.platform.runner.credentials.kit import ALICE, BOB, Env, kit_env, pid

from .kit import (
    ADDRESS,
    API_URL,
    KEYS,
    OTHER_WG,
    ROWS,
    SIGNATURE,
    TOKEN,
    WG,
    DfxApi,
    OpEnv,
    forget,
    operation,
    operation_context,
    session,
    sign_in_body,
    store,
)

Body = Callable[[Env, DfxApi, OpEnv], Awaitable[None]]
QUERY: dict[str, Any] = {
    "table": "buy_crypto",
    "select": [
        {"kind": "column", "column": "id"},
        {"kind": "column", "column": "created"},
        {"kind": "column", "column": "amlCheck"},
    ],
    "where": {
        "kind": "and",
        "children": [
            {"kind": "leaf", "column": "amlCheck", "op": "IN", "value": ["Pass", "Fail"]},
            {"kind": "not", "child": {"kind": "leaf", "column": "created", "op": "<", "value": "2026-01-01"}},
        ],
    },
    "orderBy": [{"column": "id", "direction": "ASC"}],
    "limit": 10,
}
SECRETS = (SIGNATURE, ADDRESS, TOKEN)


def run(dsn: str, monkeypatch: pytest.MonkeyPatch, body: Body) -> None:
    api = DfxApi()
    monkeypatch.setattr(client, "transport", api.transport())

    async def main() -> None:
        async with kit_env(dsn) as env:
            await env.add_ck()
            await env.add_ck(BOB)
            await body(env, api, OpEnv(env))

    asyncio.run(main())


async def _fails(env: Env, op_env: OpEnv, params: dict[str, Any] = QUERY, **kw: Any) -> OperationFailed:
    with pytest.raises(OperationFailed) as failed:
        await debug(operation_context(env, op_env, params, **kw))
    return failed.value


async def _refused(env: Env, *, wg: str = WG, actor: str | None = ALICE, owner: str | None = ALICE) -> CheckFailed:
    request = PrecheckRequest(env.context, operation(actor, wg), session(wg, owner), DEFAULTS, QUERY)
    with pytest.raises(CheckFailed) as refused:
        await PRECHECKS["dfx.debug"](request)
    return refused.value


def _hidden(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_a_session_queries_an_allowed_table_and_gets_the_result(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, api: DfxApi, op_env: OpEnv) -> None:
        record_id = store(env, sign_in_body(wgs=[OTHER_WG, WG]))
        request = PrecheckRequest(env.context, operation(), session(), DEFAULTS, QUERY)
        assert (await PRECHECKS["dfx.debug"](request)).dedupe == {}
        found = await debug(operation_context(env, op_env, QUERY))
        assert found == {"table": "buy_crypto", "keys": KEYS, "rows": ROWS, "rows_total": 2, "truncated": False}
        env.hub.check("ops/dfx.debug.params.json", QUERY)
        env.hub.check("ops/dfx.debug.result.json", found)
        assert api.paths() == ["/v1/auth/signIn", "/v1/support/debug"]
        (_m, _p, signed_in, _h), (_m2, _p2, sent, headers) = api.requests
        assert signed_in == {"address": ADDRESS, "signature": SIGNATURE}
        assert sent == QUERY  # the DTO unchanged
        assert headers["authorization"] == f"Bearer {TOKEN}"
        (audit,) = op_env.audits()
        assert (audit["action"], audit["result"], audit["target_kind"], audit["target_id"]) == (
            "dfx.read",
            "ok",
            "credential",
            record_id,
        )
        assert audit["detail"] == {
            "operation_id": audit["detail"]["operation_id"],
            "owner_id": ALICE,
            "table": "buy_crypto",
            "filter": "and(amlCheck IN, not(created <))",
            "rows": 2,
            "result": "ok",
        }
        env.hub.check("audit/dfx.read.json", audit["detail"])
        assert "Pass" not in json.dumps(audit)  # no value of the filter or the result
        # a second query signs in again: the token lived only for the first one
        await debug(
            operation_context(
                env, op_env, {"table": "asset", "select": [{"kind": "column", "column": "id"}], "limit": 1}
            )
        )
        assert api.paths()[2:] == ["/v1/auth/signIn", "/v1/support/debug"]
        assert op_env.audits()[1]["detail"]["filter"] == ""
        _hidden(
            json.dumps([found, op_env.outbox.items])
            + env.log()
            + repr(await access.signin_for(env.context, operation(), session()))
        )

    run(store_dsn, monkeypatch, body)


def test_a_task_without_a_release_is_refused(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env, api: DfxApi, op_env: OpEnv) -> None:
        missing = await _refused(env)
        assert (missing.code, missing.message) == ("not_found", access.MISSING)
        store(env, sign_in_body())  # stored, released nowhere
        assert (await _refused(env)).code == "rights_denied"
        store(env, sign_in_body(wgs=[OTHER_WG]), version=2)  # released to another workgroup
        await forget(env)
        nowhere = await _refused(env)
        assert (nowhere.code, nowhere.message) == ("rights_denied", access.NOT_RELEASED)
        store(env, sign_in_body(wgs=[WG]), version=3)
        await forget(env)
        for actor, owner in ((BOB, ALICE), (None, ALICE), (ALICE, None)):  # someone else's instruction, no person
            other = await _refused(env, actor=actor, owner=owner)
            assert (other.code, other.message) == ("rights_denied", access.NOT_OWNER)
        # released to a workgroup where the owner has no explicit access (the release ends with the access)
        elsewhere = pid("a/wg/left")
        store(env, sign_in_body(wgs=[WG, elsewhere]), version=4)
        await forget(env)
        assert (await _refused(env, wg=elsewhere)).code == "rights_denied"
        # an organization the device does not know
        unknown = cast(Any, SimpleNamespace(id=pid("s"), org_id=pid("z/org"), wg_id=WG, owner_id=ALICE))
        with pytest.raises(CheckFailed) as foreign:
            await PRECHECKS["dfx.debug"](PrecheckRequest(env.context, operation(), unknown, DEFAULTS, QUERY))
        assert foreign.value.code == "rights_denied"
        # the executor checks again
        failed = await _fails(env, op_env, actor=BOB)
        assert (failed.code, failed.message, failed.retryable) == ("rights_denied", access.NOT_OWNER, False)
        assert api.requests == []  # nothing reached the DFX API
        assert op_env.audits() == []

    run(store_dsn, monkeypatch, body)


def test_a_sign_in_that_does_not_open_or_is_unusable_is_refused(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, api: DfxApi, op_env: OpEnv) -> None:
        store(env, sign_in_body(wgs=[WG]), author="bob")  # alice's record signed by bob
        assert (await _refused(env)).code == "not_found"
        for changes in ({"api_url": "http://dfx-api.example.test/v1"}, {"signature": "with space"}, {"address": 7}):
            store(env, sign_in_body(wgs=[WG], **changes))
            await forget(env)
            assert (await _refused(env)).code == "rights_denied", changes
        store(env, sign_in_body(wgs=[WG]))
        await forget(env)
        env.hub.get_refusals.append((503, {"error": "unavailable"}))
        assert (await _refused(env)).code == "hub_unreachable"
        request = PrecheckRequest(env.context, operation(), session(), DEFAULTS, QUERY)
        assert (await PRECHECKS["dfx.debug"](request)).data == {}  # the hub answers again
        assert api.requests == []

    run(store_dsn, monkeypatch, body)


REFUSALS: list[tuple[str, tuple[int, Any], str, str, bool, str]] = [
    # (stage, answer, code, message part, retryable, audit result)
    (
        "debug",
        (400, {"message": ["Column 'x' is not allowed"], "statusCode": 400}),
        "params_invalid",
        "The DFX API refused the query: Column 'x' is not allowed.",
        False,
        "params_invalid",
    ),
    ("debug", (400, {}), "params_invalid", "The DFX API refused the query.", False, "params_invalid"),
    (
        "debug",
        (400, {"message": f"Value {SIGNATURE} of {ADDRESS}\nis wrong"}),
        "params_invalid",
        "Value [hidden] of [hidden] is wrong",
        False,
        "params_invalid",
    ),
    (
        "debug",
        (
            403,
            {
                "code": "PII_READ_LIMIT",
                "message": "Daily limit reached: sensitive personal data of at most 200 persons per UTC day",
            },
        ),
        "limit_exceeded",
        "persons per UTC day. The endpoint counts the persons",
        False,
        "limit_exceeded",
    ),
    (
        "debug",
        (403, {"code": "PII_READ_LIMIT"}),
        "limit_exceeded",
        "the count starts again at 00:00 UTC",
        False,
        "limit_exceeded",
    ),
    (
        "debug",
        (403, {"message": "Endpoint disabled", "error": "Forbidden", "statusCode": 403}),
        "upstream_error",
        "process SupportDebug",
        False,
        "switched_off",
    ),
    (
        "debug",
        (403, {"message": "Forbidden resource"}),
        "upstream_error",
        "needs the Debug role",
        False,
        "upstream_error",
    ),
    ("debug", (401, {"message": "Unauthorized"}), "upstream_error", "needs the Debug role", False, "upstream_error"),
    ("debug", (503, {"message": "down"}), "upstream_error", "not available right now", True, "upstream_error"),
    ("debug", (429, {"message": "slow down"}), "upstream_error", "not available right now", True, "upstream_error"),
    ("debug", (418, b"teapot"), "upstream_error", "no debug result", False, "upstream_error"),
    ("debug", (201, b"not json"), "upstream_error", "no debug result", False, "upstream_error"),
    ("debug", (201, {"keys": "id", "rows": []}), "upstream_error", "no debug result", False, "upstream_error"),
    ("debug", (201, {"keys": ["id"], "rows": [1]}), "upstream_error", "no debug result", False, "upstream_error"),
    ("debug", (201, {"keys": ["k"] * 101, "rows": []}), "upstream_error", "no debug result", False, "upstream_error"),
    ("debug", (201, {"keys": [1], "rows": []}), "upstream_error", "no debug result", False, "upstream_error"),
    (
        "debug",
        (201, {"keys": ["id"], "rows": [[1]] * 10001}),
        "upstream_error",
        "no debug result",
        False,
        "upstream_error",
    ),
    (
        "sign_in",
        (401, {"message": f"Invalid signature {SIGNATURE}"}),
        "upstream_error",
        "renews the signature",
        False,
        "upstream_error",
    ),
    ("sign_in", (502, {}), "upstream_error", "not available right now", True, "upstream_error"),
    ("sign_in", (201, {}), "upstream_error", "renews the signature", False, "upstream_error"),
    ("sign_in", (201, ["token"]), "upstream_error", "renews the signature", False, "upstream_error"),
]


def test_refusals_of_the_endpoint_reach_the_session_in_plain_words(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, api: DfxApi, op_env: OpEnv) -> None:
        store(env, sign_in_body(wgs=[WG]))
        for stage, answer, code, part, retryable, outcome in REFUSALS:
            api.sign_in, api.debug = (answer, None) if stage == "sign_in" else (None, answer)
            failed = await _fails(env, op_env)
            assert (failed.code, failed.retryable) == (code, retryable), answer
            assert failed.message is not None
            assert part in failed.message, (failed.message, answer)
            assert op_env.audits()[-1]["detail"]["result"] == outcome
            assert op_env.audits()[-1]["detail"]["rows"] == 0
            env.hub.check("audit/dfx.read.json", op_env.audits()[-1]["detail"])
        api.sign_in = api.debug = None
        for raised, code in ((httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "upstream_error")):
            api.raises = raised
            failed = await _fails(env, op_env)
            assert (failed.code, failed.retryable) == (code, True)
        assert [row["detail"]["result"] for row in op_env.audits()[-2:]] == ["timeout", "upstream_error"]
        _hidden(json.dumps(op_env.outbox.items) + env.log())

    run(store_dsn, monkeypatch, body)


def test_the_answer_stays_within_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [[index, "x" * 100] for index in range(50)]
    monkeypatch.setattr(executors, "MAX_ANSWER_BYTES", 2000)
    found = executors.answer("log", client.Answer(["id", "text"], rows))
    assert found["truncated"] is True
    assert found["rows_total"] == 50
    assert 0 < len(found["rows"]) < 50
    assert found["rows"] == rows[: len(found["rows"])]
    assert len(json.dumps(found).encode()) <= 2000
    assert executors.answer("log", client.Answer([], []))["truncated"] is False


def test_there_is_no_write() -> None:
    """The client sends two requests - the sign-in and the debug query - and names no other path of the API; the
    operation's DTO has no field for anything else (`test_dto`: `poll` is `params_invalid`)."""
    assert (client.SIGN_IN, client.DEBUG) == ("auth/signIn", "support/debug")
    tree = ast.parse(Path(client.__file__).read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    ]
    assert calls == ["post", "post"]  # the only calls on the HTTP client
    docstring = ast.get_docstring(tree, clean=False)
    texts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != docstring
    ]
    assert not [text for text in texts if "payIn" in text or "poll" in text.lower()]
    assert set(executors.EXECUTORS) == set(PRECHECKS) == {"dfx.debug"}
    assert API_URL.startswith("https://")
