# Runbook: runner installation and operation (LaunchDaemon, doctor)

Scope: installing `agent-runner` on a runner device (macOS arm64, Apple virtualization), running it as a system
LaunchDaemon, checking it with `doctor`, and the faults that need an operator
([05 §1](../spec/05-runner.md#1-host-daemon-agent-runner), [§2.3](../spec/05-runner.md#23-outbox-pull-websocket),
[00 §7](../spec/00-overview.md#7-coexistence-with-the-legacy-code), ADR-022, ADR-039). The legacy runner
(`swiss.dfx.agent`, `~/.local/share/agent`) is independent and stays untouched.

## 1. Prerequisites

- macOS on Apple silicon, Lima ≥ 2.3 with the `vz` VM type, PostgreSQL binaries (the runner manages its own loopback
  cluster under its home), Python 3.12, Node.js with `npm` (for Claude Code on the host, below).
- A dedicated unprivileged account (the **runner account**) that owns the Lima instances; the daemon never runs as
  root (`service install` refuses root as `UserName`). The runner account needs no `sudo`; the steps that need root
  run from an administrator account (§3).
- The agent repository checked out for that account, installed with `pip install -e ".[platform]"` in a virtual
  environment **that has pip** (`python3.12 -m venv <venv>`; `uv venv` only with `--seed`), owned by the runner
  account: the platform profile below installs the `login` extra with `<venv>/bin/python -m pip` as the runner
  account and stops with `login_extra_install_failed` when the environment has no pip. The golden build
  reads `images/`, `src/agent_guest/` and `contracts/` from this checkout; an install without a checkout (wheel)
  needs `AGENT_RUNNER_GOLDEN_TEMPLATE` pointing to a checkout's `images/golden.yaml`, else the build stops with
  `golden_source_missing` (KOR-03).
- **Claude Code on the host, at the version the golden pins.** "Connect Claude subscription"
  ([05 §11.2](../spec/05-runner.md#112-claudelogin--setup-token-relay-default)) runs `AGENT_RUNNER_LOGIN_ARGV`
  (default `claude setup-token`) **on the host**, not in a VM; the runner looks the program up in its own `PATH` (the
  `PATH` written into the plist at `service install`). Without it every subscription login ends with
  `login_cli_failed`, and a device without a platform OpenRouter key then has no model access at all. The pin is
  `claude_code` in `images/versions.json` of the release checkout, the same value `agent-runner image build` uses
  (`agent-runner image list` shows it per golden). Install it from an administrator account into a directory of the
  service `PATH` (Homebrew's Node: `/opt/homebrew/bin`), so the runner account can run but not change it. `node`
  must be in the same directory as the `claude` link: the login passes on only that directory and the system
  directories in `PATH`.

  ```sh
  V=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["claude_code"])' <checkout>/images/versions.json)
  npm install -g "@anthropic-ai/claude-code@$V"
  claude --version        # "<V> (Claude Code)"
  ```

  Check: `agent-runner doctor` prints `runner.doctor.check name=login_cli result=ok detail=version value=<V>` (§4). The runner starts this program only with an
  empty temporary home (no account profile on the host is read or written); never log in with it by hand. Update it
  only together with the golden: after a pin change passed the
  [Claude Code upgrade gate](claude-code-upgrade-gate.md), install the new version on the host when the new golden is
  released ([golden-image-release.md](golden-image-release.md)); doctor warns `version_mismatch` while they differ.

## 2. Configuration

All settings are environment variables `AGENT_RUNNER_*` ([05 §1.3](../spec/05-runner.md#13-configuration-agent_runner_-fail-closed));
an unknown name or an invalid value stops the process with `config_invalid <NAME>` (the value is never echoed).
Required for the daemon: `AGENT_RUNNER_HUB_URL` (`https://`), `AGENT_RUNNER_PLATFORM_ROOT_KEY`; GitHub features need
`AGENT_RUNNER_GITHUB_CLIENT_ID`. Platform devices may set `AGENT_RUNNER_OPENROUTER_KEY_FILE` (file 0600; refused on
org devices) and the operator's subscriptions, `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR` together with
`AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS` ([operator-subscriptions.md](operator-subscriptions.md); refused on org
devices). `AGENT_RUNNER_TEST_MODE` MUST be `0` in production. Home: `~/.local/share/agent-runner` (0700).

## 3. Install

1. As the runner account, export the configuration and enroll the device
   ([device-certification.md](device-certification.md)).
2. Build the golden image ([golden-image-release.md](golden-image-release.md)).
3. Install the service from an **administrator account** (the runner account has no `sudo`), with the runner's
   settings and nothing else of the administrator's environment, naming the runner account:

   ```sh
   sudo env -i PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
     AGENT_RUNNER_HUB_URL=… AGENT_RUNNER_PLATFORM_ROOT_KEY=… <further AGENT_RUNNER_* settings> \
     <venv>/bin/agent-runner service install --profile platform --user <runner account>
   ```

   (`--profile org` on an org device). The command writes `/Library/LaunchDaemons/swiss.dfx.agent-runner.plist` with
   `UserName=<runner account>` (from `--user`, else `SUDO_USER`), `RunAtLoad`, `KeepAlive`, `EnvironmentVariables`
   (`AGENT_RUNNER_HOME` – without the setting the default below the runner account's home; it must be the home used
   for `enroll`, else the daemon runs unenrolled –, `PATH` and every runner
   setting that differs from its default – launchd has no other configuration source; no secret is among them) and
   runs `launchctl bootout`/`bootstrap system`. The `PATH` given here is the daemon's `PATH`: it must contain Lima,
   the PostgreSQL binaries and Claude Code (§1). The platform profile also installs the `login` extra and Chromium for
   the runner account and sets `LOGIN_AUTOMATION=on`; the org profile sets it `off`. Logs:
   `$AGENT_RUNNER_HOME/log/runner.{out,err}.log`.

   The daemon **starts at once** (`RunAtLoad`), so install the service only after step 1: a daemon that is not
   enrolled keeps running with any formally valid root key, ends only its hub tasks (`not_enrolled`) and fills the
   VM pool. To prepare a device before its enrollment, stop and disable the daemon right after the install
   (`sudo launchctl bootout system/swiss.dfx.agent-runner`, `sudo launchctl disable system/swiss.dfx.agent-runner`;
   the disable survives a restart); after the enrollment `sudo launchctl enable system/swiss.dfx.agent-runner` and
   run the install command again.
4. Check as the runner account, with the runner's settings and the `PATH` of step 3 (Lima and PostgreSQL are
   looked up there; the `login_cli` check reads the `PATH` from the installed plist itself):
   `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin <venv>/bin/agent-runner status` (identity,
   hub connection, outbox backlog, VMs, sessions, pool, capacity) and the same with `doctor`.

Changing a setting means installing the service again with the new environment (the plist is rewritten).
`sudo <venv>/bin/agent-runner service uninstall` (administrator account) unloads and removes the daemon only.

## 4. `agent-runner doctor`

One line per check, in fixed order, result `ok`, `warn` or `fail` with a closed code and never a secret:

| Check | ok | Typical fail / warn and remedy |
|---|---|---|
| `os` | macOS | `unsupported` – other OS |
| `lima` | `limactl` ≥ 2.3.0 | `lima_missing` / `lima_version` – install or update Lima |
| `vz` | `vz` VM type available | `vz_unavailable` |
| `postgresql` | store answers | warn `store_missing` before the first `run`/`enroll`; `store_unavailable` – see the daemon log |
| `golden` | a `ready` golden | `no_golden` – build one |
| `landlock` | ABI ≥ 4 in the build record | warn `record_missing`; fail – rebuild the golden |
| `login_cli` | the login command (`LOGIN_ARGV`, default `claude setup-token`) is found in the daemon's `PATH` (from the installed plist; before the install the caller's) and runs; for the default command `claude --version` equals the golden's pin (value: the version) | `login_cli_missing` – not found, not executable or `--version` fails: install Claude Code (§1) or fix the service `PATH`; warn `version_mismatch` (value: the host's version) – install the pinned version; warn `version_unknown` – no answer within 10 s or no version in the output, check `claude --version` by hand; warn `pin_missing` – `images/versions.json` of the checkout is unreadable. A different `LOGIN_ARGV` (test double) is only checked for being executable: ok `executable` |
| `hub` | signed device state verifies with the pinned hub key | `hub_unreachable`, `response_unsigned`, `key_revoked`, `platform_root_mismatch`; warn `not_enrolled` |
| `clock` | skew to the hub < 5 s | `skew_seconds` – fix NTP |
| `textfile` | textfile directory writable or sink off | `textfile_unwritable` |
| `operator_subscriptions` | off, or every token file of the operator's subscriptions usable (value: the count) | warn `files_ignored`, `none_usable`, `dir_unusable` – [operator-subscriptions.md](operator-subscriptions.md) §3 |
| `test_mode` | `TEST_MODE=0` | warn `test_mode_on` – never in production |
| `operator_github` | ok `off` (not set) or `usable`: the operator's GitHub token file passes the checks of every connect ([operator-github.md](operator-github.md)) | `missing`, `not_regular`, `owner`, `mode`, `unreadable`, `format` – fix the file as in the runbook |
| `sync` | no operator marker | `device_revoked`, `sync_fatal`, `platform_stopped`, `org_stopped` (below) |

## 5. Faults that need an operator

| Marker / symptom | Meaning | Action |
|---|---|---|
| `sync_fatal` (e.g. `origin_seq_conflict`) | the hub refused the outbox in a way the runner cannot repair | find the cause (store restored from an old backup? two devices with one identity?); then `agent-runner doctor --clear sync_fatal` and restart the daemon |
| `org_stopped` | the membership log of one org failed verification (`log_rollback`/`log_fork`); the runner stopped acting for that org, others continue | treat as hub incident (compare log heads with a browser of that org); `doctor --clear org_stopped_<org id as 32 hex digits without dashes>` after resolution |
| `platform_stopped` | the platform log failed verification | as above, `doctor --clear platform_stopped` |
| `device_revoked` | decertified or key revoked; sessions, VMs and keys are gone | not clearable; `agent-runner uninstall` |
| log line `runner.inbox consumer_failed scope=<id> seq=<n> error=<code>`, repeated | **a permanently failing inbox consumer blocks its scope** (ADR-039): the event is retried at every pull, nothing later in that scope (prompts, settings, profiles of that workgroup or org) is applied; other scopes continue | 1. note scope, seq and error from the daemon log; 2. check whether the error is local (store full, disk, PostgreSQL) and fix it – the scope continues by itself; 3. if the event itself makes the consumer fail, it is a product defect: report it with the log lines (never the event content), keep the daemon running (the scope stays held, nothing is lost); a fix of the consumer releases the scope at the next pull. Do not delete cursor rows or events by hand: the order per scope and the at-least-once rule depend on them. |
| `agent-runner status` shows a growing outbox backlog | hub unreachable or refusing | check `doctor hub`; the outbox resumes without loss when the hub is back |

The `--clear` of a marker takes effect at the next daemon start (`sudo launchctl kickstart -k system/swiss.dfx.agent-runner`).

## 6. Uninstall

`agent-runner uninstall [--keep-identity]` stops the service, deletes every VM the runner created (prefix
`AGENT_RUNNER_VM_PREFIX`, pool `arp-…`, goldens `golden-…`), the store and – without the flag – the identity
(`device.json`, `keys/device.key`). With `--keep-identity` a later start continues under the same device ID
(store wipe: the outbox continues at `last_origin_seq + 1`, delivered prompts are not delivered again).
