# ADR-059: Connect GitHub with an existing token (second way beside ADR-015)

Status: Accepted (2026-11-23)

## Context
- ADR-015 connects GitHub through the device flow of a GitHub App. The repositories the owner's team works on belong
  to organisations in which the owner's account is a member but not an owner; the account cannot install a GitHub
  App there. The owner does not set up anything at GitHub.
- The owner decided (2026-11-23): GitHub shall work with the existing account, connected "by transferring the auth
  token, quite simply"; the user's own AI agent does the setup (KOR-24). Such an account already has a login on the
  user's computer (GitHub CLI) or a personal access token.
- ADR-053 lets a platform device use the operator's token for approved users. This ADR is the general form: every
  user may store a token of their own.

## Decision
- `github.connect` takes an optional `token`. Without it the device flow runs as before (ADR-015). With it the
  execution device - never the browser (CSP, `connect-src 'self'`) - checks the token with `GET /user`: login and
  account ID from the body, scopes from `X-OAuth-Scopes` (no header: a fine-grained token, `scopes: []`). A classic
  token with neither `repo` nor `public_repo` is `scope_missing`, `401` is `token_refused`, an organisation enforcing
  SAML single sign-on for which the token is not authorised is `sso_required`. No `GITHUB_CLIENT_ID` is needed.
- The device stores the usual `github` credential under the owner's `CK` with the additive field `source`
  (`device_flow`, `token`; records without it stay valid) and answers `{step:"done", login, scopes, source}`. The
  token travels HPKE-encrypted to the device like every request body and is never logged or answered.
- Every operation works with such a token. Where GitHub serves only GitHub App user tokens (`/user/installations`),
  the runner falls back to what the account reaches (`GET /user/repos`), as for ADR-053.
- Settings → GitHub offers "Use an existing token" beside "Connect"; afterwards "Connected as `<login>`" with the
  source and the scopes. Disconnect is unchanged (the credential is deleted everywhere; revocation stays at GitHub).

## Consequences
- Such a token reaches as far as the account itself (a classic token with `repo`: every repository of the account and
  its organisations). The boundaries are the formal checks of every operation (03-api §9), the optional workgroup
  repository list (ADR-054), protected branches, fast-forward pushes, draft pull requests and the audit of every
  operation. The web shows the scopes; fine-grained tokens limited to the needed repositories are recommended, not
  enforced.
- The credential is sealed under the user's `CK`: the user's browsers and the devices of their organisations can open
  it, as with the device flow.
- A token that GitHub later refuses ends operations with `github_not_connected`; the runner then asks the user to
  connect again (ADR-060).

## Alternatives considered
- Only the GitHub App (ADR-015): blocks every repository of an organisation that has not installed the App - the
  owner's case. Rejected by the owner's decision.
- Checking the token in the browser: blocked by the CSP and would put the token in contact with more code than
  necessary.
- A general GitHub API pass-through for the session: contradicts concept §10 (only defined, authorised actions).

## Deviation from DESIGN.md
- None beyond ADR-015.
