# RUN-03 acceptance (platform enrollment, reconcile engine, log and wrap verification, pull)

| Criterion | Evidence |
|---|---|
| Enrollment success | `tests/platform/runner/enroll/test_platform.py::test_successful_enrollment_writes_the_identity_last`: body validated against `api/device-enroll.request.json`, `enroll_sig`/`inv_sig` verified by the fake hub, request signed by the new device key (proof of possession), answer verified with the hub key pinned from `/v1/config`; `device.json` and `keys/device.key` written; a second enrollment is `already_enrolled` |
| Every error path without leftovers | same file: bad codes (no request), root-key mismatch, config answers (status, JSON, shape, key formats, oversize), unreachable hub, hub refusal (`invite_unknown`), unsigned or foreign-key answer, unexpected `active` answer, failing `device.json` write, half identity, missing/invalid hub URL and root key; after each, neither identity file exists |
| Pending polling with a fake clock | `reconcile/test_engine.py::test_pending_device_polls_the_state_every_30_seconds_until_active`: three state polls 30 s apart, nothing else fetched while pending, `state=pending_certification` logged once, then active and acting |
| Log verification incl. rollback/fork, other orgs continue | `reconcile/test_logs.py`: full verification and storage, paging, rollback (offered and answered head), fork (same seq other hash, divergent history), refused entries audited once with the verified prefix kept, org not assigning the device refused, platform-log rollback/fork/refusal, uncertified and decertified device halts; `orgs/test_book.py`: copies, reload with re-verification (fail closed), certification by own keys, decertified devices excluded, rights at `lh` |
| Wrap rules | `reconcile/test_wraps.py`: W1–W3 accepted and refused cases (other recipient, bad signature, not entitled, unknown wrapper, ciphertext for another key, malformed), stopped/unknown orgs wait, `cred` wraps, audit once, keys sealed with the storage key |
| Pull with gaps and paging | `inbox/test_pull.py`: gaps in `scope_seq`, `more` paging, repeats ignored, cursor persistence, round limit |
| Rejected events audited | `inbox/test_pull.py::test_events_failing_verification_are_audited_and_skipped` and siblings: signature, author right at `lh`, kind for author kind, scope mismatch, malformed, wrong key, non-object body, decertified author; L10 refresh, fork and rollback |
| Hint dispatch | `reconcile/test_engine.py::test_hints_and_connect_hook_drive_the_engine`, `inbox/test_pull.py::test_task_hint_and_connect_hook` |
| Idempotent reconciles | `reconcile/test_logs.py::test_reconcile_verifies_and_stores_everything_and_is_idempotent`: a second run stores nothing, audits nothing and fetches only state, platform log after the head and wraps |
| Contracts | every query and answer of the test hub validated against `contracts/` (`api/device-*`), audit items against `outbox/audit.json` |
| E2E | E2E-05, E2E-06, E2E-14 do not exist yet (decisions/RUN-03.md item 25) |
| Gates | `tools/wp/gates.sh RUN-03` green: ruff, mypy --strict, 100 % lines and branches (incl. the Linux job), contracts |
