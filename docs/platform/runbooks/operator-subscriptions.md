# Operator subscriptions: setting them up

Sessions of approved organizations use the operator's Claude subscriptions on a **platform device** without any user
connecting an account ([ADR-052](../adr/052-operator-subscriptions.md), [05 §10.1](../spec/05-runner.md#101-selection)).
Only the runner changes; the hub and the web client show the access as `claude_subscription`.

Responsibility: Claude subscriptions are personal. Approve only organizations of the operator itself; whether a
subscription may be used for them under its terms of use is the operator's decision.

## 1. Directory and token files (runner account)

Run as the **runner account** (the account in `UserName` of the LaunchDaemon plist):

```sh
umask 077
install -d -m 0700 "$HOME/operator-subscriptions"
```

Each subscription is one file `<name>.token` (name: letters, digits, `.`, `_`, `-`, at most 64 characters, starting
with a letter or digit) holding **one line**: the current OAuth access token of the subscription (`sk-ant-oat…`).
Refresh tokens are refused. The file must belong to the runner account and have mode `0600`; the directory `0700`.
Anything else – a symbolic link, another owner, wider rights, a FIFO – is ignored and reported in the runner log
(`runner.model operator_file_ignored file=<name> reason=<reason>`), never read.

The tool that renews the access tokens writes each new token **atomically** next to the file and renames it over the
old one (`rename` in the same directory):

```sh
# as the runner account; TOKEN comes from the renewal tool, never from a command line others can see
tmp=$(mktemp "$HOME/operator-subscriptions/.team-a.XXXXXX")   # mktemp creates 0600
printf '%s\n' "$TOKEN" > "$tmp"
mv -f "$tmp" "$HOME/operator-subscriptions/team-a.token"
```

Files with other names (`.team-a.XXXXXX`, `README`) are not part of the pool. Running sessions follow a replaced file
within about 2 s; they never end because the token was renewed. Never copy refresh tokens to the device; only the
current access token belongs in the file.

## 2. Approve organizations and restart

Two settings, both required together ([05 §1.3](../spec/05-runner.md#13-configuration-agent_runner_-fail-closed); one
without the other stops the daemon with `config_invalid`; org devices refuse them):

| Setting | Value |
|---|---|
| `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR` | absolute path of the directory of step 1 |
| `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS` | comma-separated organization IDs (UUIDs) whose sessions may use the pool |

The organization ID is the part after `/o/` in the address of the organization in the web client (`/o/<org id>/…`).
Install the service again with the two settings added to the existing ones (administrator account,
[runner-installation.md](runner-installation.md) §3 step 3); the command rewrites the plist and restarts the daemon.
Check before that no subscription login runs (a restart ends it with `device_shutdown`).

```sh
sudo env -i PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  AGENT_RUNNER_HUB_URL=… AGENT_RUNNER_PLATFORM_ROOT_KEY=… <the other AGENT_RUNNER_* settings of the device> \
  AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR=<runner home>/operator-subscriptions \
  AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS=<org id>[,<org id>…] \
  <venv>/bin/agent-runner service install --profile platform --user <runner account>
```

Removing an organization from the list (install again) withdraws the access of its running sessions on the pool at
the next renewal (`model_access_unavailable`); new sessions of it fall back to the order of 05 §10.1.

## 3. Check

As the runner account with the runner's settings (as another account every file shows `owner`): `<venv>/bin/agent-runner doctor`. The line
`operator_subscriptions` shows:

| Result | Meaning | Remedy |
|---|---|---|
| ok `off` | not configured | – |
| ok `usable` *n* | *n* token files usable, none ignored | – |
| warn `files_ignored` *n* | *n* usable, at least one file ignored | see the runner log (`operator_file_ignored … reason=`): `mode` → `chmod 600`, `owner` → write it as the runner account, `symlink`/`not_regular` → replace by a regular file, `format` → one access token per file, `unreadable` → rights of the file |
| warn `none_usable` *n* | no usable file (*n* ignored) | as above; sessions of approved organizations fall back as without the pool |
| warn `dir_unusable` *reason* | `missing`, `mode` (not 0700), `owner`, `symlink`, `not_directory`, `unreadable` | fix the directory of step 1 |

The doctor never shows a token, a file name or an organization.

Afterwards a session of an approved organization (without own subscription or API key of the owner) starts with
access `claude_subscription`; the runner log shows `runner.model operator_subscription session_id=… file=<name>`.

## 4. Operation

- **Log** (`$AGENT_RUNNER_HOME/log/runner.err.log`, never a token): `operator_subscription` (binding at the start),
  `operator_file_blocked file=… status=401|403 session_id=…` (the upstream refused the token the session holds; the file is blocked
  until it is replaced), `operator_file_switched session_id=… from=… to=…`, `operator_file_deferred file=…
  seconds=300 session_id=…` (`429`; new sessions avoid the file for 5 min, running sessions stay),
  `operator_refusal_ignored` (a session reported a further refusal within 10 min of its last block), `operator_pool_exhausted`
  (a session's file is blocked and no other is usable: it keeps its token until the file is renewed),
  `operator_pool_empty` (a start found no usable file), `operator_dir_unusable`, `operator_access_revoked`.
- **Metrics** (textfile, 08 §3): `agent_runner_operator_subscription_files{state}`,
  `agent_runner_operator_subscription_sessions{file}`, `agent_runner_operator_subscription_newest_change_age_seconds`.
  A growing age means the renewal tool stopped: tokens will expire and sessions get `401`.
- **Turning it off**: install the service again without the two settings; then the directory can be deleted.
