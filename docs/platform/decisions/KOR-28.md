# KOR-28 decisions (duplicate protection independent of the idempotency key)

Correction of finding L3 of the concept audit: with an idempotency key the runner built the dedupe tag from
`{idempotency_key, task}` instead of the type's fields, so a session that passed a new key published the same
pull-request comment again (KONZEPT §11, R-11.4/R-11.5; 03-api.md §8.3, §9; 04-crypto.md §4.10; ADR-012 amendment
KOR-28).

1. **Reproduced first.** `tests/platform/runner/operations/test_idempotency.py` and the attack test
   `tests/platform/atk/executors/test_dedupe_key_rotation.py` (real pipeline, real `github.comment` executor, GitHub
   double, a hub stand-in that decides duplicates on `{epoch, tag}` like H5/H6) failed on the old code: the same
   comment with keys `k-1`, `k-2`, `k-3` was `succeeded` three more times and reached GitHub each time.
2. **Content rule always.** `operations/dedupe.fields_for` uses the type's fields whether or not a key is given. The
   key's field set remains only for types without dedupe fields (`github.list_repositories`,
   `github.get_pull_request`, `task.set_title`), where it is the only protection and cannot lift anything.
3. **What the key still does.** It marks a repetition as the same request (03-api.md §1 "different content with the
   same ID" is a conflict, as in the usual idempotency-key contracts): the same key with the same content is a
   duplicate by the fields; the same key for other content of the same task and type, while an earlier operation with
   it holds its slot (`accepted|executing|succeeded` as in H5, same epoch, created within the type's window),
   is refused by the runner with `params_invalid` at `/idempotency_key` (`records.key_conflict`, `register.build`). The
   refusal is registered at the hub as `rejected` and audited there like every local rejection. No new error code
   (the operation error-code list stays closed and unchanged).
4. **No hub or schema change.** One registration still carries one tag; a second slot for the key would need a new
   registration field and a hub migration for no gain over (3). Contract changes are description texts only
   (`contracts/changes/KOR-28.md`); the vector file is unchanged.
5. **Windows on the device.** `operations/dedupe.WINDOWS` copies `window_seconds` of `contracts/ops/dedupe-fields.json`
   (a test compares them), like `agent_hub.operations.types.WINDOWS`.
6. **A wanted second time.** The specification has no override, and none is added: a repetition is possible after the
   type's window, after the original released its slot (`failed`, `expired`), or with other content. A future
   override would need its own audited field approved by the task owner, never a key (ADR-012 amendment).
7. **Limits (documented, not loopholes for the same content).** The key check sees only operations of the task on
   this device and compares tags of one epoch; two concurrent requests with one key may both pass it (an earlier
   operation still `received` does not hold a slot yet). Its scope is the task, so a subagent session and its parent
   share keys; a clash only refuses a keyed request, the same request without the key still runs. In each case
   only *other* content can get through; the same content is always caught by the hub's slot. The known limitation
   "a key rotation inside a window re-allows one duplicate" (04-crypto.md §4.10) is unchanged.
8. **Types without dedupe fields** keep the key's slot, so any repetition with the same key (also with other
   content) is a duplicate of the first, as before; the texts of the contracts say so. The reference crypto
   (`agent_hub.crypto.dedupe`, its runner copy and the web twin) is left unchanged: its docstrings describe
   `idempotency_fields`, which is still the key's field set; the vector `dedupe.json` is unchanged.
9. **E2E-22** now expects: the same comment again with a new key `duplicate` of the first; the key reused for another
   body `rejected` `params_invalid`; still two comment POSTs at GitHub (two distinct bodies).
