# CRY-03 acceptance (runner crypto as a copy of the reference)

| Criterion | Evidence |
|---|---|
| `agent_runner.crypto` is a copy of `agent_hub.crypto` and `agent_hub.log` with origin docstrings | 31 modules under `src/agent_runner/crypto/` (`log/` as subpackage), each starting with `Copied from agent-core <source> at <commit>.`; `ORIGIN.json` with source, commit and sha256 of the reference bytes |
| Copy deviations visible | `tests/platform/runner/crypto/test_copy.py`: `test_copy_matches_reference` (31 modules, hash after undoing origin, import renaming and the two recorded adaptations), `test_origin_lists_every_copied_module`, `test_copied_test_is_unchanged`, `test_origin_lists_every_copied_test`, `test_copies_import_nothing_from_agent_hub`, `test_tool_reproduces_the_copies_and_origin` |
| CON-02: all vectors of `contracts/vectors/` reproduced byte for byte, negative vectors included | copied `test_vectors.py` and `log/test_vectors_log.py` (`test_vectors_file_is_current`, replay of every negative vector, independent opening of the positive vectors); `test_runner_vectors.py` (every vector file has a runner section and is current) |
| CON-03: event kind matrix | `log/test_vectors_log.py::test_con03_event_kind_matrix_equals_contract` (`contracts/enums/event-kinds.json`) |
| Storage key with its own vector | `storage.py`; `test_storage.py`: key equals the shared `kdf.json` `storage_key` vector, runner-only vectors `data/storage.json` (3 purposes, 5 negative cases) current, context binding, fresh nonces |
| Gates | `tools/wp/gates.sh CRY-03`: ruff, ruff format, mypy --strict, `make platform-test` 100 % lines and branches (crypto: 2429 statements, 458 branches), Linux job green |
| Independent review | no high, 2 medium, 6 low; handled in `docs/platform/decisions/CRY-03.md` no. 12 |
