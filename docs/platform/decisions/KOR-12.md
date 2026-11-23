# KOR-12 – Documentation brought to the state of the correction packages

Decisions taken where the package text left room. Scope: the documentation needs that KOR-01 … KOR-11, ATS-03,
DR-01, LVT-01 and SPC-01 handed to DOC-01 or KOR-12, and the audit items NB-01, NB-11, NB-12, NB-16 and the runbook
part of NA-02.

1. **Delivery in two merges per repository.** The orchestrator asks for each correction to be merged on its own. A
   documentation item is a few lines; a merge runs the full gates of both repositories (about ten minutes each). The
   items were therefore delivered in two merges per repository: first the medium-severity items (restore runbook,
   export privacy, cutover risks) with their test, then the specification and register follow-ups.
2. **The specification text is updated, not only the register.** Where a correction package changed specified
   behaviour (recovery-code rotation, quota `unavailable`, prompt carry-over, outbox retention and realignment, scope
   queues, credential hold, prompt delivery, journal kinds), the specification section now describes the built
   behaviour and the register row says `spec updated`; the register header names KOR-12 next to DOC-01 for that
   status. Rows whose earlier deviation a package closed are rewritten, not deleted, so the history stays readable;
   "Unverified" bullets that a package settled are removed and replaced by a row.
3. **Accepted ADRs and other packages' decisions get pointers only.** ADR-024, ADR-027, ADR-038 and ADR-047 and the
   decisions HUB-03, HUB-09, KOR-06, SCN-07, WEB-10 (agent-core) and GST-02, RUN-02, RUN-08 (agent) receive one
   sentence "Update (KOR-xx): …" or "Superseded by KOR-xx (decision n) …" behind the outdated statement; the original
   text is kept. ADR-027's table gains the guest start-up hook (`control_routes/<name>.py:STARTUP`), because the table
   is the binding list of extension points.
4. **Two documentation tests, red before and green after.** `test_named_documentation_paths_exist` refuses a code
   span `docs/platform/<path>` in the shared documentation that exists in neither repository (it found the dead
   `acceptance/DR-01.md`); `test_register_names_no_finished_correction_as_pending` refuses "open defect until <id>" in
   the register once `decisions/<id>.md` exists (it found A-08 "until KOR-04"). Without the other checkout a path into a
   repository-specific directory (`decisions/`, `acceptance/`) is checked only locally; with it (work-package layout or
   `PLATFORM_SIBLING_CHECKOUT`) in both.
5. **Acceptance records.** `acceptance/live.md` and `acceptance/SCN-07.md` (agent-core) are not in the package's list,
   but the owner's decision on LIVE-04 and the KOR-11 report require them to stop calling LIVE-04 "skipped" and the
   30 s quiet period current. They get the owner's wording (LIVE-04: not run, optional, not needed; R-4.3 by E2E-27,
   Claude live by LIVE-03) and "Fixed by …"/"Superseded by …" sentences; measured results are unchanged.
6. **`CUTOVER.md`.** Named by the orchestrator for NB-12, NB-16 and NA-02 although outside the package's list: edge
   compression (the hub never compresses and refuses compressed request bodies), re-keying after a root-key loss as an
   unrehearsed residual risk until P11, and the restore behind the runners' one-hour outbox retention.
7. **Not changed.** The contract `ws/device` still allows 1000 sessions per heartbeat list although at most about 500
   fit into a 64 KiB frame; the runner sends at most 500. A contract change is outside a documentation package; the
   register row records it. The comment of the runner migration `0010_outbox_held.sql` names ADR-046 for the scope
   queues, which are ADR-047; code comments are outside this package (reported).
8. **ADR numbers.** Both integration states hold 001 … 047, each number once, all indexed and identical; the
   workspace copy lacked 045 and 047 and was brought level outside the repositories.
