"""MCP tool ``dfx_debug`` (ADR-102 section 5; 03-api.md section 8.1).

A shorthand of ``request_operation {type:"dfx.debug", params}``: the arguments are the query DTO of the dfx-debug CLI
(``table``, ``select``, ``where``, ``groupBy``, ``orderBy``, ``limit``, ``offset``) and become the parameters of the
operation ``dfx.debug`` unchanged. Everything else is ``request_operation``'s: the call gets a new ``operation_id``, is
checked against ``ops/dfx.debug.params.json`` (a refused DTO is still forwarded with ``local_check:"rejected"``) and
journaled as ``operation.request`` for the runner, which checks, records, audits and executes it like every operation
(the rights profile names the type ``dfx.debug``). The tool waits up to 20 s for the outcome like
``request_operation``; the answer is the same outcome object.

The tool only reads: the operation has no write, and the DTO has no field that could ask for one (no ``poll``).
"""

from __future__ import annotations

from typing import Any

from agent_guest.clock import iso_ms, uuid7
from agent_guest.mcp import Tool, ToolCall
from agent_guest.mcp_tools import _forward, request_operation

OPERATION_TYPE = "dfx.debug"

DESCRIPTION = (
    "Query the DFX API production database read-only through the structured debug endpoint (POST /support/debug) "
    "with the debug sign-in your task owner released to this workgroup (operation dfx.debug; there is no write, no "
    "poll). Arguments are the dfx-debug CLI's DTO: table, select[] ({kind:column,column,as?} | "
    "{kind:aggregate,column,aggregate:count|sum|min|max|avg,as?} | {kind:jsonb,column,jsonbPath}), where (tree of "
    "{kind:leaf,column,op,value?} with op = != < <= > >= IN 'NOT IN' LIKE ILIKE 'IS NULL' 'IS NOT NULL', "
    "{kind:and|or,children:[1-5]}, {kind:not,child}; depth <= 5, <= 50 leaves, IN lists <= 100), groupBy[] (<= 20), "
    "orderBy[] ({column,direction:ASC|DESC}, <= 20), limit (required, 1-10000) and offset. The server's allowlist "
    "(DebugAllowedColumns) decides tables and columns: id, created and updated exist on every table; ids, foreign "
    "keys, timestamps, status and enum fields, amounts, flags, asset and blockchain names and on-chain hashes are "
    "selectable; names, addresses, IBAN/BIC, phone, birthday, secrets and most free text are not (comment on "
    "buy_crypto/buy_fiat is: it carries AML codes). Filter-only: user_data.mail and bank_data.iban only as one '=' "
    "leaf per query, never under not, never selected. Presence-only (IS NULL / IS NOT NULL): wallet.address, "
    "fiat_output.frickError, fiat_output.frickAttemptHistory. jsonb paths: log.message, setting.value. Not queryable: "
    "sell, swap (use deposit_route), mros. Tables: "
    "account_merge aktionariat_registration asset asset_price bank bank_account bank_data bank_tx bank_tx_batch "
    "bank_tx_repeat bank_tx_return blockchain_fee buy buy_crypto buy_crypto_batch buy_crypto_fee buy_fiat checkout_tx "
    "country crypto_input crypto_staking custody_account custody_account_access custody_balance custody_order "
    "custody_order_step custody_provider deposit deposit_route exchange_tx faucet_request fee fiat fiat_output ip_log "
    "kyc_file kyc_log kyc_step language ledger_account ledger_leg limit_request liquidity_balance "
    "liquidity_management_action liquidity_management_order liquidity_management_pipeline liquidity_management_rule "
    "liquidity_order log notification olky_recipient organization payment_activation payment_link "
    "payment_link_payment payment_merchant payment_quote payout_order price_rule recall recommendation ref ref_reward "
    "route sanction scorechain_screening scorechain_sanction_record setting sift_error_log special_external_account "
    "staking_ref_reward staking_reward support_issue support_issue_template support_log support_message support_note "
    "system_state_snapshot trading_order trading_rule transaction transaction_request transaction_aml_check "
    "transaction_risk_assessment transaction_scorechain_reason transaction_specification user user_data "
    "user_data_relation virtual_iban virtual_iban_issuance_event virtual_iban_issuance_intent wallet wallet_app "
    "webhook. A column outside the allowlist is refused with the server's message (params_invalid). Every query hits "
    "production: prefer count aggregates and small limits. Reading sensitive personal data counts persons per account "
    "and UTC day; at the daily limit the endpoint refuses (limit_exceeded) until 00:00 UTC. Examples: "
    '{"table":"buy_crypto","select":[{"kind":"aggregate","column":"id","aggregate":"count","as":"n"}],'
    '"where":{"kind":"leaf","column":"amlCheck","op":"=","value":"Pass"},"limit":1}; '
    '{"table":"buy_crypto","select":[{"kind":"column","column":"id"},{"kind":"column","column":"created"},'
    '{"kind":"column","column":"amlCheck"},{"kind":"column","column":"comment"}],"where":{"kind":"leaf",'
    '"column":"comment","op":"LIKE","value":"%ScorechainHighRisk%"},"orderBy":[{"column":"created",'
    '"direction":"DESC"}],"limit":50}; {"table":"buy_crypto","select":[{"kind":"column","column":"amlReason"},'
    '{"kind":"aggregate","column":"id","aggregate":"count","as":"n"}],"groupBy":["amlReason"],'
    '"orderBy":[{"column":"n","direction":"DESC"}],"limit":100}.'
)


def dfx_debug(call: ToolCall) -> Any:
    now = request_operation.clock.now()
    operation_id = uuid7(now)
    body: dict[str, Any] = {
        "operation_id": operation_id,
        "type": OPERATION_TYPE,
        "params": dict(call.arguments),
        "requested_at": iso_ms(now),
    }
    error = request_operation.local_check(call.schemas, OPERATION_TYPE, body["params"])
    if error is not None:
        _forward.forward(call, "operation", {**body, "local_check": "rejected", "local_error": error})
        return {"operation_id": operation_id, "status": "rejected", "error": error}
    answer = request_operation.ask(call, body)
    if answer is None:
        return {"operation_id": operation_id, "status": "pending"}
    return _forward.outcome(call, answer, operation_id=operation_id)


TOOL = Tool(name="dfx_debug", description=DESCRIPTION, handler=dfx_debug)
