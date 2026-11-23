# HUB-14 – decisions (usage, quotas, platform budget)

## Outbox items

1. **Base handlers move to the usage area.** `usage` and `quota` are handled by `agent_hub/usage/outbox.py:HANDLERS`;
   the outbox area's simple inserts (`handlers_usage.py`, `handlers_quota.py`) are removed. The chain discovery of
   ADR-030 knew only one base area; an area now claims base handlers with the discovered `<area>/outbox.py:BASE_KINDS`
   (ADR-037; no central list). The rules stay strict: one claim per kind, the outbox area must not define a claimed
   kind, an extension without a base still stops the discovery, every kind needs a base. Other areas extend `usage`/`quota`
   exactly like any other kind. The foreign outbox tests were adjusted (registry, the moved usage/quota item tests now
   live in `tests/platform/hub/usage/`, one patch path).
2. **Live counters.** After every accepted `usage` item the hub records the `session` frame (row, newest transition) plus
   the counters as `usage`. The frame contract had no place for them: additive optional property `usage` of
   `ws/sse/objects.json#/$defs/data_session` (`contracts/changes/HUB-14.md`). Frames of state changes do not carry it.
   One frame per item. A catch-up push (≤ 500 items) can therefore send many frames for one session; the browser
   stream handles overflow with `resync` (03-api.md §5). Coalescing per push would need a push-level hook of the
   outbox area and is left out in v1 (review finding L7, accepted risk).
3. **Replay.** Strict `origin_seq` order and the receipts of HUB-10 make a resent item `replayed` without running a
   handler; neither `usage_counter` nor `usage_daily` counts twice (tested).

## Platform spend and budget

4. **`usage_daily(org_id, day, device_id, cost_micro_usd)`** (migration 0006, ADR-037). `day` = UTC date of the hub's
   **receive** time of the item (the body has no time; the device clock never decides). One row per pushing device, so
   concurrent pushes of different devices never lock the same row (no hot row, no lock-order deadlock); the spend of
   an org is the sum. Past days stay as a small operator history.
5. **What counts as platform work.** A session with `model_access = platform`; a session whose access is not reported
   yet counts when `cost_basis = provider` (only the platform's OpenRouter access reports provider cost, ADR-010), so a
   missing access report cannot hide platform spend. `api_key`/`subscription` sessions never count.
6. **Edge rule.** The budget is available while today's spend is **below** the limit; reaching it exactly exhausts it
   (same as the gateway's 429 from that point). Limit `0` allows no platform work; no limit (empty variable) = unlimited
   and `platform_budget: null`. Comparison in micro-USD against `Decimal` (no float rounding).
7. **`platform_budget`** = `{usd_per_org_day, spent_today_by_org}` for **every** org with a workgroup assigned to the
   device (from the device's verified view), an org without spend today with `0`. The package text says "orgs with
   `platform_access`"; but after a revocation existing assignments of platform devices stay (ADR-026) and an org
   missing from the map would run unlimited in the gateway, so every assigned org is listed (review finding M1). For
   org devices the entries are harmless (they hold no platform key). USD as JSON numbers (micro-USD / 10⁶).
8. **H4 budget hook** (`agent_hub/usage/budget.py:CHECK`, ADR-031): unlimited → available; requesting session with
   `subscription`/`api_key` → available (children use the parent's access, 05-runner.md §13); otherwise today's spend
   of the session's org against the limit. Refusal = `denied`/`budget_exceeded`, audited by the operations area as
   `operation.denied` (tested end to end through `POST /v1/device/operations`).
9. **Configuration for the hook.** `BudgetRequest` and the outbox `ItemContext` carry no configuration and belong to
   other packages. `agent_hub.usage.binding` keeps `PLATFORM_BUDGET_USD_PER_ORG_DAY` and `DEVICE_ONLINE_SECONDS` of the
   process: bound by the job `usage.bind_config` at job start (and hourly) and by every request of the area
   (`GET /v1/device/state` – each runner fetches it on connect, before it registers operations – and
   `GET /v1/me/quotas`). Before the first binding: unlimited and 30 s (an unconfigured hub). Consequence: a hub process
   started with `run_jobs=False` (tests) applies the budget only after the first device-state request. Failing open
   before the first binding matches ADR-031 (no registered check = unlimited) and the gateway enforces the budget on
   the runner regardless (review finding M6, accepted).

## Session start (interface for HUB-09 and RUN-12)

10. The hub cannot refuse a **main** session for the budget when it creates it: the model access is chosen by the
    runner after placement (05-runner.md §10.1: preference, credentials on the device, device kind), so at
    `POST /v1/tasks` / follow-up prompt time the hub does not know whether the session will be a platform session.
    Interface:
    - **Runner (RUN-12/RUN-04):** before it starts a session (main or child) with access `platform`, it compares
      `platform_budget.spent_today_by_org[org]` with `usd_per_org_day` (same rule as decision 6) and fails the session
      with reason `budget_exceeded` (in `contracts/enums/session-failure-reasons.json`) instead of starting it; the
      gateway answers 429 `budget_exceeded` for requests of running sessions.
    - **Hub, sessions it creates itself:** subagent children are created only after H4, i.e. after the hook (done).
      Any later hub-side creation of a session whose access is known to be `platform` asks the same hook:
      `agent_hub.operations.limits.budget_check(BudgetRequest(connection, org_id, session_id, now))` → `False` means
      refuse with `budget_exceeded` (as `denied` for operations, as session failure reason for sessions). HUB-09's
      task creation does not call it in v1 for the reason above; this is recorded here rather than changed in the
      tasks area (not owned by HUB-14).

## Quotas

11. **Polling device.** Owners = users with a stored `claude_subscription` credential. Candidates = the owner's eligible
    devices (`agent_hub.credentials.eligibility`, the devices that can decrypt the owner's `CK`) that are online
    (presence heartbeat within `DEVICE_ONLINE_SECONDS`). The candidate with the smallest ID (canonical UUID text = byte
    order) polls. A device that is not online itself polls for nobody, so at most one device polls per owner. The
    rule is one SQL query mirroring the eligibility join (a test compares it with the eligibility and presence
    functions). The duty moves at the next reconcile of the next device (runners reconcile at least every 30 s); the
    hub sends no extra hint on presence changes.
    Interface for RUN-12 (review finding M5): 05-runner.md §10.6 polls only owners whose credential is cached and who
    had an active session in the last 24 h. The hub cannot see the runner cache, so the runner MUST treat
    `quota_poll_owners` as the authoritative duty: for every listed owner it fetches the `claude_subscription` record
    (`GET /v1/device/credentials/{owner}/claude_subscription`, allowed for every eligible device) and polls, with the
    24 h activity filter applied across the owner's sessions known from the hub (not only local ones) or dropped.
    Otherwise the duty could land on a device that never polls.
12. **Quota acceptance** uses the same rule at push time: any other device (or an owner without subscription or not
    served by the device) → `403 forbidden` → item `rejected` + audit `outbox.rejected` (HUB-10 mechanism). A report
    of the previous polling device that was in flight during the hand-over is therefore rejected; the next poll of the
    new device fills the window. Each window keeps the newest `fetched_at` (unchanged from HUB-10).
13. **`GET /v1/me/quotas`**: only `owner_id = caller`, newest fetch first, then window name. Read-log action is
    `read.me.quotas` (the closed list `contracts/enums/audit-actions.json` derives it from `read_resource="me.quotas"`;
    the package text's `read.quotas` is not in the list).

## `/v1/config.models`

14. Tested with a custom catalog file: `models` equals the `access.<kind>.allowed` lists of `AGENT_HUB_MODEL_CATALOG`
    exactly (order kept), `defaults.platform` the catalog default. No code change (HUB-02's function).
