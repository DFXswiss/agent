# KOR-07 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-07 – decisions (agent)

Package: audit complete and faithful to the contracts (AUD-01 B-07, B-08, B-22). The hub and contract decisions are in
agent-core `docs/platform/decisions/KOR-07.md`; the contract change is `contracts/changes/KOR-07.md`.

1. **`get_notifications` is audited** (03-api.md section 8: every read is an audit row; SCN-04 item 9). The guest
   answers the tool itself, so the control process journals the read as `platform.read {tool, result_count}` in the
   same transaction as the take (`agent_guest/pings.py`; atomic with the `ping.delivered` records, also for an empty
   list). The new runner area `session_api` (`journal.py:HANDLERS`) records the `platform.read` event (`arguments:
   {}`) and the outbox audit `read.session.get_notifications`, exactly as the controller does for a forwarded read.
   Answering the tool in the runner instead was rejected: which pings the session fetched lives in the guest.
2. **Refused MCP requests are journaled** (`platform.refused {code, tool}`; supersedes GST-04 item 21 and, for this
   kind, GST-01 item 10: `control_routes/journal.py` `API_KINDS` gains it). `tool` is a registered tool name or null,
   never the method text or arguments. A tool's own pre-check (`get_whiteboard`) raises `ToolRefused`, so it is
   journaled like a schema refusal; refusals coming back from the runner are not (they were forwarded and are audited
   there). The runner audits a refused read tool as `read.session.<tool>` with result `rejected`, `result_count` 0, and
   writes one fact line per refusal. Not covered (left as before): `get_subagent_result` refusing a non-child locally
   (`not_found`), bodies over 1 MiB (router 413), a `get_notifications` of an inactive session (`unavailable`).
3. **Volume.** Each journaled call costs one guest record, at most one event and one audit row - the same as one
   forwarded read; neither path has a rate limit (the controller only caps concurrent forwarded reads per session).
   A session can therefore grow its own audit trail by calling in a loop; auditing every read is what 03-api.md
   section 8 asks for, so no read is dropped. A per-session read rate limit is left as an open risk (ATS/PFM).
4. **Runner pings** (`subagent.finished`, `operation.finished`) are now sent as outbox audit `ping.delivered` too
   (supersedes RUN-14 "recorded locally only"); the hub accepts them without a `session_ping` row.
5. **Refused credential records are audited** (supersedes RUN-09 items 10/24): `runner.inbound_rejected`, source
   `credential` (`orgs/audit.SOURCES`), target `credential` / `<owner>:<kind>`, code of the refusal.
6. **Guest journal schema.** The CHECK list of `journal.kind` in `schema.sql` gains the two kinds without a new
   `user_version`: code and database come from the same golden image (no migration in place, as RUN-14's ping columns).
7. **Registration.** `session_api` is a new runner area (discovered by `journal.py:HANDLERS`); the skeleton test lists
   every area and got the name. Files of other areas touched with one change each: `agent_guest/pings.py`,
   `control_routes/journal.py`, `journal.py` (`KINDS`), `schema.sql`, `mcp_tools/get_whiteboard.py`,
   `agent_runner/orgs/audit.py`.
8. **Contracts** arrive with the exact sync commit `Sync the contracts from agent-core.` on this branch (as GST-07):
   the automatic sync of the agent-core merge is refused by the agent gates until this code is in.

## KOR-07 – decisions

Package: audit complete and faithful to the contracts (AUD-01 B-07, B-08, B-22).

### B-07 – detail schemas enforced, `passkey_id`, `invite.use`

1. **Enforcement in `append_audit`.** `validate_row` (used by `append_audit` and by the read log when a row is
   recorded) now checks the detail against the schema of its action (`agent_hub.audit.details`). The contracts
   directory is not installed with the package, so the schemas are kept as code in `agent_hub/audit/detail_schemas.py`,
   generated from `contracts/audit/*.json` (`$ref` inlined, `anyOf` expanded) by
   `tests/platform/contracts/audit_details.py`. The contract test `test_audit_details.py` fails when module and
   contracts differ, and checks that the hub's checker accepts exactly what the JSON Schema validator of the contract
   harness accepts (fixtures plus boundary values of every alternative). The generator refuses keywords the small
   checker does not implement, so a new keyword can never be ignored silently. A row that breaks its schema is a
   programming error of the producer (`AuditRowInvalid`, the message names the property, never its value).
2. **Word list kept, contract renamed.** The plaintext word list of `chain.py` stays as a second guard (it holds even if
   a schema were widened by mistake). The `auth.*` contracts named the passkey `credential_id`, which the list refuses;
   the property is now `passkey_id` with the pattern of the reference the hub already writes as `target_id`
   (`contracts/changes/KOR-07.md`). `auth.login`, `auth.passkey_add`, `auth.passkey_remove` and `auth.register` carry it.
3. **`device.enroll`.** The hub wrote `{kind, state}` against the contract `{device_kind}`. The key is now
   `device_kind`; `state` (`active` | `pending_certification`) is added to the contract as an optional property because
   it is the only metadata that distinguishes an org enrollment from a platform enrollment awaiting certification.
4. **`invite.use`** is written in the transaction that sets the invitation's `used_at`, through one helper
   (`agent_hub.audit.invites`): the `accept` of an org invitation by a user or a device (`orgs/append.py`, actor = the
   appender), the private-org genesis that consumes a platform user invitation (`orgs/genesis.py`, actor = the user,
   `org_id` = the new private org) and the `device_certify` entry that consumes a platform device invitation
   (`platform/log.py`, actor = operator, `device_id` = the device; the log rules allow one certification per
   invitation, so the row is written once). `genesis.py` and `platform/log.py` belong to other areas; the change there
   is the single call that adds the audit row (package text: "fehlende Erzeuger ergänzen").
5. Tests of other areas that asserted the old (contract-breaking) details or the exact list of audit rows of a scene
   were updated to the contract (`test_register.py`, `test_passkeys_manage.py`, `test_enroll.py`, `test_genesis.py`,
   `test_ws.py`, `test_device_state.py`, `audit_api/test_list.py`); synthetic rows in the audit tests use details that
   fit their action (hash vectors recomputed independently with `hashlib`).
6. Review of B-07: the uncovered branch of `details._pattern` (no contract pattern lacks a final `$`) is tested
   directly; the outbox's pydantic detail models (`outbox/bodies.AUDIT_DETAILS`, checked before `append_audit`) are
   tested to agree with the enforced schemas, so a drift can never turn a runner's audit item into a 500; the generator
   refuses a `$` inside a pattern (only a final `$` gets the ECMA-262 anchoring). Kept: the genesis writes `invite.use`
   before `log.genesis` (same transaction, rolled back together).

### B-08 – audit completeness (hub and contracts)

7. **Runner pings** (`subagent.finished`, `operation.finished`; RUN-14 decisions 8/16, 05-runner.md section 9): the
   hub keeps no `session_ping` row for them, so it now audits their `ping.delivered` without marking anything (spec
   unchanged: "Hub ... akzeptiert ohne"). An ID that belongs to one of the hub's own pings with another kind stays
   `404 not_found`. `session_id` stays required.
8. **`source: credential`** added to `runner.inbound_rejected` (audit and outbox contracts, hub model
   `InboundRejected`), so the runner can audit a refused credential record (RUN-09 items 10/24).
9. **Two journal kinds** (`contracts/changes/KOR-07.md`): `platform.read` for the read the guest answers itself
   (`get_notifications`) and `platform.refused` for MCP requests refused before forwarding (GST-04 item 21, ATS-02
   item 17). The alternative "answer `get_notifications` in the runner" was rejected: which pings the session has
   fetched lives in the guest (the hook notice marks them delivered, the tool still returns them), the runner only
   knows the first delivery. `platform.refused` carries no method text, no foreign tool name and no arguments, so a
   session cannot smuggle content into the runner's log or the audit through it.
10. Review of the B-08 hub part:
    - **Merge order.** The new journal kinds turn the agent tests red until the agent code knows them
      (`agent_guest.journal.KINDS`), so the automatic contract sync of the agent-core merge is refused by its agent
      gates. As in GST-07, the agent branch carries the exact sync commit (`Sync the contracts from agent-core.`, made
      by `scripts/sync-contracts.sh` from the integration export) together with the code; both land in one agent merge.
    - **Trust boundary.** The guest API process (reachable by the session) may now journal `platform.refused`
      (`API_KINDS`); this supersedes GST-04 item 21 / GST-01 item 10 for that kind. The body is a closed enum pair, so
      the session controls at most which of five codes and twelve tool names appear. The runner accepts the record
      only for a session of that VM (as every session record) and writes at most one audit row per refused read call
      - the same cost per call as a forwarded read. Neither path has a rate limit (the runner only caps concurrent
      forwarded reads per session), so a session can grow its own audit trail in a loop; every read stays audited
      (03-api.md section 8) and a per-session read rate limit is left as an open risk (agent decisions KOR-07 item 3).
    - The runner-ping branch of the hub looks only at the session's own hub pings (no cross-session existence test);
      the device is trusted for its own sessions as for every runner-side audit row (docstring).
    - `outbox/bodies.py` (outside the package's file list) gets the one enum value of the contract; the contract
      commit also carries the hub model and the generated schema table, because the consistency tests of both fail
      otherwise.
11. **Documentation need (DOC-01).** 03-api.md section 7.2 and 02-data-model.md section 5 (closed journal kind list)
    lack `platform.read` and `platform.refused`; 03-api.md section 6.1 says `ping.delivered` "also marks the
    `session_ping` row", which holds for hub pings only; 02-data-model.md section 2.9 allows `session_ping.kind`
    `subagent.finished`/`operation.finished`, which the hub never creates; 05-runner.md section 4.3/GST-01 list of the
    API process's journal kinds gains `platform.refused`; the `auth.*` audit detail field is `passkey_id`.
