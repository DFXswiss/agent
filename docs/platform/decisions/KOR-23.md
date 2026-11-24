# KOR-23 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-23 – Operator subscriptions for approved organizations (runner only)

Decisions taken while implementing ADR-052. The ADR holds the design; this file names the points that were open.

1. **The gateway does not read the file; the runner provisions it again.** The work package asks the gateway to read
   the current content of the bound file for each upstream request. The gateway is part of `agent-guest-api` inside
   the VM; it cannot read a file of the host, and a guest change needs a new golden image, which the package rules out
   ("only the runner changes"). The runner therefore keeps the binding to the file and provisions the current content
   whenever the file's identity changes: the daemon task `operator_subscriptions` renews bound sessions every 2 s, the
   reconcile step `model` every 30 s. A short-lived token is replaced in the file minutes before it expires, so the
   gateway always holds a valid one; the check costs one `open`/`fstat` per file and tick.
2. **File identity** = device, inode, modification time (ns) and size. The atomic replacement (`rename`) always gives a
   new inode. A file is read only when it is a regular file of the runner account with mode 0600 or stricter, checked
   with `fstat` on the descriptor opened with `O_NOFOLLOW` relative to the opened directory (no race between check and
   read). A rejected file is checked again at every scan (a fixed mode is picked up without a change of the content).
3. **Binding in the store.** `runner.operator_binding` (session ID → file name) survives a daemon restart: the guest
   keeps running with its gateway, and without the binding the renewal would look for the owner's own record, find
   none and withdraw the session. Bindings of sessions that are no longer live are dropped by the renewal.
4. **Order of the selection.** `auto`: own subscription → own API key → pool → platform (as the package says). The
   explicit `claude_subscription` takes the pool after the own subscription. `api_key` and `platform` never use it.
   A resume that finds the owner's own credential ends the binding.
5. **Refusals.** `401`/`403` block the file only when the session's gateway holds the file's current content (the
   stamp of the provisioning carries the identity); a refusal of a token that was already replaced blocks nothing and
   the renewal provisions the new one. `429`: fixed 300 s for new bindings only; a running session stays on its file
   (prompt cache per account, Claude Code retries `429`, quota windows per account – ADR-052 §6). The `retry-after`
   header is not available to the runner (the guest's `usage` record has the status only).
6. **Metrics in the textfile only.** The outbox `metrics` item is a contract; extending it would need a hub release.
   The three gauges go to the node_exporter textfile; the `file` label is the operator's file name (no token, no
   organization). `doctor` shows counts only.
7. **Configuration.** Both settings or neither (`config_invalid` names the missing one); an empty organization list
   counts as not set. Organization IDs are parsed as UUIDs and normalized to lower case. Platform devices only, like
   `OPENROUTER_KEY_FILE`. The configuration checks the directory's type and mode; its owner is checked at every scan
   (the service is installed from an administrator account).
8. **No owner record needed.** A session on the pool is not withdrawn when the owner's own `claude_subscription`
   record is purged; it is withdrawn when its organization is no longer approved (after a restart with a changed
   list), with the session reason `model_access_unavailable`.
9. **Guest is untrusted; late records** (review findings 1 and 2). Refusals within `GRACE_S` (60 s) after the
   session's gateway got a token are ignored (a late or replayed record of an already replaced token must not block
   the renewed file); one session blocks or defers at most one file per `LIMIT_S` (600 s). Accepted residual: a
   compromised guest of an approved organization can block one file per 10 min until that file is renewed; a dead
   token is blocked at most about 60 s later than without the grace time.
10. **Quota polling** (05 §10.6) stays per owner credential; the operator's subscriptions are not polled.
11. **Further review points.** Blocks and deferrals are not persisted (a restart forgets them; the next refusal finds
    a dead file again). A `403` blocks like a `401` (no error type in the record). An explicit `claude_subscription`
    whose own record is present but cannot be used now (fetch failure, key epoch not yet here) takes the pool:
    availability wins, and a resume returns to the own subscription. The binding is put into memory before the store's
    round trip, so concurrent starts spread. A failing renewal of one session no longer stops the task's round for
    the others. Bindings stay in the store while the pool is switched off; they are dropped at the first renewal after
    it is switched on again (sessions that ended meanwhile), and `doctor` must run as the runner account (owner check).

## KOR-23 – Operator subscriptions (shared documentation)

The change lives in the agent repository (runner only: selection, token-file pool, binding, renewal, doctor check
`operator_subscriptions`, textfile metrics; see its `decisions/KOR-23.md`). No hub, contract or web change. This
repository takes the shared documents unchanged: ADR-052 (and the ADR index), `spec/05-runner.md` (§1.2, §1.3, §10.1,
§10.2), `spec/08-monitoring.md` §3, `spec/02-data-model.md` §4, `spec-deviations.md`,
`runbooks/operator-subscriptions.md` (new), `runbooks/runner-installation.md` and the runbook index.
