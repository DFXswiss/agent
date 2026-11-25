# ADR-079: No approval as a precondition – the register of prerequisites, root-signed switches and lists, and every missing prerequisite is started

Status: Accepted (2026-11-24). Amends ADR-026 (platform access), ADR-052 (operator subscriptions), ADR-053 (protected
branches no longer protect the operator's account on the platform side), ADR-075 §7 (operator's waiting list) and
ADR-076 §5 and §8 (notifications, actions).

## Context
- Rule [E-11](../KONZEPT.md#e-11): what the user or the connected account may do, the AI may do too, without anything
  being entered, unlocked or confirmed first; restricting settings are voluntary options, never a precondition, and
  every place where a task fails for a missing approval is a defect – unless it is technically required or protects
  third parties (then [E-05](../KONZEPT.md#e-05) applies). Rule E-05: a missing connection or precondition is
  detected at once and its step is started, not explained; it is the pattern for every missing precondition. Rule
  [E-19](../KONZEPT.md#e-19): a class of defects is excluded by a lasting check, not fixed in one place.
- Concept: entry without configuration ([Grundprinzip](../KONZEPT.md#k-gp)); DFX provides execution servers by
  default ([§3 Punkt 3](../KONZEPT.md#k-3-b3)); without own credentials and hardware the platform uses DFX servers
  and DFX model access ([§4 Punkt 4](../KONZEPT.md#k-4-b4)); costs are protected by the balance
  ([Idee 47](../KONZEPT.md#k-idee-47), [E-14](../KONZEPT.md#e-14)); new users join only by invitation
  ([§1 Punkt 1](../KONZEPT.md#k-1-b1), [E-18](../KONZEPT.md#e-18)).
- Built state (inventory, `KONZEPT.md` part D): a team organization needed the root-signed `org_platform_access`
  before it could use a platform device (ADR-026; since KOR-41 its tasks wait `platform_access_required`); platform
  invitations needed the platform role `inviter`; the operator's subscriptions served only the organizations listed in
  the runner's environment (`OPERATOR_SUBSCRIPTION_ORGS`, ADR-052), changed only with a daemon restart; `main`,
  `master`, `develop` and `release/*` were protected even when the workgroup set nothing
  ([A17](../KONZEPT.md#a-17)); a session without model access failed `model_access_unavailable`, an empty balance
  waited `credit_exhausted` and a workgroup without a device waited `no_device` – each only shown.
- Open questions of the owner: F1 (a start credit for every new organization instead of the approval – money) and F2
  (the operator's subscriptions for organizations outside the operator's own – contract). Until they are answered the
  approvals stay in force; this ADR builds the switches and the ways, and the operator flips them.
  *F1 answered on 2026-11-25 ([E-14](../KONZEPT.md#e-14)): there is no automatic start credit; credit is only bought
  or distributed by the operator by hand. The setting stays 0; a start credit is not pursued.*

## Decision

### 1. The register of prerequisites (E-19)
- Specification chapter [`15-prerequisites.md`](../spec/15-prerequisites.md) is the register: one row for every code
  with which a task, a session, an operation or a request can wait or be refused – the scheduling reasons, the session
  failure reasons, the operation error codes, the hub's error codes, the reasons a runner holds an operation for, and
  every code the hub or the runner raises (scan below). Each row names the class of its justification –
  `technical`, `third_party`, `option` (a restriction chosen voluntarily, a setting with a generous default),
  `rights` (the rights model of [§8](../KONZEPT.md#k-8)), `credit`, `input` – and the **handling**: the step the
  platform starts (E-05) and who starts it. There is no class "approval": a row that needs one is a defect.
- `tests/platform/test_prerequisites.py` keeps it complete: every code of the contract lists and of the hold reasons,
  and every code the hub and the runner raise as `HubError`, `CheckFailed`, `StartFailed`, `NoPlacement`,
  `AccessRefused`, `Refused` or `Rejected` – a literal or a module constant, resolved through the AST, also across
  `from … import` – must have exactly one row with a known class, a justification and a handling; a row without a
  source fails as well. A new waiting or refusal code is red until it is registered.

### 2. Switches and lists are root-signed platform-log entries
A switch or list that decides who may use the operator's resources is never a hub setting: the runner must be able to
check it itself (AUD-03 L13; KOR-55 anchors exactly these entries in the runner). New platform-log entry types
(04-crypto.md §4.8, contracts `log/platform/*.json`), appended only through the operator CLI:

| Type | Body | Effect |
|---|---|---|
| `platform_policy` | `{name, on}`; `name` ∈ `open_platform_access`, `open_platform_invites` | a switch; the latest entry of a name counts; without one it is off |
| `org_operator_subscriptions` | `{org, granted}` | the organization's members use the operator's subscription pool (ADR-052); the latest entry of an org counts |

The platform role `operator` joins `inviter` (`role_grant` / `role_revoke`): users with it receive the requests of §4.
The state gains `policies` and `operator_subscriptions` (both in `projection()` and the shared vectors). Hub, runner
(copy of the reference), web client and agent tool apply the same rules; an entry that is not signed by the pinned
root is `log_signature` and stops the log like every other entry (P4).

Compatibility as for `user_certify` (ADR-060), but wider: organization devices verify the platform log too
(`reconcile/logs.py`), so the hub refuses these entries and a `role_grant`/`role_revoke` of `operator`
(`409 peer_outdated`) until **every active device** – platform and organization – announced the feature `log.policy`.
Web client and agent tool of the same release know the entries; an older agent tool stops at them (`log_type`) until it
is updated.

Operator CLI (`agent-hub platform …`): `set-policy NAME on|off`, `grant-operator-subscriptions --org ID [--revoke]`,
`grant-role|revoke-role inviter|operator --user ID`, `settings` (the switches and lists of the log verified with the
operator's own key – never the hub's word) and `waiting` (now also organizations waiting for credit).

### 3. What the switches change
- **Platform access (a).** `platform_access(org, private)` = private, else the org's latest `org_platform_access` when
  there is one, else the switch `open_platform_access`. With the switch on, every organization without an explicit
  entry may assign platform devices; an explicit `granted:false` stays the way to exclude one organization. The hub
  keeps the latest explicit entry per org (`org.platform_access_grant`) and the switches (`hub.platform_policy`, both
  migration `0023_no_approval`) and recomputes `org.platform_access` of every organization when the switch changes;
  the projection rebuild does the same. Turning the switch **off** again takes access from every organization without
  an explicit grant at once (new assignments are refused; existing ones stay until an admin removes them, as ADR-026).
- **Invitations (f).** With `open_platform_invites` on, every user may create platform invitations
  (`POST /v1/platform/invites`, the screen, `agent-platform platform invite`); a user without the role lists and
  revokes only its **own** invitations. The invitation itself stays mandatory (§1 Punkt 1, E-18):
  `platform_invite_required` (b) is unchanged.
- **Operator subscriptions (c).** The runner serves a session from the pool when the session's organization is
  granted by its latest `org_operator_subscriptions` in the **verified platform log**; an organization without such
  an entry is served while `OPERATOR_SUBSCRIPTION_ORGS` lists it (the move into the log; **an entry of the log wins**,
  also a revocation). A change takes effect at the runner's next platform-log refresh (seconds), without a restart: a
  grant serves the next start, a revocation withdraws the access of bound sessions at the next renewal (ADR-052 §7).
  `OPERATOR_SUBSCRIPTION_DIR` alone is now valid; the list without it stays `config_invalid`. The move ends when every
  listed organization has an entry in the log; then the variable is removed at the next restart. All members of a
  granted organization use the pool without an approval of their own; since E-18 an admin can add members to a
  granted organization without the operator, which widens who uses the operator's personal subscriptions – part of
  F2. The grants are visible to every verifier of the platform log (web clients, devices): which organizations use
  the operator's subscriptions is not secret.
- **Operator GitHub account (d)** is not changed here (KOR-47 makes it the organization's account).
- **Protected branches (e), A17.** No default: a workgroup without settings protects no branch on the platform side;
  GitHub's own branch protection applies (GitHub refuses, the operation ends with GitHub's answer). A workgroup may set
  its own list (`branch_protected` as before); the web form and the agent tool no longer prefill one, so saving other
  settings never brings the old list back. An unreadable settings event still refuses every repository (fail closed).
  ADR-053 named the default list among the protections of the operator's GitHub account: that protection now comes
  from GitHub alone (A17).

### 4. A missing prerequisite is started, not explained (E-05)
- **Model access (g).** With `auto` a platform device takes the DFX model access whenever the balance allows it (as
  before). When a session start still finds no model access – an explicit own-access preference without the
  credential, or an organization device without own credentials (DFX access on own hardware is KOR-50) – the start
  fails at once with `model_access_unavailable`. The task's chat shows its owner a card that starts the connection of
  the Claude subscription (others see that the owner was asked; the situation view's action opens the same settings).
  When a model credential (`claude_subscription`, `anthropic_api_key`) of the owner is stored – by the owner
  (`PUT /v1/me/credentials/{kind}`) or by a device answering the owner's request (`PUT /v1/device/credentials/…`, the
  subscription login of ADR-011) – the hub creates, in the same transaction, the next attempt of every open task of the
  owner whose newest main session failed for this reason with prompts pending (the rules of the capacity retry of
  ADR-049: not stopped by the user, prompts pending): a new main session whose first transition carries
  `model_access_unavailable` and takes over the undelivered prompts (`agent_hub.sessions.prompts` treats both retry
  reasons alike, so every prompt is delivered exactly once). A credential stored while the start ran (newer than the
  failed session) gets its attempt in the transaction that stores the failure. Each stored credential leads to at most
  one attempt per task, so a device that cannot open the credential yet does not loop. The task leaves `failed` with
  the attempt, so "Try again" disappears with it. An explicit preference is a voluntary choice of the user (E-11) and
  is never silently replaced by paid DFX access.
- **Balance (h).** When a task's attention record enters `credit_empty`, every user with the platform role `operator`
  gets an `operator.request` (account level, no organization, `ref` = `credit_exhausted:<org>`): once per
  organization and occasion – not again while an earlier request of that organization is newer than its last
  `credit.changed` – and, against a flood of organizations, not while an earlier request of the same reason is unread
  (the waiting list names every organization). The state is in the database (the notification rows and the audit),
  not in a process. The person sees "credit has been requested"; there is no payment way (E-14). Without any operator
  the hub logs `operator_request_unanswered`. Organization names are end-to-end encrypted, so the operator sees the
  organization's ID; the need (number of waiting tasks, since when) is in `agent-hub platform waiting`.
- **Platform access with the switch off (a).** A task waiting `platform_access_required` asks the operators the same
  way (`ref` = `platform_access_required:<org>`; answered by a later `org_platform_access` of the org or any
  `platform_policy`).
- **No device.** An admin's client assigns the first certified platform candidate before it sends a task to a
  workgroup without a device (the web client did this already, `ensureDevice`; `agent-platform task create` now does
  it too). Otherwise a task waiting `no_device` notifies every admin of the organization except the task owner (who
  has `task.attention`) once per workgroup while the earlier such notification is unread (`device.needed`, `ref` = the
  workgroup); the admin's next step "Assign a computer" is offered at the notification.

### 5. Agent access (E-04)
`agent-platform platform settings` (the switches, the organizations using the operator's subscriptions and the
caller's platform roles of the verified platform log), the notifications through `agent-platform notifications list`
(new kinds), the model card through `agent-platform claude connect` (the continuation is the hub's) and the automatic
assignment through `agent-platform task create`. Operator commands are in the operator CLI (§2). Rows in
`agent-parity.md`.

### 6. What a taken-over hub can do (trust)
- It cannot forge a switch, a list entry or the role `operator`: every verifier checks the root signature.
- It can **withhold** new platform-log entries – a switch turned off, a `granted:false`, a revoked subscription – from
  a runner, which then keeps serving with the older state; the runner has no freshness bound for the platform log
  (KOR-55 anchors the runner's view).
- It can suppress or forge `operator.request` and `device.needed` notifications; they are hints, never authority: the
  operator acts through its own CLI on the log it verified itself, and `waiting` lists what the hub reports.
- This ADR closes AUD-03 L13 for team organizations and the subscription list. Two paths remain hub-trusted and
  belong to KOR-55: a `private` organization (the invitation check behind it is the hub's) and the daily platform
  budget and the balance, which the runner receives from the hub.

## Consequences
- Nothing changes for the owner until a switch is flipped: both switches are off, the operator-subscription list moves
  from the environment into the log without interruption (runner update first – it honours both – then
  `grant-operator-subscriptions` for each listed organization, then `OPERATOR_SUBSCRIPTION_ORGS` can be removed at the
  next restart).
- **Costs (F1).** With the default start credit of 0 (`AGENT_HUB_BILLING_START_CREDIT_USD`) the switches cost nothing:
  an organization without credit is never placed on a platform device (KOR-31). A start credit is granted per new
  organization; with the switches on, one person can create organizations (team organizations need no invitation;
  private ones one invitation each, and with `open_platform_invites` everybody can invite) and so multiply the start
  credit. Before a start credit above 0 is combined with the switches, the owner's answer to F1 must fix a bound
  (a start credit per founder or per invitation chain is the recommendation; billing logic is KOR-31's).
  *Not pursued:* F1 is answered with no start credit ([E-14](../KONZEPT.md#e-14)), so the bound is not needed.
- The protected-branch default is gone at once: a workgroup that relied on it without saving settings has no
  platform-side protection; GitHub's protection remains.
- An old runner, organization device or agent tool stops at the new entry types; the `log.policy` gate keeps them out
  until every active device runs this release.

## Alternatives considered
- **Hub settings** for the switches: the runner could not verify them (AUD-03 L13).
- **Automatic approval by a hub job**: still an approval, and a hub decision the runner cannot check.
- **Silent fallback to paid DFX access for an explicit own-access preference**: it would spend credit against the
  user's explicit choice.
- **A runner-side hold of the session start until the credential arrives** (like ADR-066 for GitHub operations): it
  would keep a VM slot and a starting session for an unbounded time; the hub's next attempt holds nothing.
- **One `operator.request` per organization without the unread rule**: a flood of organizations would flood the
  operator; the waiting list carries the complete picture.

## Deviation from DESIGN.md
None beyond ADR-001.
