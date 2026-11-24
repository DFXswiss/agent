# ADR-080: Model access with fallback, visible quotas and DFX access on own hardware

Status: Accepted (2026-11-24). Amends ADR-010 (selection), ADR-052 §4, §6 and Consequences (operator subscriptions:
exhaustion moves running sessions, blocks survive a restart, the operator's subscriptions are polled), ADR-062 §1 and
§3 (case "own hardware + DFX access" is reachable and is booked from the provider's figures) and ADR-076 §1 (two new
attention reasons).

## Context
- Rule [E-02](../KONZEPT.md#e-02): the platform grows with devices **and model accesses** and reports clearly when
  capacity is reached instead of failing. [§6 Punkt 2](../KONZEPT.md#k-6-b2) names "Claude-Abonnements und verfügbare
  Kontingente, soweit technisch auslesbar" among the things to synchronise. [§4 Punkt 4](../KONZEPT.md#k-4-b4): without
  own credentials the system uses DFX model access through OpenRouter. [Idee 46](../KONZEPT.md#k-idee-46-b3): own
  hardware + DFX model access is charged as tokens; [Idee 45](../KONZEPT.md#k-idee-45): fully own resources are free.
  [E-07](../KONZEPT.md#e-07): the operator's subscriptions for approved organizations; [A4](../KONZEPT.md#a-4): amounts
  are plaintext at the hub. [E-05](../KONZEPT.md#e-05): a missing precondition is started, not explained;
  [E-04](../KONZEPT.md#e-04): everything works without a browser; [E-11](../KONZEPT.md#e-11): no approval as a
  precondition; [E-19](../KONZEPT.md#e-19): whole classes of defects are excluded.
- Built state: an operator subscription that answers `401`/`403` is blocked in memory until its file changes, a `429`
  defers it for new sessions for 300 s (ADR-052 §6); a restart forgets both. A running session stays on an exhausted
  subscription and its turns end with the provider's limit error; nobody is told; there is no move to another
  access. The quota of a user's own subscription is polled (05 §10.6) and shown to that user; a long-lived token reads
  as "not readable" (KOR-40); the operator's subscriptions are never polled. DFX model access exists only on platform
  devices (ADR-062 §1: "defined by the rule but cannot occur yet").

## Decision

### 1. Cost classes and the one rule about crossing them
- Every model access belongs to one **cost class**: `own` – the user's own subscription (`subscription`) or own API key
  (`api_key`), paid by the user outside the platform; `dfx` – an operator subscription (ADR-052) or DFX model access
  through OpenRouter (`platform`, also the per-device key of §7), both charged by ADR-062.
- **Inside a class the platform falls back by itself, deterministically** (§3). **From `own` to `dfx` it never falls
  back silently**: only when the session's owner switched on "use DFX access when my own subscription is used up"
  (`fallback.own_to_dfx`, default **off**) or a person allowed it for this task (§5, the card). The rule "no silent
  fallback for an explicit preference" of `model.access` stays: an explicit `api_key` or `platform` preference has
  no fallback at all; the explicit `subscription` keeps its ADR-052 order (own subscription, then the operator pool).
- Inside `dfx` the move from the operator's subscriptions to `platform` follows `fallback.operator_to_platform`
  (default **on**). Both settings belong to the user (`PUT /v1/me/settings`), are delivered with every session of the
  owner in the device state (`fallback`), and an older hub that does not deliver them means the defaults.

### 2. Exhaustion: detected, confirmed by the provider where possible, kept across restarts
- Only subscription accesses are exhausted in this sense (an API key or OpenRouter answers `429` for a short rate
  limit; the platform budget and the credit have their own rules, ADR-026/ADR-062). An **access key** names one
  subscription: `own:<owner_id>` or `operator:<file name>`.
- **Trigger**: a gateway `usage` record with status `429` of a session on such an access. **Confirmation**:
  - *readable quota* – the runner reads the usage endpoint with the same token at once (at most once per access per
    `CHECK_S` = 60 s): the access is exhausted when a window reports `used_percent >= 100` with `resets_at` in the
    future; it is exhausted **until the latest `resets_at` of the full windows** (the provider's own release time).
    A window below 100 % means a short rate limit: nothing is marked (Claude Code retries it).
  - *quota not readable* (the endpoint refuses the token, KOR-40): exhausted after `STREAK` = 3 `429` records of the
    access within `STREAK_S` = 300 s without a successful record in between, until `now + PROBE_S` (900 s); when the
    time is up the access is tried again (the next start or resume uses it), and a further confirmed streak marks it
    again.
  - The quota polls (own subscriptions every 900 s, 05 §10.6; operator subscriptions every 900 s, §6) mark an access
    exhausted, or release it, with the same rule, also without any `429`.
- **Kept across restarts**: runner table `runner.access_limit` (access key, `until`, `readable`, `limited_at`); read at
  start, a row whose `until` passed is released. The ADR-052 `401`/`403` block of an operator file is kept there too
  (with the file's identity: a new file content releases it).
- **Untrusted guest**: a readable quota is decided by the provider, not by the record; a streak of an unreadable own
  subscription can only hold that owner's sessions; for operator files ADR-052 §6 still limits one session to one
  block or deferral per 10 min.

### 3. Order: the next available access of the same cost class
`model.access.resolve` walks the steps of the preference as before (`auto`: own subscription → own API key → operator
pool → `platform`; ADR-052 §4) and now **skips exhausted accesses**:
- operator pool: the usable files that are not exhausted, chosen by **remaining quota** (the lowest known
  `used_percent` of the file's fullest window first; files with an unknown quota after those with a known one), then
  as before (fewest bound sessions, unused longest, name);
- a step after an exhausted `own` access is a `dfx` step only when `fallback.own_to_dfx` or the task's consent allows
  it; `platform` after an exhausted operator pool only with `fallback.operator_to_platform`;
- the result of the same inputs is always the same access (no randomness, no time except the stored `until`).

When no allowed access is left the result is **wait**: `until` = the earliest release among the skipped accesses
(their `until`), and `choice` = a `dfx` access would be available but the crossing rule bars it. A **new session**
then starts on the skipped access with the earliest release (so its VM, its gateway and its Claude Code are ready) and
is paused before its first prompt (§4); nothing fails with `model_access_unavailable` because of an exhausted
subscription.

### 4. Running sessions: move, pause, continue by itself
Reconcile step `model_fallback` (after `billing`, on every desired state and every 5 s by the task of the same name):
1. **Same kind** – a session on an exhausted operator file whose pool has another selectable file is rebound in place
   (the gateway renewal of ADR-052 §5; no restart, the model stays). ADR-052 §6 is amended: a *confirmed exhaustion*
   moves running sessions; a short `429` still does not.
2. **Other kind** (a model change, e.g. operator subscription → DFX access over OpenRouter): Claude Code needs another
   model environment, so the session is paused like a credit pause (ADR-062 §6: interrupt, suspend under the
   deliverer's lock, recorded first in `runner.model_pause`) and resumed at once; the resume resolves the access anew
   (§3) and the session receives one fixed sentence as its continuation prompt (§5).
3. **Nothing allowed** – the session is suspended with the reason `model_quota_exhausted`, `wait_until` and
   `dfx_choice` (§3) in its transition; a prompt that arrives meanwhile stays pending (as for credit). The pause ends by
   itself when its `until` is reached, when an access becomes available earlier (a quota poll releases it, a file is
   renewed) or when the owner's setting or the task's consent allows a `dfx` access; the session is resumed and gets
   the continuation sentence; a session that was idle stays suspended until its next prompt, which is then delivered.
4. A **subagent session** (one turn) is moved in place (point 1) but never paused: with no allowed access of its kind
   it ends with `model_quota_exhausted`; its operation fails and the parent learns it like any ended subagent.

### 5. The human and the session are told
- **Session**: one sentence as the continuation prompt, `"The platform switched this session's model access from
  <from> to <to> because <from> is used up until <time>. Continue the interrupted work."` or, without a change,
  `"The model quota is available again. Continue the interrupted work."` (no user text; stable prompt ID per pause).
- **Human**: every switch writes the audit row `session.model_switched` (from/to access and model, cause, until;
  metadata only) and an in-app notification to the task's owner ("Modell gewechselt: … → …"); the task view shows the
  line. A wait is the attention record of the task (ADR-076): `working/quota_wait` "Wartet auf Modellkontingent –
  weiter um 14:05" when no person is needed, `needs_decision/quota_choice` "Dein Abo ist bis 14:05 erschöpft – mit
  DFX-Zugang weiterarbeiten?" when `dfx_choice` is set. The card offers "Mit DFX-Zugang weiterarbeiten" (`POST
  /v1/tasks/{task_id}/model-consent`, task writers; sets the task's consent, delivered as `model_consent` with the
  task's sessions; audited `task.model_consent`) and "Immer so" (the setting of §1). Without an answer the task
  continues by itself at the release time on the own subscription.

### 6. Quotas visible, as far as readable
- **Own subscriptions**: as before (05 §10.6, `GET /v1/me/quotas`), plus the latest observed limit: the `quota` outbox
  item may carry `limited: {at, until, readable}` (`runner.access_limit`), stored in `hub.quota_limit`.
- **Operator subscriptions**: the runner of a platform device polls every file's token like an own subscription (every
  900 s and at a confirmation, §2) and sends one snapshot item `operator_quota` when something changed (at most every
  60 s): per file its label (the file name chosen by the operator), state (`usable`, `exhausted`, `blocked`,
  `ignored`), windows (or `readable: false`), `limited_at`, `until` – never a token. The hub keeps the newest snapshot
  per device (`hub.operator_quota`).
- `GET /v1/orgs/{org_id}/quotas` (members of the org): the caller's own subscription, the operator's subscriptions when
  the organization may use them (verified platform log, ADR-079), whether DFX access over OpenRouter is available, and
  a summary (`usable`, `exhausted`, `next_release`). "Not readable" is shown honestly with the last observed limit
  time. The web shows it in the organization settings ("Modellzugang und Kontingente") and as a counter in the
  situation view; the CLI has `agent-platform quotas --org ORG` (E-04, `agent-parity.md`).
- The runner sends the new fields and items only to a hub whose device state carries `model_fallback: {api: 1}`.

### 7. DFX model access on own hardware (idea 46 case 3)
Two variants were weighed:

| | (i) Limited key per org device, created by a platform device | (ii) Relay through a platform device |
|---|---|---|
| Main key | stays on the platform device (only the management key can create keys; it never leaves) | stays on the platform device |
| Measuring | the provider's own figures per key (`GET /api/v1/keys/{hash}`: spent USD), exact for money | exact per request |
| Zero knowledge | prompts go from the org device straight to the provider; no DFX machine and not the hub sees them | every prompt of the own hardware passes a DFX device in plaintext; the hub would have to carry the traffic (devices only talk to the hub) or a platform device would need a public endpoint – both break §5 point 2 or the network rules |
| Cost risk | bounded per key by the provider (`limit`), updated every minute to the org's credit | bounded by the relay |
| Availability | own hardware works while DFX devices are down | own hardware stops when the relay is down |
| Cost | no traffic through DFX | all model traffic through DFX |

**Decision: variant (i).** OpenRouter offers it reliably: keys created through `POST /api/v1/keys` with a management
key, each with a USD `limit`; the plaintext key is returned once; `GET`/`PATCH`/`DELETE /api/v1/keys/{hash}` read the
spend and change or remove the key. (A management key of a personal account is refused by these routes – the
operator needs one of an organization account.)
- **Issuer**: a platform device whose runner has `AGENT_RUNNER_OPENROUTER_MANAGEMENT_KEY_FILE` (0600, platform devices
  only, never logged, never sent) announces itself with the item `dfx_key_state {issuer: true}`. The hub elects the
  online issuer with the smallest device ID.
- **Request**: for every active org device of an organization with billing active and not exhausted, and without a
  key, the issuer's device state lists `dfx_keys.requests: [{request_id, org_id, device_id, device_kem, headroom_usd}]`
  (`device_kem` from the hub's device registry). The issuer creates one key per org device (`name` = `dfx-` + the
  first 8 characters of the request ID, `limit` = `headroom_usd`), seals it with HPKE to `device_kem` (info binds
  request, org, device and key reference), signs the envelope with its device signing key, forgets the key and sends
  `dfx_key_issued {request_id, org_id, device_id, key_ref, envelope}`. The hub stores the envelope (ciphertext only) in
  `hub.dfx_key` and delivers it in the org device's state (`dfx_key`).
- **Org device**: accepts the envelope only when its signer is a platform device certified in its verified platform log
  and the header names this device and its own KEM key (a hub-made key could route prompts to someone else's provider
  account: refused). It keeps the envelope sealed in `runner.dfx_key` and opens it only for a provisioning. With such
  a key the strategy `platform` is available on the org device; the session's access is `platform`, its usage items
  carry the origin `platform`, and the hub does **not** charge them (an org device can neither cause nor avoid a
  charge, ADR-062 §1).
- **Measuring and booking**: the issuer reads every issued key's spend every 60 s and sends the difference since the
  last report as `dfx_key_usage {key_ref, org_id, device_id, from, to, cost_micro_usd}` (one transaction with its meter
  row `runner.dfx_key_meter`, so nothing is counted twice or lost). The hub books a `tokens` charge for the org and
  the device: amount = provider cost × the price list's factor `platform` (the provider cost *is* the purchase price),
  quantity `{source: "provider_key", key_ref, from, to, cost_micro_usd}`; it is attributed to the organization and
  the device, not to a task (the provider reports per key; the per-session token counts stay visible as usage).
- **Bounding the cost**: on every poll the issuer sets `limit = spent + headroom_usd` (the hub computes `headroom_usd`
  from the org's balance divided by the `platform` factor, at most `AGENT_HUB_DFX_KEY_HEADROOM_USD`, default 5). An
  exhausted organization gets `disabled: true` (the key is switched off at the provider), a device that leaves the
  organization or is removed gets `revoke: true` (the key is deleted, the hub drops the envelope).
- **Operator subscriptions stay on platform devices** (open question F2; ADR-052 §2): no token of the operator's pool
  ever reaches an org device.

## Consequences
- New runner tables `runner.access_limit`, `runner.model_pause`, `runner.dfx_key`, `runner.dfx_key_meter`; new hub
  tables `hub.quota_limit`, `hub.operator_quota`, `hub.dfx_key`, columns for the session wait, the task's consent and
  the user's fallback settings; outbox kinds `operator_quota`, `dfx_key_state`, `dfx_key_issued`, `dfx_key_usage`; all
  additive. An older runner keeps working against a newer hub (it sends none of it); a newer runner sends nothing new
  to an older hub.
- Release: hub first, then runners. For case 3 the operator creates a management key of the provider's organization
  account and places it on the issuing platform device; without it own hardware simply has no DFX access (as before).
- Guest unchanged: the runner learns the release time from the usage endpoint, not from the `429` headers (the guest
  journal carries the status only).
- Residual risk: a compromised hub could obtain a limited key by naming a key of its own as `device_kem` – money up
  to `headroom_usd`, booked and visible, never content.

## Alternatives considered
Moving rate-limited sessions on every `429` (drains every window in turn, ADR-052 §6); a fixed waiting time after a
`429` (the provider names the release); silent fallback from own to DFX access (contradicts §1 and costs the user money
without consent); one key per organization shared by its devices (every device would hold the same key and a removed
device could keep spending); the relay of §7 (ii).
