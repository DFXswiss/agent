# ADR-090: Every Claude subscription logged in centrally – several logins per owner, measured, used only where the owner switches it on

Status: Accepted (2026-11-25)

## Context
- Rule [E-31](../KONZEPT.md#e-31): every Claude subscription of a user is logged in on the platform, nothing runs on a
  computer of the user. [E-30](../KONZEPT.md#e-30) (KOR-68) shows the remaining quota of all of a user's subscriptions;
  [E-07](../KONZEPT.md#e-07) lets approved organizations use the operator's subscriptions; [E-04](../KONZEPT.md#e-04)
  and [E-27](../KONZEPT.md#e-27) require every step without a browser; [E-25](../KONZEPT.md#e-25) forbids new
  protection layers nobody ordered. Accesses of a person are never shared without that person's explicit switch.
- Built before: one `claude_subscription` record per user (`hub.credential`, unique per owner and kind except
  `github`), stored by a device answering `claude.login` (setup-token relay or mailbox automation,
  [ADR-011](011-claude-subscription-login.md), 05-runner §11) or by the user's agent tool
  ([ADR-056](056-delegated-agent-access.md)); it serves the owner's own sessions and its quota is polled by one
  elected device (05-runner §10.6). The operator's subscriptions are token files that a process on the operator's
  computer renews and copies to the platform device ([ADR-052](052-operator-subscriptions.md)); the organizations
  that may use them are root-signed in the platform log ([ADR-079](079-no-approval-as-precondition.md)).
- The pattern for several records of one kind exists: GitHub identities ([ADR-078](078-github-identities.md) §1).

## Decision

### 1. A subscription is a `claude_subscription` record (stage a)
- A **subscription** is one `claude_subscription` credential record of a user, named by its record ID. A user may
  hold up to `MAX_SUBSCRIPTIONS` (40, the size of the reported list of KOR-68) of them; the owner is always a user (no
  company subscriptions). The hub sees what it sees of every record (ID, owner, kind, version, epoch, times) plus the
  primary mark – never a token, an address, a name or a note.
- **The primary record stays as it is.** Every user's existing record is primary (migration), the endpoints from
  before (`/v1/me/credentials/claude_subscription`, `/v1/device/credentials/{owner}/claude_subscription`, the
  rotation, the web client, the agent tool's `claude connect` and `claude login`) address it only, and its body keeps
  the old form (`token`, `email`, `kind`, `expires_at`), because the deployed web client opens it, refuses any other
  form and would then block a rotation of the user's `CK`. It serves the owner's **own sessions** exactly as before
  (05-runner §10.1); its use is not switchable and it has no name or note (the overview shows it as "own sessions"
  with its address).
- **Further subscriptions** are created **without** the mark (`PUT /v1/subscriptions/{id}` by the owner, or a device
  answering `claude.login` with `subscription_id`) and never become primary: there is no endpoint that moves the mark
  (a hub-held mark could otherwise let the hub make a subscription serve sessions its owner did not choose). They serve
  no session until the owner switches them on (§3): the default is "measure only". The new endpoints refuse the
  primary record for writes (`forbidden`: it is written through the endpoints from before); reading and deleting it
  there is allowed.
- **Body** of a further subscription (`contracts/credentials/claude_subscription.json`, additive): `name` (≤ 64
  characters, the owner's label), `note` (≤ 500, what it is for) and `use` (§3). `token` becomes optional: a record
  without a token is a subscription that is **not logged in** (after "log out", or before its first login); it
  serves nothing and is not measured. Name, note, address and use are sealed like the token: only the owner and the
  owner's eligible devices read them (E2E like every credential).
- **Rotation** of the owner's `CK`: as for GitHub (ADR-078 §1) the rotation must contain the primary records, and
  further subscriptions may be left out (the deployed web client does not know them). They keep their epoch until
  they are re-sealed: the device that measures the owner (§4) re-seals every further subscription under an older
  epoch than the newest one it holds (version + 1, author the device, `PUT /v1/device/subscriptions/{id}` without a
  request: allowed only for a non-primary subscription whose stored epoch is older than the owner's current one, the
  new version under the current epoch); the owner's clients may do the same.

### 2. Login on the platform device, renewal by the device (stage a)
- `claude.login` (05-runner §11.2/§11.3) takes optional `subscription_id` (the further subscription to fill; a new
  UUIDv7 chosen by the client creates it), `name` and `note` (taken for a new record; for an existing one only when
  given) and, for `automatic`, optional `email` (the address to sign in with when it is not the mailbox's own
  address, e.g. an alias delivered into it). With `subscription_id` the device reads the current record (if any),
  keeps its name, note and use, puts the new token and address and stores it with `PUT /v1/device/subscriptions/{id}`
  while answering the request; without it everything is as before (the primary record). A runner that understands
  the new fields announces the feature `subscriptions` (ADR-060 decision 6); clients send them only to a device that
  announced it (`GET /v1/me/devices` lists the features), so an older runner never gets a body it refuses.
- The authorization happens in the owner's own browser (link and code as today) or by the mailbox automation; the
  token is created on the device and never sent to a computer of the user.
- A setup token lives a year and has no refresh token; "renewal by the device" is therefore the device's own new
  login (the relay or the automation, started by the owner or the owner's agent with one command), never a copy from
  elsewhere. A token the usage endpoint refuses shows as "not readable" (§4); a token the model endpoint refuses
  blocks the pool entry (§3) – both lead to "log in again".
- **Log out** and the changes of name, note and use are written by the owner's client (browser or agent tool) as a
  new version of the record (author the owner or the owner's own-identity agent): it opens the record in memory,
  keeps the token (or drops it for "log out") and seals it again; nothing of it is stored or shown on the user's
  computer, as for every credential today. The provider offers no revocation of a setup token that the platform could
  call; the result names where the owner revokes it at the provider. **Remove** deletes the record (hard delete).

### 3. Use per subscription, default "measure only" (stage b)
- `use {pool: bool, orgs: [org_id]}` in the sealed body says which sessions besides the own ones (primary mark) may
  use the subscription: `orgs` names organizations explicitly (≤ 50); `pool: true` puts it into the operator's pool
  for the organizations the root-signed platform log grants the operator's subscriptions (ADR-079) and counts only
  when the owner holds the platform role `operator` in the verified platform log (for anybody else the root's list is
  not the owner's switch). Missing or empty `use` = measure only. A user never gets someone else's subscription unless
  its owner switched it on for that very organization (or, as the operator, for the pool).
- **The runner trusts only the sealed body.** The hub keeps a plaintext mirror (`hub.subscription_use`: record,
  organization, pool) that the owner's client sends with the record write (`PUT /v1/subscriptions/{id}` `use`), only
  to know which devices to tell. A device that re-seals or logs in a record keeps its `use` (an eligible device can
  read the token anyway; trusting its `use` adds nothing). The desired state of a device lists `shared_subscriptions: [{subscription_id,
  owner_id}]` for the organizations it serves; the runner fetches each record by ID (`GET
  /v1/device/subscriptions/{id}`, eligible for the owner's `CK`), verifies it as every record and uses it for a
  session of organization O only when the sealed `use` allows O. A hub can hide a subscription, never widen its use.
  A device that cannot open the owner's `CK` cannot use the subscription (it is skipped).
- **Selection** (05-runner §10.1): the shared subscriptions join the operator pool of ADR-052 as a second source
  beside the token files. `auto`: own subscription (primary) → own API key → **pool** (token files for approved
  organizations, shared subscriptions whose `use` allows the session's organization) → `platform`. Within the pool the
  rules of ADR-052 and ADR-081 apply unchanged (most room, fewest bound sessions, binding to the source not the value,
  blocks on `401`/`403` until the source changes – a new login is a new record version –, exhaustion, cost class
  `dfx`, reported as `claude_subscription`). Pool entries of records are named `sub-<record ID>` in bindings, limits
  and logs; the `operator_quota` snapshot lists the token files only (records are measured per record, §4).
- A use switched off (new version without the organization) withdraws the subscription at the next renewal: a bound
  session moves to another usable pool entry in place, or waits/fails as for an exhausted pool (ADR-081).

### 4. Measurement and overview (stage a)
- The elected device of an owner (`quota_poll_owners`, now every owner with any subscription record) polls, besides
  the primary record as before, every further subscription that holds a token, every 900 s with the rules of 05-runner §10.6, and sends `quota {owner_id,
  subscription_id, …}` (additive; only to a hub whose device state carries `subscriptions.api >= 1`). The hub keeps
  the newest reading per record (`hub.subscription_quota`, dropped with the record) and serves
  `GET /v1/me/subscriptions`: per record the metadata, the primary mark, the measuring device, the last reading
  (windows and `fetched_at`) or `unreadable` with its time, and whether the record has a hub-visible use (stage b).
  "Not logged in" is known to the owner's client only (the token is sealed); the client shows it with "log in now".
  The usage endpoint may refuse setup tokens (05-runner §10.6); such a subscription shows "not readable" with the
  time of the refusal instead of windows – the readings are as good as the provider allows.
- The agent tool (E-04, E-27): `agent-platform subscriptions list` (every subscription with label, address, status
  – logged in, not logged in, not readable –, use, last reading and measuring device), `add --name [--note]
  [--automatic [--email]]` (creates the record through the login and answers the authorization link as `next`),
  `login --id` (log in again), `rename`, `note`, `use --id (--measure | [--pool] [--org …])`, `logout --id`,
  `remove --id`; each in the lookup (`words`, `example`, KOR-67) and in `agent-parity.md`.
- The KOR-68 overview shows these readings beside the reported ones; equal `key` = the record ID.

#### 5. Moving off the token files (stage b)
The token files stay readable as long as `OPERATOR_SUBSCRIPTION_DIR` is configured. Per account the operator logs
in centrally (`use.pool`) and then deletes that account's file, so no account sits in the pool twice (each entry
would count its own bound sessions). A session bound to a deleted file moves in place to another usable pool entry at
its next renewal (2 s; no restart of the session, no new prompt). With no file left, the operator removes the
variable; the daemon restart keeps every session (bindings are stored, the guests keep running). Then the process on
the operator's computer that renews and copies the files is switched off.

## Consequences
- The hub additionally sees how many subscriptions a user holds, which is primary, the time and device of each
  reading and, for shared ones, which organizations may use them (04-crypto §7). A hub that drops a mirror row only
  hides a subscription; it cannot let any organization use a subscription its owner did not switch on. Like every
  record, a hub can withhold a newer version from a device that never saw it (the device then still acts on the older
  `use`); a device that saw a version never goes back (no rollback). Bounding this further would be a new protection
  layer (E-25).
- A shared subscription is usable on the devices that hold its owner's `CK` – in practice the platform device that
  logged it in. That a subscription is shared with an organization is the owner's decision and responsibility (terms
  of use, as ADR-052).
- Roll-out: hub first (it accepts the old items and requests), then runners (they announce nothing new; they send the
  new item form only to a hub that announces it), then the agent tool; the web view follows KOR-68.

## Alternatives considered
- A use list at the hub only: a hub could hand any subscription to any organization (zero-knowledge hub, ADR-003).
- Sharing by copying the token into a record of each organization: copies of a personal access; a new login would
  have to be copied again.
- Keeping the file sync and only adding names: the token would still be renewed on the operator's computer (E-31).

## Deviation from DESIGN.md
None beyond [ADR-052](052-operator-subscriptions.md).
