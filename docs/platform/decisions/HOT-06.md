# HOT-06 decisions

1. **The guest owns the baseline, not the runner.** The only platform path into a workspace is the guest's
   `files/import`; making the imported tree the scanner's baseline there covers `github.checkout`, the ATK doubles
   and the work VM's results (KOR-29) without a runner change and without a new control endpoint. A runner-side
   "ignore these paths" message would need a second round trip and could race the scan.
2. **Survey the staging tree, not the installed one.** The staging directory is private to the control process,
   so its files cannot change between hashing and the move; the move is one `rename` that keeps inodes, mtimes and
   sizes. Surveying after the move would race the session.
3. **Pre-scan under the lock instead of discarding.** Session changes still pending when an import starts are
   reported first (normal records), then the import replaces the tree. Changes the session makes inside the target
   directory in the instant between the pre-scan and the rename are overwritten by the replace the session asked
   for; they are not reported because they no longer exist.
4. **Index write failure after the move keeps the in-memory baseline.** The import has happened; reverting the
   known state would report every imported file at the next pass. The next record writes the index again.
5. **Limits are a constructor value with defaults** (`scanner.Limits`), not `AGENT_GUEST_*` variables: the guest
   configuration refuses unknown variables and is changed by parallel packages; the defaults are what production
   needs. Defaults: 100 records per pass (a normal agent turn touches a handful of files; 100 is far above any
   hand edit and far below the thousands of a checkout), 1 000 records and 8 MiB of patches per 10 minutes and
   session (8 MiB is 128 maximal patches; a viewer loads the whole task timeline).
6. **`check` is never summarised.** One `Write`/`Edit` of the agent is one record at once, with its patch while
   the window allows (it counts against it), beyond the window without the patch; the package text demands that
   single edits keep appearing at once, and dropping the patch keeps a looping agent's bytes bounded.
7. **Excluded directories by name, at any depth but the `repos/<owner>/<name>` levels; `.gitignore` not
   evaluated** (ADR-057 §2). Committed build output such as an action's `dist/index.js` is therefore not shown in
   the chat (it is still committed). `build` and `out` are
   not excluded: in many repositories they hold authored sources.
8. **Summary `path`.** The record needs a `path` so a previous web client renders one calm line; it is the common
   directory of the summarised paths, else the first path.
9. **ADR number 057.** KOR-24 took 056 on the integration branch while this package was open; the ADR index must
   be gapless. KOR-29's unmerged branch also uses 056 and will renumber when it lands.
10. **Full-chain E2E for the session's own mass change is not possible yet.** A session cannot run anything
    (execution ban) until the work VM of KOR-29 exists, so 2 000 files can only be produced by the session user in
    the Linux suite (`tests/platform/guest_linux/capture/test_floods_linux.py`, real root and session user in the
    guest-ci VM); the checkout case runs through the whole chain as E2E-40 in agent-core.
