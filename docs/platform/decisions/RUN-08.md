# RUN-08 decisions (platform reads of the sessions, `platform.read`, read log)

Small decisions of the package that answers the forwarded MCP read tools on the runner (03-api.md §8.1, §6.1, §10;
05-runner.md §2.2, §2.4; 02-data-model.md §2.9; 04-crypto.md §4.3, §5.12; ADR-007, ADR-024, ADR-038).

## Placement and registration
1. **No second `platform.query` handler.** The journal registry allows one handler per kind and RUN-11 already owns
   it with the discovered point `<area>/query_tools.py:TOOLS` (ADR-038), which records every outcome as the
   `platform.read` event `{tool, arguments, result_count}` and the `read.session.<tool>` audit and answers the pending
   call. This package registers its tools there (`queries/query_tools.py`) instead of the `queries/journal.py` named in
   the package text; the per-tool modules are `queries/tools/<tool>.py` as planned. Nothing of RUN-11 is changed.
2. Besides the six tools of the package text (`list_workgroups`, `list_members`, `list_tasks`, `get_task`,
   `get_task_messages`, `get_whiteboard`) the area also answers `get_context`, `list_operations` and `get_operation`:
   the guest forwards them as `platform.query` and documents RUN-08 (`get_operation`: RUN-07's `Pipeline.wait_answer`,
   as the RUN-07 report hands over). Without a provider they would only time out. `get_subagent_result` stays with
   RUN-11, `get_notifications` is answered in the guest (RUN-14).
3. The inbox consumer is `queries/consumers.py:CONSUMERS` (ADR-039); the hub endpoint helper is
   `queries/device_api.py` (not `tasks.py`, which is the daemon-task discovery name); shared tool parts are in
   `queries/common.py`.

## Local projection (`projection.py`, migration `0007_read_event.sql`)
4. One table `runner.read_event` keeps every verified event of the kinds `prompt`, `session.output`,
   `whiteboard.entry|edit|delete` and `device.profile` exactly as received (ciphertext and signature) with the author's
   verified signing key at `lh` and the clear header fields the reads select by (`scope`, `scope_id`, `scope_seq`,
   `task`, `ref`, `author`, `ts`, the hub's `received_at`). Text is opened only into memory while a read is answered
   (scope key of the event's epoch from the store, signature checked again) - the pattern of `profile_event` and
   `prompt_event`. Prompts are kept here as well instead of reading RUN-05's `prompt_event` (no second area's table is
   read directly; the duplicate ciphertext is small).
5. Idempotence: `ON CONFLICT DO NOTHING` on the event ID and on `(scope_id, scope_seq)`, so a replayed event under a
   new `scope_seq` (ADR-039) and a repeated page change nothing.
6. Names (org, workgroups, members, task titles) are read through RUN-04's library `sessions.profiles.name` (latest
   `org.profile`, `wg.profile`, `member.profile`, `task.meta` per object; titles only from the session's own workgroup
   scope). Device names come from the latest kept `device.profile` ("latest" = signed `ts`, then event ID).
7. An object whose profile event the device does not hold is shown by its ID (names have `minLength 1`); a task
   without `task.meta` has the empty title (`minLength 0`). Names are cut to 256 characters.
8. Whiteboards are replayed per level like the hub projection (`agent_hub.whiteboards.projection`): entry ID = `ref`,
   the first `whiteboard.entry` wins, `edit`/`delete` apply only to an existing, not deleted entry of the same level
   (org scope; workgroup scope without task; workgroup scope of the same task). The order is the hub's `scope_seq` of
   the one scope a level lives in. `created_at`/`updated_at` are the hub receive times of the creating / latest event
   (as `GET /v1/whiteboards`). Entry IDs are unique across levels as at the hub: an entry is shown only if its
   creating event is the first kept `whiteboard.entry` with that ID (hub receive time, then event ID); the device
   sees the org scope and its own workgroups, which covers every level a session can read.
9. `get_task_messages`: `seq` = `scope_seq` of the workgroup scope, `after` exclusive, default limit 100, `next_after`
   = the last examined `scope_seq` while more follow. Outputs of every session of the task (also subagents) are chat
   items. Events that do not open are skipped (the tool has no "unverifiable" item); the cursor still moves.
10. Answer budget: the guest takes answer bodies up to 1 MiB, so listed items stop at 768 KiB of JSON text as the guest
    link encodes it (non-ASCII escaped). A single chat item larger than that on its own is cut to fit (only possible
    with an unusually escaped text); `get_whiteboard` returns the newest entries that fit (at most 1000) and
    `list_members` the leading members that fit (the tool has no paging). Texts are cut to the output-schema limits
    (whiteboard 65536, chat 262144 characters): the hub cannot check plaintext sizes, and one over-long entry must not
    make the whole answer fail the guest's schema check.

## Rights (`rights.py`, ADR-024)
11. Rights are evaluated at every call against the verified log (`OrgBook`), never cached: the owner must be an active
    user of the org (else `forbidden` for every read); workgroup-level reads (tasks, messages, workgroup/task
    whiteboards, workgroup members, operations, `get_context`) additionally need the owner's `read`/`write` access to
    the session's workgroup (else `forbidden`); organization-level reads (`list_workgroups`, org members, org
    whiteboard) stay available ("Lesen eingeschränkt"). A device that cannot act for the org or is no longer assigned
    to the session's workgroup answers `unavailable`.
12. Other workgroups: names only (`list_workgroups`, `my_access` null without access). Archived workgroups are left
    out. A task of another workgroup - also one this device serves - is `not_found`, like an unknown task (03 §1: reads
    never reveal foreign objects).
13. `list_members`: scope `org` = active users (`admin`/`member`) and devices (`device`); scope `workgroup` = users with
    access (default workgroup: every user `write`) and assigned devices with `access: "write"` (they author the session
    events of the workgroup). Decertified platform devices are left out.

## Hub data (`device_api.py`)
14. `list_tasks`, `get_task` and the task state of `get_context` come from `GET /v1/device/workgroups/{wg}/tasks` and
    `GET /v1/device/tasks/{task}` (verified hub answers). The hub is still not trusted to widen the scope: rows of
    another org or workgroup and rows/fields outside the contract form are dropped (lists) or make the read
    `unavailable` (single task); `403`/`404` = no such task; transport errors and other refusals = `unavailable`.
15. `get_task_messages` and `get_whiteboard` (task level) ask the hub whether the task exists in the session's
    workgroup (`not_found` otherwise), also when kept events name it: the hub projects task whiteboards only for tasks
    of the event's workgroup, and an ID named only by events is not a task. Hub lists are cut to the contract maxima
    (100 sessions, 1000 operations); more task rows than asked for are cut and give no `next_before`.
16. `list_operations` lists the operations of the task that **this device** registered (`runner.operation`, newest
    first): the hub's task endpoint has no creation time; `get_task` lists all operations of the task. Error codes and
    types outside the contract are not shown (`error_code: null` / row left out).
17. `get_operation` answers only for an operation of the session's task and workgroup (`not_found` otherwise, also for
    operations this device does not know) through `Pipeline.wait_answer`; no pipeline = `unavailable`.

## Arguments
18. Every tool checks its arguments again (closed key set, types, ranges, integers never booleans): the guest already
    validated them against the tool schema, but the VM is untrusted towards the host (00-overview.md §3, B4). The
    `get_whiteboard` rule "`task_id` exactly for level `task`" is checked here too (GST-07 hand-over).

## Read log
19. Recording is RUN-11's dispatcher (decision 1): one `platform.read` event and one audit per call, also for refused
    reads (`denied` for `forbidden`/`not_found`, `failed` otherwise). The tools set `result_count` and the audited
    target (`org`, `workgroup`, `task`, `operation` or the session). Observation for RUN-11: arguments that a tool
    refused as `params_invalid` are recorded verbatim in `platform.read`, which then does not match
    `events/platform.read.json` (only a misbehaving guest can send them; the event stays sealed and signed).
    Narrowed by KOR-03 (decision 11): refused arguments get no `platform.read` event.

## Tests and files outside the area
20. `tests/platform/runner/test_store.py` (RUN-01's schema list) gains the table `read_event`, as every earlier
    migration did.
21. Residual of RUN-04's profile store (not changed): `profile_event` keeps one `task.meta` per task ID across scopes,
    so a writer of workgroup A can shadow the title of a task of workgroup B with a newer `task.meta` naming B's task ID
    in A's scope; the reads of B then show the empty title (never A's text: the scope check of `profiles.name`).
22. E2E-15, E2E-19 and E2E-26 do not exist yet (SCN-04, SCN-06, SCN-07); the acceptance evidence is the unit and
    integration suite `tests/platform/runner/queries/`.

## Review (independent subagent)
23. 0 high, 7 medium, 9 low. Fixed: M1 (whiteboard text cut to 65536), M2 (hub session/operation lists capped), M3
    (`list_members` answer budget), M6 (entry IDs unique across levels; task existence always from the hub), L8
    (non-string `level`/`scope`/`state` are `params_invalid`), L9 (rows beyond `limit` cut without cursor), L12 (the
    consumer returns instead of asserting), L16 (tests for each of these and for a `read`-only owner's whiteboards).
24. Rejected with reasons: M4/M5 (`list_operations`/`get_operation` read RUN-07's store): RUN-07 is merged and hands
    `get_operation` to this package explicitly (report: "RUN-08 must use `Pipeline.answer/wait_answer`"), the guest
    documents both tools as RUN-08's, and `operations.records` offers no listing function; the single SQL statement is
    read-only. Should RUN-07 ever register a provider, the registry fails loudly (`registry_duplicate`), never
    silently. M7 (verbatim arguments of refused reads in `platform.read`) belongs to RUN-11's dispatcher and is handed
    over in the report (decision 19). L10 (an entry whose latest event does not open is left out, decision 9), L11
    (archived workgroups, decision 12), L13 (decision 20), L14 (sequential store sessions, well within the 10 s read
    limit), L15 (the owner ID comes from the hub's desired state platform-wide; it cannot widen the content scope:
    the device holds keys only for its workgroups and the rights are still those of a verified member).
