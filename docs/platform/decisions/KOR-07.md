# KOR-07 – decisions (agent)

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
