# The operator's GitHub account on a platform device

Approved users connect GitHub at once with a token of the operator's GitHub account: "Connect GitHub" in the
settings answers "Connected as …" without a device code (ADR-053, 05-runner.md §11.4). Every other user keeps the
device flow of the GitHub App (ADR-015). Only platform devices accept the settings; an org device refuses to start
with `config_invalid OPERATOR_GITHUB_TOKEN_FILE`.

**Approve only people you would give the token to.** The connection is stored like every GitHub connection, sealed
for the user: the user's browser and the devices of the user's organizations can open it, so an approved user can
read the token (ADR-053). Revoking the token at GitHub is the only way to take it back.

The token reaches as far as the account. Use a dedicated account for AI work with access to the needed repositories
only, and a token with the rights the operations need (classic token: scope `repo`; fine-grained token: contents,
pull requests and issues read/write, metadata read on the selected repositories). A commit that changes
`.github/workflows/` can only be pushed with the scope `workflow` (fine-grained: "Workflows" read/write); without it
such a push fails at GitHub. Granting it lets agents change CI: decide per account. A fine-grained token reaches only
repositories of the account's owner or its organizations, not repositories where the account is only a collaborator
(use a classic token for those). Without a list in the workgroup settings every repository the account reaches
is allowed (ADR-054); a list set by the workgroup's admins limits every operation to exactly its repositories.

## 1. Set up

As the runner account (the account the LaunchDaemon runs as):

```bash
umask 077
install -d -m 0700 "$HOME/operator-github"
read -rs TOKEN                           # paste the token; it stays out of the shell history
tmp=$(mktemp "$HOME/operator-github/.token.XXXXXX")
printf '%s\n' "$TOKEN" > "$tmp"           # one line: the token; nothing else in the file
unset TOKEN
mv -f "$tmp" "$HOME/operator-github/github.token"
```

The file must be a regular file (no symlink), owned by the runner account, mode 0600 or stricter, with exactly one
line, in a directory owned by the runner account that nobody else may write (0700 as above). As the administrator, install the service again with all previous `AGENT_RUNNER_*` settings plus:

```bash
AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE=<home of the runner account>/operator-github/github.token
AGENT_RUNNER_OPERATOR_GITHUB_USERS=<user id>[,<user id>...]
```

(see [runner-installation.md](runner-installation.md): changing a setting means installing the service again). Both
settings are needed together. A user ID is the UUID of the user's platform account.

Check as the runner account: `agent-runner doctor` shows `operator_github ok usable`. After a connect the daemon log
shows `github_connect done request_id=… source=operator`; the token, the account and the users are never logged.

## 2. Replace the token

Write the new token to a temporary file in the same directory and rename it over the old one (step 1, `mktemp` and
`mv -f`); never edit the file in place. The runner reads the file at every connect, so no restart is needed.
Connections made before keep the token they stored: affected users disconnect ("Disconnect" in the settings) and
press "Connect GitHub" to take the new token. Revoke the old token at GitHub afterwards; connections still holding it then end
operations with `github_not_connected` until the user connects again.

## 3. Approve and withdraw users

Change `AGENT_RUNNER_OPERATOR_GITHUB_USERS` and install the service again. Withdrawing a user stops new connects
through the operator's token; that user's next connect uses the device flow (which needs a GitHub App). An existing
connection of the withdrawn user stays until the user disconnects ("Disconnect" in the settings) or the operator
revokes the token at GitHub; a withdrawn user may also have read the token (see above). To end every connection at
once, revoke the token at GitHub, place a new one and let the approved users connect again. The hint after
"Disconnect" in the settings points to the GitHub App authorization; for the operator's token it does not apply -
only the operator revokes it.

## 4. Faults

| Sign | Cause and fix |
|---|---|
| "Connect GitHub" fails with "A credential the device needs is missing."; doctor `operator_github warn <reason>`; log `operator_github file_unusable reason=<reason>` | `missing`: no file (or no directory) at the path; `directory`: the directory is not the runner account's or others may write it (`chmod 700`); `not_regular`: a directory or symlink; `owner`: not owned by the runner account; `mode`: rights for group or others (`chmod 600`); `unreadable`: cannot be opened; `format`: not exactly one line with the token. Fix the file as in step 1; the reason is logged once until it changes |
| "Connect GitHub" fails with "The service refused the request"; log `github_connect failed … source=operator step=user kind=unauthorized status=401` | GitHub refuses the token (revoked, expired or wrong): place a valid token (step 2) |
| The repository list is empty or misses a repository | the account has no access to it at GitHub; give the account access, then list again |
| A push of a commit that changes `.github/workflows/` fails | the token lacks the scope `workflow` (see above) |
| An operation ends `repo_not_allowed` | the workgroup limits its repositories and this one is not on the list: add it, or clear the list to allow every repository of the connection |

Counters (08-monitoring.md §3): `agent_runner_operator_github_connects_total` and
`agent_runner_operator_github_connect_failures_total`.
