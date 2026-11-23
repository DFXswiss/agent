# RUN-02 acceptance (runner sync base: outbox, signed HTTP client, WebSocket client)

| Criterion | Evidence |
|---|---|
| Gapless `origin_seq` under concurrent writers (M0), real PostgreSQL | `tests/platform/runner/sync/test_outbox.py::test_gapless_under_concurrent_writers_with_rollbacks`: eight writers on their own connections, every third transaction rolled back, numbers exactly `1..N`; rolled-back enqueue leaves no number; autocommit outside a transaction refused |
| Bodies encrypted with the storage key | every item kind (contract fixtures) round-trips; plaintext absent from `body_ct`; swapped ciphertexts give `decrypt_failed`; a corrupt row is fatal `outbox_corrupt` |
| Seal-once over retries, reseal | `test_push.py`: the hub stores the event, the answer does not verify, the retry sends the identical request bytes and the hub answers `replayed`; decrypted body, epoch, `lh`, author checked; `reseal` waits for a key of a newer epoch, then reseals and replaces `sealed` |
| Gap / conflict / rejected | gap rewinds to `detail.expected` and resends; impossible `expected` is fatal; conflict persists `sync_fatal`, logs `runner.alert origin_seq_conflict`, ends the loop, cleared by `clear_fatal`; forged (unsigned, other key, stale, altered) conflict answers are ignored; rejected items logged and skipped |
| Batch limits and rate | 1201 items in batches 500/500/201 with 250 ms spacing; 60 events of 64 KiB text split below 4 MiB with the first batch full |
| Wipe continuation | hub at `last_origin_seq` 7, fresh store with two items → renumbered 8, 9 and pushed; a pending device sends nothing |
| Signature and answer verification (fake hub) | the fake hub verifies every request signature (window, nonce); 13 manipulation modes of the answer (unsigned, other key, bad/short/padded signature, altered body, stale or malformed time, bad key ID, repeated header, content encoding, oversize) are transport errors; redirects not followed; refusal codes from the closed list only |
| WebSocket | signed upgrade against a real loopback server; order reconcile → push → pull; heartbeat validated against `ws/device/heartbeat.json` with registered and default providers; backoff 1.2/2.4/…/36 s (jitter extremes) and reset after 30 s stable with a fake clock; redirect and refused signature fail the attempt |
| Hint dispatch | registry handlers (async and sync) in arrival order; invalid, unknown and failing hints logged by type only |
| Contracts | every request, answer and frame of the fake hub validated against `contracts/` (`api/device-outbox.*`, `api/device-state.response`, `outbox/<kind>`, `ws/device/*`) |
| Gates | `tools/wp/gates.sh RUN-02` green: ruff, mypy --strict, 100 % lines and branches (incl. the Linux job on the runner device), contracts |
