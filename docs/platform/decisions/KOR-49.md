# KOR-49 decisions (no approval as a precondition)

Design: [ADR-079](../adr/079-no-approval-as-precondition.md); register: [15-prerequisites.md](../spec/15-prerequisites.md);
rules [E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19), interpretation
[A17](../KONZEPT.md#a-17).

1. **One entry type for both switches.** `platform_policy {name, on}` with a closed list of names instead of one type
   per switch: a further switch is one name more (contract enum, verifiers), not a new type with its own gate. Any
   value is allowed (an entry that repeats the current value is harmless and keeps the log simple to write).
2. **An explicit entry wins over the switch.** `org_platform_access` of an organization decides before
   `open_platform_access`: a `granted: false` stays the way to exclude one organization while the switch is on, and
   existing grants keep their meaning when the switch is turned off.
3. **`operator` is a platform role, not the root.** The root key is offline and is no user; requests need recipients
   who read notifications. The role is granted like `inviter` (`grant-role operator`), root-signed and visible to every
   verifier.
4. **Requests are aggregated.** One unread `operator.request` per operator and reason suppresses further ones (a flood
   of new organizations must not flood the operator); the waiting list (`agent-hub platform waiting`) carries every
   organization. "Once per occasion" per organization is derived from the database (notification rows, `credit.changed`
   audit rows, platform-log entries), never from process memory.
5. **`device.needed` skips the task owner.** The owner already gets `task.attention` (`device_missing`, ADR-076 §5);
   admins who are not the owner were not told before.
6. **The model-access continuation is the hub's, not a runner hold.** A held session start (the ADR-066 pattern) would
   keep a VM slot for an unbounded time. The hub's next attempt reuses the capacity-retry machinery (ADR-049): the
   prompt routing treats both retry reasons alike, so each prompt is delivered exactly once. A credential stored by a
   device (subscription login, ADR-011) triggers it as well. One attempt per stored credential and task: a device that
   cannot open the credential yet does not make the hub loop.
7. **An explicit own-access preference is honoured.** With `subscription` or `api_key` chosen explicitly and no
   credential, the platform does not fall back to paid DFX access (the user's voluntary choice, E-11); it starts the
   connection instead.
8. **`agent-platform task create` assigns a device.** The web client did this already before sending (`ensureDevice`);
   the agent tool now does the same for admins (rule E-04 parity, E-05). A choice (`--device`) skips it.
9. **The register covers every code, not only approvals.** All hub error codes, session failure reasons, operation
   error codes, scheduling reasons and held reasons, plus every code raised in hub and runner (AST scan resolving
   module constants and `from … import`). A narrower register would let a new refusal code slip in unclassified.
   The operator's own command lines (`cli.py`) and the copy of the reference crypto are not scanned (messages, and
   codes that are already in the enum).
10. **The feature gate covers every active device.** Organization devices verify the platform log too, so `log.policy`
    is required of platform and organization devices before the hub appends a switch, a list entry or a grant of the
    role `operator` (the ADR-060 gate for `user_certify` stays as it was).
11. **No protected-branch default anywhere.** Runner (`DEFAULT_PROTECTED = ()`), agent tool, web form and the contract
    (`contracts/ops/rules.json` `protected_branches.default = []`). GitHub's refusal of a protected branch keeps its own
    code (the GitHub area belongs to KOR-47 and is not touched).
12. **Start credit stays per organization.** The billing logic is KOR-31's. ADR-079 records that the switches cost
    nothing with the default start credit of 0 and that F1 must bound a start credit above 0 (per founder or per
    invitation chain) before it is combined with the switches.
13. **Findings of the independent review, kept as they are** (low): the operator pool falls back to
    `OPERATOR_SUBSCRIPTION_ORGS` while the runner has no verified platform log (the behaviour before ADR-079 during
    the move; once the variable is removed it denies); open invitations have no per-user quota (was part of the
    owner's decision F1; F1 is answered without a start credit, [E-14](../KONZEPT.md#e-14)); an attempt after a model
    credential of the other kind than an explicit preference fails again and waits (the card stays); the `log.policy`
    gate also holds back a revocation of the role `operator` until every device runs this release; an automatic device
    assignment stays when the task creation after it fails (the device is useful for the next task). The texts "credit
    has been requested" and "the administrators have been asked" hold once the operator has the role `operator`
    (rollout step); an unread request of the same reason stands for further organizations (aggregation).
14. **A credential and a failure in the same moment.** Both transactions take an advisory lock of the owner
    (`model_retry.lock_owner`, before any task lock), so one of them always sees the other committed and the
    continuation is never lost.
15. **E2E-61 shows the hub refusing a switch not signed by the root** (`log_signature`); that the runner refuses such
    an entry too is proven by the shared negative vectors (`P4_policy_signed_by_hub`,
    `P4_operator_subscriptions_signed_by_hub`) that the runner's copy replays; a hub that serves a forged entry cannot
    be staged in T1 without a forged hub.
