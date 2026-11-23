# ADR-030: Outbox receipts and outbox handler chains

Status: Accepted (2026-11-22)

## Context
03-api.md §6.1 requires that the hub recognises a resent outbox item: an already stored `origin_seq` with an
identical body is `replayed`, a different body is `409 origin_seq_conflict` (fatal for the runner). 02-data-model.md
§2.3 only keeps `device_stream.last_origin_seq`; events carry `origin_device_id`/`origin_seq`, but the other eight item
kinds (`session_transition`, `vm_state`, `usage`, ...) leave no trace that ties a row to its `origin_seq`, and rejected
items leave none at all. Without a record the hub can neither detect a conflicting resend nor answer a replay of a
rejected item consistently.

ADR-027 §4 names the extension point `<area>/outbox.py:HANDLERS` and the package plan (HUB-10) asks that later areas
(operations, quotas, whiteboards, monitoring) can *extend* the effects of an item kind. The generic `merge_mappings` of
the registry refuses a key defined by two areas, so it cannot express "a second effect for the same kind".

## Decision
1. **Receipts.** Migration `0002_outbox_receipt` adds `hub.outbox_receipt (device_id, origin_seq) PK, item_kind,
   item_sha256 bytea(32), status ('ok'|'rejected'), error NULL, received_at`. Every stored item (accepted or rejected)
   gets one row in the transaction that advances `device_stream.last_origin_seq`; `item_sha256` is the SHA-256 of the
   canonical item text `{"body":…,"item_kind":…}` (sorted keys, no whitespace). A resent `origin_seq` ≤ `last_origin_seq`
   is `replayed` when the digest matches and `409 origin_seq_conflict` otherwise. An item answered with `reseal` gets no
   receipt (it was never stored, 04-crypto.md §5.12). The table holds no content: event bodies are ciphertext anyway,
   and the metadata of the other kinds is stored in plaintext in its own table.
2. **Retention.** Receipts older than 7 days are deleted by the job `outbox.prune_receipts` (hourly), except the newest
   one of each device. A resend of an `origin_seq` whose receipt is gone is answered `replayed` (runners reuse the
   sealed bytes of an item for every retry, 05-runner.md §2.3; a week-old unacknowledged item is not checkable any more,
   and refusing it would stop the device for good).
3. **`quota_snapshot.resets_at` becomes nullable** in the same migration: the outbox contract allows `resets_at: null`
   for windows without a known reset time (contract `outbox/objects.json` `body_quota`), the column was `NOT NULL`.
4. **Handler chains.** `<area>/outbox.py:HANDLERS` maps an item kind to one handler per area. The outbox area
   (`agent_hub/outbox/outbox.py`) registers the base handler of every kind; another area may register a handler for an
   existing kind, which runs **after** the base handler in sorted area order, in the same savepoint and with the same
   item context. A refusal by any handler of the chain rejects the item and undoes the chain. A kind without a base
   handler, a key outside `contracts/enums/outbox-item-kinds.json` or a non-callable value stops the discovery
   (`RegistryError`). So no area overrides another, and every area can add effects. A handler refuses an item only with
   `HubError` (status below 500); any other exception fails the whole push. For `event` the event is stored by the
   push's event phase before the chain runs, so a refusing extension marks the item `rejected` but cannot remove the
   event; extensions of `event` should therefore not refuse.

## Consequences
- Replay and conflict detection is uniform for all item kinds and independent of the target tables.
- The hub schema gains one table beyond 02-data-model.md §2 (`tests/platform/hub/test_schema.py` lists it with a
  reference to this ADR); the first later migration makes the migration tests count the shipped migrations.
- Later packages extend item effects by adding a file, as ADR-027 intends.

## Alternatives considered
Comparing only events (other kinds always `replayed`: a conflicting resend of a transition or a usage delta would go
unnoticed), a digest column on `device_stream` for the last item only (a batch of 500 needs every item), audit rows per
item (the audit chain is not a sync log and would grow by every metrics sample), one handler per kind (later areas
would have to edit the outbox area's files).

## Deviation from DESIGN.md
None beyond ADR-001.
