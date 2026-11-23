# RUN-09 decisions (device requests, GitHub connect, credential cache, GitHub executors I)

Small decisions of the package that handles end-to-end encrypted device requests, connects GitHub accounts by the
device flow of a GitHub App, keeps credentials in the runner cache and executes the first five GitHub operation types
(05-runner.md §2.4, §2.6, §11.1, §11.4, §12.1; 03-api.md §4.10, §6.1, §6.2, §9; 04-crypto.md §4.4, §4.5; ADR-015).
No decision here changes an architecture rule, so there is no ADR.

## Device requests (`agent_runner/requests/`)
1. One service per daemon (`service_for`), fed by the hint `request {request_id}`, by reconcile step 70 `requests`
   (the `requests` list of the desired state) and drained by the daemon task `requests` (05 §1.1 lists it). Each root
   request runs as its own asyncio task (`Flow`); the handler of its kind comes from `<area>/requests.py:HANDLERS`.
2. Verification order (05 §2.4): envelope form and header → the ID asked for → addressed to this device → the user is an
   active user of an acting org and this device is in the user's `CK` readers (`crypto.log.wraps.cred_readers`, the
   same set the hub uses for eligibility) → the user's signature and decryption → time → body is a JSON object.
   Codes in the audit (`runner.inbound_rejected`, source `request`): `header_invalid`, `device_not_assigned` (wrong
   device or a user the device does not serve), `key_unknown` (no such user in the verified logs),
   `signature_invalid`, `decrypt_failed`/`hpke_short`, `time_skew`, `request_invalid`, `forbidden` (a follow-up whose
   root is not running for the same user and kind). The closed error list has no "expired" code: an expired request
   (older than 15 min) and one more than 120 s in the future are both `time_skew`.
3. A refused request is never answered (its sender is not trusted), audited once and remembered as `rejected` in
   `runner.device_request`; the row of a request whose header could not be read gets kind `unknown` and the nil user.
   Refusals that can pass later are **transient** (review H1, L4): `key_unknown`, `device_not_assigned` for a user
   the device does not serve yet (its log copy can be behind the hub, e.g. right after the auto-assign batch of a
   fresh private org), a time more than 120 s in the future, and a follow-up whose root was not processed yet. They
   are audited once per request and code, not remembered, request a reconcile run, and the request is verified again
   when offered again (hint, next desired state).
4. At most one active flow per user and kind: a newer root (header `ts`, then ID) replaces the running flow (the hub has
   already failed the older chain and hinted `reconcile`); an older root is answered `{step:"failed", code:"busy"}`.
5. A flow is abandoned without answer when its root is missing from two consecutive desired states. One missing
   listing is tolerated because the state of a running reconcile may have been fetched before the request was created.
6. Responses: `seq` per request, stored (migration `0006_device_request_seq.sql`, column `last_seq`) before the
   response is sent; transport errors resend the identical envelope (1 s, 2 s, 4 s), a refusal of the hub ends the
   flow (`RequestClosed`). A number used up without reaching the hub (cancelled send, crash) makes the hub answer
   `409 seq_conflict` with a lower `expected_seq`; the response is then sealed again with that number (review M1). A
   request that was `active` when the daemon stopped is answered `device_shutdown` when seen again; on SIGTERM the
   running flows are cancelled and answered `device_shutdown` (best effort); their rows stay `active` until that
   answer arrived (review M2).
7. A kind without a registered handler (`claude.login`, `credential.check`, `mailbox.check` until RUN-13) is answered
   `{step:"failed", code:"upstream_error"}`: the closed failure list has no "unsupported" code. A handler that raises or
   returns without a final response gets `upstream_error`; the request's 15 minutes end the flow with `timeout`.
8. Follow-ups (`follows` = root) are handed to the running flow (`Flow.follow_ups`, `Flow.wait_follow_up`) and answered
   with `Flow.respond(..., to=follow_up)`; a final response to any request of the chain ends the chain (03 §4.10).

## Credential cache (`agent_runner/credentials/`)
9. `credential_cache.record` keeps the record exactly as the hub stores it (02-data-model §4: "encrypted record as
   stored at the hub"): it is encrypted under the owner's `CK`, whose keys are sealed with the storage key. The
   storage-key context `credential_context` of the crypto copy is therefore not used.
10. A fetched record is verified before it is cached: author signature, header names the owner and kind asked for, the
    author is the owner or a device in the owner's `CK` readers of an acting org (its key from the verified log; this
    device's own key for records it wrote), and the version is not older than the cached one (rollback refused,
    `version_conflict`). A refused record is logged and counted (`verify_failures`) but **not audited**: the closed
    `source` list of `audit/runner.inbound_rejected.json` has no `credential` value (a contract package can add it).
11. `credentials_changed` and the hint `credentials {owner_id}` re-read **every** kind of the owner: the hub clears its
    notice per kind read, and a `404` purges the cached kind (disconnect). Purges call listeners registered with
    `credentials.cache.subscribe` (RUN-12 withdraws model credentials from gateways there).
12. Device writes (`put`) use the newest `CK` epoch the device holds, the record ID of the current record (a new UUIDv7
    for the first) and `version + 1`; `409 version_conflict` re-reads and retries (3 attempts). The current record is
    read only for version and ID (header form checked, not its author): a record written by a device that was removed
    since no longer verifies but must not block a reconnect (review M4). The cache upsert never replaces a newer
    version (review L5).

## GitHub (`agent_runner/github/`)
13. The device-flow handler lives in `github/device_flow.py`, not `connect.py` as the package text says: `connect.py`
    is the sync area's discovered extension point (`<area>/connect.py:HOOKS`, ADR-028), and a module of that name
    without `HOOKS` stops the daemon (`registry_invalid`).
14. Device-flow failures map to the closed list of `contracts/requests/objects.json`: `expired_token` and the end of the
    flow → `timeout`; `access_denied`, other OAuth errors and malformed answers → `upstream_error` (no "denied" code);
    network errors/`5xx` are polled again up to 5 times in a row, then `upstream_unreachable`; a token outside
    printable ASCII ≤ 4096 → `token_invalid`; no `CK` of the owner on this device → `credential_missing`.
    `slow_down` uses the interval GitHub names, at least the previous one + 5 s. `expires_in` is capped at 3600 s
    (contract maximum). `scopes` keeps the comma-separated scopes that fit the contract pattern (App user tokens
    have none).
15. Pre-checks (`github/op_prechecks.py`): `github.list_repositories` checks that the owner's credential opens (no
    GitHub call) and passes the allow-list to the executor; the four repository types call `GET /repos/{repo}` with
    the owner's token: `404` → `repo_not_accessible`, `401` → `github_not_connected`, anything else `upstream_error`.
    The registry allows one pre-check per type, so `checkout`, `commit` and `push` get theirs from RUN-10, which calls
    `precheck.repo_access` first.
16. Failures of executors: `401` → `github_not_connected` (not retryable); `404` → `not_found`; network, `5xx`, `429`,
    the secondary limit and an unexhausted primary limit → `upstream_error` retryable with `retry_after` (the pipeline
    retries 2/8/30 s); other refusals → `upstream_error`. A primary limit (`x-ratelimit-remaining: 0`) closes a rate
    gate per owner: the next request waits for the reset when it is at most 60 s away, else fails retryable.
17. Answers are checked against the result contracts before they are returned; e.g. a non-`https` `html_url` is
    `upstream_error`. The hermetic double must therefore answer with an https public URL (`public_url`), as the
    suites here do; E2E setups that start the double on plain loopback HTTP must set it too.
18. `create_branch`: a full SHA is used directly, a short SHA is resolved by `GET /commits/{sha}`, a name by
    `GET /git/ref/heads/{name}`. GitHub answers an existing ref with `422 Reference already exists` (the double too),
    so `branch_exists` covers `409` and that `422`. On a retried attempt a branch that already points at the resolved
    commit is success (the first attempt created it and only the answer was lost).
19. `create_pull_request`: the open-PR lookup uses `head=<repo owner>:<head>`; a `422 A pull request already exists`
    (a PR opened between lookup and create) is answered with that PR and `existing:true`; `422` naming the `head` or
    `base` field → `not_found`; other `422` (e.g. no commits between) → `upstream_error`.
20. `get_pull_request` keeps patches while their total stays ≤ 1 MiB **and** the whole result ≤ 200 KiB of body JSON:
    the result travels in one `operation.result` event (ciphertext ≤ 256 KiB, 04 §4.3), which the 1 MiB of 03 §9 alone
    would exceed. Before patches the rest is fitted into the same budget: the newest comments go first, then the body
    is cut, then the last files, then the title (review M3). Titles/bodies are cut to the contract lengths, entries outside the contract are left out; each sets
    `truncated`.
21. `list_repositories` leaves out repository entries that do not fit the result contract (logged with a count) and
    lists each repository once (case-insensitive), sorted.
22. Pagination uses `per_page=100` and follows `Link: rel="next"` using only its path and query on the configured API
    host, so the token is never sent to another host. `download` (for RUN-10's tarballs) follows exactly one redirect
    to `codeload.<web host>` (or `<web origin>/codeload/…`, the double) without the `Authorization` header.

## Review (independent subagent) and limits
23. Shared test files edited outside the package's paths, both only to keep the suite true to the new state:
    `tests/platform/runner/test_store.py` (column list of `device_request`, as every migration package does) and
    `tests/platform/runner/operations/test_wiring.py` (it asserted that no package registers pre-checks; it now asserts
    that the operations area registers none itself).
24. Not changed (review M5): refused credential records are not audited, see item 10; closing it needs `credential` in
    the closed `source` list of `contracts/audit/runner.inbound_rejected.json` (agent-core contract package).
25. Not changed (review L2): `GET /repos/{repo}` succeeds for a public repository even without the App installation, so
    the pre-check does not prove the installation for public repositories; a later write then fails `upstream_error`
    (`403`). Proving it costs a listing of every installation's repositories per operation; left as a documented limit.
26. Hub refusals while reading the owner's record during an operation mean the device is not eligible
    (`github_not_connected`); only transport errors are retryable (review L3). During `github.connect`, a hub refusal
    is `upstream_error`, a transport error `upstream_unreachable`.
27. Not changed (review L7): request bodies are checked to be JSON objects; each handler validates its own body.
    httpx's 30 s timeout is per phase (connect/read/write/pool), not per whole request.
