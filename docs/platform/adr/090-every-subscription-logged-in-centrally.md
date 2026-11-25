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
  hold up to `MAX_SUBSCRIPTIONS` (40) of them; the owner is always a user (no company subscriptions). The hub sees
  what it sees of every record (ID, owner, kind, version, epoch, times) plus the primary mark – never a token, an
  address, a name or a note.
- **Primary record.** As for GitHub identities, a user has at most one primary `claude_subscription` record; the
  existing record of every user is primary (migration), so nothing changes for anybody. The endpoints from before
  (`/v1/me/credentials/claude_subscription`, `/v1/device/credentials/{owner}/claude_subscription`, the rotation, the
  agent tool's `claude connect`) address the primary record only. The primary record is the one that serves the
  owner's **own sessions** (05-runner §10.1, unchanged). Further subscriptions are created **without** the mark and
  serve no session at all until the owner switches them on (§3): the default is "measure only". The owner moves the
  mark with `PUT /v1/subscriptions/{id}/primary` (`{primary: true|false}`; `false` removes it, so no subscription
  serves the own sessions). Deleting the primary record promotes none.
- **Body** (`contracts/credentials/claude_subscription.json`, additive): `name` (≤ 64 characters, the label the owner
  sees), `note` (≤ 500, what it is for), `use` (§3). `token` becomes optional: a record without a token is a
  subscription that is **not logged in** (after "log out", or created before its first login); it serves nothing and
  is not measured. Name, note, address and use are sealed like the token: only the owner and the owner's eligible
  devices read them (personal data, E2E like every credential).
- **Rotation** of the owner's `CK`: as for GitHub (ADR-078 §1), the rotation must contain the primary record;
  non-primary subscription records may be left out and keep their epoch until a current client re-seals them
  (`agent-platform subscriptions rewrap`, run after every rotation by the tool).

### 2. Login on the platform device, renewal by the device (stage a)
- `claude.login` (05-runner §11.2/§11.3) takes optional `subscription_id` (the record to fill; a new UUIDv7 chosen by
  the client creates it), `name` and `note` (taken for a new record; for an existing one only when given) and, for
  `automatic`, optional `email` (the address to sign in with when it is not the mailbox's own address, e.g. an alias
  delivered into it). With `subscription_id` the device reads the current record (if any), keeps its name, note and
  use, puts the new token and address and stores it with `PUT /v1/device/subscriptions/{id}` while answering the
  request; without it everything is as before (the primary record). The authorization happens in the owner's own
  browser (link and code as today) or by the mailbox automation; the token never touches a computer of the user.
- A setup token lives a year and has no refresh token; "renewal by the device" is therefore the device's own new
  login (the relay or the automation, started by the owner or the owner's agent with one command), never a copy from
  elsewhere. A token the provider refuses shows as "login refused" (§4) and leads to "log in again".
- **Log out** writes the record without `token` (name, note and use stay; author the owner). The provider offers no
  revocation of a setup token that the platform could call; the result names where the owner revokes it at the
  provider. **Remove** deletes the record (hard delete, as every record).

### 3. Use per subscription, default "measure only" (stage b)
- `use {pool: bool, orgs: [org_id]}` in the sealed body says which sessions besides the own ones (primary mark) may
  use the subscription: `orgs` names organizations explicitly (≤ 50); `pool: true` puts it into the operator's pool
  for the organizations the root-signed platform log grants the operator's subscriptions (ADR-079) and counts only
  when the owner holds the platform role `operator` in the verified platform log (for anybody else the root's list is
  not the owner's switch). Missing or empty `use` = measure only. A user never gets someone else's subscription unless
  its owner switched it on for that very organization (or, as the operator, for the pool).
- **The runner trusts only the sealed body.** The hub keeps a plaintext mirror (`hub.subscription_use`: record,
  organization, pool) that the owner's client sends with the record write (`PUT /v1/subscriptions/{id}` `use`), only
  to know which devices to tell. The desired state of a device lists `shared_subscriptions: [{subscription_id,
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
- The elected device of an owner (`quota_poll_owners`) polls, besides the primary record as before, every
  non-primary record that holds a token, every 900 s with the rules of 05-runner §10.6, and sends `quota {owner_id,
  subscription_id, …}` (additive; only to a hub whose device state carries `subscriptions.api >= 1`). The hub keeps
  the newest reading per record (`hub.subscription_quota`, dropped with the record) and serves
  `GET /v1/me/subscriptions`: per record the metadata, the primary mark, the measuring device, the last reading
  (windows and `fetched_at`) or `unreadable` with its time, and whether the record has a hub-visible use (stage b).
  "Not logged in" is known to the owner's client only (the token is sealed); the client shows it with "log in now".
- The KOR-68 overview shows these readings beside the reported ones; equal `key` = the record ID.

### 5. Moving off the token files (stage b)
The token files stay readable as long as `OPERATOR_SUBSCRIPTION_DIR` is configured. Once every subscription of the
operator is logged in centrally with `use.pool` (or the organizations), the operator removes the variable; at the
next daemon start the pool holds only shared subscriptions, and every session bound to a file moves in place to a
usable shared subscription at its next renewal (no restart of a session, no new prompt). The process on the operator's
computer that renews and copies the files is then switched off.

## Consequences
- The hub additionally sees how many subscriptions a user holds, which is primary, the time and device of each
  reading and, for shared ones, which organizations may use them (04-crypto §7). A hub that drops a mirror row only
  hides a subscription; it cannot let any organization use a subscription its owner did not switch on.
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
