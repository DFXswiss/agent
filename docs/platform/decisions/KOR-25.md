# KOR-25 – decisions

GitHub in daily work: issue operations, connecting an existing token, asking for the connection when an operation
needs it. Architecture: ADR-059 (existing token), ADR-060 (held operations).

1. **Hold only for a missing connection.** A `github.*` operation is held (≤ 15 min) only when its formal check or
   pre-check fails with `github_not_connected`; `repo_not_allowed` is rejected at once. There is no "allow
   repository" card and no reason `repo_approval` (owner's decision 2026-11-23 19:52; without a list every repository
   of the connection is allowed, ADR-054). The contract value was removed before it was ever released.
2. **Flood limits.** ≤ 3 held operations per session (a fourth is rejected at once), ≤ 1 `waiting` event per session
   and reason per 60 s; an operation whose event was suppressed gets it at a later scan. The event carries only
   checked values (known type, valid repository name, device times).
3. **Registration expiry** (10 min in `received`) counts from the end of the hold; a request already older than
   15 min is rejected at once.
4. **Existing token is checked on the device**, never in the browser (CSP). A missing `X-OAuth-Scopes` header means
   a fine-grained token (`scopes: []`); a classic token without `repo`/`public_repo` is `scope_missing`; `401`
   `token_refused`; SAML enforcement `sso_required`. No `GITHUB_CLIENT_ID` needed for this way.
5. **Operator token (ADR-053) stores `source: "token"`** and answers `{step:"done", login, scopes, source}` like the
   token way (it is an existing token, not one of the device flow).
6. **Repository list fallback** to `GET /user/repos` when `/user/installations` answers `403` or `404` (logged
   `repositories_fallback`).
7. **No new error codes:** `410` (issues disabled) is `not_found` with "Issues are disabled in this repository.", an
   SSO refusal is `upstream_error` with a fixed message (GitHub's authorisation URL is never passed on).
8. **Listing types read one GitHub page**; `github.list_issues` leaves pull requests out and follows a page of only
   pull requests up to 5 GitHub pages, so "the newest issue" is found behind pull requests; `next_page` names the
   page after the last one read.
9. **Dedupe:** `github.create_issue` on repo + sha256(title) + sha256(body, empty when left out), 24 h;
   `github.update_issue` has none (setting values is idempotent); reading types none (audited by registration).
10. **`github.update_issue`** needs one of state/title/body/labels; `state_reason` only with a fitting `state`
    (`reopened` ↔ `open`, `completed`/`not_planned` ↔ `closed`).
11. **`github.comment`** already serves issues and pull requests (issue comment endpoint): documented, unchanged.
12. **Golden:** the guest validates operation types against its bundled contracts, so the new types need a new golden
    (guest version 0.2.0). The session context (GitHub connection state and the rule) comes verbatim from the runner
    and reaches VMs of the old golden too; the static harness rules (`images/harness/RULES.md`) arrive with the new
    golden.
13. **ADR numbers 059/060:** 055 to 058 were taken on `ad5f2026-live` by packages that landed earlier.
14. **E2E:** the platform scenarios (E2E-20, E2E-22 extended) are written and type-checked but not run: the
    full-stack suite needs the test device tooling of INF-05; hermetic runner tests (real PostgreSQL, GitHub double)
    and web Playwright tests (hub + device double) cover the flows meanwhile.
