# RUN-10 decisions (GitHub executors II: checkout, commit, push; guest file import and export)

Small decisions of the package that brings repository content into a session workspace and changes back to GitHub
without the `git` binary (05-runner.md §12 step 3, §12.1; 03-api.md §7.1, §9; ADR-015; decisions CTR-02 no. 8).
No decision changes an architecture rule, so there is no ADR.

## Guest (`agent_guest/files.py`, `control_routes/files.py`)
1. The control process is root and the session user may swap any part of its workspace for a symbolic link at any
   time. Nothing follows a link: directories are opened one by one relative to their parent with
   `O_NOFOLLOW | O_DIRECTORY`, files with `O_NOFOLLOW` (and `O_NONBLOCK`, so a FIFO never blocks), and checked with
   `fstat` after opening. Whether a refusal was caused by a link is decided with `lstat` (Linux answers `ELOOP`, macOS
   `ENOTDIR`).
2. Import extracts the whole archive into a staging directory `<session dir>/.files-<random>` (inside the root-owned
   0711 session directory, invisible to the session) and moves the result into place with one `rename`. A refused
   archive therefore leaves the workspace unchanged. `replace=true` first moves the existing target - whatever it is,
   a link included - into the staging directory and removes it there; a target that reappears in between is tried
   three times, then `state_conflict`. `replace=false` (not used by the runner) needs a missing or empty directory.
3. Codes: paths (absolute, `..`, `.`, empty segment, `.git` segment, control characters, backslash, invalid UTF-8;
   the rules of `ops/rules.json#/path`, which name guest file transfers) → `path_invalid`; member types other than regular file and directory (links, hard links, devices, FIFOs, sparse) and
   malformed or duplicated members → `request_invalid`; more than 100 000 members, 512 MiB, or an extended header
   (PAX, GNU long name) over 64 KiB → `size_exceeded` (the header is refused before `tarfile` reads it into memory). The
   `.git` rule is the one the contract names for `rel_path` (`ops/rules.json#/path`); git never stores `.git` in a tree.
4. Ownership is the session user and its primary group, which `sessions.py` creates with the same ID; files 0600,
   directories 0700, the archive's modes, owners and set-ID bits are ignored. The executable bit is lost by design
   (the workspace is `noexec`); `github.commit` keeps the parent's `100755` instead.
5. Export copies every file once into a spool (memory up to 1 MiB, else a file in the staging directory) while hashing
   it, so manifest and content always agree although the session keeps writing. Only regular files owned by the
   session user are exported (a hard link to a file of another owner would otherwise leak it); others are skipped
   `unreadable`, links `symlink`, everything else `special`. `.git` directories are left out; entries whose names are
   not valid paths cannot be listed in the manifest and are left out silently; directories deeper than 128 levels are
   `unreadable`. Every directory entry seen counts against the 100 000 limit (skipped ones too), so a session cannot
   make the root process build an unbounded manifest. The answer stream removes its staging directory when it ends,
   is closed, or is collected unread (a client that went away before the body was sent).
6. Sessions: unknown → `404 not_found`, stopped or failed → `409 state_conflict` (as for pings). A request refused
   before its body is read closes the connection (routing rule), so the runner sees a transport error for a refused
   import of a session that ended; that is retried and then `upstream_error`.
7. The shared contract test of GST-01 (`test_server.py::test_control_routes_match_the_contract`) knew only `query` and
   `body` request kinds; `contracts/guest/INDEX.json` defines `query+tar`, so the test maps it to a raw body (one line).

## Runner (`agent_runner/github/`)
8. Modules: `checkout.py`, `commit.py`, `push.py` (executor and pre-check each), `gitobj.py`, `tarsafe.py`,
   `snapshot.py` (scratch, guest access, export snapshot), and `access.py` (pre-check client and `GET /repos/{r}` with
   the codes of RUN-09's `precheck.repo_access`, returning the answer so the default branch and `html_url` need no
   second request). The registry files `executors.py` and `op_prechecks.py` got the three types; RUN-09's test of the
   exact executor set now checks its five entries as a subset (this package's suites check the new ones).
9. Checkout (05 §12.1 "reject … links" read as: never create them): the ref is resolved in the pre-check (`GET /repos/{r}/commits/{ref}`; absent ref = the default branch of
   `GET /repos/{r}`), so the dedupe field `ref` is the commit SHA. The archive bound (512 MiB) is counted by the writer
   and the client's own bound is lifted for the download, so an oversized archive is `size_exceeded`, not
   `upstream_error`. Links and devices inside a GitHub tarball are **skipped** (a repository may contain symbolic
   links; they are never created); absolute, `..` and `.git` paths refuse the archive (`path_invalid`).
10. The guest refuses chunked bodies (routing rule), so the re-packed tar (≤ 512 MiB) is sent as bytes with a
    `Content-Length`; the guest client is not changed.
11. The workspace directory is `repos/<owner>/<name>` in lower case (the allow-list is lower case), so a commit finds
    the checkout whatever case the session writes. The base SHA per (session, repository) is the stored result of the
    latest succeeded checkout of that session (`runner.operation`), so no new table or migration is needed.
12. Commit semantics: without `paths` the snapshot is the complete set of regular files of the repository; with
    `paths` only files at or below a listed path are compared. Symbolic links and submodules of the parent are kept
    (the checkout never creates them, so their absence is no deletion), and so does every parent entry at or below a
   path the export skipped (`symlink`, `special`, `unreadable`: a transient read error must not delete a file at
   GitHub); a regular file of the session replaces a link,
    submodule or directory at its path. Executable bits of existing files are kept, new files are `100644`. Deletions
    are sent first in the tree request (`sha: null` with the parent's mode and type).
13. The path rule applies to added and modified files only (an unchanged file never blocks a commit). Limits count
    changed files only: added, modified and deleted (≤ 500), each added or modified file ≤ 10 MiB,
    together ≤ 50 MiB. The secret scan runs on every added or modified file of the snapshot (bytes read as Latin-1, so
    the ASCII patterns match in any encoding); unchanged files are not scanned - the commit does not add a secret that
    is already in the parent. The message names only the pattern ID, never the content or path.
14. A recursive tree listing that GitHub truncates refuses the commit with `size_exceeded` (it could not be compared).
    An empty change set is allowed (GitHub accepts an empty commit).
15. The executor checks each stored blob against its SHA before upload and each blob SHA GitHub answers; a difference
    in either, or in the tree SHA, is `tree_mismatch` (not retryable). A missing snapshot (daemon restart: `tmp` is
    emptied at start) is `not_found`.
16. Scratch `tmp/op-<id>` is removed when the operation ends; after a retryable failure the commit keeps it for the
    next attempt. The commit pre-check deletes the export as soon as it is read and keeps only the blobs it will
    upload (≤ 50 MiB), because operations the hub refuses or finds duplicate never reach an executor (the pipeline is
    RUN-07's and offers no hook for them); `sweep` removes directories older than 2 h (longer than any operation can
    wait: 10 min unregistered, 30 min accepted, retries) before each new snapshot or checkout. `plan` and the secret
    scan are linear in the number of files and run in a worker thread, so a large repository never blocks the daemon.
17. Push: an identical branch is not written; `compare` `behind`, `diverged` or `404` (no common history) →
    `non_fast_forward`; a `422`/`409` on create or update reads the ref again (at the commit = an earlier attempt won).
    The result URL is `<html_url of GET /repos/{r}>/tree/<branch>`: the configured web URL is the device-flow host and
    may be a loopback address in tests, while GitHub's own `html_url` is always https (result contract).
18. The commit pre-check reaches the guest through the daemon's installed pipeline (`operations.pipeline.existing`);
    the pre-check request carries no session environment. Without a pipeline or VM it refuses with `not_found`.

## Tests
19. The RUN-10 kit (`tests/platform/runner/github/run10_kit.py`) runs the RUN-07 pipeline with the real executors and
    pre-checks against the GitHub double, and a guest whose file endpoints are the real `agent_guest.files` on a
    temporary workspace (the test user is the session user). The double redirects tarballs to its public https host;
    the kit's transport sends that host to the double's loopback address (what DNS would do).
20. Linux-only checks (real root, a real session user with the production layout) are in
    `tests/platform/guest_linux/test_files_linux.py`; the any-OS suite covers every line and branch of the guest code
    on its own.

## Independent review (subagent)
21. 1 high, 4 medium, 4 low. Fixed: H1 (quadratic `plan` on the event loop → linear, worker thread), M2 (header bombs
    → 64 KiB cap on extended headers in `tarsafe` and the guest), M3 (scratch disk → export deleted, blobs trimmed,
    sweep after 2 h), M4 (backslash rule of `rules.json` in guest and `tarsafe`; path rule only for changed files), M5
    (every export entry counted), L6 (skipped entries kept, never deleted), L7 (staging directory of an unread export
    stream), L9 (gate results in the acceptance file).
22. Not changed: L8 (the re-packed checkout tar is held in memory once, ≤ 512 MiB): the guest refuses chunked bodies
    and the guest client sets the body headers itself, so a streamed body with a `Content-Length` needs a change of
    `guestlink/client.py` (not this package's). The runner device has ample memory and checkouts are rare; noted as an
    open risk.
