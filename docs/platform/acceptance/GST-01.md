# GST-01 acceptance (guest skeleton: two processes, control socket, journal, schema validator)

| Criterion | Evidence |
|---|---|
| Tests against real unix sockets and a real HTTP server in tmp | `tests/platform/guest/test_control.py` (control socket, api.sock, in-process and `python3 -m agent_guest.control` subprocess), `test_apisock.py`, `test_server.py` (`127.0.0.1`, port 0) |
| macOS and Linux | `make platform-ci` on macOS; the same suite (2659 tests) in the Ubuntu 24.04 `guest-ci` VM (kernel 6.8) as an unprivileged user and as root |
| Token constant-time, wrong → 401 | `test_wrong_or_missing_token`, `test_two_authorization_headers`, `test_token_file_is_read_per_request` (SHA-256 digests + `hmac.compare_digest`) |
| Journal long poll, ack, prune with fake clock | `test_journal.py` (`test_long_poll_*`, `test_ack_and_prune`, `test_unacknowledged_records_are_kept`), `test_control.py::test_journal_long_poll`, `test_prune_loop` |
| Concurrency, API process writes only through api.sock | `test_concurrent_appends_through_api_sock` (16 threads × 25 appends, gapless 1..400), `test_api_process_modules_never_load_the_journal` |
| Validator against all contract fixtures and own edge cases | `test_schema.py`: every schema of `contracts/` loads under the keyword whitelist; all 1757 fixtures classified correctly; booleans never integers, `4.0` not integer, ECMA `$`, `$ref` local/relative/siblings, pointers |
| No import outside the standard library | `test_server.py::test_guest_imports_only_the_standard_library` |
| Gates | `tools/wp/gates.sh GST-01` green: ruff, `mypy --strict`, 100 % lines and branches (macOS + Linux combined) |
