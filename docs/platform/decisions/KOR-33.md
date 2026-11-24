# KOR-33 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent`
(INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent`
repository, then the note of the former `agent-core` repository.

## Note of the former `agent` repository

### Runner/Guest

#### Guest

1. **Context archive from the guest, not from the host.** The guest exports the context (`POST
   /v1/sessions/{id}/context/export`) as root with the `O_NOFOLLOW` walk of `files.py` (`contexts._ContextExport`
   extends `_Export`): only files of the session user; the transcript is limited to the conversation's own files
   (`projects/<key>/<id>.jsonl` and its companion directory, `todos/<id>*`, `file-history/<id>/`), so a session cannot
   plant settings or instructions into its successor.
2. **Verbatim transcript.** Entries are never filtered or reordered; the export cuts the incomplete last line of
   **every** exported `.jsonl` (subagent files too). A file that shrinks while it is read is `503 unavailable`.
3. **Workspace as it was** (review 1 no. 5): execute bits (`0755`/`0644`), empty directories and links whose target
   stays inside the workspace (absolute targets below the work directory made relative) are exported and restored;
   protected names (`.git`, `.claude`, `CLAUDE.md`, `AGENTS.md`, ...), links leaving the workspace, devices/FIFOs,
   foreign and unreadable files and too deep directories stay out but are counted (`context.json`
   `workspace_omitted`). `.git` stays protected (ATS-03): a restored checkout is no git repository any more, the
   session is told how many entries were left out.
4. **Heavy work at the stage, not in the create** (no. 9). The stage (`/srv/sessions/.contexts/<id>`, root 0700)
   extracts, checks, rewrites old session directory and cwd key byte for byte in contents and names, and writes
   `transcript-baseline.json`; `POST /v1/sessions` with `restore` only renames and `lchown`s (the runner gives that
   request a 300 s timeout). Unused stagings go after 6 h.
5. **Stage checks** (nos. 13, 19, 20): only `context.json`, `transcript/`, `work/`; `transcript/` holds only the files
   of `claude_session_id` under the key of `context.json` `cwd`; links only below `work/` with a relative target
   inside it. Transcript lines are **not** parsed any more: integrity comes from AEAD, digest and signed manifest, and
   Claude Code tolerates its own damaged lines (spike t9c). Deep JSON (`RecursionError`) is `request_invalid` in
   `context.json` and ignored in transcript records.
6. **A->B->A** (no. 14): `restore` for an existing `suspended` session of the same ID replaces its conversation files
   and workspace with the staged ones and resumes it; without a matching staging `409 state_conflict`.
7. **No new guest error code**, no chat flood after a restore (scanner baseline, transcript baseline) - unchanged.

#### Runner

1. **Area `agent_runner/contexts/`, only discovered extension points.** `session_state.py:OBSERVERS`,
   `session_request.py:AMENDERS`, `tasks.py:TASKS` and the new `vm_retire.py:RETIRE_HOOKS` of the new VM extension
   point `vm/extensions.py` (ADR-027 style, like `sessions/extensions.py`; called by `VmManager` before a running VM is
   stopped or deleted, and before a stopped VM is replaced by a newer golden). Edits outside the area: the Resumer,
   two guest client calls and the restore create timeout (`guestlink/client.py`), `vm/lifecycle.py`/`vm/tasks.py`
   (hook calls), `github/tarsafe.py` (`links="symlinks"`, `Entry.mode/target`), settings, `quota_exceeded`.
2. **Crypto copy** of agent-core `agent_hub/crypto/context.py` (copy tool, `ORIGIN.json`, commit `5aa9fb38`); agent-core
   crypto and `contexts/vectors.py` did not change since, so the copy was not re-run.
3. **Version rule (spike).** Target Claude Code >= highest `version` of transcript lines that parse; a version part
   has at most nine digits, longer ones are ignored (no. 20).
4. **Export and upload separate** (nos. 2, 8): two loops in task `contexts`; up to 4 exports at a time, one per session
   (`ContextEnv.session_lock`); inspect, compression, sealing, decryption and cache I/O in worker threads; linear
   buffers in `archive.py`. A checkpoint of a VM that exists but does not run stays due (no attempt counted) until the
   VM runs or is deleted; before stop/delete the hook takes the due checkpoints (marks every live main session of the
   VM due; bounded 120 s), a stopped VM with due checkpoints is restarted instead of replaced.
5. **Dedupe on content** (no. 11): the hash covers only transcript and workspace members (path, kind, execute bit,
   link target, content), not `context.json`.
6. **Upload outcomes** (nos. 6, 7, 12): transport/`unavailable`/`rate_limited` retried; `404` on the first chunk = hub
   without the route (or session): `refused`, no further uploads of it, the newest stays local; `404` after accepted
   chunks or commit refused for missing chunks = expired upload: object `refused` (local copy kept), the checkpoint
   due again with the content hash forgotten, so a new object ID goes up; other refusals `refused` (log). The hub
   writes `session.context_refused` itself (contract of the hub package).
7. **Cache** (no. 12): the newest object of each task always stays; older ones go once they are not uploading; over
   `context_cache_bytes` the oldest non-newest go in any state; `cache_full` log if still over.
8. **Which checkpoint** (nos. 1, 6, 10): first the final checkpoint of the continued session (`resume_from`, or the
   session itself on a VM change): same device - wait until its state settled and nothing is due (sealed locally is
   enough) or its VM does not run; other device - re-read `GET /v1/device/state` every 5 s until `context.session_id`
   names it; at most `CONTEXT_WAIT_SECONDS` (180), then `stale`. Then all cached objects (refused included) and the
   hub reference, ordered like the hub: own session, source, other sessions by creation time (local) or object time
   (other device), then UUIDv7. Residual: on another device a periodic checkpoint of the source can count as final
   (at most one interval of work, `stale=false`); a source that never checkpointed delays the start by the wait.
9. **Hub reads** (no. 3): backoff 1, 2, 4, 8, 15 s ... within the same 180 s; `404`/other refusals at once. Residual
   (documented): a hub outage longer than the wait starts fresh, later checkpoints of that session supersede the old
   context at the hub (the old one stays in the cache of the authoring device until superseded there).
10. **Damaged cache** (no. 18): a cached copy failing the checks is read again from the hub when the hub holds it
    (committed or named in the reference) and repairs the cache; a local-only copy takes the emergency path.
11. **Author check** (no. 17): the author must be a device assigned to the workgroup in a log state of the manifest's
    epoch; an epoch newer than the verified log is `context_unavailable`. Residual: within one epoch an unassigned
    device stays valid (unassignment normally rotates the epoch).
12. **VM change of a resume** (no. 4): `vm_id` changes only after the guest accepted the create; a retry restores again.
    A crash between create and update makes the retry stage onto an existing sandbox (refused) - fallback audit, rare.
13. **Audits** (no. 16): `session.context_restored {object_id, workspace: full|partial|omitted|empty, stale}` is stored
    with the restore and sent when the guest reports `agent_starting`; `session.context_fallback {reason}` at once.
14. **Failed restored starts** (no. 15): stays "retry" (sessions are final); the checkpoint is marked only when the
    restored session fails `agent_start_failed`/`sandbox_failed` before it reached `working`/`idle`/`suspended`.
15. **Harness sentence** (nos. 5, 22): same VM / another VM of this device / VM of another device, checkpoint time,
    workspace state and count left out, stale note, work VM.
16. **Bounds** (no. 21): the inflated archive may hold the content limits plus 11 KiB per allowed entry (header, PAX
    path and link, padding).
17. Migration `0017_context.sql` (columns `restore_audit`, `restore_started` added); subagent sessions are not
    checkpointed; PR links unchanged.

#### Review 1 (Runner/Guest) - status

| # | Status | How |
|---|---|---|
| 1 | fixed | Runner 8 (wait for final checkpoint, `stale`) |
| 2 | fixed | Runner 1, 4 (retire hook, waiting dues, separate loops, parallel exports) |
| 3 | fixed | Runner 9 |
| 4 | fixed | Runner 12 |
| 5 | fixed | Guest 3, Runner 15 |
| 6 | fixed (runner part) | Runner 6, 8; hub audit `session.context_refused` by the hub package |
| 7 | fixed | Runner 6 |
| 8 | fixed | Runner 4 |
| 9 | fixed | Guest 4 |
| 10 | fixed | Runner 8 |
| 11 | fixed | Runner 5 |
| 12 | fixed | Runner 6, 7 |
| 13 | fixed | Guest 2, 5; runner `archive.inspect` no longer refuses lines |
| 14 | fixed | Guest 6 |
| 15 | fixed | Runner 14 |
| 16 | fixed | Runner 13 |
| 17 | fixed | Runner 11 |
| 18 | fixed | Runner 10 |
| 19 | fixed | Guest 5 |
| 20 | fixed | Guest 5, Runner 3 |
| 21 | fixed | Runner 16 |
| 22 | fixed | Runner 15 |

#### E2E corrections

The E2E run of the package found two product faults; both are fixed as a class (KONZEPT E-19) with lasting tests.

1. **The restore sentence never reached the model.** Claude Code 2.1.330 keeps the system prompt of a conversation
   in its transcript and reuses it on `--resume`, ignoring a new `--append-system-prompt-file`; after a restore (and
   after a suspend and resume on the same VM) the model saw its old platform session ID and old facts. The harness
   file is therefore no channel to a resumed model. The runner now sends the sentence as `restore.note` of `POST
   /v1/sessions` (contract change, additive: `restore` is new in this package) instead of appending it to
   `context_markdown`. The guest keeps, in the session's root-only meta file, the fact lines the conversation knows
   (`harness.fact_lines`: context and session block) and a pending notice (`sessions.notice_keys`): a fresh start
   knows the current facts; a restore or a resume into a new sandbox knows nothing certain and is told the note (or
   a fixed sentence) with every current fact line; a resume of the sandbox's own conversation is told a fixed sentence
   with the changed lines only when a line changed. The new hook addon `hook_addons/notice.py` takes the notice once
   (api.sock op `session.notice`) for the first `PreToolUse`, `PostToolUse` or accepted `UserPromptSubmit` answer -
   Claude Code adds `additionalContext` to the conversation, and 2.1.330 sends no `SessionStart` hook (ADR-029) - and
   the facts become known. The meta file is replaced atomically: a restart of the control process neither loses nor
   repeats the notice; a failed write leaves it pending. Contract: `mcp/objects.json` `session_notice` and
   `hook_context` (the hook responses accept a session notice, optionally followed by a ping notice). Facts are
   compared line by line; the static rules (`RULES.md`) are not part of the notice. Not carried in the context
   archive: what an earlier VM's conversation knew is unknown after a restore, so every fact is told.
2. **The idle checkpoint missed the last turn.** The runner takes it when the guest reports `idle`, which the `Stop`
   hook reports before Claude Code has written the turn; the first checkpoints held 545-623 bytes and restoring one
   failed with `agent_start_failed`. The guest export now waits for a quiet conversation file (`contexts.settle`:
   not written for 1 s, polled every 0.1 s, at most 20 s; for an `idle` session also until the last conversation
   record is a final assistant answer, read from at most the last 8 MiB; `working` and starting sessions at once) and
   refuses a conversation without a complete turn (`user` and `assistant` record) with `state_conflict` - the runner
   drops such a request (nothing to save). The runner also refuses such an archive itself (`archive.inspect`:
   `context_incomplete`, so an older guest cannot produce one): never sealed, and at a restore the reported emergency
   path `context_invalid` instead of a failing start. When the wait runs out the file is exported as it is (an
   incomplete last line is cut as before).

Open contract point (agent-core): `guest/session-context-export.response.json` does not list `workspace_omitted`
(optional count) and `guest/context-stage.request.json`/`changes/KOR-33.md` still say "regular files and directories
only" and "every line must be complete JSON"; the runner accepts archives with and without `workspace_omitted`.

## Note of the former `agent-core` repository

### Hub

1. Migration is `0019_session_context.sql`, not 0013: `agent_hub.db.migrate` refuses gaps, so 0013 cannot migrate on
   this branch. It is renumbered to the next free number on rebase (its content does not depend on the number).
2. New error code `quota_exceeded` (413) with the additive error detail `quota {scope: object|org, limit, used}`.
3. Hub-side audit `session.context_saved {chunks, size, pruned}` on commit (actor device); the runner sends
   `session.context_restored {object_id}` / `session.context_fallback {reason}` (they need `session_id`).
4. `session_row.context_restore`/`context_reason` and the device-state `context` are optional in the contracts
   (additive, old consumers unaffected); the hub always sends the two session fields.
5. The metric `agent_hub_session_context_total{result,reason}`: the hub has no Prometheus exposition, so it is a log line
   `metric agent_hub_session_context_total result=… reason=…` plus a process counter; the audit rows are the durable count.
6. An upload expires 6 h after its last chunk (not after its creation), so a slow large upload is not cut off.
7. Subagent sessions cannot upload (`400 request_invalid` at `/session_id`).
8. A pull-request tag stays linked to the first task; the same tag from another task is `409 id_conflict`.
9. `hub.pr_link` is the only table whose name matches the "no pull-request table" guards (ADR-014); the
   tests allow it explicitly because it holds only an opaque HMAC tag.
10. Review 1 decision 6: a chunk refused for a quota writes the hub audit row `session.context_refused {reason,
    scope, limit, used, requested}` (actor the device, result `denied`) in a transaction of its own after the refused
    one was rolled back; every refusal is one row (a runner keeps its local copy and does not retry a `413`).
11. Review 1 decision 10: "newest" is (`session.created_at` at the hub, object ID) descending in the list of a task,
    the device-state `context` and the pruning; `completed_at` and device clocks no longer order anything. A late
    commit of an earlier session is kept until the next commit of the task prunes it (never the one just committed).
12. `session.context_restored` carries `workspace` (`full|partial|omitted|empty`) and `stale`; the outbox handler maps
    `partial`/`omitted` to `context_reason` `workspace_partial`/`workspace_omitted` (closed list
    `common.json#/$defs/context_reason`; the constraint `session_context_reason` allows them only with `restored`).
    The audit detail checker gained `boolean` leaves for `stale`.
13. The web consumes `contracts/vectors/context.json` (`web/src/core/crypto/context.ts`): the browser holds the
    workgroup key and can compute the pull-request tag for `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`.
14. ADR-072 links the concept with absolute URLs into the agent repository: `KONZEPT.md` exists only there until the
    repositories are merged (INF-06), and the ADR must resolve in both checkouts.

## After the union of the repositories (INF-06)

1. **Numbers on the united tree:** ADR-072 (was 061/058/059 on the separate branches), hub migration
   `0019_session_context.sql`, runner migration `0017_context.sql`, scenario E2E-55 (registered in
   `e2e/platform/traceability.json` and `07-testing.md`).
2. **Work VM (KOR-29) and resume:** the work VM is not part of the context. It is removed when a session ends, is
   paused or the runner restarts; a continued session requests a new one when it needs it, and the session notice
   says so.
