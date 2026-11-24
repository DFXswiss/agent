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
