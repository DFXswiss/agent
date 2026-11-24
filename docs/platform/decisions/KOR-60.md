# KOR-60 – decisions

Package: evidence for a fresh context per task (KONZEPT idea 43), the live view of running subagents (idea 26, section
6 point 7), a measured capacity of one execution device (E-02, AUD-04 section 6) and the counter of kernel refusals
(decision D16 of the concept review).

1. **Fresh context is checked by the model double, not by the spec alone.** Two new matchers of
   `agent_doubles.model.scenario`: `max_user_messages` (messages with role `user`) and `text_absent` (texts that must
   appear nowhere in the request: system prompt, every message's text and tool results). A request that breaks them
   is refused like any other mismatch, so a carried-over context makes E2E-08 red at the first turn of the second
   task. The spec checks the same on the double's journal as a second, readable proof.
2. **One user message, not one message.** Claude Code 2.1.330 sends its environment as a separate message with role
   `system` after the user's prompt; a fresh first turn therefore has two messages, one of them from the user (seen
   in the first T1 run: `['user', 'system']`). The matcher counts user messages.
3. **"Two tasks of the same user in the same workgroup VM" is E2E-08 itself.** Both tasks are created by the same
   user in the org's default workgroup; the second starts on the workgroup's running VM. The spec now asserts the
   same `vm_id` for both main sessions instead of adding a third task.
4. **E2E-08 task list order.** The list is ordered by newest activity (06-web.md section 5.1, KOR-32); the follow-up
   makes the first task the most recently active one. The spec expected creation order and was red on every device
   since KOR-32 (noted in the INF-07 report); fixed in the spec (E-19: the test was wrong, not the product).
5. **Live view in E2E-24 by holding the child's final answer back.** The step `child-done` answers after 40 s
   (`delay_ms`); the parent waits with four `get_subagent_result` calls of 30 s (sequential in Claude Code, as E2E-17
   already relies on). While the child waits, Claude Code's TUI keeps redrawing, so the terminal grows; the child's
   first answer reaches its chat tab (the transcript follower writes it before the turn ends); the monitoring
   overview lists the child with its own sample in state `working`. After each of the three checks the spec reads the
   child's state from the hub and requires it to be unfinished. The "pending" note in the header is gone.
6. **PERF-06 measures; it sets no target.** Bounds per step (start p95 ≤ 30 s, first answer p95 ≤ 45 s, hub health
   p95 ≤ 1 s, runner status p95 ≤ 10 s, device memory ≤ 85 %, the sessions of one VM ≤ 80 % of its memory, no failed
   session). The capacity is the last step within every bound; the first broken bound, or the device's VM slots,
   names the bottleneck. The spec fails only when it could not measure at all. Step sizes and maxima come from
   `E2E_PERF_CAPACITY_*`, so a smaller trial run and the exclusive measurement use the same code.
7. **PERF-06 answers through a side step.** Where the run stops is not known in advance, and an ordered step of the
   double must be consumed (the orchestrator checks `state.ok`). The first task uses its own prompt and the one
   ordered step; every further task is answered by a side step.
8. **Sessions stay alive between steps.** Each session ends its turn and stays `idle` with its Claude Code process
   in its sandbox (`SESSION_IDLE_SECONDS` 1800 s is longer than either part of the measurement), so the load of a step is all
   earlier sessions plus the new ones - the realistic case of many open tasks.
9. **The kernel-refusal counter counts what reaches a tool result.** The guest image runs Linux 6.8 (Ubuntu 24.04),
   which has no Landlock audit records (from 6.15); the image has no auditd and no eBPF tooling, and a kernel audit
   client over netlink would not be "small". The guest counts the `session.tool` journal records with `is_error`
   whose result names `EACCES`/`EPERM` or their texts as `sandbox_denials` (contracts/changes/KOR-60.md); the runner
   exports `agent_runner_session_sandbox_denials_total{vm,session}`; the sessions dashboard shows it next to the hook
   denies. A refusal that a program swallows without reporting it to Claude Code is not counted - named as a residual
   point with a package proposal in the report.
10. **The attack case is E2E-51, not an ATK case.** The PreToolUse policy only allows reads and writes inside the
    workspace, which Landlock allows as well, and hooks fail closed (KOR-40); a refusal by Landlock itself therefore
    needs a failed hook, which the ATK suite covers without counting. A call that the hook rightly allows and the
    kernel refuses is a file inside the workspace that the session user may not read: the spec has root put one there
    (owner root, mode 0600, route `POST /sessions/{id}/locked-file`, spec-only) while the double holds the session's
    first answer back, and reads the guest's session metrics (route `GET /sessions/{id}/guest-metrics`, the runner's
    own source): `sandbox_denials` 1, `hook_denies` 0.
11. **Counter-check of E2E-08 is not committed.** The deliberate context carry-over (the runner prefixes the first
    prompt of every new main session with the first prompts of the earlier ones) is a script in the internal board,
    applied to a working copy for one run and reverted; its run and result are in the report.
