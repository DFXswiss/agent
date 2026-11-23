# GST-07 decisions (platform tools, pending calls, platform queries)

Small decisions of the package that completes the session platform API in the guest (03-api.md §8.1, §12;
05-runner.md §12 step 1, §13; 02-data-model.md §5 `pending`; ADR-012, ADR-024).

## Tools and registration
1. Every tool of 03 §8.1 except `get_context` (GST-04) and `get_notifications` (RUN-14) is a module
   `mcp_tools/<tool>.py:TOOL`. Shared forwarding helpers live in the sub-package `mcp_tools/_forward/`: the registry
   lists plain modules only (`registry.modules`), so a helper module next to the tools would be taken for a tool and
   refuse the start (`registry_invalid`).
2. Read tools (`list_workgroups`, `list_members`, `list_tasks`, `get_task`, `get_task_messages`, `get_whiteboard`,
   `list_operations`) reuse GST-04's `mcp.forward_query` with the handler limit of 10 s (05 §5.1; package text) instead
   of `QUERY_WAIT_SECONDS` (20 s, the operation wait of 03 §12). No answer is the tool error `timeout`.
3. `get_operation` and `get_subagent_result` forward their `wait_seconds` unchanged; the runner holds its answer until
   the object is final or `wait_seconds` has passed. The guest waits `wait_seconds` + 10 s, at most 33 s (the `/mcp`
   route limit is 35 s, GST-04). GST-01's `pending.wait` limit rises from 30 s to 33 s for this (only change in
   `control_routes/pending.py`). No answer in time: `get_operation` returns `{operation_id, status:"pending"}` (as
   `request_operation` does; nothing is known to be final), `get_subagent_result` the tool error `timeout` (its
   result needs the child's state, which the guest does not know). A runner error answer with code `timeout` is passed
   on unchanged.
4. Answers that name another object than the one asked for (`get_task.id`, `get_operation.operation_id`,
   `get_subagent_result.session_id`, `request_operation.operation_id`) are `unavailable`, like results that do not
   match the tool's output schema (GST-04). The answer route already refuses results that are no tool's result at all
   (`guest/pending-answer.request.json`, `400 request_invalid`); the tool then times out.

## `request_operation`
5. The guest creates `operation_id` (UUIDv7 from the API process clock) and `requested_at`, journals
   `operation.request` through `pending.create` (kind `operation`) and waits up to 20 s (03 §12) for the runner's answer
   (sent when the operation is final, 05 §12 step 7). No answer: `{operation_id, status:"pending"}`.
6. Local check (05 §12 step 1): a `type` outside `ops/INDEX.json` is `unknown_type`; `params` that do not match
   `ops/<type>.params.json` are `params_invalid` with the JSON pointers below `/params` (≤ 32, pointers longer than the
   contract's 256 characters are dropped). Such a request is still journaled as `operation.request` and answered at
   once `{operation_id, status:"rejected", error}` (a normal result, not a tool error: the output schema has the
   `rejected` status for it). The journal body carries `local_check:"rejected"` and the error as `local_error`
   (additive contract change, see no. 12); every other request carries `local_check:"passed"`. `rejected` is binding
   for the runner (the session was told `rejected`; executing it anyway could duplicate a retried operation), `passed`
   is only a hint (the runner repeats the formal checks).
7. Tool arguments that break `mcp/tools/request_operation.input.json` (no `type`, `params` not an object, extra keys,
   key longer than 128) are refused by GST-04's MCP server with `params_invalid` before any handler runs; without a
   valid `type` and `params` object there is no valid `operation.request` body to record. `mcp.py` is GST-04's file.

## Sessions and scope
8. The session of every call is the one of the bearer token (`call.session_id`, resolved by GST-04's route); no tool
   takes a session from its arguments, and the journal record carries that session. Unknown, foreign and stopped
   session tokens are `401` before anything is journaled.
9. `get_subagent_result`: the guest refuses the caller's own ID (`not_found`, a session is never its own child) and
   forwards every other ID. The guest has no reliable session tree (its `session` table has no parent, and a child the
   hub accepted may not have reached the guest yet, state `requested`); the runner (RUN-11) holds the tree and answers
   `not_found` for anything that is not a child of the calling session. Scope and rights of the read tools and of
   `get_operation`/`list_operations` are likewise the runner's (RUN-07/RUN-08, ADR-024).

## Journal
10. "Every call is journaled" (03 §8): every forwarded call by its pending record (`platform.query` or
    `operation.request`, including locally rejected operations); every call the model makes, also a refused one, by
    the transcript capture (`session.tool`, GST-05). Calls refused by the MCP server before a handler runs are not
    journaled a second time (GST-04 decision 21; the API process may journal only `session.hook`, `usage`,
    `ping.delivered`, `guest.error`).
11. Unanswered pending rows stay `waiting`: every MCP call is bounded by its own wait, and a late answer of the runner
    (an operation that ends after the call returned `pending`) is still stored. No expiry job is added.

## Contract changes (agent-core, `contracts/changes/GST-07.md`)
12. `operation.request` gets the optional fields `local_check` (`passed`|`rejected`) and `local_error` (additive;
    05 §12 step 1 names the flag, the body had no field for it).
12a. `get_whiteboard`'s input was a top-level `oneOf`. `tools/list` hands the dereferenced schema to Claude Code, which
    passes it to the model API as the tool's `input_schema`; the model API refuses `oneOf`/`anyOf`/`allOf` at the top
    level, so registering the tool would break every model request of a session. The definition is now a closed plain
    object `{level, task_id?}`; the rule "`task_id` exactly for level `task`" is checked by the tool (`params_invalid`,
    nothing forwarded) and must be checked by the runner. A test asserts that every listed input schema is a plain
    object (`test_tools_list_names_the_managed_settings_tools`), and agent-core's `test_ctr02.py` checks every tool input
    definition.

## Tests of other packages adjusted
13. GST-04's `tests/platform/guest/mcp/test_mcp.py` asserted exactly one registered tool and used `list_tasks` as an
    unknown tool; it now checks the registry membership and uses `delete_task`. Its ATK-37 unit fixture takes the real
    `get_whiteboard` tool, which carries the `task_id` rule. GST-01's `test_control.py` refuses `timeout: 34` instead of
    `31`.

## Affected E2E/ATK tests
14. E2E-19, E2E-21, E2E-22, E2E-24 (SCN-04) and ATK-33, ATK-36, ATK-37 (ATS-02) do not exist yet; they are written by
    the scenario and attack packages. Their guest-side behaviour is covered by `tests/platform/guest/mcp_tools/`
    (argument abuse as in ATK-37, scope and session binding as in ATK-33/ATK-36 on the guest side).

## After the independent review
15. Review: 0 high, 2 medium, 5 low. Fixed: M1 (the local rejection was not forwarded: now `local_check`/`local_error`,
    no. 6 and 12), L3 (`get_operation` without an answer in time is `pending`, no. 3). Rejected with reasons:
    - M2 (tool-argument violations of `request_operation` not forwarded): 05 §12 step 1 forwards "unknown type or
      invalid params", which the handler does; arguments outside the tool schema never reach a handler (GST-04's
      `mcp.py`, outside this package) and the transcript records them (no. 7, 10).
    - L4 (the plain `get_whiteboard` schema accepts `level:"org"` with `task_id` in `platform.query`/`platform.read`):
      the guest never forwards such a call; RUN-08 must check the rule itself (handed over in the report and in
      `contracts/changes/GST-07.md`).
    - L5 (edits to tests of GST-01/GST-04): unavoidable, no. 13. L6: no. 10. L7: the contract copy in `agent` arrives
      with the agent-core merge before this branch is merged.
