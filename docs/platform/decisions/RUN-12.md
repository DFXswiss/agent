# RUN-12 decisions (model access of every kind, cost and context, quotas, budget)

Small decisions of the package that selects and provisions the model access of a session for all three kinds, turns
the gateway's usage into outbox deltas with their cost basis, polls subscription quotas and the platform key's spend
and enforces the daily platform budget (05-runner.md §2.6, §10, §1.3, §1.5; 03-api.md §6.1, §7.1, §7.2, §9;
08-monitoring.md §3; ADR-010, ADR-025, ADR-026). No new ADR: the one architecture-relevant change (item 3, removal of
an extension point of ADR-032) is recorded as a note in ADR-032.

## Catalog and selection (`model/catalog.py`, `model/selection.py`)
1. The packaged catalog `agent_runner/model/model-catalog.json` is the contract example
   `contracts/model-catalog.example.json` byte for byte in content (a test keeps them equal and validates the file
   against `contracts/model-catalog.json`); `AGENT_RUNNER_MODEL_CATALOG` replaces it. The package data line in
   `pyproject.toml` is a separate commit (ADR-027 §6). RUN-04's built-in platform-only copy is gone.
2. Prices are optional for the runner: a model without `price_per_mtok` costs 0 at list price (a catalog without
   prices still starts sessions); present prices must be finite, ≥ 0 and ≤ 10⁶ USD per Mtok.
3. All three kinds are built-in strategies; `subscription`/`api_key` are available when the owner's credential opens
   on this device. The access kinds are a closed list, so the discovered extension point
   `<area>/model_access.py:STRATEGIES` of RUN-04 could only ever conflict and is removed; `selection.strategies()`
   returns the built-ins (the session environment keeps its field). ADR-032 carries a note.
4. For `api_key`/`subscription` the chosen model is the owner's `model_id_pref` when the catalog allows it, else none
   (Claude Code's default, no `--model`, no model environment). The guest passes a chosen model as `--model`.

## Resolution and provisioning (`model/access.py`, `model/provision.py`)
5. `access.resolve(env, desired)` walks the preference's kinds lazily: a credential is only opened for the kind being
   tried, so `auto` with a subscription never decrypts the API key. Unavailable for a kind: no record (`404`), a record
   this device cannot open (no `CK` of its epoch), a body without a usable secret (printable ASCII, no spaces, ≤ 4096),
   or a hub/transport failure during the on-demand fetch (logged with the error type; `auto` goes on).
6. Session start (`sessions/start.py`) and resume (`prompts/resume.py`) call `resolve` (one-line changes outside the
   package's paths: `_model` became async). A platform start while the org's budget is spent fails with
   `budget_exceeded` (a session failure reason), not `model_access_unavailable`; the owner's own access is never
   limited by the platform budget.
7. Owner credentials are provisioned with upstream `MODEL_UPSTREAM_ANTHROPIC`, `x-api-key: <key>` (API key) or
   `Authorization: Bearer <token>` (subscription; the gateway appends `oauth-2025-04-20`), the catalog allow-list of
   the kind and no provider allow-list or budget.
8. What a provisioning was built from is remembered per daemon (`Stamp`: kind, owner, record version, SHA-256 of the
   platform key, budget) – never a secret. After a daemon restart the map is empty, so each running session is
   provisioned once more on the first refresh (harmless: the new provisioning replaces the old one).

## Renewal, withdrawal, budget (`model/gateways.py`, `model/reconcile.py`, `subagents/op_prechecks.py`)
9. Reconcile step `model` (order 75, after the credential step 70) renews the credential of every session whose
   guest holds one (`sandbox_ready` … `idle`; not while its start/stop task runs and not while it resumes – the resume
   provisions itself, possibly with another kind) when the record version, the platform key or the org's budget
   (`platform_budget` of the desired state) changed. The budget renewal is how several VMs of one org share one daily
   budget: each provisioning carries the hub's `spent_today_by_org`, which includes what the other gateways reported
   (RUN-04 review note 29). Cost a gateway observed but the hub has not received yet is reset by a renewal, so the
   org can overspend by at most the not-yet-reported cost of one reconcile interval (the hub and the next renewal
   catch up); GST-04 has no way to carry it over (review L1, accepted).
10. Withdrawal (05 §2.6) only for a **deleted** record (purged: `404`): the contract of `PUT …/credential` has no
    "remove" form, so the gateway gets a provisioning with the placeholder secret `withdrawn` and an **empty model
    allow-list** (every request is refused with `model_not_allowed`; the spec's `model_access_unavailable` is the
    session's reason, set at once), then the session fails with `model_access_unavailable` (with its
    `session.status` event) and its guest session is stopped at once. Triggers: the purge listener of the credential
    cache (registered by the first run of the step) and the renewal for a record that is no longer cached (a purge
    the listener missed, or one that hit a session whose start was running: the first run after the start withdraws
    it, at most one reconcile interval later). A platform key file that cannot be read, a record that does not open
    on this device yet (no `CK` of its epoch) or a fetch failure keep the current provisioning and are retried
    (review M1). Renewal and withdrawal of a session run under one lock and a withdrawn session is never renewed, so
    a renewal that decrypted the old record cannot put it back after a purge (review M2); the record version of a
    stamp is the cached one before and after opening, `-1` when they differ (review M5).
11. `subagent.request` has one pre-check per type (ADR-041), owned by RUN-11; the budget refusal (`budget_exceeded`
    for a parent with platform access while the budget is spent) is added there (small change outside the package's
    paths). The `model_id` check of a subagent already exists in `operations/checks.py` (RUN-07) and now sees the full
    catalog.

## Usage (`model/usage.py`, `model/journal.py`)
12. The `usage` journal handler moved from `sessions/journal.py` (RUN-04) to `model/journal.py:HANDLERS` as the
    package text asks (the registry refuses a kind registered twice); the RUN-04 tests that drive a journal loop with
    usage records merge both handler maps.
13. Cost basis follows the session's recorded access: `platform` → OpenRouter `usage.cost` (`provider`; 0 when the
    response carried none, e.g. a refusal or an upstream error); `api_key` → catalog list price (`list_price`);
    `subscription` → the same as `subscription_equivalent`; a session without recorded access counts as `provider`
    (what the hub also assumes). Thinking tokens are part of Anthropic's `output_tokens` and are not priced again.
14. Context: a request that carried no context (no input tokens or no window: refusals, failed requests) keeps the
    session's last figures (kept per daemon in memory), so the hub's latest-wins context fill does not drop to 0.
15. A provider outside the allow-list writes the audit and increments the daemon counter
    `provider_outside_allowlist` (`model.usage.counters`) plus a fact line with the provider label; 08-monitoring.md
    lists no metric for it, so it is not exported.

## Quotas (`quota/`)
16. `quota/parsers.py` is a copy of `agent_cli.usage.windows_snapshot`/`spend_snapshot` with their helpers, bound to
    the two documents (field names of the spike's usage shape: `utilization`, `limits[].kind` `weekly_all`/
    `weekly_scoped`, `percent`, `scope.model.display_name|id`; OpenRouter `data.usage*`, `limit*`). Per-model window
    names keep `[A-Za-z0-9._-]` (others become `_`, ≤ 64, first limit per model wins) to fit the contract pattern; a
    `used_percent` outside 0…1000 is invalid (contract range); `severity` is taken when it is a short lower-case word.
17. Eligible owners: in `quota_poll_owners`, `claude_subscription` cached on this device, and a local session of the
    owner not ended or ended within 24 h. Feature detection remembers the record version that got `401`/`403`; a new
    version (new login) is detected again at once, the same one after 24 h. Redirects are failures (never followed).
    A document with no window writes no item (the contract needs at least one). `Retry-After` accepts ASCII
    delta-seconds or an HTTP date and is capped at 24 h. The unavailable state is only logged: `body_quota` has no
    field for it, so the hub shows "not available" from the absence of a snapshot (open point for HUB-14/WEB-10,
    review L8).
18. Platform spend: `GET <MODEL_UPSTREAM_OPENROUTER>/v1/key` (= `https://openrouter.ai/api/v1/key`) every 900 s;
    `quota.openrouter.spend_metrics(context)` is the interface RUN-15 asked for: windows `daily`, `weekly`, `monthly`,
    `total` of `agent_runner_device_model_spend_usd`, None before the first read and when the last successful read is
    older than 45 min. The sampler (RUN-15) passes it to `textfile.metrics_of` (one-line change).
19. One daemon task `quota` (05 §1.1 lists it) ticks every 30 s; each poller keeps its own schedule.

## Tests
20. Header injection is tested with the runner's bodies through the real guest gateway and the real model double in
    the test process (`tests/platform/runner/model/gateway/`, fixtures of the guest suites imported). The usage
    endpoints are a local HTTP double in the tests (`tests/platform/runner/quota/double.py`, loopback, package port
    range) that refuses requests without the exact bearer and OAuth beta headers.
21. E2E-16, E2E-27 and E2E-36 do not exist yet in `agent-core/e2e/platform/specs/`, and SCN-05 has not delivered
    LIVE-01; per the package text they are not run here. Their runner-side steps are covered by the unit suites
    (`test_access.py`: E2E-36 matrix and E2E-27 kinds/budget; `test_usage.py`, `quota/`: E2E-16 counters and quotas).

## Review (independent subagent): accepted without change
22. L4: the context memory holds one small entry per session of the daemon's lifetime and is updated before the
    journal page commits; a rolled-back page is replayed and writes the same figure again. Accepted.
23. L7: the subagent budget pre-check counts an unknown desired state (no session service yet) as unlimited; the
    gateway (429) and the session start refuse again, and the hub enforces the budget. Accepted.
