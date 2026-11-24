# KOR-47 – decisions

GitHub identities: several accounts, company accounts, choice per operation, session and task, defaults per
organization and workgroup, commit author, committer, co-authors and signature. Architecture:
[ADR-076](../adr/076-github-identities.md); contract changes: `contracts/changes/KOR-47.md`.

## Stage a – pre-study
1. **GitHub signs no REST commit made with a user token.** From GitHub's documentation (ADR-076 "Pre-study"): only
   web-interface commits, `createCommitOnBranch` and bot commits of a GitHub App without custom author, committer
   or signature are signed by GitHub. `createCommitOnBranch` cannot set author or committer. The package therefore
   offers `signing` `none` or `key` (own SSH key per identity), no "signed by GitHub".
2. **Proof against the real service** is left to the operator: `scripts/probes/probe_github_identity.py` (never part
   of the gates) creates three commits without a ref in a test repository and prints GitHub's verification, the
   attribution and whether the commit ID matches the runner's own serialisation of the signed commit. Whether the
   REST `signature` field accepts an SSH signature (the documentation names PGP) is the open point it answers; the
   double implements the documented verification rules.
3. **Abort criterion not reached:** questions (1)–(3) were answered from the documentation within the day; the
   signature is built (stage b) and its real-service proof is the probe.

## Design review (fresh context, before building)
4. Findings and how they were taken (high H, medium M, low L):
   - H1 old runners would ignore choices → feature `github.identities`; the hub refuses choices and defaults while
     an assigned device has not announced it and denies an old runner's `github.*` registration when a choice
     applies.
   - H2 copies of the operator's token look like personal accounts → copies are written with `source:"operator"`;
     co-author lookup skips them (and, on a platform device, records of the operator's GitHub account ID). Copies
     made before on other devices stay the user's own account (documented residual, as today).
   - H3 "primary = oldest" was fragile → explicit `is_primary`, changed only by the owner, no promotion on delete.
   - H4 old browsers could not rotate `CK` with a second record → non-primary records may be left out of a rotation
     and are re-sealed by a current client.
   - H5 another member's prompt ran on the owner's account → the acting human is the verified author of the newest
     delivered prompt of the task.
   - H6 any device could write a company record → only admins author company records; readers check it.
   - M1 foreign org accounts → usable are own, the task organization's and the operator's account only.
   - M2 the hub can swap choices → **not taken**: choices are hub columns; a swap can only pick another identity the
     acting human may use (the runner enforces §2), so rights never widen; signed choices in events would need new
     event kinds in every verifier for an attribution-only risk. Documented in ADR-076 "Consequences".
   - M3 roles and endpoints of defaults → defined (03-api §4.8, agent tool).
   - M4 dangling choices → reconnecting fills the same record (`identity_id`, or the primary by GitHub account ID);
     a writer can change the task's choice.
   - M5 org-key rotation → obsolete with the devices-only scope (decision 6).
   - M6 hub visibility → 04-crypto §7 rows.
   - M7 trailer check too narrow → `co-authored-by:`, `signed-off-by:`, `committed-by:` in any case with leading
     spaces.
   - M8 operator account leaving the platform device → documented; readers are the devices of its organization.
   - L1–L5 → documented (signing key reach, probe compares commit IDs, codes registered, KOR-52 uses the session
     identity, status).
5. **Contract after review:** the device returns the body of a company account to the admin's client (end-to-end
   encrypted to that admin) instead of storing it, and `github.connect` may name the record to fill
   (`identity_id`).

## Stage b
6. **Company accounts are readable by devices only** (key model KM-35, KOR-54 decision 7): the `cred` scope of the
   organization, readers = its assigned member devices; an admin writes every version under a fresh epoch with the
   wraps for every reader device and cannot read it back. The first draft (org key `OK`, readable by every member)
   contradicted KM-35 and was replaced before it was built.
7. **The hub's usability check is lenient** (own record, or a company record of an organization the caller is a
   member of); the strict rule (task organization, operator's organization) is the runner's, which knows the
   operator's organization.
8. **The acting human is the author of the newest delivered prompt** of the task; KOR-46 may refine it through the
   one runner function `acting_user`.
9. **Defaults are company accounts only.** A personal account as a default would let other members act as that
   person (A15).
10. **Commit attribution uses the noreply address** `<id>+<login>@users.noreply.github.com` and the login as name;
    no address of a person is stored.

## After the rebase onto HOT-14 and the independent review of the code
11. **The acting human is HOT-14's** ([ADR-070](../adr/070-personal-connections-follow-the-instruction.md)): stamped
    at receipt from the turn's prompt author; no acting user → `github_not_connected` at once. The package's own
    "newest delivered prompt" rule (decision 8) was dropped; `credentials/acting.py` opens every personal record,
    co-authors included, for which it returns only login and account ID (attribution, never the token).
12. **A default whose record does not open holds the operation** (review M1): only a default the acting human may not
    use is skipped, so a workgroup set to a company account never falls back to a personal or the operator's account.
13. **The pre-check compares the token's account with the record** (review M3): `GET /user` with the identity's token
    must name the record's `user_id`, so a record cannot claim another GitHub account for the attribution.
14. **Company accounts reach every device of the organization** (review M5): the readers of the organization's
    credential key are all its assigned member devices, for the operator's organization too. Documented in ADR-076
    §7; restricting the operator's account to platform devices needs the root-signed list of KOR-49.
15. **The hub's choice check stays lenient** (review L4): the hub does not know the operator's organization; the
    runner refuses a company account of another organization with `identity_forbidden`.
