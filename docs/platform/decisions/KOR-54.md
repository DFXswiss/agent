# KOR-54 – decisions

Package: the key model over all 47 ideas – described in one place, checked against the code, gaps fixed for the
cryptography packages. Concept references: [§5 point 5](../KONZEPT.md#k-5-b5), [§13 point 1](../KONZEPT.md#k-13-1),
[idea 30](../KONZEPT.md#k-idee-30), [§16 point 7](../KONZEPT.md#k-16-b7); rules [E-12](../KONZEPT.md#e-12),
[E-13](../KONZEPT.md#e-13), [E-18](../KONZEPT.md#e-18), [E-19](../KONZEPT.md#e-19); interpretations
[A3](../KONZEPT.md#a-3), [A7](../KONZEPT.md#a-7).

1. **Own file next to the specification, pointer in 04-crypto.** The key model is
   [`docs/platform/key-model.md`](../key-model.md), linked from [04-crypto §11](../spec/04-crypto.md#11-key-model). A new
   numbered spec file would change the central list of `tests/platform/test_docs.py` and collide with the numbers that
   packages in flight claim (14-community); a long section inside 04-crypto would mix formats with the "who holds
   what" view. The formats stay in 04-crypto; the key model repeats none of them.
2. **Rules with IDs and evidence, checked by a test.** Each rule `KM-NN` names shared vectors (by file and name), test
   files (for rules only one component enforces) or a gap with its package. `tests/platform/test_key_model.py` fails
   when evidence disappears, when a gap package has no requirement section, or when the concordance misses an anchor
   of the concept file (every section, idea, rule E-NN and interpretation A-N). This is the check of E-19 for the
   model: a later change of a rule must bring its evidence.
3. **New vectors only through the existing generator.** The three rules without shared vectors (KM-06 only log
   principals receive wraps, KM-10 the org key needs a workgroup, KM-15 no credential key without consent) got
   negative vectors in `agent_hub.log.vectors` (`log-wraps.json`, `log.json`). Negative vectors are run generically by
   all three verifiers, so no consumer list changed. All three pass against the current code; the comparison found no
   defect in the verifiers.
4. **The agent vector is a guard, not a statement about the future.** `principal_outside_log_is_no_recipient` uses an
   agent key as recipient: today an agent holds the user's identity and is never a wrap recipient (ADR-056). KOR-57
   makes the agent a sub-principal and must change this vector consciously (key model §11, KOR-57 point 3).
5. **Pending designs are named, not described as built.** Community key (KOR-26), context checkpoints (KOR-33) and
   organization-wide device assignment (KOR-41) are on branches, not on `develop`. The model names them "carried
   (pending …)" and lists what each must add when it lands; it does not treat them as built.
6. **Support per person, not a shared key** (requirement on KOR-56): a shared support key cannot be revoked for one
   person without new keys for every release and hides who read. Recorded as the model's requirement; KOR-56 decides
   in its ADR within it.
7. **Company credentials need their own scope** (requirement on KOR-47): neither `OK` (every member holds it) nor one
   user's `CK` (must survive the user). A finding of the comparison that changes the order (key model §12).
8. **Reading across workgroups adds no wrap rule** (requirement on KOR-45, key model §8): the device reads only what it
   holds by assignment; content read from another workgroup lands in the session's own workgroup scope (VM, context
   checkpoints), which KOR-45 must limit or state.
9. **PRF check without a new account.** The procedure for people ([prf-devices.md](../manual-checks/prf-devices.md))
   uses "Add passkey" on an existing account: the product already refuses a passkey without PRF and labels every
   passkey ("Signs you in and opens your data"), so no new self-report is needed and nothing new reaches the hub. The
   0.1.0 procedure is marked as superseded (it predates E-12/E-13).
10. **Small corrections in 04-crypto:** the header and §10 still named the former two-repository layout and
    `agent_runner.crypto.vectors` as generator; they now name the generator in `agent_hub` and the checked runner copy.
11. **Requirements rows:** R-14.1, K-30 (the admin read right is built since ADR-058; the row said "missing") and K-16
    point to the key model and its gaps.

## After the review (two reviewers with fresh context: cryptography and accuracy; completeness against the concept)

12. **Copies of the identity at rest are part of the model:** the remembered browser (IndexedDB, non-extractable key)
    and the agent tool's state (sealed identity plus handover secret next to the agent key) are listed in §1, §2, §5
    and §6, with KM-38. A leaked identity has no remedy today; the rekey of the user's identity is a **required** part
    of KOR-57 (it was "optional" in KOR-57's text), because rotating scopes does not shut out a copy of the identity
    the new epochs are wrapped to.
13. **Revocation table completed:** unassignment from one of several workgroups, the non-atomic `CK` rotation,
    `org_platform_access` withdrawn, `user_decertify`, `wg_archive`, invitation revoked after a lookup (decision D2),
    leaked identity, platform root and hub response key.
14. **Statements corrected:** KM-04 (keys also arrive in the invitation package), KM-09 (only team orgs are
    root-anchored; private orgs are KM-31), `device_assign` and the assigning user's `CK` (only with explicit access in
    a consented org), KM-32 (write → read rotates nothing: R is about losing read), §7 (malicious JavaScript and the
    ADR-060 certification risk named), the agent's keys (its X25519 key opens the sealed identity).
15. **KM-21 gets no grace vector of its own:** the clause "only from authors entitled to the new epoch" in E6 cannot be
    reached on its own, because E5 at receipt refuses a removed or excluded author first (`hub_E5_at_receipt`,
    `E5_decertified_device`). The rule is stated that way.
16. **Reading across workgroups (§8) follows KOR-45's own boundary:** way A (keys by assignment) is the default and
    needs no change of the verifiers; way B (a read release per task, which KOR-45's text also names) is a new
    entitlement and therefore Spur-K work – decision D3. Content read from another workgroup is copied into the
    reading workgroup's scope like a human copy (idea 30); the default states and logs it, the stricter mode is a
    voluntary setting (E-11), not the default (the first draft had it the other way round, which would have made almost
    nothing readable).
17. **Support:** a release goes to "DFX support" as a team (idea 29); entitlement = valid support role ∧ release, wraps
    per person by catch-up, so a new support person needs no re-wrap by the user and a leaving one rotates only what it
    could read.
18. **Company credentials:** devices-only readers, as KOR-47 states; the operator account for other organizations'
    tasks needs the root-signed list of KOR-49 as entitlement.
19. **Status "decision needed"** in the concordance for idea 35 (the assigned device necessarily holds keys); three
    decisions for the orchestrator (D1–D3) in key model §11.
20. **The check is stricter:** test evidence names the test (`path::name`, the name must occur in the file); the
    key-bearing points of §1, §3, §5, §8, §13 and §16 need rows; a "carried" row may not cite an unbuilt rule; a gap row
    must cite a rule with that gap; "–" is a target only for status "none". New negative vectors have positive controls
    in the same log state (`W2_org_key_for_device_with_workgroup`, `cred_device_of_org_with_consent`).
21. **PRF procedure:** names both "Add passkey" buttons and the confirmation step, warns against "Remember this
    browser" during the check, and fills only the PRF and Login columns of the release table; Registration, Recovery,
    Terminal and Countdown stay open instead of being copied from emulated runs.
