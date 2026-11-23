# RUN-14 decisions (content-free pings to sessions, runner and guest)

Small decisions of the package (05-runner.md §9, §5.5, §12 step 7, §13; 03-api.md §6.1, §6.2, §7.1, §7.2, §8.1,
§8.2; 02-data-model.md §5; ADR-020).

## Modules
1. Runner `pings/`: `send.py` (`send_ping`, `Pinger`, `stable_ping_id`), `reconcile.py` (step 70 `pings`),
   `hints.py` (hint `ping`), `journal.py` (`ping.delivered`), `subagent_finished.py` (RUN-11 `NOTIFY`),
   `op_finished.py` (RUN-07 `NOTIFY`). Guest: `pings.py` (`PingStore`), `control_routes/pings.py` (route and the
   api.sock ops `ping.notice`, `ping.take`), `hook_addons/pings.py`, `mcp_tools/get_notifications.py`.

## Interfaces
2. `send_ping(session_id, kind, level, ref, ping_id, *, context)`: the five fields of the package text, plus the
   daemon context as keyword (every runner area reaches its collaborators through the context). It returns whether
   the guest kept the ping.
3. RUN-07 and RUN-11 call the ping through their existing discovered extension points (`op_finished.py:NOTIFY`,
   `subagent_finished.py:NOTIFY`, ADR-027 §4, ADR-041/ADR-038) instead of an import of `send_ping` in their modules:
   the "optional hooks" are now always served by this area (the wiring tests of both areas assert it), and no shared
   module of another package is edited beyond the ping ID below.
4. RUN-11's `ping_id(child)` was a UUIDv5, which the guest contract (`common.json#/$defs/uuid`, UUIDv7 pattern)
   refuses. Both runner pings now use `stable_ping_id(kind, ref)`: the 48 timestamp bits of the (UUIDv7) `ref`, the
   rest from SHA-256 over `kind:ref`, version 7 and variant bits set. Stable across restarts, valid by contract.

## Runner
5. New table `runner.ping` (migration 0008): the runner must know the `kind` of a delivered ping for the audit detail
   (`ping.delivered {ping_id, kind, via}`) while the journal record carries only `{ping_id, via}`; it also keeps
   runner pings that found no running guest so the reconcile step sends them later (`sent_at`). Rows of sessions that
   ended more than an hour ago are deleted on every flush.
6. Scope: the hub resolves a whiteboard entry to one `session_ping` row per active session in scope (HUB-15) and
   lists only rows of sessions placed on this device. The runner takes a listed ping only for a session it runs that
   is not `stopping`/`stopped`/`failed`, and sends only to sessions the guest knows (`sandbox_ready` … `resuming`,
   VM running); earlier sessions get the ping on a later run. A ping is never typed into the TUI.
7. The `ping` hint carries no ping ID, so it cannot be delivered directly (the audit must name the hub row). The
   handler resolves the hint's scope (org/workgroup/task) against the local non-ended sessions and wakes the reconcile
   engine only when one is in scope; the step then fetches the durable list.
8. Audit: only the first delivery of a ping is recorded (`delivered_at`); only hub pings (`whiteboard.entry`) produce
   the outbox audit `ping.delivered` - the hub answers `not_found` for a `ping_id` without a `session_ping` row, which
   would turn every runner ping into a rejected outbox item. Runner pings are recorded locally and in the fact log.

## Guest
9. `ping` table gets `level`, `ref` (`get_notifications` returns them, 03 §8.1) and `noticed_at` (the hook notice is
   attached once per batch), plus a `kind` CHECK. The schema version stays 1: the guest database is created with the
   golden image and never migrated in place (decisions/GST-01.md), and these columns ship with the same golden.
10. Batch = the pings neither noticed by a hook nor fetched by the tool. The notice counts that batch; its kinds are
    distinct in the contract order `whiteboard.entry, subagent.finished, operation.finished`
    (`mcp/objects.json#/$defs/notice`). Each noticed ping is journaled `ping.delivered {via:"hook"}`.
11. `get_notifications` lists every ping not fetched yet - also those a hook already noticed (the session needs the
    IDs to act) - oldest first, at most 1000, marks them delivered and journals `{via:"tool"}`. It is answered by the
    guest (no `platform.query`): the guest owns the pings, so there is no runner read and no `read.session.*` audit;
    the deliveries are audited through `ping.delivered`.
12. A denied PreToolUse still carries the notice (03 §8.2 names PreToolUse without restriction; the deny answer stays
    as decided). A blocked UserPromptSubmit and every other event, `Stop` included, never consume or carry it.
13. A repeated `POST …/pings` of a ping that was already noticed or fetched journals its `ping.delivered` again, so a
    runner that lost its row (store wipe) can still mark the hub row; an unchanged repeat of an open ping is a no-op,
    the same ID with other fields `409 id_conflict`, a stopped/failed session `409 state_conflict`.

## Review outcome (independent review, no high findings)
15. `get_notifications` without a read audit (medium): kept. The tool reads only the guest's own ping table, not
    platform data; the call itself reaches the runner through the transcript (`session.tool`, like every call that
    is not forwarded, GST-07), and every returned ping is audited as `ping.delivered {via:"tool"}`.
    `read.session.get_notifications` stays in the closed audit list for a later forwarding variant.
16. No hub audit for runner pings (medium): kept (decision 8); the spec says "audited (`ping.delivered`, which also
    marks the hub `session_ping` row)", and the hub (HUB-15) answers `not_found` without a row. Follow-up for the
    spec owner: either let the hub accept `ping.delivered` for `subagent.finished`/`operation.finished` without a
    row, or state in 05 §9 that only hub pings are audited.
17. A noticed batch can be lost when `hooks._decorate` drops the text after the op committed (medium, latent):
    today no other addon returns text, so the joined text cannot fail the `notice` pattern, and the notice is
    validated against the same pattern by the tests. `hooks.py` (GST-03) owns the decoration; a commit-after-validate
    protocol would need a change there. The ping itself is not lost: `get_notifications` still lists it.
18. Spec drift (medium): 02-data-model.md §4 (new `runner.ping`) and §5 (guest `ping` columns `level`, `ref`,
    `noticed_at`) should be amended by the documentation package (DOC-*); decisions 5 and 9 give the content.
19. `stable_ping_id` refuses a `ref` that is no UUID with `PingInvalid` (low); the no-tmux spy now also covers the hook
    notice and `get_notifications` (low).

## Tests and E2E
14. The E2E specs E2E-24 and E2E-26 do not exist yet (SCN-06); they are skipped per the package text. The acceptance
    criteria are covered by the unit/integration suites `tests/platform/runner/pings/` (real PostgreSQL, the
    contract-checking hub double, a fake guest) and `tests/platform/guest/pings/` (real control process and API
    server, tmux and prompt delivery spied).
