# GitHub execution accounts

Static scripts that load this manifest select GitHub accounts from
`$AGENT_HOME/github-accounts.json`. Installation creates no accounts or
bindings. A missing file, `{}`, or null/empty `accounts` and `sessions` leaves
those covered GitHub executors unconfigured. There is no implicit account from
hub pairing, the host login, or environment tokens. CLI paths that never load
this file are outside this enforcement; see the remaining gaps below.

The following is an operator-supplied example, not an installed default:

```json
{
  "accounts": {
    "worker-a": {
      "login": "example-worker-a",
      "gh_config_dir": "/absolute/path/to/worker-a/gh",
      "git": {
        "name": "Example Worker A",
        "email": "worker-a@example.com",
        "signing_format": "ssh",
        "signing_key": "/absolute/path/to/worker-a/signing-key"
      }
    },
    "worker-b": {
      "login": "example-worker-b",
      "gh_config_dir": "/absolute/path/to/worker-b/gh"
    }
  },
  "sessions": {
    "implementation-session": "worker-a",
    "review-session": "worker-b"
  }
}
```

Add as many named accounts and session bindings as needed; no fixed account list
or count is built in. Each account references its own GitHub CLI configuration
directory. Tokens belong there, not in this manifest, activity payloads, or git.
The optional `git` object supplies the identity and signing configuration for
scripted Git operations; it is required when that account performs a Git push.
Signing formats are Git's `ssh`, `openpgp`, or `x509` values. GitHub operations
without Git writes need only `login` and `gh_config_dir`.

For an account whose credentials and signing key live in a container, an optional
`command_prefix` supplies the static executor argv, for example
`["docker", "exec", "-i", "worker-container"]`. An optional `worktree_paths`
object maps absolute host worktree roots to absolute paths in that executor,
for example `{"/srv/worker/data": "/data"}`. The longest matching root is used
for Git's `-C` argument. Account configuration and signing-key paths refer to
the executor's filesystem. These are trusted operator settings, not commands
from a model or an issue. No prefix or path mapping is installed by default.

The executor runs `gh api user` with that configuration before execution and
requires the returned login to match (case-insensitive). It clears inherited
GitHub token variables for the child process, does not change the process-wide
environment, and does not switch the active account in another configuration
directory. Authentication failure or a mismatch blocks the action; there is no
fallback. Git operations use the selected account's credential helper and
signing configuration. Before supported `fetch` / `push` / `pull` / `clone`
forms, the runner asks Git for the effective remote URL via
`git remote get-url [--push] --all` (which already applies a distinct `pushurl`
and `insteadOf` / `pushInsteadOf` rewrite effects) and rejects
credential-bearing, non-HTTPS, or non-`github.com` network remotes. Explicit
URL arguments are resolved the same way through a temporary command-scoped
remote. Only transfer forms with one explicit repository argument are accepted
(for example `git fetch -- origin`, `git push -- origin HEAD:refs/heads/feature`,
and `git push --set-upstream origin feature`); implicit default-remote forms,
`fetch --all` / `--multiple`, and `--repo` combined with a different positional
repository are refused. Rejection messages never echo URL values. Local Git
metadata commands are unchanged and are not treated as network transfers. Use
HTTPS GitHub remotes; SSH transport and interactive credential prompts are
disabled for this account runner.

`agent github pending` binds each GitHub activity to its session's configured
account. `execution_account` records the account name and expected login. A
retry with a different binding is refused. Reusing an existing PR also requires
its author to match; a PR authored by another account is not silently adopted.

The same session selection applies to assignment scans, PR-merge scans,
supervised issue reads, and the `pushed`/`mergeable` steps of `agent run`.
The `mergeable` step passes an explicit `--repo` together with a PR number or
branch selector: when the task already records the pull-request target
(`repo` + numeric `ref`), that pair is used (fork targets may differ from
origin) and Git signing identity is not required; otherwise the branch comes
from mapped `git -C` and the repo from the validated origin remote. Either path
keeps container-backed accounts independent of the executor's default working
directory. Account selection is script configuration, never an instruction
taken from an issue body or a model's activity payload. Configure existing
sessions explicitly before enabling these operations after upgrading.

Hub pairing and event ownership are separate: using a second GitHub execution
account does not re-pair the device or change ownership of its rows.

**Remaining gaps.** This manifest does not cover legacy ambient `gh` paths that
never load it. One reachable example is `agent a38` visibility lookup
(`gh repo view` when `--private` is omitted), which still uses the host `gh`
login. Configurable AI accounts and roles are part of the
[empty-default requirement](../DESIGN.md#198-configuration-starts-empty) and are
not implemented by this manifest.

Transfer options are deliberately limited to the explicit allowlists in
`github_accounts.py`. Unknown options (including custom receive/upload programs),
implicit or multiple repositories, and per-command global configuration/context
overrides are rejected rather than guessed. A transfer may use one mapped `-C`
working directory; validation and execution use that same directory. Automatic
submodule transfers are disabled so a validated parent remote does not authorize
another remote. Other Git commands are not a sandboxed command interface; only
trusted static scripts may supply executor argv.

Every effective URL returned for a named remote must identify the same GitHub
owner/repository, case-insensitively, including every additional push URL.
Fetch and push URL lists must also agree before a transfer is allowed.
