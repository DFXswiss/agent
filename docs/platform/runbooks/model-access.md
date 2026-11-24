# Model access: fallback, quotas and DFX access on own hardware

Used-up subscriptions are passed over within their cost class, waits name their release time, quotas are visible per
organization, and organization devices can use DFX model access through a limited key of the model provider
([ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md), [05 §10.7–§10.9](../spec/05-runner.md#107-fallback-exhaustion-and-waits-adr-080-sections-15),
[12-billing.md §4a](../spec/12-billing.md#4a-case-3-the-provider-key-of-an-org-device-adr-080-7)). Devices are named
by role only: hub device, platform device (a runner device of kind `platform`), organization device.

## 1. Release order

Everything is additive; an older runner keeps working against the new hub and a new runner sends nothing new to an
older hub (it waits for `model_fallback.api` in the device state).

1. **Hub** of this release: `agent-hub migrate` (migration `0025_model_fallback` adds columns with defaults and new
   tables only), then restart. From now on the device state carries `model_fallback: {api: 1}`, every user's settings
   carry `fallback` (own subscription → DFX access off, operator subscriptions → DFX access over OpenRouter on), and
   `GET /v1/orgs/{org}/quotas` answers.
2. **Runners** of this release on every **platform device** first (they confirm exhaustion, move and pause sessions,
   poll the operator's subscriptions and send `operator_quota`), then on the organization devices (they accept a DFX
   key and pause their `platform` sessions when the credit is used up). The runner migrations `0023_model_fallback`
   and `0024_dfx_key` run at start.
3. **Web client**: comes with the hub; nothing to do.
4. Optional, only for DFX access on own hardware: the management key (section 2). Without it organization devices
   simply have no DFX access, as before.

Rollback: stop at the step that misbehaves. A runner of the previous release ignores the new fields; the hub's new
columns and tables stay unused.

## 2. The provider's management key (DFX access on own hardware)

Billing must be active (a price list in force, [credit.md](credit.md)), so that every key's spend can be booked.

1. In the model provider's account of the **organization** kind (a personal account's management key is refused by
   the key routes), create a **management key**. It can only create, read, change and delete keys; it is never sent
   anywhere and never leaves the platform device.
2. On **one or more platform devices** (the hub elects the online one with the smallest device ID as issuer; a second
   one takes over on failover), as the runner account:

   ```sh
   umask 077
   printf '%s\n' "$MANAGEMENT_KEY" > "$HOME/openrouter-management.key"   # the value from the provider, not typed on a shared screen
   chmod 0600 "$HOME/openrouter-management.key"
   ```

   and set in the runner's environment `AGENT_RUNNER_OPENROUTER_MANAGEMENT_KEY_FILE=<that path>`. Optional:
   `AGENT_RUNNER_DFX_KEY_MAX_HEADROOM_USD` (default 5) – the most any key may spend beyond its reported spend, a cap
   the hub cannot raise. Restart the runner. An organization device refuses to start with this variable.
3. On the hub, optional: `AGENT_HUB_DFX_KEY_HEADROOM_USD` (default 5) – the headroom per key; the hub divides each
   organization's balance over its keys and never gives a key more than this. Restart the hub after a change.

Within a minute the issuer announces itself; for every active organization device of an organization with credit the
hub lists a request, the issuer creates one limited key per device (name `dfx-<8 characters>`), seals it to that
device and forgets it. The organization device then offers `platform` access. Every minute the issuer reads each key's
spend; the hub books the difference to what it booked before as a `tokens` charge with the factor `platform`.

Replace the management key: write the new value to the same file atomically (temporary file in the same directory,
then `mv -f`), restart the runner, then delete the old key at the provider. Existing per-device keys stay valid.

Withdraw DFX access on own hardware: remove the variable and restart the issuing runners. Keys already issued keep
working until their limit; to stop them at once, delete the `dfx-…` keys at the provider – the organization devices
then answer `402` and their sessions wait for model access.

## 3. Checks

- `agent-platform quotas --org <org_id>` (or the settings section "Model access and quotas") shows the own and the
  operator's subscriptions with state and next release, "not readable" with the last observed limit, and whether DFX
  access is available.
- A used-up operator subscription: running sessions move to the next file within one reconcile (about 5 s); with every
  file used up and `operator_to_platform` on, they continue on DFX access; the task view shows "Model switched".
- A used-up own subscription without the setting: the situation view shows "Your subscription is used up until
  14:05 – continue with DFX access?" (or "Waiting for model quota – continues at 14:05" without DFX access); the task
  continues by itself at that time.
- DFX access on own hardware: the organization's consumption grows with token charges of the organization device
  (`agent-hub billing show --org <org_id>`); the provider's key list shows one `dfx-…` key per organization device,
  each with a limit a few dollars above its spend.
- A runner restart keeps used-up and blocked subscriptions (`runner.access_limit`); nothing has to be re-learned.
