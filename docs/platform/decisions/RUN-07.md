# RUN-07 decisions (operation pipeline: formal checks, dedupe tag, registration, result)

Small decisions of the package that runs every `request_operation` through "register-then-execute" (05-runner.md §12,
§6.5; 03-api.md §8.3, §9, §11, §12; 04-crypto.md §4.10; ADR-012). The architecture-level points are ADR-041.

## Store and wake-up
1. Migration `0004_operation.sql` extends `runner.operation` (pending call, VM, request time, idempotency key, the
   guest's local error, the check output `checked`, the stored registration body, `duplicate_of`,
   `child_session_id`, `result_event_id`, `retryable`, the session's `answer`, `answered_at`), adds a state CHECK
   (the hub's operation states) and the tables `workgroup_settings` and `operation_result`.
2. The journal handler only records (`received`) and sends `NOTIFY runner_operation`; the long work never runs in the
   journal transaction. The pipeline's listener connection gets the notification after the commit; a scan every 5 s
   covers a missed one and restarts.

## Formal checks (03 §9, `contracts/ops/rules.json`)
3. The runner repeats the parameter schema itself (closed objects, integers never booleans, lengths in code points)
   instead of trusting the guest's `local_check:"passed"`; `params_invalid` lists every JSON pointer (`/params/<key>`,
   RFC 6901 escaping, at most 32). Order: type, parameters, commit paths (`path_invalid` with
   `/params/paths/<i>`), allow-list, protected branch, GitHub connection, push commit, subagent model; the first
   failing step gives the code.
4. Branch names are normalised (`refs/heads/` removed) before every rule, the protected-branch match and the dedupe
   tag; the effective parameters (normalised branches, `draft` from `pr_draft_default`) are kept in `checked.params`
   for the executor. The params event carries the parameters exactly as the session sent them.
5. "GitHub connection present" = a cached `github` credential of the task owner (`runner.credential_cache`). RUN-09
   owns the cache (05 §2.6) and must keep it current (fetch on the `credentials` hint and `credentials_changed`).
6. `github.push` "commit created by an earlier `github.commit` of this task" is checked against the succeeded commit
   operations of the task on this device; otherwise `not_found` (the closest code of the closed list). A commit made
   on another device or before a store wipe is not known (documented limitation).
7. Rights (H2), subagent limits (H4: depth, running, total) and budget stay with the hub, which decides them as
   `denied`. A local pre-check would turn the hub's `denied` into a locally `rejected`; the table of 00 §3.1 marks the
   runner side as an optional pre-check. RUN-11 may add a pre-check for the subagent counts through
   `op_prechecks.py`.
8. The secret patterns, commit, checkout, pull-request, comment, subagent and title limits are constants in
   `operations/rules.py` (compared with `rules.json` by a test) for RUN-09/RUN-10; the scan of the commit snapshot is
   RUN-10's pre-check.
9. A known type without an executor on this device (and checkout/commit without a pre-check that supplies the
   dedupe fields) is rejected locally `unknown_type` with the message "not enabled on this device". Until RUN-09,
   RUN-10 and RUN-11 merge, only `task.set_title` executes.
10. A pre-check that raises anything but `CheckFailed` is `upstream_error` (logged); pre-checks map their own
    transient GitHub errors.

## Dedupe tag and registration (04 §4.10, 03 §8.3)
11. The dedupe key is derived from the workgroup key of the epoch the params event is sealed with (the newest held
    epoch), so tag and event always use the same epoch. Locally rejected operations send `dedupe: null` (H5/H6 do not
    run for them).
12. The registration body is stored before the first attempt and reused for every retry (the hub's idempotency rule
    compares content; a re-sealed event would be `409 id_conflict`).
13. A guest-rejected request may name any type; the plaintext `type` of the registration is the requested type when
    it matches `contracts/common.json#/$defs/operation_type`, else `invalid.type`. The encrypted params event keeps the
    exact type and parameters (audit).
14. Parameters that would make the params event exceed E8 (256 KiB) are left out (`params: {}`) and the operation is
    rejected `size_exceeded` (unless it was already rejected for another reason, whose code stays).
15. While the device holds no workgroup key the registration waits (2 s steps) until the 10 min expiry.
16. Retries: transport errors, decisions outside the contract, `429` and `5xx` with the sync backoff (1 s doubling to
    30 s, ±20 %, at least `Retry-After`). Any other refusal is final: `failed` with `rights_denied` (403) or
    `upstream_error`; the hub never knew the operation, so no event or transition is written.
17. Expiry (`received` 10 min, 03 §12) is measured from the time the runner recorded the request (store clock) and
    the pipeline clock since; on expiry the session gets `failed` `hub_unreachable` `retryable:true` and the row is
    `expired`. When the registration was sent (the hub may have recorded it), the `operation.result` event
    (`expired`) and the `expired` transition release the hub's slot; a hub that never knew the operation skips the
    items. Before any registration nothing is written for the hub.

## Decision, execution, result
18. `rejected` answers use the local error (with pointers and message) when the runner or the guest rejected it,
    otherwise the hub's code; `denied` always the hub's code. Every operation the hub decided gets an
    `operation.result` event (`rejected`/`denied` with the error; no transition, the hub's row is final already);
    only a final refusal of the registration (the hub never recorded it) has none.
19. Retries inside one executor run: first attempt plus at most 3 retries after 2 s, 8 s and 30 s (ADR-012 lists
    three delays); `retry_after` raises a delay; a `retry_after` above 60 s (05 §12.1 rate-limit rule) ends the
    retries; then `failed` with `retryable:true`. The `attempt` column counts attempts.
20. An executor that raises anything but `OperationFailed`, or returns no JSON object, is `upstream_error`
    (non-retryable).
21. A result whose `operation.result` event would exceed E8 ends `failed` `size_exceeded`; executors (RUN-09
    `get_pull_request` patches) must size results to fit.
22. `operation.result` (header: workgroup scope, the session's task and session, `ref` = operation ID), then the
    `operation_transition` and the stored answer are written in **one** transaction; the outbox order is the
    delivery order.
23. A result with `existing:true` (open PR found by the executor) is answered `status:"duplicate"` without
    `duplicate_of` (03 §9).
24. An operation found `executing` when the pipeline (re)starts ends `failed` `timeout` `retryable:true` and is never
    executed again.
25. `task.set_title`: device-authored `task.meta {title}` (header `session=""`, `ref=""`) through the outbox, result
    `{}`.

## Duplicates and answers
26. A duplicate's original counts only within the requesting operation's workgroup (local row: also the same type;
    stored result event: the same workgroup), so a hub naming a foreign original never moves plaintext of another
    workgroup into a VM (00 §3 B4). The hub's `original.result_event_id` is not compared (the workgroup check is the
    boundary; within a workgroup the session may read the operations anyway, ADR-024). A duplicate's answer comes from the original's local row (it ran here) or its verified `operation.result` event
    (another device; one inbox pull per second while waiting). It waits within the MCP wait (20 s from the request);
    nothing final gives `pending` with `duplicate_of`. An original that failed gives `failed` with its error. A
    duplicate writes no event and no transition (the hub stored the decision).
27. "The MCP call already returned `pending`" = 19 s or more since the request: the larger of the row's store age
    and the guest's `requested_at` against the device clock at pick-up (a runner that lagged behind the journal or
    restarted), then the pipeline clock (the guest waits 20 s; a spare ping is content-free). A final answer then also calls
    the `operation.finished` notifier (RUN-14); without one it is logged. The answer is sent anyway (the guest keeps
    late answers, GST-07 decision 11).
28. The notifier runs only after the answer reached the guest (or could not be routed), at most once per operation
    and daemon. Answer delivery: up to 3 attempts (1 s, 2 s); a guest refusal (unknown or answered pending call) ends the
    attempts; unanswered final operations younger than 15 min are retried by the next scans. A guest rejection is
    never answered again (the guest already returned `rejected`).
29. `Pipeline.answer` / `wait_answer` give the current outcome of an operation for `get_operation` (RUN-08, which owns
    the `platform.query` handler and the read scope).

## Tests of other packages adjusted
30. `tests/platform/runner/test_store.py` (RUN-01) lists the columns and tables of migration 0004.

## Affected E2E/ATK tests
31. E2E-21, E2E-22, E2E-24 (SCN-04) and ATK-33 (ATS-02) do not exist yet. Their runner-side behaviour is covered by
    `tests/platform/runner/operations/` (exact codes per rule, duplicates with one execution, idempotency key reuse,
    unknown and smuggled types never executed, retries within one operation).

## After the independent review (2 high, 8 medium, 7 low)
32. H1: a hub answer `accepted`/`duplicate` for an operation registered as rejected is a contradiction (the hub is
    untrusted, 00 §3 B2): never executed, no original's result; an `accepted` one ends `failed` with the local code
    (event and transition release the slot), the session gets `rejected` with the local error (log line
    `hub_contradiction`). H2: item 26.
33. M1: journal bodies with a NUL character or a lone surrogate anywhere, or nested deeper than 64, are refused as
    malformed records (PostgreSQL `jsonb` would refuse them and roll back the whole journal page forever).
34. M3: an `accepted` operation whose request is older than 29 min (the hub expires `accepted` after 30 min; one
    minute margin) is not executed: `expired` `timeout` with event and transition.
35. M4: the final transaction (and every final write) is tried 3 times on store errors (1 s, 2 s); only then the
    error propagates (the row stays `executing`, which the next run ends `failed timeout` without executing again).
36. M5/L1: error codes are restricted to the closed list (`OPERATION_ERROR_CODES`, compared with the contract):
    executor and pre-check codes outside it become `upstream_error`, guest codes `params_invalid`, a hub decision
    with another code is not a decision (retried). Messages that are not printable ASCII of 1-1024 characters are
    dropped.
37. L2: only `size_exceeded` from the draft check means "too large"; any other refusal of the params event is
    `params_invalid`. L5: the title also refuses C1 control characters (NEL is a line break; stricter than the schema
    pattern, which the guest checks first). L7: the `dedupe_tag` column is filled; the pick-up times are dropped when
    an operation's task ends.
38. Not changed, with reasons: L3 (the params event of a guest-rejected request keeps the exact type and params,
    which `body_operation_params` cannot express; the audit value outweighs it, readers must accept unknown types);
    L4 (1 + 3 attempts, item 19, follows the three delays of ADR-012); L6 (`task.meta` from the device carries the
    title only, like 03 §10 allows; a reader that merges fields keeps the description - WEB/RUN-08 must merge, not
    replace); L7 rights/limit pre-checks (item 7) and the unbounded number of operation tasks (bounded by the
    guest's request rate and the hub's limits).
