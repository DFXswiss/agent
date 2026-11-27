"""Executor of `dfx.debug` (ADR-102 sections 4 to 6; 05-runner.md section 12.3; `<area>/executors.py:EXECUTORS`).

The device queries the DFX API itself - no work VM, no slot: the task owner's released sign-in
(`access.signin_for`, checked again at execution) opens on the device for this one query, the client signs in, keeps
the token in memory for the query only and sends the DTO unchanged (`client.query`, read only: there is no write).

- **Answer**: `{table, keys, rows, rows_total, truncated}`; the last rows are left out (`truncated`) until the answer
  is at most `MAX_ANSWER_BYTES`. A key list or a row count beyond the contract is `upstream_error`.
- **Refusals of the endpoint** reach the session in plain words (E-05): `400` -> `params_invalid` with the endpoint's
  message (a column or table outside its allowlist); `403` `PII_READ_LIMIT` -> `limit_exceeded` (the endpoint counts
  the persons whose sensitive data an account reads per UTC day and refuses beyond its limit; the session is told
  when the count starts again and how to avoid it); `403` `Endpoint disabled` -> `upstream_error` naming the kill
  switch (process `SupportDebug`: nothing is retried, nothing read); any other `401`/`403` -> `upstream_error`
  naming the sign-in's role; a refused sign-in -> `upstream_error` naming the owner's remedy; `429` and `5xx` ->
  `upstream_error`, retryable; no answer in time -> `timeout`, retryable. A message is passed on only as printable
  ASCII, bounded, and with the address, the signature and every other secret of the sign-in removed.
- **Audit** (section 6): every attempt writes `dfx.read` through the outbox (target the sign-in record) with the
  operation, the owner, the table, the filter summary (`dto.filter_summary`: columns and operators, never a value), the
  number of rows answered and the outcome - never a result value, never the sign-in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from agent_runner.dfxdebug import access, client, dto
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed

MAX_ANSWER_BYTES = 200 * 1024
MAX_KEYS = 100
MAX_KEY_LENGTH = 128
MAX_ROWS = 10000
MAX_MESSAGE = 600
LIMIT_CODE = "PII_READ_LIMIT"
SWITCHED_OFF = "Endpoint disabled"
LIMIT = (
    " The endpoint counts the persons whose sensitive personal data an account reads per UTC day and refuses beyond"
    " its daily limit; the count starts again at 00:00 UTC. Until then, query without sensitive columns."
)
OFF = (
    "The DFX debug endpoint is switched off (process SupportDebug); nothing was read. It stays off until the DFX API"
    " admins switch it on again."
)
ROLE = "The DFX API refused the debug query for this sign-in; the wallet needs the Debug role."
SIGN_IN = (
    "The DFX API refused the debug sign-in; the task owner renews the signature with"
    " agent-platform credentials set-dfx-debug."
)
BUSY = "The DFX API is not available right now; try again later."
SLOW = "The DFX API did not answer in time; try again later."
BROKEN = "The DFX API answered with something that is no debug result."


def _message(text: str | None, sign_in: access.SignIn) -> str:
    """The endpoint's message for the session: no secret of the sign-in, printable ASCII, bounded."""
    found = text or ""
    for secret in (sign_in.signature, sign_in.address):
        found = found.replace(secret, "[hidden]")
    found = "".join(char if " " <= char <= "~" else " " for char in found).strip()
    return found[:MAX_MESSAGE]


def _refused_query(said: str) -> str:
    return f"The DFX API refused the query: {said.rstrip('.')}." if said else "The DFX API refused the query."


def failure(refused: client.Refused, sign_in: access.SignIn) -> tuple[str, OperationFailed]:
    """The audit result and the failure of a refusal (module docstring)."""
    said = _message(refused.message, sign_in)
    if refused.status == 429 or refused.status >= 500:
        return "upstream_error", OperationFailed("upstream_error", retryable=True, message=BUSY)
    if refused.stage == "sign_in":
        return "upstream_error", OperationFailed("upstream_error", message=SIGN_IN)
    if refused.status == 400:
        return "params_invalid", OperationFailed("params_invalid", message=_refused_query(said))
    if refused.status == 403 and refused.code == LIMIT_CODE:
        return "limit_exceeded", OperationFailed("limit_exceeded", message=_refused_query(said) + LIMIT)
    if refused.status == 403 and said == SWITCHED_OFF:
        return "switched_off", OperationFailed("upstream_error", message=OFF)
    return "upstream_error", OperationFailed("upstream_error", message=ROLE if refused.status in (401, 403) else BROKEN)


def answer(table: str, found: client.Answer) -> dict[str, Any]:
    """The result object within `MAX_ANSWER_BYTES` (module docstring)."""
    keys, rows = found.keys, found.rows
    if (
        len(keys) > MAX_KEYS
        or len(rows) > MAX_ROWS
        or not all(isinstance(key, str) and len(key) <= MAX_KEY_LENGTH for key in keys)
    ):
        raise OperationFailed("upstream_error", message=BROKEN)
    result: dict[str, Any] = {"table": table, "keys": keys, "rows": [], "rows_total": len(rows), "truncated": False}
    room = MAX_ANSWER_BYTES - len(json.dumps(result).encode()) - 64
    kept = 0
    for row in rows:
        room -= len(json.dumps(row).encode()) + 1
        if room < 0:
            break
        kept += 1
    result["rows"] = rows[:kept]
    result["truncated"] = kept < len(rows)
    return result


async def _audit(ctx: OperationContext, sign_in: access.SignIn, rows: int, outcome: str) -> None:
    op, env = ctx.operation, ctx.env
    row = {
        "action": "dfx.read",
        "result": "ok" if outcome == "ok" else "failed",
        "task_id": op.task_id,
        "session_id": op.session_id,
        "target_kind": "credential",
        "target_id": sign_in.record_id,
        "detail": {
            "operation_id": op.id,
            "owner_id": sign_in.owner_id,
            "table": ctx.params["table"],
            "filter": dto.filter_summary(ctx.params.get("where")),
            "rows": rows,
            "result": outcome,
        },
    }
    async with env.transaction() as conn:
        await env.outbox.enqueue(conn, "audit", row)


async def debug(ctx: OperationContext) -> dict[str, Any]:
    try:
        sign_in = await access.signin_for(ctx.context, ctx.operation, ctx.session)
    except CheckFailed as refused:
        retryable = refused.code == "hub_unreachable"
        raise OperationFailed(refused.code, retryable=retryable, message=refused.message) from None
    table = str(ctx.params["table"])
    try:
        result = answer(table, await client.query(sign_in, ctx.params))
    except client.Refused as refused:
        outcome, failed = failure(refused, sign_in)
    except httpx.TimeoutException:
        outcome, failed = "timeout", OperationFailed("timeout", retryable=True, message=SLOW)
    except httpx.TransportError:
        outcome, failed = "upstream_error", OperationFailed("upstream_error", retryable=True, message=BUSY)
    except OperationFailed as broken:
        outcome, failed = "upstream_error", broken
    else:
        await _audit(ctx, sign_in, len(result["rows"]), "ok")
        return result
    await _audit(ctx, sign_in, 0, outcome)
    raise failed


EXECUTORS: dict[str, Callable[..., Any]] = {"dfx.debug": debug}
