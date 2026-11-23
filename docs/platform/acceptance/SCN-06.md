# SCN-06 – acceptance evidence (M2d scenarios: E2E-25, E2E-26)

Devices by role only: **runner device** (T1: everything; T2: runner, VMs, doubles) and **second device** (T2: hub,
PostgreSQL, browsers, orchestrator). Golden image: shared TEST_MODE cache of the integration state (ADR-036), warm pool
2 VMs, model double reached in the VMs through the guest loopback tunnel (ADR-034).

## E2E (07-testing.md section 2.1)

| Run | Specs | Result |
|---|---|---|
| `e2e.sh SCN-06 --topology t1 --only E2E-25,E2E-26 --repeat 3` | both | 3/3 iterations green |
| `e2e.sh SCN-06 --topology t2 --only E2E-25,E2E-26 --repeat 3` | both | 3/3 iterations green |

End checks of every iteration: no egress violation, model double `state.ok`, audit chain verified (`agent-hub audit
verify`), runners alive, the run's VMs deleted.

| Spec | Evidence |
|---|---|
| E2E-25 | Three levels only (UI level bar, hub projection, `level=project` refused). Org board: the admin writes; a reader and a writer member see it live without any control. Workgroup board: the writer creates, edits and deletes; the reader's second browser follows each change live and never gets a control; the history shows Created/Edited/Deleted in the reader's dialog and as `whiteboard.entry/edit/delete` from the hub. Task board on the admin's task: the writer writes, the reader reads; task entries stay off the workgroup board. The hub refuses the reader's direct writes on all three levels and her edit (`400 author_not_writer`, 03-api.md section 11), and the member writer's org-level write; the refused edit left no event. Canary search over every hub table and the plaintext scan (DB dump, hub and PostgreSQL logs, bytes sent to browsers): no entry text, name or prompt. |
| E2E-26 | A workgroup entry with "Notify participants" while the Research session's turn waits on a held model request: the reader and the writer of the workgroup get the notification live (badge, toast, row; the row opens the board), the member without access and the author get none; one content-free hub `session_ping` for the Research session only (whole row compared), none for the idle default-workgroup session. The guest stores the ping while the turn still waits; the notice of 03-api.md section 8.2 is attached to the PreToolUse answer of `Read` (journal: `ping.delivered via:hook` right after that `session.hook`), reaches the model only in the request after it, and the session calls `get_notifications` (`via:tool`, same ping) and `get_whiteboard`, whose result is the first request carrying the entry text. One prompt event, `working → idle` once, all scripted steps consumed. Pane input log (strace on the guest's tmux server, pane's pty master): 0 writes during the turn; positive control afterwards sees typed text byte for byte. No content in the guest's ping rows and journal, the hub's ping, notification and audit rows; plaintext scan clean. |

Decisions and found defects: `docs/platform/decisions/SCN-06.md`.
