# KOR-53 – decisions

Package: "Prozesse optimieren" – a button starts an analysis task that only reads and proposes. Concept references:
[Idee 33](../KONZEPT.md#k-idee-33), [§2](../KONZEPT.md#k-2) point 5, [§8 point 5](../KONZEPT.md#k-8-b5),
[§8 point 6](../KONZEPT.md#k-8-b6); rules [E-04](../KONZEPT.md#e-04), [E-05](../KONZEPT.md#e-05),
[E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19); interpretation [A4](../KONZEPT.md#a-4). Design:
[ADR-097](../adr/097-process-analysis.md), reviewed with fresh context before the build (three high, seven medium and
five low findings; all worked into the ADR or answered below).

1. **An analysis is an ordinary task with `analysis`.** §2 point 5 asks for a task; chat, costs, attention and
   follow-ups come for free. The hub stores `{scope, since, until}` as plaintext metadata (`hub.task.analysis`).
2. **Read only through the profile, assigned in the creating transaction.** The built-in profile `analysis` (classes
   `read`, `delegate`, `ask`) is the snapshot of every analysis task; reach `workgroup` for scope `wg`, `all` for scope
   `org`. Every write, work VM and browser needs an operation, which the hub refuses (H2b) on every runner – so "read
   only" does not depend on the runner version. A person may only narrow it (E-11), never widen or clear it (`403
   forbidden`, reason `analysis`).
3. **No work VM** (the package let the design decide): the tools return aggregated, paged metadata; a VM would be a
   second place for the metadata of several workgroups, with internet access, for no question the tools cannot
   answer. The model's web search is not offered to an `analysis` session either.
4. **No orchestrator for an analysis session** (review finding): the orchestrator holds the user's full agent access;
   `409 orchestrator_unavailable` reason `analysis`.
5. **The brief lives in the harness rules**, named at the session start for a main session (depth 0) with the profile
   `analysis`; children carry the profile name but get no brief (review finding). The trigger is the profile name, not
   a new device-state field: an older runner or guest then simply runs a read-only session without the brief. An own
   profile named `analysis` from before this package would give the brief to an ordinary task's main session – a
   text, no right; new own profiles cannot take the name (it is a built-in).
6. **Four tools, one workgroup per call, for every session.** E-11: what a person may read, the person's AI may read;
   so the tools are not analysis-only. One workgroup per call keeps ADR-089's intersection exact; the organization
   level (audit rows without a workgroup, organization billing) is not readable by sessions.
7. **Counts instead of paging through 92 days** (review finding, the package's biggest uncertainty): `group_by` on
   audit (action × result) and operations (type × state × error code) gives the patterns of a whole period in one
   answer per workgroup.
8. **A hub cannot hand free text to the model through the audit** (review finding): the runner keeps only flat tokens of
   an audit row's detail and drops untokenised `target_id`s.
9. **Privacy of the report.** The technical boundary is what the session may read (owner ∩ instructing human). The
   report is written into the task's workgroup like anything a person copies (ADR-089 §5); the brief keeps other
   workgroups to names and totals, and the dialog says before the start (and before adopting to the organization)
   who will be able to read it. A stricter mode was rejected: in the default workgroup an admin's analysis would see
   almost nothing (E-11).
10. **Usage per session started in the period**, with the session's whole counters and charges – figures are not
    split at the period's edges; a provider-key share without a session belongs to no workgroup. `total` only on the
    first page, so paging does not re-aggregate.
11. **Prompt and title are fixed per language** (`contracts/tasks/analysis.json`, de/en), the same in web and agent
    tool; the report follows the prompt's language.
12. **Indexes without `CONCURRENTLY`.** Migrations run in a transaction; appends to `hub.audit`, `hub.operation` and
    `hub.session` wait while the three indexes are built (seconds at the current volume, during the planned hub
    update).
13. **Error codes.** The widening of an analysis profile is `403 forbidden` (a right the person does not have on this
    task) with `detail.reason` `analysis`, not a new code; the error detail's `reason` list gained `analysis`.
14. **Review of the code (fresh context).** Worked in: figures only outside the own workgroup for the `analysis` role
    (the package's biggest risk, now technical, not only the brief); the analysis name kept when an analysis task is
    narrowed or starts a child with its own profile; tighter audit tokens; unknown error codes kept as null and
    `truncated` on every cut; reads without `since` limited to 92 days; usage by task aggregates only the page's
    tasks; a hub that answers more than asked makes the read `unavailable` (cutting would lose rows silently); the web search also refused by the guest's hook for
    `analysis`; the agent tool leaves the "not in the future" check to the hub. Recorded, not changed: workgroup VM
    samples follow the VM's current workgroup (ADR-097 §7); an older hub answers an unknown route with the same
    `404 not_found` as a route, so a new runner in front of an old hub reads empty lists – the rollout is hub first
    (ADR-097), and an analysis task cannot be created on an old hub at all.
