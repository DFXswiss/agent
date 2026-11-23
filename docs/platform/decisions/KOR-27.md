# KOR-27 – The operator's GitHub account: "Connect GitHub" connects approved users at once (runner only)

Decisions of the package (ADR-053 holds the architecture).

1. **Deployed web client and hub.** The web client of the deployed release takes `{step:"done", login}` as the first
   response: `connectProgress` (`web/src/core/github/connection.ts`) reads the last verified response and returns
   `done` before looking at `device_code`, and `checkResponseBody` (`web/src/core/requests/bodies.ts`) accepts `done`
   with `login` for `github.connect` without a preceding step. The response contract
   (`contracts/requests/github.connect.response.json`) allows `done` at any sequence number; the hub sees only sealed
   bodies. So the operator's way needs no change outside the runner. Test: every response of the operator's connect is
   checked against the response contract (`test_an_approved_user_is_connected_at_once_with_the_operators_token`).
2. **Binding to the signing user.** The approval is checked against `Flow.user`, the user of the request header the
   runner verified with that user's signature key (`RequestService.verify`). A request whose header names an approved
   user but carries another user's signature is refused before any handler runs; the file is not read (test
   `test_a_forged_request_for_an_approved_user_never_reads_the_file`).
3. **Settings check only the path.** The operator replaces the file atomically while the daemon runs, and may place it
   after the start; `config` checks the path (absolute), the users (UUIDs, lower case) and the pairing, and refuses
   both on org devices. Type, owner, mode and content are checked at each read; `doctor` runs the same checks.
4. **File checks.** `lstat` before opening (regular file, owner = effective uid, no group/other rights), `O_NOFOLLOW`
   open and the same checks on `fstat` of the opened file (a replacement between check and open is not read). At most
   4098 bytes are read; the content is the token, optionally ended by one line feed. Reasons: `missing`,
   `not_regular`, `owner`, `mode`, `unreadable`, `format`; each logged once until it changes or the file was usable.
5. **Codes.** An unusable file is `credential_missing` (the web client says "A credential the device needs is
   missing."); a refused token (`401` on `GET /user`) is `upstream_error` like the device flow's step `user` ("The
   service refused the request."). No new code (no contract change).
6. **Scopes.** From `X-OAuth-Scopes` of `GET /user` (classic tokens); fine-grained tokens name none (`[]`). The
   device flow keeps the scopes of its token answer.
7. **Repository list.** Fallback to `GET /user/repos` only on `403` (`forbidden`) of `/user/installations`, which is
   GitHub's answer for a token without the App; other failures stay as before. Same page limit (100 pages of 100) and
   result limit (10000). All other GitHub operations use `/repos/...` routes that a classic or fine-grained token
   serves unchanged; tests run get/create pull request, branch, comment, checkout, commit and push with a personal
   token of the double.
8. **GitHub double.** Seed keys `personal_tokens` and `collaborator` for users; personal tokens get `403` on the
   installation routes, `X-OAuth-Scopes: repo`, reach own and collaborator repositories with write access; new
   route `GET /user/repos` for both token kinds.
9. **Counters, not a gauge of approved users.** `agent_runner_operator_github_connects_total` and
   `_connect_failures_total` (device counters, in memory like the others). No metric names users or the account.
10. **ADR number 053.** ADRs are numbered without gaps on the base branch (`test_docs`). The package was first
    written on `develop` as 052; on the live branch KOR-23 holds 052 (operator subscriptions), so this ADR is 053.
11. **Linux part.** No guest code is touched; the Linux part of the gates ran unchanged for the combined coverage.
12. **Review finding "the token reaches the user" (high) - kept, documented.** The stored `github` credential is
    sealed under the user's `CK`; the user's browser and the devices of the user's organizations can open it, so an
    approved user can read the operator's token. The package requires exactly this way (`credentials.cache.put`,
    record `github {token, …}`), and the deployed web client and contracts admit no other: a reference instead of
    the token would need a new credential kind or contract change (excluded: no contract, no hub, no web). ADR-053
    and the runbook now say plainly that approval hands the token to the person and that only a revocation at GitHub
    takes it back; keeping the token on the device is named as an alternative for KOR-25.
13. **Review fixes.** Runtime check of the device kind in `approved` (besides the configuration); the token file's
    directory must be the runner account's and writable by nobody else (reason `directory`); runbook: disconnect
    before connecting again (the settings offer "Connect" only while disconnected), `workflow` scope, fine-grained
    tokens and collaborators, `read -rs`, the hint after "Disconnect"; ADR: workgroup admins set the allow-list,
    shared rate limit. Tests: connect without `GITHUB_CLIENT_ID`, a repository that exists but is not reachable,
    other failures of `/user/installations` are no fallback, org device at run time, directory checks.
14. **Review findings not changed.** Rate limit per user instead of per token (low; documented in ADR-053); the
    connect -> pull-request test calls pre-checks and executors directly, while checkout, commit and push run through
    the full pipeline with the formal checks (allow-list refusal included) - together they cover the way.
