# SCN-02 – acceptance evidence (M1 scenarios B: E2E-10/11/12/13/14/23/34/35, plaintext scan)

Devices by role only: **runner device** (T1: everything; T2: runner, VMs, doubles) and **second device** (T2: hub,
PostgreSQL, browser, orchestrator). Golden image: shared TEST_MODE cache of the integration state (ADR-036), adopted by
the run's runner, warm pool 2 VMs; model double on the runner device, reached in the VMs through the guest loopback
tunnel (ADR-034).

## E2E (07-testing.md section 2.1)

| Run | Specs | Result |
|---|---|---|
| `e2e.sh SCN-02 --topology t1 --only E2E-10,E2E-11,E2E-12,E2E-13,E2E-14,E2E-23,E2E-34,E2E-35 --repeat 3` | all eight | 3/3 iterations green |
| `e2e.sh SCN-02 --topology t2 --only E2E-12,E2E-13,E2E-14,E2E-23 --repeat 3` | E2E-12, -13, -14, -23 | 3/3 iterations green |

Standard end checks of every iteration: no egress violation, model double `state.ok`, runners alive, VMs of the run
deleted (audit-chain check reported as not available until HUB-16).

What each spec proves (beyond the catalog row):

| Spec | Evidence |
|---|---|
| E2E-10 | Two sessions of one workgroup in the same VM at the same time (B held working); distinct session users, UIDs, units and processes; A's Read/Grep/Glob/Write on B's workspace denied by PreToolUse (rules 6, 6, 6, 5; `hook.denied` rows with tool and rule only); with A's identity B's file does not exist inside A's mount namespace and is unreadable outside it; B can read it; B's files unchanged, A wrote in its own workspace; both tasks finished. |
| E2E-11 | Default and second workgroup of one org: two hub `vm` rows with distinct IDs, one per workgroup, both on the platform device; each session's tmux server is in the Lima instance the runner keeps for its VM. |
| E2E-12 | Live view during the session; per chunk `browser apply − event ts` (VM clock offset measured and subtracted, only chunks received after the view opened): p95 T1 0.85 / 0.53 / 0.52 s, T2 0.62 / 0.64 / 0.64 s. Typing into the view: no non-GET request, no WebSocket, no character on the screen, nothing reaches the model; text typed into the pane: blocked, `session.prompt_refused`. After a reload (passkey) the view replays the history; the decrypted `session.terminal` frames equal the recorder's asciicast file exactly. |
| E2E-13 | Prompt, Write/Read summaries, file change (path, sha256, size) and answers in order; after 60 s idle the session is `suspended`; the follow-up resumes it (`suspended → resuming → working`, unit command line `--resume <id>`, the model request carries the earlier turn). |
| E2E-14 | Runner link and browser link cut after the first output: the turn finishes offline, the runner outbox grows gapless (`count = max(origin_seq)`), the hub receives nothing; runner link back: everything acknowledged (`last_origin_seq` = outbox max), unique `origin_seq` in scope order, no `sync_fatal`, no `runner.*` alert; browser link back: the chat shows all six outputs once and in order. |
| E2E-23 | Bash/Agent/Task never offered to the model (asserted on every request) and refused as unknown tools; absolute Glob and Read of the git binary denied by the policy (`hook.denied` rule 3/6); no `.git` in the workspace; the first request's system prompt holds the rules, org/workgroup/task/session IDs and the platform tools. |
| E2E-34 | Hooks forced to HTTP 500 before the task: Writes to managed settings, project and user settings, `.mcp.json`, the harness file and RULES.md plus a shell call all fail; every hook journaled without a decision, no `hook.denied`; managed settings, RULES.md and mcp.json byte-identical to before, harness still root 0444, no settings or `.mcp.json` in the workspace, no file with tampered content. |
| E2E-35 | Text typed into the pane and the used first prompt typed again: both blocked (`reason: unregistered`), the model saw neither; a prompt from the chat is accepted; the identical prompt event sent to the hub again is replayed and not delivered twice. |

## Plaintext scan (09-milestones.md M1)

Every spec ends with `POST /checks/plaintext` over a fresh hub database dump, the hub and PostgreSQL logs and every
byte the hub sent on the browser link (relay tap, HTTP-decoded): the canaries of prompts, answers, file contents,
titles, terminal bytes and display/workgroup names – raw, hex and base64 at all alignments – were found nowhere, in
every iteration of both topologies; the browser's live stream was captured each time.

## Found and fixed on the way

- Golden: `fwupd-refresh.timer` of the base image reached a CDN over HTTPS after a clone's boot (egress check, T1);
  masked in the slim step (agent).
- Web test helper: creating a workgroup in an org with a platform device needs the verified platform state.
