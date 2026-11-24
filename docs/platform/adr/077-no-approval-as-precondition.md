# ADR-077: No approval as a precondition – the register of prerequisites, signed platform switches and lists, and every missing prerequisite is started

Status: Accepted (2026-11-24)

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
  ([Idee 47](../KONZEPT.md#k-idee-47), [E-14](../KONZEPT.md#e-14)); new users, customers and devices join only by
  invitation ([§1 Punkt 1](../KONZEPT.md#k-1-b1), [E-18](../KONZEPT.md#e-18)).
- Built state (inventory, `KONZEPT.md` part D): a team organization needs the root-signed `org_platform_access`
  before it may use a platform device (ADR-026; since KOR-41 its tasks wait `platform_access_required`); platform
  invitations need the platform role `inviter`; the operator's subscriptions serve only the organizations listed in
  the runner's environment (`OPERATOR_SUBSCRIPTION_ORGS`, ADR-052), changed only with a daemon restart; `main`,
  `master`, `develop` and `release/*` are protected even when the workgroup set nothing ([A17](../KONZEPT.md#a-17));
  a session without model access fails `model_access_unavailable`, an empty balance waits `credit_exhausted` and a
  workgroup without a device waits `no_device` – each only shown, nobody is asked to act.
- Interpretation [A17](../KONZEPT.md#a-17) (protected branches) and the open questions F1 (start credit instead of
  approval: money) and F2 (operator subscriptions for customers outside DFX: contract) of the owner. Until the owner
  answers F1 and F2 the approvals stay in force: this ADR builds the switches and the ways, and the operator flips
  them.

## Decision

### 1. The register of prerequisites (E-19)
- Specification chapter [`15-prerequisites.md`](../spec/15-prerequisites.md) is the register: one row for every code
  with which a task, a session, an operation or a request of a person or an agent can wait or be refused – the
  scheduling reasons (`contracts/enums/task-states.json`), the session failure reasons
  (`session-failure-reasons.json`), the operation error codes (`operation-error-codes.json`), the session waiting
  reasons (`session.status` `waiting_for`), and the hub error codes answered with `403`. Each row names the class of
  its justification – `technical` (the step cannot work without it), `third_party` (protects other people, their
  data or their money), `option` (a restriction a user or admin chose voluntarily, E-11), `rights` (the rights model
  of the concept, [§8](../KONZEPT.md#k-8): reading and writing per workgroup, admin), `credit` (the balance, Idee 47),
  `input` (the request itself is wrong; the agent corrects it) – and the **handling**: the step the platform starts
  (E-05) and who starts it. There is no class "approval": a row that needs one is a defect.
- `tests/platform/test_prerequisites.py` keeps it complete: every code of the four contract lists and of the `403`
  codes, and every code literal raised in the hub and the runner as `HubError`, `CheckFailed`, `StartFailed`,
  `NoPlacement` or `AccessRefused`, must have exactly one row with a known class and a non-empty handling; a row
  without a code in these sources fails as well. A new waiting or refusal code is therefore red until it is
  registered with its justification and its triggered step.

### 2. Switches and lists are root-signed platform-log entries
A switch or list that decides who may use the operator's resources is never a hub setting: the runner must be able to
check it itself, or a taken-over hub could give platform devices and the operator's subscriptions to any
organization (AUD-03 L13; KOR-55 anchors exactly these entries in the runner). New platform-log entry types
(`04-crypto.md` §4.8, contracts `log/platform/*.json`), appended only through the operator CLI:

| Type | Body | Effect |
|---|---|---|
| `platform_policy` | `{name, on}`; `name` ∈ `open_platform_access`, `open_platform_invites` | a switch; the latest entry of a name counts; without one it is off |
| `org_operator_subscriptions` | `{org, granted}` | the organization's members use the operator's subscription pool (ADR-052); the latest entry of an org counts |

and the platform role `operator` beside `inviter` (`role_grant` / `role_revoke`): users with it receive the requests
of §4 (`operator.request` notifications). The state (`PlatformState`) gains `policies` and `operator_subscriptions`;
`projection()` lists both. Verifiers – hub, runner (copy of the reference), web client, agent tool – apply the same
rules; a switch entry that is not signed by the pinned root is `log_signature` and stops the log like every other
entry (P4). Compatibility as for `user_certify` (ADR-060): the hub refuses these entries and `role_grant` of
`operator` (`409 peer_outdated`) until every active platform device announced the feature `log.policy`; the web
client and the agent tool of the same release know them.

Operator CLI (`agent-hub platform …`): `set-policy NAME on|off`, `grant-operator-subscriptions --org ID [--revoke]`,
`grant-role operator|inviter --user ID`, `revoke-role operator|inviter --user ID`, and `settings` (prints the
switches, the organizations with an explicit platform access and the operator-subscription list of the verified log).

### 3. What the switches change
- **Platform access (a).** `platform_access(org, private)` = private, else the org's latest `org_platform_access`
  when there is one, else the switch `open_platform_access`. With the switch on, every organization without an
  explicit entry may assign platform devices (`device_assign` / `device_assign_org` with `platform_seq`) and its
  tasks no longer wait `platform_access_required`; an explicit `granted:false` stays a way to exclude one
  organization. The hub recomputes `org.platform_access` for every organization when a switch entry is appended.
  Costs stay protected by the balance (KOR-31: an organization without credit is never placed on a platform device)
  and the start credit of F1 (`AGENT_HUB_BILLING_START_CREDIT_USD`).
- **Invitations (f).** With `open_platform_invites` on, every certified-or-registered user may create platform
  invitations (`POST /v1/platform/invites`, the screen and `agent-platform platform invite`), not only holders of
  `inviter`. The invitation itself stays mandatory (§1 Punkt 1, E-18): `platform_invite_required` (b) is unchanged
  and registered as `third_party`.
- **Operator subscriptions (c).** The runner serves a session from the pool when the session's organization is
  granted in the **verified platform log** (`org_operator_subscriptions`) or – during the move only – listed in
  `OPERATOR_SUBSCRIPTION_ORGS`. A change takes effect at the runner's next platform-log refresh (the reconcile loop,
  seconds), without a restart: a new grant serves the next start, a revocation withdraws the access of bound
  sessions at the next renewal (as ADR-052 §7). `OPERATOR_SUBSCRIPTION_DIR` alone is now a valid configuration (the
  list comes from the log); `OPERATOR_SUBSCRIPTION_ORGS` without the directory stays `config_invalid`. All members
  of a granted organization use the pool, none needs an approval of their own. Who outside the operator's own
  organization is granted is the owner's question F2; until then the same organizations as today.
- **Operator GitHub account (d)** is not changed here (KOR-47 makes it the organization's account).
- **Protected branches (e), A17.** No default: a workgroup without settings protects no branch on the platform side;
  GitHub's own branch protection of the repository applies (GitHub refuses, the operation fails with GitHub's
  answer). A workgroup may set its own list (`settings.workgroup` `protected_branches`), which is checked as before
  (`branch_protected`). An unreadable settings event still refuses every repository (fail closed).

### 4. A missing prerequisite is started, not explained (E-05)
- **Model access (g).** With `auto` a platform device takes the DFX model access whenever the balance allows it (as
  before). When a session start still finds no model access (an explicit own-access preference without the
  credential, or an organization device without own credentials; DFX access on own hardware is KOR-50), the start
  fails at once with `model_access_unavailable` (detected immediately), the task's chat shows its owner a card that
  starts the connection of the Claude subscription (§8.2 of `06-web.md`, the same flow as the settings), and when
  the owner stores a model credential (`claude_subscription` or `anthropic_api_key`) the hub creates the next attempt
  of every open task of that owner whose newest main session failed for this reason with prompts pending – a new
  main session whose first transition carries `model_access_unavailable` and takes over the undelivered prompts, as
  the capacity retry of ADR-049 does. The task continues by itself; nobody repeats the instruction. An explicit
  preference is a voluntary choice of the user (E-11) and is never silently replaced by paid DFX access.
- **Balance (h).** When a task of an organization waits or stops for `credit_exhausted`, every user with the platform
  role `operator` gets one `operator.request` notification (account level, `ref` = `credit_exhausted:<org>`) per
  occasion – again only after a later `credit.changed` of that organization. The person sees "Credit has been
  requested" instead of an explanation; there is no payment way (E-14). The operator's list of waiting work
  (`GET /v1/platform/waiting`, `agent-hub platform waiting`) also lists organizations waiting for credit.
- **Platform access (a, switch off).** A task waiting `platform_access_required` sends the same `operator.request`
  (`ref` = `platform_access_required:<org>`) once per organization until an `org_platform_access` or switch entry
  follows.
- **No device.** A task waiting `no_device` (no device assigned, but the organization may assign one) notifies every
  admin of the organization once per workgroup while the earlier such notification is unread (`device.needed`, `ref`
  = the workgroup): the admin's next step "Assign a computer" (web §7.3, `agent-platform device assign`) is started,
  not only shown to the task owner.

### 5. Agent access (E-04)
Every new view and action has its command: `agent-platform platform settings` (the switches and the operator-
subscription list of the verified platform log, the caller's own platform roles), the notifications through
`agent-platform notifications list` (new kinds), and the model card through `agent-platform claude connect` (the
continuation is the hub's). Operator commands are in the operator CLI (§2). Rows in `agent-parity.md`.

## Consequences
- Nothing changes for the owner until he flips a switch: both switches are off, the operator-subscription list moves
  from the environment into the log without interruption (runner update first – it honours both – then
  `grant-operator-subscriptions` for each listed organization, then `OPERATOR_SUBSCRIPTION_ORGS` can be removed at
  the next restart).
- After F1 = yes: `set-policy open_platform_access on` and `set-policy open_platform_invites on`; the remaining
  `org_platform_access` grants keep their meaning (explicit wins).
- Removing the protected-branch default changes behaviour at once: a workgroup that relied on the default without
  saving settings no longer has platform-side protection; GitHub's protection remains.
- An old runner stops at the new entry types; the `log.policy` feature gate keeps them out until every platform
  device runs this release.

## Alternatives considered
- **Hub settings** for the switches: rejected – the runner could not verify them (AUD-03 L13).
- **A per-organization start approval kept, with automatic approval by a hub job**: still an approval, and the job
  would be a hub decision the runner cannot check.
- **Silent fallback to paid DFX access for an explicit own-access preference**: would spend the organization's credit
  against the user's explicit choice.
- **A runner-side hold of the session start until the credential arrives** (like ADR-066 for GitHub operations): it
  would keep a VM slot and a starting session for an unbounded time; the hub's next attempt holds nothing.

## Deviation from DESIGN.md
None beyond ADR-001.
