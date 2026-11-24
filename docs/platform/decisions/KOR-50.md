# KOR-50 decisions (model access with fallback, visible quotas, DFX access on own hardware)

Design: [ADR-080](../adr/080-model-access-fallback-and-dfx-access-on-own-hardware.md); specification
[05-runner.md §10.7–§10.9](../spec/05-runner.md#107-fallback-exhaustion-and-waits-adr-080-sections-15),
[12-billing.md §4a](../spec/12-billing.md#4a-case-3-the-provider-key-of-an-org-device-adr-080-7), runbook
[model-access.md](../runbooks/model-access.md). Rules [E-02](../KONZEPT.md#e-02), [E-04](../KONZEPT.md#e-04),
[E-05](../KONZEPT.md#e-05), [E-07](../KONZEPT.md#e-07), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19);
[§4 Punkt 4](../KONZEPT.md#k-4-b4), [§6 Punkt 2](../KONZEPT.md#k-6-b2), [Idee 45](../KONZEPT.md#k-idee-45),
[Idee 46](../KONZEPT.md#k-idee-46), interpretation [A4](../KONZEPT.md#a-4).

1. **Variant (i) for DFX access on own hardware: a limited provider key per organization device.** The relay through
   a platform device (variant ii) would carry every prompt of own hardware in plaintext through a DFX device and
   either through the hub or a public endpoint – against the zero-knowledge rule and the network rules. The key per
   device keeps the main key on the platform device (only the management key creates keys and it never leaves),
   measures with the provider's own figures, bounds the risk per key by the provider's limit and keeps own hardware
   working while DFX devices are down. One key per organization shared by its devices was refused: a removed device
   could keep spending.
2. **Two cost classes, one crossing rule.** `own` (own subscription, own API key) and `dfx` (operator subscription,
   platform access, provider key). Inside a class the platform falls back by itself and deterministically; from `own`
   to `dfx` never silently – only with the owner's setting or a person's consent for the task. The existing rule "no
   silent fallback for an explicit preference" stays.
3. **Default settings.** `fallback.own_to_dfx` **off** (paid access only by choice, E-11), `fallback.operator_to_platform`
   **on** (both are DFX access and charged; waiting would only stop the work, E-02). Both belong to the user, are sent
   with every session of the owner in the device state, and an older hub that sends none means these defaults.
   `PUT /v1/me/settings` without `fallback` keeps the stored settings, so older clients cannot reset them; the web and
   the agent tool still send the stored values back with every other change.
4. **Thresholds.** Provider check at most once per access per 60 s; a streak of 3 `429` records within 300 s without
   a success marks an access whose quota is not readable, until now + 900 s; `own` credentials that cannot be used
   right now are retried after 300 s; a `402` of a provider key marks it for 120 s (two issuer polls). A single `429`
   marks nothing (Claude Code retries it); `weekly:<model>` windows do not count (Claude Code changes the model itself).
5. **Headroom 5 USD.** The hub divides an organization's balance (÷ the `platform` factor) over its keys, at most
   `AGENT_HUB_DFX_KEY_HEADROOM_USD` (5) each; the issuer caps it once more with its own
   `AGENT_RUNNER_DFX_KEY_MAX_HEADROOM_USD` (5), so a compromised hub cannot raise a key beyond that.
6. **Booking of a provider key per organization and device, split by session cost.** The issuer reports the
   provider's cumulative spend; the hub books only the difference to the highest figure booked (idempotent across
   repeats, failovers and crashes) with the factor `platform`, split over that device's sessions by the provider cost
   they reported since the last booking (largest remainder); a share without reported session cost stays with the
   organization and the device. The session usage items of such sessions are not charged themselves (no double
   booking).
7. **Visible quotas per organization, honest where unreadable.** `GET /v1/orgs/{org}/quotas` for every member; the
   operator's subscriptions appear by the file name the operator chose, only when the organization may use them. "Not
   readable" is shown with the last observed limit time; nothing is estimated. The situation view's counter is hidden
   while the organization has no subscription to list.
8. **The task view shows the newest switch** from the audit row `session.model_switched` (members read the rows of the
   workgroups they may read); a separate event kind was not needed.
9. **Agent tool (E-04).** `agent-platform quotas --org`, `settings set --fallback on|off --fallback-platform on|off`
   and `task model-consent --task` (the existing group `task`; ADR-080 §5 writes it `tasks`). The status entries of
   the situation view carry `wait_until`.
10. **Guest unchanged.** The runner learns the release time from the usage endpoint, not from the `429` headers; the
    guest journal carries the status only.
11. **Owner's decisions D11 and F2 are not yet entered as interpretations in part C of `KONZEPT.md`** (it ends at
    A18): this package follows the draft decisions – D11 (DFX access on own hardware through a limited provider key)
    and F2 (operator subscriptions stay on platform devices; no token of the pool ever reaches an organization
    device). They are to be entered there as interpretations before the merge; this package does not edit `KONZEPT.md`.
