# ADR-057: Platform imports are the scanner's baseline; file floods become summaries

Status: Accepted (2026-11-23)

## Context
- The guest's workspace scanner (05-runner.md §6.4, `agent_guest/scanner.py`) reports every new, changed or deleted
  file of a session's workspace as `session.file {path, op, sha256, size, patch}`; a new file's patch is its whole
  content. It already took the tree present at the first start as its baseline ("files the runner imported before
  the agent started are not session changes"), but not trees the platform puts into the workspace later.
- In production a session checked out a repository (`github.checkout` → guest `files/import`). The next scan
  reported every file of the repository, thousands of records with many megabytes of patches, encrypted, synced to
  the hub and delivered to every viewer of the task; the browser tab froze. The operation itself is already visible
  as an operation card with its result.
- The same flood follows any mass change inside the workspace: dependency installs, a formatter over the whole
  repository, code generators, unpacked archives, results of a work VM.
- The only path by which the platform writes into a workspace is the guest's `files/import` (callers:
  `github.checkout`, the ATK doubles, and the work VM of KOR-29 for results). Branch switches, pulls and rebases are
  not platform operations (the platform has no git working copy; `github.create_branch` and `github.commit` work on
  GitHub, the latter only exports), and a resumed session keeps its VM and workspace.

## Decision
1. **An import is the baseline, in the guest.** `files.import_tar` takes a `guard(dir, tree_fd)` that is called with
   the fully extracted, private staging tree before it is moved, and returns the context the move runs in. The
   control route passes `Capture.import_guard(session)`: the scanner surveys the staged tree (hash, size, mtime and
   cached text of every file, the same rules as a scan) and then, **holding the scanner lock**: (a) runs a normal
   scan, so session changes made before the import are reported; (b) the tree is moved into place with the existing
   single `rename`; (c) the known entries below `dir` are replaced by the survey without records (no `delete` for
   the replaced tree, no `write` for the new one) and the index file is written. The rename keeps the inodes, so
   the surveyed mtime and size are those of the installed files; a change by the session after the rename alters
   mtime or size (or the content `check` hashes) and is reported normally. A failed move keeps the known state.
   A session without a running capture (suspended, stopping) gets a scanner that loads and writes the index under
   the capture lock, so a later start does not report the import either; after a restart of the control process
   the index already holds the import.
2. **Excluded directories.** Directories named `node_modules`, `bower_components`, `.venv`, `venv`, `__pycache__`,
   `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `.nox`, `.gradle`, `.next`, `.nuxt`, `.svelte-kit`,
   `.turbo`, `.parcel-cache`, `.cache`, `.terraform`, `dist` and `target` are neither walked nor checked (except
   as the owner or name level of a checkout directory `repos/<owner>/<name>`, so a repository called `dist` is
   scanned; an import into an excluded directory surveys nothing): they hold
   regenerated, not authored content and one install can exceed the walk's 100 000 entries, which would also stop
   deletions from being reported. An index of an earlier version drops their entries silently. `.gitignore` is not
   evaluated: the session writes it (it could hide its own changes from the record) and a partial implementation
   of its semantics would mislead.
3. **Bulk and window.** A scan pass with more than `bulk_files` (100) records writes one
   `session.file {path, op: "summary", files, deleted, bytes, paths, reason: "bulk"}` instead (common directory,
   at most 20 paths, no patches). Per session and `window_seconds` (600) at most `window_files` (1 000) single
   records and `window_bytes` (8 MiB) of patches are written; the rest of a pass becomes one summary with
   `reason: "limit"`. `check` - one file the agent wrote, reported by PostToolUse - is always written singly at once
   (it is paced by the model's tool calls); within the window with its patch, beyond it without (a few hundred
   bytes per record, so the bytes stay bounded). The known state follows every
   record, summarised or not, so nothing is reported twice and later edits diff against the latest content.
   The bounds are a `Limits` value of the scanner with these defaults (not an environment variable yet: the guest's
   configuration is fail-closed and shared by parallel packages).
4. **Contract.** The summary is a third, additive `oneOf` branch of `body_session_file` (agent-core,
   `contracts/changes/HOT-06.md`). The event kind stays `session.file`: the hub only stores ciphertext, the runner
   passes the body through unchanged, and a previous web client shows the summary as one "Changed `path`" line
   (unknown `op` falls back to `edit`). HOT-05 renders it properly.

## Consequences
- If the index cannot be written right after the move, the in-memory baseline stays and the import still
  succeeds (a line on stderr); the next record writes the index.
- Summaries are not counted against the window: at most one per scan pass (5 s), each a few kilobytes.
- A transient scanner (session without capture) holds the capture lock while it starts; without an index this
  hashes the workspace once and delays other sessions' capture events for that time (rare: an import into a
  suspended or stopping session).
- A checkout produces the operation card and no file records; the first edit of an imported file has its patch as
  long as the file's text fit into the scanner's text cache (16 MiB per session, as for the start baseline).
- Changes the session makes inside `dir` in the instant between the pre-scan and the rename are overwritten by the
  replace the session itself requested and are not reported (they no longer exist). A crash of the control
  process between the rename and the index write (no I/O in between but the index file) would report the import
  after the restart; accepted.
- Files below excluded directories never appear in the chat; they are still committed by `github.commit` if the
  session lists them.
- Deploying needs a new guest in the golden image; runner and hub are unchanged (see the HOT-06 report).
