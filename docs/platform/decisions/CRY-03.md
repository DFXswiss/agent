# CRY-03 decisions (runner crypto)

Small decisions of the runner crypto package (04-crypto.md §1–§6, §10; 05-runner.md §1.4, §2.4; ADR-022;
ADR-027 §2).

## Copy of the reference
1. Layout: `agent_hub.crypto` → `agent_runner.crypto`, `agent_hub.log` → `agent_runner.crypto.log` (a subpackage,
   because the package owns only `src/agent_runner/crypto/`). The vector generators (`vectors.py` of both
   packages) are copied as well: the runner regenerates every file of `contracts/vectors/` with its own copy and
   compares byte for byte (`test_vectors_file_is_current`, `test_every_vector_file_has_a_runner_section`), and the
   negative vectors are replayed through the runner code.
2. A copy differs from its reference only by (a) a first docstring paragraph
   `Copied from agent-core <source> at <commit12>.` (the last agent-core commit that changed a copied directory,
   so unrelated agent-core merges leave the copy unchanged), (b) renamed module names on import lines and (c) explicit
   text-block adaptations kept in `tests/platform/runner/crypto/copy_tool.py` (`SOURCE_ADAPTATIONS`) and recorded
   per file in `src/agent_runner/crypto/ORIGIN.json`. There are two adapted modules: `vectors.py` (area discovery
   imports `agent_runner.crypto.<area>.vectors` below the crypto package; docstring wording) and `log/vectors.py`
   (import order after renaming, required by ruff's isort). No rule or format differs.
3. `ORIGIN.json` stores, per module, the sha256 of the **reference file bytes** at the named agent-core commit
   ("normalisation of the import lines" = undoing (a)–(c)). `test_copy_matches_reference` normalises every copy and
   compares the hash, so any other edit of a copy fails; the hash can also be checked directly against agent-core
   with `git show <commit>:<source> | shasum -a 256`. `test_origin_lists_every_copied_module` refuses an unlisted
   module; `storage.py` is the only runner-only module.
4. The reference unit tests are copied too (`tests/platform/runner/crypto/` and `…/crypto/log/`), with renamed
   import lines and `TEST_ADAPTATIONS`: `test_make_vectors_script_is_a_thin_caller` is dropped (the synced
   `contracts/tools/make_vectors.py` imports `agent_hub` and runs only in agent-core), module names inside code
   strings and the repository-root depth of the log tests are adapted. Prose in test docstrings keeps naming the
   reference modules and the hub suite IDs (`U-HUB-log`, …); by location they belong to `U-RUN-crypto`. They carry
   the 100 % coverage of the copy, as they do in agent-core. `ORIGIN.json` (`tests`) records the hash of each
   reference test and of its copy (`copy_sha256`); `test_copied_test_is_unchanged` and
   `test_origin_lists_every_copied_test` make a hand edit or an extra file visible.
5. Vector section files of agent-core under `contracts/tools/vectors_*.py` would import `agent_hub` and cannot run
   in agent; none exist. If agent-core adds one, the runner must get the section as `agent_runner/crypto/<area>/`
   code – `test_every_vector_file_has_a_runner_section` makes the gap visible.

## Updating the copy
6. After an agent-core merge that changes `src/agent_hub/{crypto,log}/` or their tests, from the agent worktree:
   `python tests/platform/runner/crypto/copy_tool.py --agent-core <agent-core clone> --rev
   origin/ad5f2026-agent-platform` (reads through `git show`, checks nothing out), then `make platform-ci`.
   `--check` only reports differing files. A failing adaptation block (`found 0 times`) means the reference changed
   at that place: update the block in `copy_tool.py`, never edit a copied module by hand. Commit the result in one
   signed commit (`Update the runner copy of the reference crypto.`).

## Storage key and local sealing (`storage.py`)
7. The storage key is `kdf.storage_key(device_sig_seed)` (04 §3), already part of the shared vectors
   (`kdf.json`, `storage_key`); `test_storage_key_matches_the_shared_vector` checks it.
8. The local format is not specified in 04: `seal_local` = `nonce(12) ‖ AES-256-GCM(key, plaintext, AAD)` with
   `AAD = P ‖ "runner-storage\0" ‖ purpose ‖ "\0" ‖ context` and a fresh random nonce (no caller-chosen nonce,
   as for event headers in CRY-01). The AAD binds a ciphertext to its use and row, so a ciphertext moved to another
   row (another `origin_seq`, scope epoch or credential) does not open. Same blob form as identity bundles
   (`aead.seal_blob`), errors `decrypt_failed`/`header_invalid` of `aead`. One key serves the device's lifetime
   with random 96-bit nonces; the AES-GCM limit of 2^32 seals per key (collision probability ≤ 2^-32) is far above
   the expected volume (outbox items and scope keys of one device, typically well below 10^6 per month). A
   per-row subkey was not added; if a device ever approached the limit, re-enrollment gives a new seed.
9. Purposes (closed): `outbox` (`outbox.body_ct`, context `<origin_seq>:<item_kind>`, so both columns are
   authenticated), `scope_key` (`scope_key.key_ct`, context `<scope>:<scope_id>:<epoch>`), `credential` (the
   cached credential record, which is already the hub ciphertext of 02 §4; context `<owner_id>:<kind>:<version>`,
   so a row cannot be replaced by an older version). Decrypted credential material is never stored (05 §2.6);
   whether RUN-09 seals the cached record in addition is its choice. Contexts are 1–1024 printable ASCII
   characters without `\0`, numbers 0…2^63−1 (`bigint`); bad purposes or contexts are programming errors
   (`ValueError`), not inbound data.
10. The runner-only vectors (`tests/platform/runner/crypto/data/storage.json`: key from the shared seed, one blob
    per purpose, five negative cases) are generated by `test_storage.py` and checked to be current; they are not a
    contract amendment because no other implementation reads runner storage.

## Review
12. Independent review: no high findings. Medium (both fixed): the `credential` purpose suggested storing decrypted
    material (now the cached record with version binding, decision 9); copied tests had no checksum (now in
    `ORIGIN.json`, decision 4). Low fixed: outbox context binds `item_kind`, credential context binds `version`,
    numbers bounded to `bigint`, test that no copy imports `agent_hub`/`agent_core`, acceptance file added.
    Low documented: nonce budget (decision 8), hub suite IDs in copied tests (decision 4).

## Other
11. `agent_runner.encoding` (RUN-01, dependency-free b64u for config and identity) stays; the crypto copy has its own
    `encoding`, as RUN-01 decision 1 foresaw.
