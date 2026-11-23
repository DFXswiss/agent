# RUN-09 acceptance evidence (device requests, GitHub connect, credential cache, GitHub executors I)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/credentials/` (kit `kit.py`), `tests/platform/runner/requests/`,
  `tests/platform/runner/github/` (GitHub double fixtures in `conftest.py`).
- Real PostgreSQL store, the real sync runtime (signed hub client, outbox) and org logs verified by the real reconcile
  steps. The hub double extends the RUN-03 platform hub with `GET /v1/device/requests/{id}`,
  `POST /v1/device/requests/{id}/responses` (gapless `seq`, identical retries, closed after `final`),
  `GET|PUT /v1/device/credentials/{owner}/{kind}` (device signature, `expected_version`, `CK` epoch, active request of
  the owner) and validates every request and answer against `contracts/api/`. Requests and records are sealed, and
  responses opened, with the crypto reference functions and the vector identities.
- GitHub: the GitHub double (DBL-02) as a real HTTP server on a loopback port of the package range, with its journal
  for the exact REST sequence and its fault injection; mock transports only for answers the double cannot give
  (malformed answers, foreign `Link` hosts, races).
- Covered:
  - device-request frame: decryption and dispatch, responses `seq` 1..n with `final`, follow-ups to the running root,
    newer root replaces / older root `busy`, no handler, failing/silent handlers, request expiry (`timeout`), hub closed
    (`forbidden`), transport retries with identical envelopes, restart (`device_shutdown`), shutdown, abandonment after
    two listings without the root, reconcile step, hint, daemon task;
  - refused requests (no answer, audit `runner.inbound_rejected` source `request`, counter, remembered): forged
    signature, other device, expired, future, unknown user, user the device does not serve, malformed envelope,
    envelope under another ID, body not an object / not JSON, encrypted to another key, foreign follow-ups;
  - credential cache: verified fetch and cache, decryption only inside `opened` (body wiped after), fetch on demand,
    purge on `404` with listeners, refused records (signature, owner/kind, author not eligible, unknown author,
    malformed, rollback), refresh of every kind, missing key / wrong key / non-object body, device writes (create,
    update keeping the ID, newest epoch, version-conflict retry, refusals), hint and reconcile step;
  - `github.connect` against the double: device code relayed, `authorization_pending`, `slow_down` (5 → 10 s),
    `expired_token` → `timeout`, `access_denied` → `upstream_error`, poll disconnects and 5xx (limit), device-code
    failure, wrong client ID; credential stored under `CK` authored by the device, token never in logs or responses;
    REST sequence `POST /login/device/code`, `POST /login/oauth/access_token` (n), `GET /user`;
  - GitHub client: fixed headers and token, `5xx`, `429` with `Retry-After`, secondary limit with/without
    `Retry-After`, primary limit (wait ≤ 60 s once, else retryable), exhausted limit gating the next request,
    disconnect, `401`, `403`, `404`, `422`, redirects refused, invalid/too large bodies, pagination over 101 items,
    codeload download without token;
  - executors (results validated against `contracts/ops/<type>.result.json`) and exact journals:
    `list_repositories` (`GET /user/installations`, `GET /user/installations/31/repositories`), `get_pull_request`
    (pull, files, comments; truncation), `create_branch` (`GET …/git/ref/heads/main`, `POST …/git/refs`; full/short SHA;
    `branch_exists` for 409 and 422; retry success; 5xx/429/secondary retryable), `create_pull_request` (lookup then
    `POST`; existing open PR → `existing:true`; race; no commits; unknown head), `comment` (one `POST`; `not_found`);
    missing/revoked/unusable connection → `github_not_connected`;
  - pre-checks: allow-list data, `GET /repos/{repo}` for the repository types, `repo_not_accessible`,
    `github_not_connected`, `upstream_error`;
  - through the RUN-07 pipeline with the double: pre-check + one `POST` per comment, a 5xx retried within the same
    operation (attempt 2), existing PR → `succeeded` and MCP `duplicate`, `repo_not_accessible` rejected after one
    `GET`, no connection → nothing reaches GitHub.
- Coverage of `agent_runner/{requests,credentials,github}`: 100 % lines and branches.

## E2E
E2E-20, E2E-21 and E2E-22 do not exist yet (SCN-04 writes them); they were not run.
