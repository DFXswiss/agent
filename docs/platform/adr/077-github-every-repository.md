# ADR-077: GitHub reaches every repository of the account, and a repository it does not reach ends with one next step

Status: Accepted (2026-11-24)

## Context
- Rule [E-29](../KONZEPT.md#e-29): the GitHub connection works for every customer, always and everywhere, with every
  repository the customer's GitHub account can reach - own account, own or third-party organisation, fork. Rule
  [E-05](../KONZEPT.md#e-05): what is missing is triggered, not explained; [E-27](../KONZEPT.md#e-27): an agent can do
  it without a person.
- "Connect GitHub" used the device flow of a GitHub App ([ADR-015](015-github-app-device-flow-rest-only.md)). GitHub
  documents that a GitHub App user access token reaches only resources "in an account where it is installed"
  (docs.github.com, "Authenticating with a GitHub App on behalf of a user"). Only organisation owners install apps on
  an organisation ("Installing a GitHub App from a third party"); members may only request it. So a user's own fork
  or an organisation of a third party was not reachable, and the operation ended with `repo_not_accessible` and no
  way forward.
- GitHub documents the device flow for OAuth Apps without a client secret ("The `client_secret` is not needed for the
  device flow"; "Authorizing OAuth apps"), the scope `repo` as "full access to public and private repositories" within
  what the user already has, and `workflow` as needed to add or change workflow files ("Scopes for OAuth apps").
  Organisations created today restrict OAuth Apps by default until an owner approves them ("About OAuth app access
  restrictions"); members request the approval from their authorised applications. Tokens created by apps are
  authorised for SAML single sign-on automatically; a `403` names the authorisation URL in `X-GitHub-SSO`
  ("Authenticating to the REST API"). Fine-grained tokens can be pre-filled by URL parameters (`name`, `target_name`,
  `expires_in`, permissions; "Managing your personal access tokens"). The GitHub CLI prints its login token with
  `gh auth token`.

## Decision
1. **Connect with an OAuth App first.** The runner's `github.connect` with `{}` runs the device flow of an OAuth App
   (`GITHUB_OAUTH_CLIENT_ID`, scopes `repo workflow`) when it is set up, else the GitHub App's as before; `{app}`
   names one. The token reaches every repository the account reaches - the way the GitHub CLI works - so one sign-in
   covers the own account, forks, collaborator repositories and organisations. A flow that is not set up answers
   `flow_unavailable`. The GitHub App and the existing token (ADR-065) stay as equal ways.
2. **Every access refusal is held with one next step.** A repository-level pre-check that GitHub refuses for access
   (`404`; `403` for an organisation's OAuth App restriction, its ban of classic tokens, or SAML single sign-on) is
   held like a missing connection (ADR-066) with the reason `github_access` and the step `next {action, owner, url?}`
   the runner derives from the refusal and the kind of token (GitHub's token prefixes): `connect` (a GitHub App token
   and the OAuth App is set up), `install_app` (`<web>/apps/<slug>/installations/new`), `approve_app`
   (`<web>/settings/connections/applications/<client id>`), `authorize_sso` (the checked URL of `X-GitHub-SSO`),
   `new_token` (a pre-filled fine-grained token for the owner, or a classic token with `repo,workflow`),
   `check_access` (the account itself has no access: the repository's page). The person who gave the instruction
   (HOT-14) sees it as a card with one button or link (one card per owner of the step); types that write to the
   repository itself (`create_branch`, `commit`, `push`) also need the right to push in the pre-check (a public
   repository is readable with nearly any token), and a `403` access refusal that only the execution meets ends
   `repo_not_accessible` with the same step in its message (it is already registered and cannot be held). An agent
   sees `next` (command or link) in `task show`, and the session the same step in the error message. The runner checks the access again every 30 s and at once when a new
   credential arrives, so an installation, an approval or an authorisation at GitHub lets the operation go on by
   itself; after 15 minutes it is rejected with the step in the message.
3. **No dead button.** A runner announces its GitHub ways on the live channel (`github.ways`, `github.device_flow`,
   `github.oauth_app`, `github.operator`); the hub lists them in `GET /v1/me/devices`. The web offers "Connect
   GitHub" only when a device runs a sign-in and otherwise says so and offers the token with a pre-filled link to
   create one; the card's "connect" signs in through the OAuth App on a device that announces it.
4. **Without a browser.** `agent-platform github connect --from-gh` stores the token of the computer's GitHub CLI
   login (sealed to the device like `--token -`).

## Consequences
- Every repository the account can reach is reachable with one sign-in, except where GitHub itself requires an owner
  (an organisation restricting OAuth Apps, a missing installation of the GitHub App) or the user (SSO); those are held
  with the one link that resolves them and go on afterwards.
- The operator registers an OAuth App once (no secret; device flow enabled) and sets `GITHUB_OAUTH_CLIENT_ID`, and
  optionally `GITHUB_APP_SLUG` for the install link. Without it everything works as before plus the held steps.
- The OAuth token has the classic `repo` scope, which is as broad as the user's account; this is what the owner asked
  for (E-29). A workgroup's optional repository list stays the boundary inside the platform (ADR-054).
- Runner migration `0019_operation_access` (held reason and step); additive contract changes; an older web shows a
  `github_access` card as a neutral row and the operation ends after 15 minutes as before.

## Alternatives considered
- Only the GitHub App with an install link per owner: needs an organisation owner for every third-party organisation
  and does not reach a fork before the user installs the app on the own account - more steps for the same result.
- Only tokens: works everywhere but needs typing; kept as the way for agents (`--from-gh`) and as the fallback.

## Deviation from DESIGN.md
- None. (ADR-015's rejection of an OAuth App with the `repo` scope as "too broad" is reversed by rule E-29; see the
  addendum there.)
