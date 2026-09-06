# GitHub execution accounts

Static scripts select GitHub accounts from `$AGENT_HOME/github-accounts.json`.
Installation creates no accounts or bindings. A missing file, `{}`, or null/empty
`accounts` and `sessions` leaves GitHub execution unconfigured. There is no
implicit account from hub pairing, the host login, or environment tokens.

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
signing configuration. Use HTTPS GitHub remotes; SSH transport and interactive
credential prompts are disabled for this account runner.

`agent github pending` binds each GitHub activity to its session's configured
account. `execution_account` records the account name and expected login. A
retry with a different binding is refused. Reusing an existing PR also requires
its author to match; a PR authored by another account is not silently adopted.

The same session selection applies to assignment scans, PR-merge scans,
supervised issue reads, and the `pushed`/`mergeable` steps of `agent run`.
Account selection is script configuration, never an instruction taken from an
issue body or a model's activity payload. Configure existing sessions explicitly
before enabling these operations after upgrading.

Hub pairing and event ownership are separate: using a second GitHub execution
account does not re-pair the device or change ownership of its rows.

This document covers GitHub execution. Configurable AI accounts and roles are
part of the [empty-default requirement](../DESIGN.md#198-configuration-starts-empty);
they are not implemented by this manifest.
