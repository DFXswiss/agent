# DBL-02 decisions (GitHub double)

Small decisions of the GitHub double package (07-testing.md §9; 05-runner.md §11.4, §12, §12.1; 03-api.md §9;
ADR-015; conventions of `agent_doubles.common` from DBL-01).

## Layout
1. Besides the listed modules (`__main__.py`, `server.py`, `repos.py`, `deviceflow.py`, `faults.py`) the package adds
   `objects.py` (git objects and their SHA-1 ids, no git binary) and `render.py` (the JSON forms of the answers). The
   journal, the state document, the HTTP server and the CLI options come from `agent_doubles.common`.
2. One server plays the API host, the web host (device flow) and the codeload host: tests and the E2E orchestrator set
   both `GITHUB_API_URL` and `GITHUB_WEB_URL` to the double. The tarball route redirects to
   `<web>/codeload/{owner}/{repo}/legacy.tar.gz/{sha}` (GitHub: `codeload.github.com/…`); the runner's redirect rule
   (05 §12.1, "codeload host of the configured web URL") therefore has to accept the web host itself when the web URL
   is the double. Recommendation for RUN-09: a runner setting for the codeload base (default
   `https://codeload.github.com`, in tests `<double>/codeload`) instead of widening the redirect rule. `--public-url` / `GitHubDouble(public_url=…)` replaces the bound address in answers when clients
   reach the double through another address.

## Fidelity to GitHub where the specification words it differently
3. Creating an existing ref answers **422 `Reference already exists`**, as GitHub does; 03 §9 writes "409 at GitHub ⇒
   `branch_exists`". GitHub uses 409 only for an empty repository. The runner (RUN-09) maps 422 with that message (and
   409) to `branch_exists`; LVT-01 confirms the form against the real API.
4. Other ref errors follow GitHub: `PATCH` of a missing ref → 422 `Reference does not exist`; a non-fast-forward update →
   422 `Update is not a fast forward`; an unknown object → 422 `Object does not exist`; an unknown ref in
   `GET /repos/{r}/commits/{ref}` → 422 `No commit found for SHA: <ref>` (recorded); an unknown tarball ref → 404.
5. `PATCH …/git/refs/…` with `force: true` is refused (422) **and** recorded as a failure: the runner never forces
   (03 §9 `github.push`), so a forced update in a test is a defect.
6. Tree creation: `sha: null` deletes a file or a whole directory; deleting a path that does not exist is refused (422)
   so a wrong deletion list is noticed; an entry of type tree replaces the directory at its path; `base_tree` must be a
   tree id. Submodules (`160000`) are not modelled and refused. `GET /compare/{a}...{b}` of unrelated histories answers
   404 `No common ancestor between … and ….`; the merge base is a best common ancestor in the graph (dates only break
   ties). Error texts start with `Invalid tree info` (GitHub's
   texts for these cases vary; the runner keys on the status only).
7. Errors carry GitHub's body form `{"message", "documentation_url", "status"}` (recorded); validation errors carry
   `errors` items with `resource`, `field`, `code` (and `message` for `custom`). The documentation URL is generic.
8. Patches (`files[].patch`) are unified diffs with three context lines and the `\ No newline at end of file` marker,
   as recorded; binary files (NUL byte or invalid UTF-8) have no `patch` and count no lines. Renames are not detected.

## Requests the double treats as defects (failures in `/_double/state`)
9. API requests without `X-GitHub-Api-Version` or with an `Accept` other than `application/vnd.github+json` are answered
   (GitHub would answer them) but are failures; an unsupported version answers 400 and is a failure; a request without
   credentials answers 401 `Requires authentication` and is a failure. An unknown token answers 401 `Bad credentials`
   without failure (revoked or deleted tokens are legitimate test cases).
10. A codeload request that carries an `Authorization` header is a failure (a token forwarded across the redirect).
    Private-repository redirects carry a download token in the query (as GitHub's codeload URLs do) that is valid for
    that repository and commit only; public repositories need none.
11. Unknown routes (including wrong methods and `HEAD`), unparsable or non-object JSON bodies and chunked bodies
    (`transfer-encoding` instead of `content-length`) are failures, as in the model double.
12a. The device-flow endpoints answer JSON when `Accept` names a JSON type (`application/json` or
    `application/vnd.github+json`, the header 05 §12.1 puts on every GitHub request), else form-encoded like GitHub
    without `Accept`. Which form github.com sends for `application/vnd.github+json` is not recorded (no GitHub App
    exists for a live check); RUN-09 should send `Accept: application/json` on these two web-host endpoints, LVT-01
    notes the real answer if a device flow is ever run live.

## Rights
12. A repository is reachable with a token when an installation visible to the token's user covers it (installation
    `users`, or the user's own account; selection `all` or the listed repositories); anything else is 404, as GitHub
    answers for repositories outside the App installation. Writes need the installation permission (`contents` for
    git data and refs, `pull_requests` for pull creation, `issues` for comments), else 403
    `Resource not accessible by integration`. Defaults: contents/issues/pull_requests write, metadata read.

## Determinism and time
13. Object timestamps are logical (2026-01-01T00:00:00Z plus one second per seeded or created object), so commit ids are
    reproducible. Device-flow timing and rate-limit resets use an injected wall clock (`clock=`), which
    `POST /_double/clock {"advance": s}` moves forward.
14. Device and user codes, issued tokens (`ghu_` + 36 characters), download tokens and request ids come from counters.
    `slow_down` adds five seconds to the interval (as GitHub); polling earlier than the interval answers `slow_down`;
    `POST /_double/device` decides (`authorize` with a seeded login, `deny`, `expire`, or `slow_down` for the next
    `count` polls). A device code yields its token once. The page `GET /login/device` is informational.
15. Rate-limit headers are counted per user (5000 per hour window); faults produce `Retry-After`, secondary limits and
    `x-ratelimit-remaining: 0` with a reset `reset_in` seconds ahead (`exhaust` keeps the normal answer, so the runner's
    "wait until reset" rule is testable without a refusal). Faults on API routes are applied after the header checks
    (a retried request is still checked) and their answers carry the rate-limit headers.
15a. Two faults alter an answer instead of replacing it, so the runner's defensive paths are testable: `tree_mismatch`
    (`POST …/git/trees` stores and returns a tree with an extra file `.double-tree-mismatch`, 03 §9 `tree_mismatch`) and
    `truncated` (`GET …/git/trees/…` answers `truncated: true` with the first `limit` entries).

## Seeding, faults, expectations, state
16. `POST /_double/seed` adds; names already present are refused and a refused seed applies nothing.
    A token belongs to one user only. `POST /_double/reset` empties the world (in place, so concurrent requests keep one
    lock), the faults and expectations, the device grants, the failures and the journal in memory and restarts the
    code, token and id counters (journal sequence numbers continue, as in every double) (unlike the model double, whose scenario stays loaded: the GitHub world is seeded per test).
17. Every armed fault is an expectation: `state.done` stays false until it fired as often as armed. Expectations
    (`route`, optional `repo`, `count`) are exact: fewer keeps `done` false, more is a failure (E2E-22 "the double got
    one POST"). `ok` = done and no failure. Faults and expectations name routes by template (`METHOD /path/{param}`),
    `*` for any route; the journal records the template as `route` and an injected fault as `fault`.
18. `GET /_double/repos/{owner}/{repo}` lists refs, pull requests and comments for assertions in E2E tests. Issues
    can be seeded (number, title, author) as comment targets for `github.comment`; issues and pull requests share the
    number sequence of a repository. Other issue routes are not part of the runner's surface.

## Shapes
19. The fixtures under `tests/platform/doubles/github/fixtures/` are real answers recorded read-only from GitHub's public
    example repository, or the examples of the REST reference where no recording was possible without a GitHub App
    (installations, device flow, created blob); every value is replaced by a neutral value of the same JSON type, so
    they contain no personal data. The double must send exactly the recorded key sets with the same types. Values the
    double does not model (stars, mergeability, avatars) are fixed neutral values.
20. Pagination follows GitHub: `per_page` default 30, at most 100, invalid values fall back to the defaults; `Link`
    with `prev`, `first`, `next`, `last`. Pull lists are newest first; the `head` filter needs `owner:branch`.

## Review
21. Independent review (fresh context): no high findings. Fixed: JSON for the vnd `Accept` in the device flow (12a),
    `tree_mismatch`/`truncated` faults (15a), header checks before faults and rate headers on fault answers (15),
    world reset in place and counters restarted (16), compare without common ancestor and graph-based merge base (6),
    comments on seeded issues (18), duplicate tokens refused (16), wording of 11. Passed on to the specification owner
    (not changeable here): 422 versus 409 for an existing ref (3) and the codeload redirect host (2).
