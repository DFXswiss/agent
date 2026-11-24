# KOR-65 – decisions (GitHub with every repository, rule E-29)

1. **Solution with the fewest steps that holds:** "Connect GitHub" signs in with an **OAuth App** (device flow, scopes
   `repo workflow`), like the GitHub CLI; the GitHub App and the existing token stay. Evidence from GitHub's
   documentation (2026-11-24) in [ADR-077](../adr/077-github-every-repository.md): a GitHub App user token reaches
   only accounts where the app is installed (only owners install on organisations); the OAuth device flow needs no
   client secret; `repo` covers what the account reaches; new organisations restrict OAuth Apps by default, members
   request approval. The GitHub App alone would need an owner for every third-party organisation and an installation
   on the own account before a fork works.
2. **`workflow` scope:** without it GitHub refuses commits that add or change `.github/workflows/*` - such a
   repository would not "work". `read:org` is not requested (nothing uses it).
3. **Which next step:** derived from the refusal and the token's prefix (GitHub's documented token formats `ghu_`,
   `gho_`, `ghp_`, `github_pat_`); a GitHub App token gets `connect` when the OAuth App is set up (one sign-in reaches
   everything) rather than an install link. A `404` of an OAuth, classic or unknown token means the account itself
   has no access (`check_access`, link to the repository). The restriction message of GitHub ("OAuth App access
   restrictions", "forbids access via a personal access token (classic)") is matched case-insensitively; the message
   itself is never passed on. The SSO URL of `X-GitHub-SSO` is passed on only when it is exactly
   `<web>/orgs/<org>/sso?authorization_request=<id>` on the configured web host, else `<web>/orgs/<owner>/sso`.
4. **Recheck every 30 s** for an unchanged credential (an installation or approval at GitHub reaches the runner only
   through a new request), at once for a new credential. The hold stays 15 minutes (as ADR-066); after that the
   message names the step, so the session or the person can repeat the request later.
5. **Error object unchanged:** the next step travels in the `waiting` event (`next`) and in the message text, not as
   a new member of the operation error (older guests check the error against their bundled contract).
6. **Ways announced as features** (`github.ways`, `github.device_flow`, `github.operator`) on the existing
   `Agent-Features` header; the hub lists them in `GET /v1/me/devices` (additive). A device that announces no GitHub
   ways (older runner) counts as running the sign-in, as before. `github.operator` counts as a sign-in because users
   approved for the operator's account connect with `{}`; others get `flow_unavailable` with its own text and the token
   way, never a silent failure.
7. **`revocable`** in web and tool: a device-flow record (`source` `device_flow` or none) with a `ghu_` or `gho_` token;
   a stored token is never revoked (it may be the GitHub CLI's).
8. **`github.connect` request schema** stays one object with optional `token` and `app` (a `oneOf` would let the
   contract harness's mutation check fail for the empty body); both together are refused by the device
   (`upstream_error`).
9. **Not built:** the list of the GitHub App's installations in the settings (not needed once the OAuth App signs in;
   the install link appears where it is the one step). KOR-47 (several accounts) is not anticipated: the step always
   concerns the acting user's one connection.
10. **E2E in T1 only;** the real GitHub is never contacted (the double implements OAuth tokens, organisation policies
    and the second device-flow client). Whether GitHub answers a private repository of a restricting organisation with
    `403` (as the double) or `404` is not documented; with `404` the step would be `check_access` instead of
    `approve_app` - a residual risk named in the report.

## Independent review (fresh context) and what was done
- **High – a readable repository is not a writable one:** the pre-check of `github.create_branch`, `github.commit`
  and `github.push` now also needs the right to push (`reach.check_write`: `permissions.push` false -> held with
  `check_access` "may read but not write"; a GitHub App user token whose installations do not cover the repository ->
  held with the step of a repository the token does not see). A `403` access refusal at execution time (after the
  pre-checks passed; e.g. a comment in a public repository of an organisation that restricts OAuth Apps) ends
  `repo_not_accessible` with the same step in the message: the operation is already registered and cannot be held
  (deviation from "every operation is held", accepted; the session repeats it after the step).
- **High – the step "connect" could loop:** devices announce `github.oauth_app`; the card's sign-in sends
  `{app:"oauth_app"}` and offers only such devices; the agent tool names `github connect --app oauth_app`.
- **Medium:** a token step becomes `connect` whenever the OAuth App is set up and the token is not its own (one
  connection for every owner instead of a token that replaces it); cards fold per owner of the step (two owners, two
  steps); a step's link must lead to `https://github.com/` (or the doubles' loopback host) in the web and the tool;
  pre-filled token names `agent <owner>` and 90 days. Not changed: a `404` of the OAuth App's own token stays
  `check_access` (GitHub does not document whether a restricting organisation answers `404`); `list_repositories`
  does not list what the token does not reach (GitHub does not name it either).
- **Low:** the recheck times of operations that were not held are dropped; a misplaced docstring moved.
- The operator's account (ADR-053): a repository it does not reach gets `check_access` - the right step is that the
  repository's owner gives that account access, which the sentence "ask its owner for access" covers.
