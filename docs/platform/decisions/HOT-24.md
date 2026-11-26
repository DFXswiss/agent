# HOT-24 – Decisions

No VM stays on an old golden forever because a session state can never be saved. Design: ADR-093 addendum HOT-24.

1. **Evidence first.** Guest 0.2.0 exists with and without `POST /v1/sessions/{id}/context/export` (`db40d8bc8` does
   not contain `69173353b`); the guest's router answers an unknown route `404 not_found`, the same as an unknown
   session. `_drop` only logged, `mark_vm_due` made such sessions due at every `flush_vm`, `GoldenSwitch` counted its
   starts in memory - so every release started the held VMs three more times.
2. **"Worked" from the deliveries, not a new column.** A session worked when a prompt was accepted for it here
   (`runner.prompt_delivery` `accepted`): the rows are never deleted, so the rule holds for every session from before
   the package without a backfill. Read in SQL by the contexts area, like `runner.orchestrator` before.
3. **"Never savable" only for answers that stay** (`not_found`, `size_exceeded`). The package's example "aufgebrauchte
   Versuche" is narrowed on purpose after the design review: unreachable guests, a missing epoch key, `unavailable`
   (now postponed) and runner errors pass; marking them would freeze the last turn of a session that worked. They stay
   `dropped`; the persisted refresh limit ends the loop.
4. **`nothing` only without an earlier checkpoint** (no archive hash and no cached object): an older checkpoint
   must not be taken for the whole state.
5. **A request covers what the member saw.** The hub sends the time of the newest request (`vm_switch_at`); a repeated
   request now updates `switch_requested_at` (before: kept). Only holds reported to a hub of `vm_upgrade_version` 2
   before that time are covered (`golden_switch_hold.reported_at`, only for listed holds; up to 64 listed, uncovered
   first). With an older hub a request covers nothing (the member could not see the consequence), and a request
   without a time never counts as new twice (no refresh loop with an older hub).
9. **The mark belongs to the VM** (`impossible_vm_id`): a session moved by hand is judged by its new guest.
6. **Counters in the store**: `runner.golden_switch_attempt`, `runner.golden_switch_request` (migration
   `0037_checkpoint_impossible`). A start without a free slot counts no attempt; attempts start again with a new or
   newer request and when the VM ran for its sessions.
7. **`_from_stopped` unchanged in its choice** (Abgrenzung): it restarts a VM held by `checkpoint_impossible` for a new
   session; it replaces it only when a request covers all its holds. Remaining risk named in the ADR.
8. **No guest change**, so no guest version bump (PREAMBLE §4 point 6 does not apply). Contracts additive:
   `vm_upgrade_hold` gains `checkpoint_impossible` and `checkpoint_at`, the device state `vm_switch_at` and
   `vm_upgrade_version` (`contracts/changes/HOT-24.md`).
