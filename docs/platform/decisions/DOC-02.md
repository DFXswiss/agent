# DOC-02 decisions

Cutover plan `docs/platform/CUTOVER.md` (09-milestones.md M4). Plan only: nothing was deployed, no DNS, tunnel or
access-policy change was made and nothing was pushed to the infrastructure repository. The same file is in both
repositories.

1. **One shared copy instead of a pointer.** `CUTOVER.md` is authored in agent-core and kept byte-identical in agent;
   it joins the shared documentation of DOC-01 decision 1 (`SHARED` in `tests/platform/test_docs.py`, both
   repositories), so the sibling check keeps the copies equal and its links stay inside `docs/platform/`. The entry
   page lists it (the entry-page test now expects the link); the runbook index, `runbooks/hub-operation.md` and
   `PLATFORM-DESIGN.md` §7 link to it instead of naming it as code.
2. **The hub's container recipe belongs to the infrastructure stack PR.** agent-core has no image definition for
   `agent_hub` (the top-level `Dockerfile` is the legacy hub and stays untouched, ADR-001). The infrastructure
   repository's stack pattern builds images inside the VM from source with the commit as tag, so the plan specifies
   the recipe (Python 3.12, release web build into `src/agent_hub/static/`, `pip install ".[platform]"`, one uvicorn
   worker on 8788, no host port) and leaves the file to the stack PR (CUTOVER §3.1, prerequisite P4).
3. **RP ID = the production host itself, never a parent domain.** The hub derives the RP ID from
   `AGENT_HUB_PUBLIC_URL` and has no separate setting; a parent domain would also let sibling hosts request
   assertions. Fixed with written owner approval before the first real passkey (CUTOVER §5, P7).
4. **Staged opening for external users is the recommendation.** The current access layer admits staff identities
   only (risk M4). The plan recommends a staff-only first phase (with narrow bypasses for the device and operator
   paths, which cannot complete an interactive access login) and a later, owner-approved opening of the client and
   anonymous API paths (S13); the decision itself stays with the owner (P2).
5. **The hub signing key is treated as irreplaceable.** Runners, the operator record and invitation packages pin
   the hub key; there is no rotation. The plan therefore requires an offline copy and allows free regeneration only before
   the platform-log genesis pins it in the operator record (S7, S3).
6. **Point of no return = the first passkey of a user outside the operator group.** Before it, a wipe and restart
   from an empty database is an acceptable rollback; after it, rollback means unrouting the hub, never discarding it.
7. **ADR-045 mirrored into agent-core.** ATS-03 added ADR-045 and its index row only in agent, so the shared
   documentation of the two repositories differed and the sibling check failed for every package with both
   worktrees. The file and the index row are copied unchanged into agent-core (DOC-01 decision 1: `adr/` identical).
8. **Review findings (independent review subagent).** Fixed: bypasses for `/v1/config` and `/v1/health` under a
   staff-only access layer; a database init step (`hub-db-init`) before `hub-migrate`; the image recipe with Node,
   `git` and `.git` so that the in-image release build reproduces the published hash; the S6 check uses the
   rate-limit key instead of the hub's access log (uvicorn does not apply proxy headers); `grant-org-access` and a
   smoke test for shared organizations; the branch for an already offline legacy hub; no reuse of legacy secrets;
   the hub-key cut-off at S7; `doctor` instead of `status` for `active`; the classification of `/v1/platform/…`;
   the runner `MODEL_CATALOG`; one trusted proxy address instead of a subnet; rollback rows for S11 and S12.
   Not changed: the docs test checks links, anchors, indexes and equality of the copies; commands and settings of
   the plan were checked by the reviewer against the code, a test that parses prose for command names would be
   brittle. The same wrong `status` wording in `runbooks/device-certification.md` §1.5 is corrected there as well.
