# KOR-40 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-40 – decisions (guest, runner, golden, attack suite)

Package: hardening from the concept review AUD-03 (gaps L11, L12, L17). Architecture: ADR-064;
the audit side is ADR-063 (second part of this note). Concept references: [§3](../KONZEPT.md#k-3),
[§6 point 2](../KONZEPT.md#k-6-b2), [§10 point 5](../KONZEPT.md#k-10-b5), [E-08](../KONZEPT.md#e-08),
[E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19).

### Decisions

1. **The turn gate refuses, it does not end the session** (review finding H2): an absorbed interrupt or a background
   request of Claude Code after `Stop` must not kill a healthy session. Without tools the model cannot act, so a
   refused agent-loop request is enough to keep an unconfirmed prompt from acting; the refusal is visible
   (`guest.error turn_unconfirmed` → `session.error`).
2. **Only decision hooks end a session** (review finding M2): `UserPromptSubmit`, `PreToolUse`, `Stop`,
   `StopFailure`. A failure of `PostToolUse`, `Notification` … journals `hook_handler_failed` only.
3. **A hook answer later than the hook timeout counts as failed** (review finding M1): Claude Code went on without
   it, so for `PreToolUse` one tool ran with layer 2 only; the session ends.
4. **No "hook silent" rule in the delivery** (review finding H2): the pane heuristic is not proof of a hook outage,
   and the gate already keeps an unconfirmed prompt from acting. A hung API process means no gateway either; prompts
   are refused visibly (`delivery_failed`).
5. **`count_tokens` and requests without tools pass** (review finding H2/L1); "can act" = non-empty `tools`, a
   `tool_choice` or non-empty `mcp_servers`.
6. **Queued input during a confirmed turn** (review finding H1): the only writer to the pane is the platform's
   delivery, which types only into an `idle` session (ADR-016); typing into a running turn needs root in the VM.
   Recorded as residual risk in ADR-064, no content binding of prompts in the gateway (Claude Code wraps prompts,
   attachments and compaction summaries in ways a byte comparison would misjudge).
7. **One gated fault switch** (review finding M3): `fault.fault_mode()` is the only reader of
   `AGENT_GUEST_FAULT_HOOKS` and returns nothing outside a TEST_MODE golden; hook handler, gateway and control ops use
   it. New value `open` for the attack suite's mode `fault` (layer 2 alone); `500` keeps the production reaction.
8. **ATK-38 run `outage`** instead of a third suite mode: a third mode would need the OS probe, the detector control
   and a report column for every attack; the outage run is ATK-38 alone after the control in the VM of mode `fault`
   (the switch `500` would end the other attacks' sessions). Verdict mode `outage`, criteria `ended`, `no_turn`,
   `no_execution`, `no_foreign_write`, `no_foreign_read`, `no_egress`, `specific`.
9. **Sockets** (review finding H3): loopback TCP/UDP between sessions was already closed by nftables (session UIDs
   reach only `127.0.0.1:7700`) and Landlock (no TCP bind). Abstract unix sockets: `bind` and `listen` refused by the
   unit, autobind closed by a seccomp filter on `setsockopt(SOL_SOCKET, SO_PASSCRED|SO_PASSPIDFD)` in the launcher,
   io_uring refused (newer kernels bind and listen through it). `EPERM`, not a kill. The self-test's `tcp_bind` now
   expects `EPERM` (the filter answers before Landlock). ATK-29/ATK-30 list the new self-test checks. A per-session
   network namespace was rejected again (ADR-045).
10. **`session.error` only for sessions on this VM and codes of the closed list**; VM-level records stay in the log.
11. **Subagents stay one-shot** (L17, checked) – superseded by ADR-088 (KOR-45, interpretation A10): intended (ADR-017, ADR-038; idea 26 "berichtet an seinen
    übergeordneten Agenten"); no step limit inside the turn, the parent starts another child or resumes.
12. **"GitHub trennen" revokes only what the platform minted** (L17): GitHub's unauthenticated
    `POST /credentials/revoke` with the token of a purged `github` record, only for a GitHub App user token (`ghu_`)
    of the device flow, and only by the device that minted it (`runner.github_minted` holds its sha256) while that
    device is still an eligible reader of the owner's records (code review, findings H1/H2: a device that lost its
    membership also sees the record vanish, but the user did not disconnect). Never the operator's token (ADR-053;
    no `ghu_` token, and on a platform device a token equal to the operator's file is skipped too) and never a
    transferred token (it would sign the user out of their CLI).
    The cache got a before-purge hook (`subscribe_before_purge`), registered by the reconcile step `github_revoke`
    (order 6). The GitHub double answers the route.
13. **Subscription quotas with a long-lived setup token** (L11): not readable is shown honestly (hub marker of an
    `unavailable` report, see the second part of this note); no live probe with a real setup token (the subscriptions are
    read-only for this work and none has a setup token).

### Golden

The guest changed (`hooks.py`, `gateway.py`, `fault.py`, `control_routes/turns.py`, `sessions.py`,
`sandbox/unit.py`, `sandbox/landlock.py`, `sandbox/probe.py`, `config.py`): a new golden is needed. Nothing in
`images/` changed.

## KOR-40 – decisions (hub, contracts, web)

Package: hardening from the concept review AUD-03 (gaps L5, L11, L12, L17). Architecture: ADR-063 (audit), ADR-064
(hooks, sockets, guest errors; first part of this note). Concept references:
[§6 point 4](../KONZEPT.md#k-6-b4), [§8 point 6](../KONZEPT.md#k-8-b6), [§10 point 8](../KONZEPT.md#k-10-b8),
[E-19](../KONZEPT.md#e-19).

### Decisions

1. **Completeness lives in the request pipeline, not in every route** (ADR-063 §1). A per-route declaration would
   have edited every `routes.py` of every area (and every parallel package); the pipeline sees every write anyway.
   The class "a write without a row" is closed by construction; "a write without its *own* row" is caught by the
   suite (`every_write_audited` in `tests/platform/hub/conftest.py`), because 100 % coverage runs every success path.
2. **Replays declare themselves** (`agent_hub.audit.recorder.unchanged`) instead of a list of replay routes: an
   idempotent repeat that changed nothing gets the generic row; a changing call without a row stays a defect. Static
   list `audit/generic_routes.py`: the two WebAuthn `begin` ceremonies and `POST /v1/recovery/finish` (challenges),
   `POST /v1/device/outbox` (telemetry items are recorded with the outbox receipt) and the device-request responses
   (encrypted parts; state changes are audited).
3. **`deferred()`**: a request that stored only the session's own events needs no pipeline row – the audit job
   writes `event.append_batch` for them (ADR-063 §4).
4. **Pipeline rows go through the read log's windows** (first row written at once, repeats counted in
   `audit.repeat`), so device telemetry and retries do not flood the chain; `read.repeat` of the draft was renamed
   `audit.repeat` for that reason.
5. **No hub metric for unaudited writes**: the hub exposes no metrics endpoint; the counter
   `agent_hub.http.writelog.UNAUDITED` and an error line are the operator's signal.
6. **Refusals before a principal is known are not audited per request** (no or broken signature, source limits,
   body limit, unknown route, `429`): they name no verified actor and are bounded by the failed-signature limit.
   (Review finding M7 rejected for this reason; the limiter keeps counting them.)
7. **Refused rows keep only IDs of the actor's own organizations** (review finding M4), for reads and writes alike
   (`agent_hub.audit.placement`): a stranger cannot place rows in another organization's audit view; the chain still
   holds them for the operator.
8. **The read key includes a query fingerprint** (review finding H4): paging through a list is many rows, not one
   row with a count.
9. **A read that cannot be recorded is answered `503`** – no data leaves the hub without its row. A refusal whose
   row cannot be written is still answered as a refusal (nothing left the hub), the failure is logged.
10. **Migration `0015_event_audit_cover`** seeds every existing scope at its current head (their events already have
    their own rows). Number chosen as the next free one on the united tree (`0012` to `0014` went to
    parallel packages); `0016_quota_unreadable` follows it.
11. **ADR numbers 063/064**: the next free numbers after ADR-062 on the united tree.
12. **`/v1/stream`**: the route declaration allows `read_resource` on streaming `GET`s; the contract rule of
    `api/INDEX.json` changed accordingly (additive). `read.stream.terminal` rows are written by the route (the watched
    set comes from the query); a failing write abandons the opened stream.
13. **Web – runtime and reachability** (L11): runtime per session in the session facts (`first_working_at` or
    `created_at` to `ended_at` or now; an ended session of an older hub without `ended_at` shows none), per task in the
    task header (`created_at` to `closed_at`), reachability of the session's device from `GET /v1/orgs/{org}/devices`
    (`online`), read every 30 s while shown. The work-VM runtime of KOR-29 stays separate.
14. **Web – `session.error`** (L11): a chat item with the text of its code (closed list); an unknown code is shown as
    such. For the situation view (KOR-39) the kind is a blocking sign; KOR-39 maps it when it lands.
15. **Web – "GitHub trennen"** (L17): the browser opens the record anyway; it now also tells whether the token is one
    the platform minted (`ghu_`). The dialog says either "the devices revoke the access at GitHub" or "it stays valid
    at GitHub (operator's account or transferred access)" with the settings link.
16. **ATK-05 reference** (L17): R-10.4 and R-13.3 named ATK-05 (WebSearch); the Agent/Task attacks are ATK-02 and
    ATK-03. Corrected in `01-requirements.md`, `10-work-packages.md`, `e2e/platform/traceability.json` (a defect fix
    of the matrix, not an acceptance change) and noted in `acceptance/FINAL.md`.
17. **Child session chat tab** (L17): E2E-24 now opens the child's chat tab and expects its own answer.

### Security review of the design (fresh context) – findings and handling

| Finding | Handling |
|---|---|
| H4 read aggregation hides bulk reads | query fingerprint in the read key (decision 8) |
| M4 refused rows in foreign audit views | confined to the actor's organizations (decision 7) |
| M5 flooding of the chain / audit lock | first row per key and window only; per-key volume bounded by the existing rate limits; group commit not needed at today's volume (measured, see report) |
| M6 replays as defects, background work | `unchanged()`/`deferred()` (decisions 2, 3); no route appends rows after its response |
| M7 signature failures not audited | rejected (decision 6) |
| L2 batch rows overlap other events | the batch row covers the session's own kinds of one session; other kinds keep their own rows |
| L3 stream watch set changes | the set is fixed per stream (query) |
| H1–H3, M1–M3, L1, L5, L6 | guest and runner, first part of this note |

### Measurements

Rows of `hub.audit` per hermetic E2E run (T1, one iteration, taken from the hub database at the end of the run),
before (base of the package) and after (this package):

| Run | Before | After | Change |
|---|---|---|---|
| A: E2E-10, E2E-19, E2E-22, E2E-24 | 1 465 | 543 | −63 % |
| B: E2E-06, E2E-13 (after: and E2E-47; before, E2E-40 of the lane timed out) | 480 | 219 | −54 % |

- Before, `event.append` was 61 % of run A (`session.terminal` 577, `session.hook` 140, `session.tool` 56,
  `session.output` 17, `session.file` 5 rows); after, the session's own events of run A (528) are 10
  `event.append_batch` rows. Reads were 24 % (A) and 63 % (B) before, 37 % and 48 % after (the first read of a key
  per window).
- The `audit.repeat` rows of windows still open when the database was taken are not in these figures: at most one
  per read key and window, so at most as many as the first reads of the run (A 199, B 105); run A stays below 742
  rows (−49 %) in any case.
- The rows the package adds (refusals, `request.write`, stream reads, the six routes) are included in "after".
