# GST-01 decisions (guest skeleton: two processes, control socket, journal, schema validator)

Small decisions of the guest-core package (05-runner.md §5.1, §4.4, §4.6 nos. 1, 3, 4, 7; 03-api.md §7; 02-data-model.md
§5; ADR-008, ADR-022, ADR-027 §4).

## Modules
1. Besides the owned modules the package adds two small root modules in its own area: `routing.py` (HTTP/1.1
   plumbing shared by the control socket and port 7700: `Route`, `Router`, `Request`, `Response`, `HttpError`,
   handler base class) and `clock.py` (injectable clock, `iso_ms`, stdlib UUIDv7). Tests for configuration and
   registry live in `test_server.py`. Everything is standard library only (test
   `test_guest_imports_only_the_standard_library`).
2. `__main__.py` keeps `--version` and help without arguments (FND-00 skeleton tests) and adds `agent-guest control` /
   `agent-guest api`; the units keep starting `python3 -m agent_guest.control` and `python3 -m agent_guest.server`.

## Extension points (ADR-027 §4)
3. `control_routes/<name>.py` exports `ROUTES` (sequence of `routing.Route`) and/or `API_OPS` (op name →
   `op(context, args) -> dict` on `api.sock`). ADR-027 names only `ROUTES`; api.sock ops are the control-side half of
   the API process's handlers (prompt check, file records, pending calls), so they live next to the control routes
   instead of in a new central list. `api_routes/<name>.py:ROUTES`, `mcp_tools/<tool>.py:TOOL`,
   `hook_addons/<name>.py:ADDON` as in ADR-027; the types of `TOOL` and `ADDON` are left to GST-04/GST-03, the registry
   returns them keyed by module name in sorted order. `API_OPS` is an addition to the table of ADR-027 §4 (same
   discovery rule); the module docstring of `registry.py` is its reference until the ADR table is amended. Missing attribute, wrong type or duplicate → `RegistryError`
   (`registry_invalid` / `registry_duplicate`), so no process starts with half a registry.
4. A route declares `request_schema` (contract path), `body` (`json`, `none`, `raw`), `max_body` and `path_params`
   (name → schema reference). Invalid path parameters answer `404 not_found` like unknown objects; invalid bodies or
   query parameters `400 request_invalid` with `detail.pointers` (at most 32). Raw routes (tar streams, model requests)
   get a bounded reader; unread rest closes the connection.

## Control socket
5. The control token is read from the token file on every request (the runner writes it at clone time; a restart is not
   needed) and compared as SHA-256 digests with `hmac.compare_digest`; a missing or empty file, a scheme other than
   `Bearer` (case-insensitive, RFC 9110) or two `Authorization` headers authorize nobody. Authentication runs before routing, so an unauthenticated
   caller cannot probe paths (`401` for every path).
6. Wrong method on a known path answers `404 not_found` (the guest error list of `guest/error.response.json` is closed
   and has no `method_not_allowed`); errors raised by `http.server` itself (unknown method, malformed request line)
   are JSON bodies too. Chunked request bodies are refused (`400`); every body needs exactly one `Content-Length` of
   ASCII digits. Any error before the body was read closes the connection, so an unread body is never parsed as the
   next request (no request smuggling on keep-alive connections). JSON nesting too deep for the parser is
   `request_invalid`.
7. Unexpected handler exceptions answer `503 unavailable`, the traceback goes to stderr (journald), never to the client.
8. Sockets are created under `umask 0177` and then set to `0660` with their group (`agent-ctl`, `platform`); a stale
   socket file is replaced, any other file at the path refuses the start (`socket_path_invalid`). Backlog 128.

## api.sock and the API process
9. The API process does not cache the session table itself: it asks the control process per request
   (`session.resolve {token_sha256}`), whose in-memory cache is the only copy (filled once at start, then changed only
   through `State.save_session`, i.e. control calls). Only the SHA-256 of the bearer token crosses the socket; the
   control process compares it with every cached hash (no early exit). This fixes spike defect 1 without a second cache
   that could go stale; the cost is one unix-socket round trip (sub-millisecond) per authenticated request.
10. `journal.append` from the API process is limited to `session.hook`, `usage`, `ping.delivered` and `guest.error`.
    Kinds that need control-side checks get dedicated ops in later packages (prompt origin with `prompt_registry`,
    `operation.request`/`platform.query` only through `pending.create`, file records read as root), so a compromised
    API process cannot forge transcript output, session states or prompt acceptances. A `session_id` must name an
   active session (not `stopped`/`failed`), as for `pending.create`.
11. The API process binds `127.0.0.1` only (not configurable). Port: `AGENT_GUEST_API_PORT`, default 7700, `0` for
    tests. Handler limit 10 s (05 §5.1, hooks and MCP) through a worker thread; a route may set its own `timeout`
    because 03 §12 lets MCP `request_operation` wait 20 s and `get_operation` up to 30 s (the two sections differ;
    GST-04/GST-07 set the longer limits on those routes only). After the limit the client gets `503` and the handler
    result is dropped (Python cannot cancel threads). The process shared by all sessions of a VM accepts at most 256
    connections and runs at most 64 handlers (abandoned ones included); beyond that it answers `503` or closes the
    connection, so one session cannot exhaust it.
12. `GET /v1/health` on port 7700 needs no token and returns only `{state:"ok", guest_version}` after a `hello` round
    trip to the control process (`503` if that fails), so it doubles as the "port 7700 reachable" probe.

## Journal (02 §5)
13. Additions to the specified tables: `journal.acked_at` (time of the runner's acknowledgement), `pending.answered_at`
    and table `guest_meta` (assignment, broken flag). Pruning deletes records acknowledged more than 24 h ago; this
    satisfies both 02 §5 ("acked and older than 24 h": acknowledgement is never before the record) and ADR-022 ("24 h
    after runner acknowledgement"). Assignment and broken state must survive `Restart=always`, so they are stored.
14. A failed `COMMIT` rolls the transaction back and restores the head, so the connection never stays inside a
    transaction. `ack(through)` beyond the head answers `409 state_conflict` (a runner cursor from an earlier VM must not acknowledge
    records that do not exist yet); acknowledging again keeps the first time.
15. WAL mode with `synchronous=FULL`: a record is durable before the runner can read it (ADR-022). File created `0600`
    (`O_NOFOLLOW`), parent `0700`; schema applied when `user_version` is 0, any other version than 1 refuses the start
    (the golden image pins the schema, 02 §6).
16. Every journal body written through `Journal.insert` is validated against `guest/journal/<kind>.json`; bodies are
    stored as canonical JSON (sorted keys, no NaN). `head` comes from `sqlite_sequence`, so it survives pruning.
17. Long polls (`wait` ≤ 25 s) wait on a condition that every committing append notifies; `close()` wakes them. The
    wait uses the monotonic clock; the injected clock drives timestamps and pruning (fake clock in tests).

## Pending calls (library for GST-04/GST-07)
18. `pending.create` writes the pending row and its journal record (`operation.request` / `platform.query` with the new
    UUIDv7 `pending_id`) in one transaction; only active sessions may create. `pending.wait` (≤ 30 s) checks that the
    pending call belongs to the asking session. `POST /v1/pending/{id}/answer` stores one answer; an identical repeat
    is accepted (runner retries), a different one is `409 state_conflict`. Expiry of unanswered calls
    (`guest.error pending_expired`) is left to GST-07.

## Health (03 §7.1)
19. `claude_version` comes from `/opt/claude/install.json` (`version`, written by the golden installer),
    `landlock_abi` from `/opt/agent-guest/build.json` (`landlock_abi`, written by VM-01); missing files give
    `unknown` / `0`, malformed files refuse the start. `boot_id` is read once at start. Sessions are listed sorted by
    ID, at most 64 (schema limit).
20. `POST /v1/assign` repeated with identical values answers `{}` (idempotent retry). Stricter than the wording of
    03 §7.1 (which names only another workgroup): a different `vm_id` or `org_id` for the same workgroup is also
    `already_assigned`, because a VM ID mismatch means the runner talks to a VM it does not know. A broken VM refuses
    every assignment with `409 state_conflict`.

## Contracts at run time
21. The guest validates against the JSON Schemas at run time, so the schemas must be present in the VM. Configuration
    `AGENT_GUEST_CONTRACTS` defaults to `contracts/` next to the installed package (`/opt/agent-guest/contracts` in the
    golden image); without `common.json` there the control process refuses to start (`contracts_missing`). **Interface
    for VM-01:** the golden build must copy `contracts/` (at least `common.json`, `guest/`, `mcp/`, `ops/`) to
    `/opt/agent-guest/contracts` and include it in the golden-cache hash.

## Configuration
22. `AGENT_GUEST_*` names outside the documented list refuse the start (`config_invalid <NAME>`), as for the runner
    (RUN-01 decision 3). Group names and paths are checked; the API host is fixed.
