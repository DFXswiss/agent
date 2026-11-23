# KOR-39 – decisions (situation view "autonomous first")

Architecture: [ADR-064](../adr/064-situation-view-and-attention-categories.md). Concept: [Idee 32](../KONZEPT.md#k-idee-32),
[Idee 31](../KONZEPT.md#k-idee-31), rules [E-02](../KONZEPT.md#e-02), [E-04](../KONZEPT.md#e-04),
[E-05](../KONZEPT.md#e-05), [E-19](../KONZEPT.md#e-19).

1. **Categories at the hub, not in the client.** The hub derives one attention record per task from metadata it
   already holds; a client-side derivation would have to page through every task of every workgroup (E-19) and exist
   twice (web and agent tool, E-04).
2. **A separate projection table** `hub.task_attention` instead of columns on `hub.task`: operation writers would
   otherwise lock the task row and change the lock order of the tasks area. The attention row is locked first inside
   the refresh and is the last lock of every transaction.
3. **The agent asks with an operation** (`human.question`, `human.approval`) instead of a new event kind: the type is
   visible like every operation's type, the question is sealed in `operation.params`, audit and idempotency come for
   free. No new MCP tool.
4. **No content-derived signal** (design review): the first draft marked a turn whose last sentence ended with "?" as a
   question. That is one bit taken from content and unreliable; dropped. Instead Claude Code's own asking tool
   (`AskUserQuestion`), recognized by name, becomes a `human.question` operation.
5. **Questions count only in the main session's last turn** (design review): an agent that carries on after asking
   starts a new turn, so its earlier question no longer shows; a refused operation counts only when no later operation
   of the same type succeeded. A subagent cannot ask a person (`rights_denied`); it reports to its main agent.
6. **Ordering**: "Needs your decision" longest waiting first (nothing starves), the other groups newest first. A phase
   change inside "Running" does not move the entry (`since` changes only with the category).
7. **Notifications** (design review): one per occasion; workgroup-wide causes (no computer, computer unreachable, all
   busy) once per owner, workgroup and reason while unread; no second notification for a failed task (`task.failed`);
   no `task.idle` for a task that needs a decision or is stuck.
8. **Approvals are prompts** starting with `Approved.` or `Declined.` (English, the same in every locale), so the agent
   needs no new input channel and the web client and the agent tool send the same text.
9. **"Try again" only where it can help** (session, guest, computer, capacity); a refused action gets "Open task";
   an empty balance says that the operator sets it (E-14) and offers "Continue".
10. **Numbers**: migration `0011_task_attention`, E2E scenario `E2E-47` (numbers 41–46 are taken by parallel
    packages); the ADR number 064 avoids the numbers 058–062 used on parallel branches.
