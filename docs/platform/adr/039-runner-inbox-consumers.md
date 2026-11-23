# ADR-039: Runner inbox consumers

Status: Accepted (2026-11-21)

## Context
05-runner.md §2.3/§2.4 make the inbox (RUN-03) pull every event of the device's scopes and verify it before anything
acts on it; the events are then needed by other areas: prompts (RUN-05), workgroup settings (operations), profiles
(harness context, `get_task_messages`) and others later. ADR-027 §4 and ADR-028 list no extension point for this, a
package must not edit another package's module, and the consumers must never see an event that failed verification
or receive one out of order.

## Decision
The inbox discovers consumers with `registry.discover` (areas in sorted order), implemented in
`agent_runner/inbox/dispatch.py`:

| Module and attribute | Value |
|---|---|
| `<area>/consumers.py:CONSUMERS` | sequence of `Consumer(name, kinds, handle)`; names unique (`[a-z][a-z0-9._-]{0,63}`), `kinds` a non-empty subset of the event kinds of `04-crypto.md` §4.3. `handle(context, conn, event)` receives a `VerifiedEvent` (org, scope, `scope_seq`, `received_at`, checked header, decrypted body, the sealed event). |

- Only events that passed E1–E5 (author right at `lh`, decertified platform devices excluded), L10 and decryption
  with a held scope key reach a consumer. Refused events are audited (`runner.inbound_rejected`) and never handed on.
- `handle` runs in the transaction that advances the scope's cursor (at-least-once). A consumer that raises rolls the
  transaction back; the scope waits and is retried at the next pull, so the order per scope is kept.
- Kinds without a consumer are verified (refusals audited) but not decrypted; only the cursor moves.
- `scope_seq` is assigned by the hub outside the signed header, so a hostile hub can serve an old, validly signed event
  again under a new `scope_seq`. The signed `event_id` (unique by E9) is the identity of an event: the inbox refuses an
  ID it already handled in this daemon (`event_conflict`), and every consumer MUST key its effects on `event_id`
  (prompts: `prompt_delivery.event_id`; "latest wins" kinds such as `settings.workgroup` compare the signed header,
  never the arrival order), so a replay across restarts has no effect either.

## Consequences
- RUN-05 and later packages add `consumers.py` in their own area and never edit the inbox.
- A permanently failing consumer blocks its scope (visible in the log as `runner.inbox consumer_failed`); consumers
  must be idempotent and should not raise for content they decide to ignore.

## Alternatives considered
Hint handlers for a synthetic per-event hint (no ordering, no transaction with the cursor), every area pulling for
itself (several cursors per scope, repeated verification), a central dispatch table in the inbox (edited by every
later package).

## Deviation from DESIGN.md
None beyond ADR-001.
