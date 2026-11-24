# ADR-052: Operator subscriptions for approved organizations (runner only)

Status: Accepted (2026-11-23); amended by ADR-078 (2026-11-24): the organizations come from the root-signed
`org_operator_subscriptions` of the verified platform log (changed by the operator CLI, effective at the runner's next
platform-log refresh without a restart; an entry of the log wins over the environment); `OPERATOR_SUBSCRIPTION_ORGS`
remains for the move only, and `OPERATOR_SUBSCRIPTION_DIR` alone is a valid configuration. Points 2 and 7 below read
accordingly.

## Context
- A session needs model access (05-runner.md §10.1). Without an own `claude_subscription` or `anthropic_api_key` of
  the owner and without the platform key (`OPENROUTER_KEY_FILE`) it fails with `model_access_unavailable`.
- The operator of a platform device holds Claude subscriptions and wants the sessions of selected organizations to use
  them without any user connecting an account. The tokens are short-lived OAuth access tokens; a process outside the
  runner renews them and replaces a file every few minutes.
- The change must not need a new hub release, a contract change or a new golden image: the hub, the web client and the
  guest stay as they are.

## Decision
1. **Runner only, reported as `subscription`.** The pool is a runner feature. Towards the hub (`session_transition`)
   and the web client the access kind is `subscription` (`claude_subscription`) like the owner's own subscription; the
   gateway in the guest gets the same provisioning body (`Authorization: Bearer <token>`, `oauth-2025-04-20`). That the
   operator's pool was used is visible only in the runner log (`runner.model operator_subscription session_id=…
   file=…`) and in the runner's textfile metrics.
2. **Configuration** (05 §1.3, fail-closed): `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR` (absolute path of a directory,
   not a symbolic link, mode 0700) and `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS` (comma-separated organization IDs).
   Both or neither: one without the other is `config_invalid` (an empty organization list counts as not set). Platform
   devices only: an org device refuses the directory (`config_invalid OPERATOR_SUBSCRIPTION_DIR`), like
   `OPENROUTER_KEY_FILE`. Both are carried into the LaunchDaemon plist by `service install`.
3. **Files.** `<name>.token` (name `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`), one line, one access token of the format the
   subscription login accepts (`sk-ant-oatNN-…`; a refresh token or anything else is ignored). The directory must be
   owned by the runner account with mode 0700, each file must be a regular file of the runner account with mode 0600
   (no rights for group and others). Every file is opened with `O_NOFOLLOW` relative to the opened directory and
   checked with `fstat` before a byte is read: a symbolic link, another owner, wider rights or anything but a regular
   file is ignored, logged once per change (`operator_file_ignored file=<name> reason=<reason>`, never the content)
   and never read. A file is read again only when its identity (device, inode, modification time, size) changed; the
   atomic replacement by `rename` always gives a new inode.
4. **Selection** (05 §10.1). With `auto`: own subscription → own API key → **operator pool** → `platform`. With the
   explicit `subscription`: own subscription → **operator pool**. The pool applies only when the session's organization
   is listed and a file is usable; otherwise the next step follows as before, and an organization that is not listed
   sees no difference at all. No usable file: the start ends with `model_access_unavailable` as before (or falls
   through to `platform` with `auto`).
5. **Bound to the file, not to the value.** A session is bound to one file (table `runner.operator_binding`, file name
   only, kept across daemon restarts because the guest and its gateway keep running). The gateway lives in the guest
   VM and cannot read a host file, so "the gateway reads the current content for every upstream request" is realized
   by the runner: the stamp of the provisioning carries the file's identity, and the daemon task
   `operator_subscriptions` renews every bound session every 2 s (`model.gateways.refresh`; an unchanged file does
   nothing and is not read). A token replaced in the file therefore reaches the gateway within about 2 s, long before
   the old access token expires; a session never ends because its token was renewed. The 30 s reconcile step `model`
   renews them as well.
6. **Choice and refusals.** A new binding takes the usable file with the fewest bound sessions, then the one unused the
   longest (last binding or last answered request), then by name. The runner learns the upstream status of each request
   from the gateway's `usage` journal record:
   - `401`/`403` for the content the session was provisioned with blocks the file until it changes (a refusal of a
     token that was already replaced in the file blocks nothing). The next renewal (≤ 2 s) moves the session to another
     usable file. When there is none, the gateway keeps what it holds (logged once) and the session follows as soon as
     the file is renewed.
   - `429` defers the file for 300 s for new bindings only. The runner does not see the `retry-after` header (the
     journal record carries the status only, and the guest must not change), so the fixed 300 s of the work package
     applies. A running session stays on its file: Claude Code retries a `429` itself, the prompt cache of a
     conversation belongs to one account and is lost by a move, and moving rate-limited sessions would drain every
     subscription's window in turn instead of one. Only a refusal of the credential itself (`401`/`403`) moves a
     session.
   - A refusal within 60 s after the session's gateway got a token (start, renewal, move) is not held against the
     file: the record may belong to a request sent before with a token that has since been replaced (records reach
     the runner after the renewal, also as a backlog after a daemon restart). A truly dead token is blocked by the
     first refusal after these 60 s.
   - The guest is untrusted and the status comes from its journal. A session can report only for the file it is bound
     to, and one session blocks or defers at most one file per 10 min (further refusals are logged once,
     `operator_refusal_ignored`, and ignored); every block names the session. A block ends with the next replacement
     of the file (minutes). A compromised guest cannot read another file's token.
   - Blocks and deferrals are kept in memory; after a daemon restart a dead file is found again by the first refusal.
   - A `403` blocks like a `401`: the record carries the status, not the error type.
7. **Withdrawal.** When the organization is removed from `OPERATOR_SUBSCRIPTION_ORGS` (daemon restart), the next
   renewal withdraws the access of its bound sessions (`model_access_unavailable`, like a deleted credential). A purge
   of the owner's own `claude_subscription` record does not affect a session bound to the pool.
8. **Secrets.** Tokens live only in the runner's memory (the cache of each file's current content) and in the
   provisioning body sent to the guest. They never appear in a log line, a metric, an error, the store or the hub.
9. **Observability.** Textfile metrics (08 §3): `agent_runner_operator_subscription_files{state}` (`usable`,
   `blocked`, `deferred`, `ignored`), `agent_runner_operator_subscription_sessions{file}` (label = file name without
   `.token`, chosen by the operator; no token, no organization) and
   `agent_runner_operator_subscription_newest_change_age_seconds`. They are not part of the outbox `metrics` item (no
   contract change). `agent-runner doctor` reports `operator_subscriptions`: ok `off`, ok `usable` (count), warn
   `files_ignored` (count of usable files), warn `none_usable` (count of ignored files), warn `dir_unusable` (reason);
   never a file name or an organization.

## Scope and responsibility
Claude subscriptions are personal. The pool is meant for organizations of the operator itself (its own staff and
projects on the operator's platform device). Which organizations are listed, and whether the use of a subscription for
them is covered by the subscription's terms of use, is the operator's decision and responsibility; the platform only
enforces that nobody else can use the pool and that the tokens never leave the runner and the gateway. The general way
for users to connect their own account is unchanged (05 §11, ADR-011).

## Consequences
- No hub, contract, web or guest change; a runner update and a daemon restart deliver it.
- The session's usage is priced like a subscription (`subscription_equivalent`) and attributed to the session's owner
  in the hub as before; per-file attribution exists only in the runner metrics and log.
- Quota polling (05 §10.6) stays per owner credential; the operator's subscriptions are not polled (their windows are
  the operator's concern).
