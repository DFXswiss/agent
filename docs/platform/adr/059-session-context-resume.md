# ADR-059: The original session continues - encrypted context checkpoints and restore on any VM

Status: Accepted (2026-11-23)

## Context
- [Idea 40](../KONZEPT.md#k-idee-40): on a later change request the platform finds the earlier task/session and restores
  **the original Claude Code session with its complete context**, also on another suitable Lima VM; "it is not enough
  to start a new session with a summary". [Idea 43](../KONZEPT.md#k-idee-43) (sentence 4): continuing a task uses the original
  session and its context. [Idea 39](../KONZEPT.md#k-idee-39): after the task is closed, logs, results **and context** are
  kept. [§16 point 3](../KONZEPT.md#k-16-b3): "the session is resumable after completion"; [§16 point 7](../KONZEPT.md#k-16-b7): still to
  be checked "session restore across devices". [Idea 44](../KONZEPT.md#k-idee-44) (basis only): sessions are synchronised
  continuously so that they continue on another device without losing the last synchronised context. The concept
  lives in the agent repository ([KONZEPT.md](../KONZEPT.md)); the links are absolute so that they resolve from both
  repositories.
- Before this ADR the platform continued the original Claude Code session only while the main session was `idle` or
  `suspended` on **the same VM** (`claude --resume`, `agent_runner.prompts.resume`, ADR-025). After `stopped`/`failed`,
  "Close task", a VM change or deletion, or more than seven days (guest retention), the hub created a new main session
  that started **fresh** and was told to read the chat (`RESTART_LINE`, decisions RUN-05 no. 20, 03-api.md §4.6.3) -
  exactly the path idea 40 excludes. The audit AUD-04 (N1, W9) ranks this the most important gap.
- Spike `spikes/session-resume` (workspace, KOR-33): see "Evidence" below.
- Constraints: the hub is zero-knowledge (00-overview.md §3); transcripts and workspaces contain secrets; the VM is
  untrusted for the host (B4); the session is untrusted for the guest (B5); forward and backward compatibility with
  runners and hubs in production (PROJECT.md decision 10).

## Decision

### 1. What "the session" is
The concept's session is the **Claude Code conversation** (`claude_session_id`, its transcript) together with the
**workspace** it worked on. The hub keeps its model of platform sessions: every run of a task's main agent is a row
of `hub.session`; `stopped`/`failed` stay final. A follow-up after `stopping`/`stopped`/`failed` or on a closed task
still creates a new main session row (03-api.md §4.6.3), but that row **continues the same conversation**: the runner
restores the newest context checkpoint of the task into the new sandbox and starts `claude --resume
<claude_session_id>` with the **same** `claude_session_id`. The transcript keeps every earlier turn; nothing is
summarised. The `idle`/`suspended` fast path on the same VM is unchanged.

### 2. Checkpoint content (context archive v1)
A checkpoint is a tar archive produced by the guest (`POST /v1/sessions/{id}/context/export`), members in this order:
1. `context.json`: `{"v":1,"agent_kind","agent_version","claude_session_id","session_id","task_id","cwd",
   "created_at","transcript_files","workspace":"included"|"omitted_too_large"|"empty","workspace_files",
   "workspace_bytes"}` (`cwd` = the session's work directory at export time).
2. `transcript/<path>`: only the conversation's own files below the session's Claude configuration directory
   (`home/.claude/`): `projects/<cwd slug>/<claude_session_id>.jsonl` and the companion directory
   `projects/<cwd slug>/<claude_session_id>/**` when present, `todos/<claude_session_id>-*.json`,
   `file-history/<claude_session_id>/**` when present (spike: only the JSONL is required; the companion directory
   holds large tool results whose stored paths must stay valid; anything else - `settings*.json`,
   `.claude.json`, `commands/`, `agents/`, `skills/`, hooks, credentials - is **never** exported, so a session cannot
   plant configuration into its successor).
3. `work/<path>`: the workspace (`/srv/sessions/<id>/work`): regular files with their execute bit (modes reduced to
   `0644`/`0755`), directories including empty ones, and symbolic links that stay inside the workspace (as links);
   devices, FIFOs, links leaving the workspace and protected names are left out, and every omission is counted in
   `context.json` (review amendment 5). When the workspace exceeds the workspace limit (§6) it is left out
   (`"workspace":"omitted_too_large"`) and the transcript is still saved; the restored session and the user are told
   so (`context_reason` `workspace_omitted`, or `workspace_partial` when entries were left out).
A JSONL line that is incomplete at export time (the agent was writing) is cut off at the last line feed; the export
does not parse transcript lines otherwise (AEAD, digest and manifest secure the integrity; Claude Code tolerates its
own damaged lines, review amendment 13).
The archive is read on the host with the untrusted-archive rules of `agent_runner.github.tarsafe` (regular files and
directories only, normalised relative paths, sizes and counts checked) before it is encrypted.

### 3. Encryption (zero-knowledge, keys of the task's workgroup)
- The archive is compressed (gzip, level 6) and split into chunks of at most **2 MiB** of compressed bytes.
- Object key: `k_ctx = HKDF(ikm=WK_e, salt=salt (32 B random per object), info=P‖"context\0"‖object_id)` with the
  current workgroup epoch `e` (04-crypto.md §3; `WK_e` is held by members and by devices assigned to the workgroup -
  "keys like the other contents of the task").
- Chunk `i`: `nonce12 ‖ AES-256-GCM(k_ctx, chunk, AAD = P‖"context-chunk\0"‖canonical({"index":i,"object":id}))`.
- Manifest (canonical JSON, clear): `{"v":1,"object","org","wg","task","session","author","epoch","salt","chunks",
  "size","digest","ts"}` where `size` = sum of the chunk ciphertext lengths and `digest` = b64u SHA-256 over the
  concatenation of the SHA-256 of every chunk ciphertext in index order. `sig = Ed25519_device(P‖"context\0"‖
  canonical(manifest))` by the authoring device.
- The hub sees only IDs, epoch, sizes, counts, digests and times (added to 04-crypto.md §7). Plaintext exists only on
  the execution device (runner scratch, 0700, removed after use) and inside the VMs of the workgroup.
- A reader (runner of the restoring device) checks: the manifest signature with the author device's key from its
  verified membership log of the org, the author was assigned to the workgroup (log), manifest `org/wg/task` equal
  the restoring session's, `chunks`/`size`/`digest` equal the downloaded chunks, every chunk decrypts (AEAD), the
  decompressed archive passes tarsafe and `context.json` names the same `task_id` and a `session_id` of that task.
  Any failure is `context_invalid` (fallback, §7). The hub can withhold or roll back to an older checkpoint of the
  same task (like any withheld event); it cannot forge or read one.

### 4. When checkpoints are taken (runner, continuous)
For main sessions (depth 0) the runner takes a checkpoint when the guest reports `idle`, `suspended`, `stopped` or
`failed` (the guest keeps directories after an end, so this works after the agent ended), and every
`context_interval_s` (default 600 s) while the session is `working`, and before a VM is stopped, deleted or replaced
by a new golden (bounded wait; a due checkpoint stays due until the VM runs again or is gone). One checkpoint per
session runs at a time; a due checkpoint during a running one is coalesced. Export and upload are separate steps:
exports of several sessions run in parallel (bounded), uploads in the background; hashing, compression, encryption
and file writes run outside the event loop. A checkpoint whose transcript and workspace members are identical
(digest over those members only, not over `context.json`) to the session's previous one is not uploaded again. The
runner keeps the newest encrypted checkpoint of each task in its home (`contexts/`, sealed chunks and manifest as
uploaded; at most `context_cache_bytes`, default 20 GiB, for all objects, the newest of each task kept) so that a
restore on the same device needs no download and a checkpoint not yet uploaded (hub unreachable, quota) is not lost;
upload is retried with the outbox backoff. A hub without the routes (`404` on the route) gets no uploads, only the
newest local copy is kept. An upload the hub no longer knows (`404`, the 6 h expiry) is discarded and the checkpoint
becomes due again under a new object ID. Subagent sessions are not checkpointed: they end
with their parent, and the parent's transcript holds their reports (decisions/KOR-33).

### 5. Hub storage and API
- Tables `hub.context_object` (object ID, task, session, org, wg, author device, epoch, chunk count, size, digest,
  manifest + signature, state `uploading`/`complete`, created/completed times; foreign key to `hub.task` **on delete
  cascade** - a checkpoint is deleted with its task) and `hub.context_chunk` (object, index, ciphertext `bytea`,
  SHA-256). PostgreSQL keeps chunks transactional and inside the existing backup.
- Device API (signed; upload and commit: the calling device is assigned to the session's workgroup **and** the main
  session runs or ran on it, rule E7):
  `PUT /v1/device/contexts/{object}/chunks/{index}` `{session_id, ct}` (idempotent for identical bytes, `409
  id_conflict` otherwise); `POST /v1/device/contexts/{object}/commit` `{manifest, sig}` (verifies signature, session,
  chunks, digest; marks `complete` atomically; idempotent; one commit per task at a time); `GET
  /v1/device/contexts/{object}` → `{manifest, sig, created_at, completed_at}` and `GET
  /v1/device/contexts/{object}/chunks/{index}` → `{ct}` for any device assigned to the workgroup (a restore may run
  on another device). An upload in progress or an object of another workgroup is never listed or downloadable.
- **Order.** "Newest" is (creation time of the checkpoint's session at the hub, object ID UUIDv7) descending
  everywhere - the list, the device state and the pruning; never a device clock or the time of the commit, so a late
  upload of an earlier session never supersedes its successor's (review amendment 10).
- `GET /v1/device/state`: a main session item names `resume_from` (the task's previous main session, **on any device**
  - the same-device condition of 03-api.md §4.6.3 is removed) and `context` `{object_id, session_id, created_at,
  size}` = the newest complete checkpoint of the task's main sessions (`session_id` tells the runner whether it is the
  final checkpoint of `resume_from`, §7). Old runners ignore the new field (the device
  state is read with tolerant field access) and keep their fresh-start behaviour; new runners on an old hub see no
  `context` and fall back (§7) - forward and backward compatible.
- Browser and agent (KOR-24 parity: the same documented API without a browser): continuing is the existing
  `POST /v1/tasks/{task}/prompts`; `GET /v1/tasks/{task}/contexts` (`task:read`) lists the checkpoints' metadata;
  `GET /v1/sessions/{id}` and the session rows carry `context_restore` (`restored`, `fallback` or null) and
  `context_reason`.

### 6. Quotas and deletion (settings with generous defaults, PROJECT.md 13/14)
Hub: `AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES` (default 8 GiB per checkpoint), `AGENT_HUB_CONTEXT_KEEP` (default 3 complete
checkpoints per task; older ones are deleted when a newer one completes), `AGENT_HUB_CONTEXT_ORG_BYTES` (default
200 GiB per organisation; refusal `413 quota_exceeded` with the numbers; the hub writes the audit row
`session.context_refused {reason, scope, limit, used, requested}`, the runner keeps the local copy and a restore on
the same device uses it), uploads without a new chunk for 6 h are removed by a job. Runner: workspace limit `context_workspace_bytes`
(default 4 GiB), transcript limit `context_transcript_bytes` (default 4 GiB). Reaching a limit gives a clear message
(audit row, metric, log line), never a silent loss of older checkpoints.

### 7. Restore (runner) and the reported emergency path
- A new main session whose desired entry names `context`: when `context.session_id` is not `resume_from`, the final
  checkpoint of the previous session is still due; the runner waits for it (same device: the local checkpoint, sealed
  locally is enough; another device: it reads the device state again) up to `context_wait_s` (default 180 s) and
  then continues with the newest available one and says so (`stale`, review amendment 1). The request amender
  fetches the checkpoint (local cache first - a copy that fails verification falls back to the hub's - else the hub,
  with backoff up to `context_wait_s` for transient errors; `404`, an invalid object or an incompatible version
  fail at once), verifies and decrypts it (§3; the author must have been assigned to the workgroup at the log state
  of the checkpoint) into a 0700 scratch directory, checks compatibility (`agent_kind` equal; the target Claude Code
  version must be equal to or newer than the highest `version` in the transcript - older versions silently skip
  unknown entry types; an unparseable version is `version_incompatible`), stages it in the target guest
  (`POST /v1/contexts/{session}/stage`, root-only staging directory, only the files of that `claude_session_id`; the
  rewriting and the ownership change happen there, with their own timeouts) and sets `restore {claude_session_id,
  object_id}` in `POST /v1/sessions`, which then only moves the staged tree. The guest moves the staged transcript into the new session's Claude directory
  under the slug of the **new** work directory, replacing literally `/srv/sessions/<old>/` by `/srv/sessions/<new>/`
  and the old slug by the new one in the transcript and in the file names of the companion directory (without it the
  model sees the old directory and a "(was …)" note; entries are never filtered or reordered - filtering broke a
  compacted transcript in the spike), the workspace into `work/`, owned by the session user, and starts
  `claude --resume <claude_session_id>`. Its harness context gets **one** sentence about what changed (a new VM or
  device only when it changed, checkpoint time and whether it was the newest, workspace restored fully, partly or not,
  a work VM - KOR-29 - must be requested again); the transcript itself is never replaced or summarised.
- A suspended session whose VM is gone (deleted, replaced by a new golden, another VM of the workgroup) is restored
  the same way under its own session ID instead of failing with `vm_start_failed`; its new `vm_id` is stored only
  after the create succeeded. A restore into a guest that still has a suspended session of that ID (A → B → A)
  replaces its directories.
- The guest's workspace scanner takes the restored tree as its baseline (it starts after the restore, HOT-06's rule):
  a restore never produces `session.file` events for restored files.
- **Emergency path only:** when no checkpoint exists for a task that had an earlier session (`context_missing`), or
  the checkpoint cannot be used (`context_invalid`, `context_unavailable` - hub/cache unreachable after retries,
  `version_incompatible`, `restore_failed` - the guest refused the stage or the start with `--resume` failed), the
  session starts fresh with the earlier harness line telling it to read the task's messages, and the runner sends
  the audit row `session.context_fallback {reason}`; the hub stores it on the session (`context_restore=fallback`,
  `context_reason`), counts it (`agent_hub_session_context_total{result,reason}`) and the web shows "This task was
  continued by a new AI because …" on the task. A successful restore sends `session.context_restored {object_id,
  workspace, stale}` once the guest reports `agent_starting`; the hub stores `context_restore=restored` and, for
  `workspace` `partial` or `omitted`, `context_reason` `workspace_partial`/`workspace_omitted`, which the web shows
  ("Continued with the full conversation; the workspace was only partly restored" / "… was too large and was not
  restored"). A `--resume` start that fails in the start window of a restored session is marked `restore_failed`;
  the session stays final and "Retry" applies (sessions are never revived). Never silent.

### 8. Pull request → task (idea 40 sentence 1, needed by idea 41)
Repository and PR number are confidential. The relation is stored as an opaque tag:
`hub.pr_link (wg_id, epoch, tag, task_id, session_id, created_at)`, `tag = HMAC-SHA256(HKDF(ikm=WK_e,
info=P‖"pr-link"), canonical({"number":n,"repo":"<owner>/<name> lower case"}))`. The runner registers it after a
successful `github.create_pull_request` (`POST /v1/device/pr-links`); a member or the user's agent who knows repo
and number and holds `WK_e` asks `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` (`wg:read`) → `{task_id,
session_id, created_at}` (each epoch it holds). The button on the PR (idea 41) is a later package.

### 9. Interfaces kept open
- KOR-31 (credit exhausted → "controlled stop, resumable"): stopping a session with `session.control {action:"stop"}`
  now always leaves a checkpoint (§4, taken at `stopped`); the next prompt after a credit restores it (§7). The
  billing package only needs to stop instead of failing; no extra resume mechanism.
- Idea 44 / AUD-04 N6 (device failure): the checkpoint is device-independent; failover only has to place the next
  main session on another device - the restore path is the same.
- KOR-29: the work VM is not part of the context; after a restore it is requested again.

## Review amendments

An independent review (security, zero-knowledge, data loss) of the first implementation led to these changes, all
reflected above:

1. A follow-up right after a stop or close waits a bounded time for the final checkpoint of `resume_from` and marks a
   restore from an older one as `stale` (§7).
2. VM stop, deletion and golden replacement take due checkpoints first; due checkpoints are never dropped; export
   and upload are separate, exports run in parallel (§4).
3. Transient fetch errors are retried with backoff up to `context_wait_s`; only `404`, invalid objects and
   incompatible versions fall back at once (§7).
4. A suspended session moved to another VM stores its new `vm_id` only after a successful create (§7).
5. The workspace keeps execute bits, empty directories and internal links; omissions are counted and reported as
   `workspace_partial`/`workspace_omitted` to the session and the user (§§2, 7).
6. A chunk refused for a quota is audited by the hub (`session.context_refused`); the device keeps and uses its
   local copy (§§4, 6).
7. An upload the hub expired is discarded and taken again under a new object ID (§4).
8. Hashing, compression, encryption and file writes run outside the event loop; buffers are bounded (§4).
9. The heavy restore work happens at the stage with its own timeouts; the create only moves the tree (§7).
10. Checkpoints are ordered by (creation of their session at the hub, object ID) everywhere (§5).
11. Unchanged checkpoints are detected by a digest over the transcript and workspace members only (§4).
12. The local cache limit covers all objects and keeps the newest of each task; an old hub gets no uploads (§4).
13. Transcript lines are not parsed at export; only an incomplete last line is cut (§2).
14. A restore replaces the directories of a suspended guest session of the same ID (§7).
15. A failed `--resume` start stays "Retry"; it is marked only inside the start window of a restored session (§7).
16. `session.context_restored` is sent only after the guest reports `agent_starting` (§7).

Lower-ranked findings (author assignment at the log state, fallback from a damaged cache to the hub copy, staging
only the conversation's files, guarded number parsing, a headroom derived from the file count, the "new VM" sentence
only after a change) are fixed as well.

## Evidence
Spike KOR-33 (two clones of the golden, Claude Code 2.1.330, model double journalling every request): a transcript
created by user A in VM 1 was resumed by user B (other UID) in VM 2 in another work directory with `--resume`; the
resumed request contained every earlier turn and the session ID stayed the same (the file is appended to;
`--fork-session` would create a new ID). Only `projects/<slug>/<id>.jsonl` is required. The `version` field is
ignored; 2.0.76, 2.1.300, 2.1.330 and 2.1.331 resumed each other's transcripts without any message (older versions
drop unknown entry types silently). A 62 MB transcript resumed in 2.8 s, one compacted after 104 MB in 1.3 s;
compacted transcripts resume across VMs. Damaged lines are never reported. Details: `decisions/KOR-33.md`.

## Consequences
- Idea 40/43.4/39/§16 hold after close, stop, failure, VM deletion, more than seven days and on another VM or device.
- The hub stores large opaque objects (MB to GB per task); quotas and the cascade bound it.
- The transcript format is internal to Claude Code and may change; the version rule and the reported fallback keep
  a broken format visible instead of losing the task.
- The `RESTART_LINE` path of RUN-05 stays only as the reported emergency path.

## Alternatives considered
- Keep the same platform session row and revive it after `stopped`: breaks the final states, transitions and audit
  history of 02-data-model.md §2.4.1 for no gain; the conversation identity is `claude_session_id`.
- Store checkpoints as events: events are fanned out to browsers and limited to 256 KiB; checkpoints are device-only
  bulk data.
- Filesystem storage at the hub: outside the transactional backup; PostgreSQL `bytea` chunks of 2 MiB are adequate.
- Summary into a new session: explicitly excluded by idea 40.

## Deviation from DESIGN.md
None beyond ADR-001. Replaces decisions RUN-05 no. 20 ("a new session never resumes with `--resume`") and the
fresh-session sentence of 03-api.md §4.6.3.
