# RUN-08 acceptance evidence (platform reads of the sessions)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/queries/`: `test_tools.py`, `test_rights.py`, `test_recording.py`, `test_operations.py`,
  `test_projection.py`, `test_wiring.py` (kit `kit.py`).
- Real PostgreSQL store, the real inbox (pull, verification E1–E6, consumers), the real sync runtime (outbox, sealer,
  signed hub client), RUN-07's pipeline and RUN-11's `platform.query` dispatcher; a read goes the real way from the
  guest journal record to the answer of the fake guest. The fixture org is built from real signed log entries: two
  workgroups the device serves and one it does not, an admin, two members with `read`/`write` access, an org device;
  events are sealed with the reference crypto and served by the contract-checking hub double, which also answers the
  device task endpoints. Every tool result is validated against `contracts/mcp/tools/<tool>.output.json`, every
  error against `mcp/tool-error.json`, every pushed outbox item by the hub double, audit details against
  `contracts/audit/read.session.<tool>.json` and `platform.read` bodies against `events/platform.read.json`.
- Covered:
  - every tool against the fixture org: names of all workgroups with `my_access` (foreign workgroup: name only),
    members of the org and of a workgroup, tasks with titles and pages, a task with sessions and operations, the chat
    of a task in scope order with paging and the answer budget, whiteboards of every level (edit, delete, first entry
    wins, cross-level edits ignored), `get_context`, `list_operations`, `get_operation` (final and pending);
  - foreign objects: tasks, chats and task whiteboards of another workgroup are `not_found`, also for a workgroup the
    device serves; hub rows of other workgroups are dropped;
  - rights at call time: workgroup access revoked (org level stays, workgroup level `forbidden`), granted again,
    owner removed (`forbidden` everywhere), owner that is no user, device unassigned or org stopped (`unavailable`);
  - one `platform.read` event and one `read.session.<tool>` audit per call with tool, arguments and `result_count`,
    results `ok`/`denied`/`failed`;
  - hostile or broken hub answers, unreachable hub, events that do not open (wrong key, missing epoch), invalid
    arguments of every tool.
- Coverage of `src/agent_runner/queries/`: 100 % lines and branches.

## E2E
E2E-15, E2E-19 and E2E-26 do not exist yet (SCN-04, SCN-06, SCN-07); they are omitted per the work-package rules.
