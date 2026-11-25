# KOR-48 – decisions

Package: pull requests at a glance and "Fix conflicts" ([ADR-084](../adr/084-fix-conflicts-by-merge-commit.md);
rules [A2](../KONZEPT.md#a-2), [E-04](../KONZEPT.md#e-04), [E-05](../KONZEPT.md#e-05), [E-19](../KONZEPT.md#e-19)).

1. **Optional tests.** KOR-43 landed during this package: the fixed instruction asks the session to run the
   repository's tests with `test.run` before the merge commit when it has tests (step 4; web `core/pulls/instruction.ts`
   and `agent_platform/pulls.py` share one text checked by a test). The text was never released before, so it stays v1.
2. **Merge in the runner, not with `git`.** The runner has no `git` binary (ADR-015); the three-way merge is done
   in Python (diff3 on `difflib` matching blocks). A merge may report a conflict where `git` would merge cleanly;
   the AI resolves it. Never the reverse in the tested cases.
3. **Status read as a device request.** Tokens never leave a device and repositories are confidential, so the
   fresh state comes from a device request `github.pull_status`, not from a hub route.
4. **Old links without body.** Links registered before this package have no `sealed` body; they are listed as
   "details unavailable" with their task. No backfill (the hub cannot, the runner does not keep the relation).
5. **Device-registered links cannot be deleted.** Only links a person made by address can be removed.
6. **Forks not supported.** A pull request whose head lives in another repository needs other rights and a push to
   that repository; `github.merge_base` refuses it (`merge_unsupported`) and the view shows the button as not
   available. A later package can add it.
7. **No client backfill of old links.** Reading every task's `github.create_pull_request` results to seal old links
   would scale with the history; old links are few (the registration exists since KOR-33) and keep their task.
8. **Dedupe of a merge commit without a new field.** The dedupe field set of `github.commit` (shared with the hub,
   its vectors and older runners) stays; the value of `tree_sha` is `<tree sha>+<merge parent>` for a merge commit
   (text fields are allowed by the normalisation), so a merge commit never shares the slot of a single-parent commit
   with the same parent and tree (code review M2).
9. **Design review** (fresh context): all high and medium findings taken over; see ADR-084 "Consequences".
10. **Code review** (fresh context): both high findings fixed (the workspace digest is stored only after a succeeded
    commit, with its SHA; a committed but unpushed state counts as clean only while the head contains it), medium:
    the message no longer offers `discard` without the person's consent, merge-commit dedupe value, deleted head files
    need the decision `deleted`, stronger differential test, blobs uploaded before the import; low: push marks the
    preparation only on its own branch, skipped export entries count as changes. Left: TOCTOU between pre-check and
    executor (the session waits for the operation), one-by-one status reads (inside the request limit), the old-hub
    fallback on `/sealed`.
11. **First-load guard 221 KiB.** After the rebases onto `develop` this package measured 221.5 KiB against the guard
    (219 KiB, raised to 220 KiB by KOR-57 meanwhile). The pull-request texts now load with the pull-request code
    (`core/i18n/lazy/pulls.ts`, `extendCatalogs`, 06-web.md §12.2; only the navigation label stays in the first load,
    area `pullnav`) and the error texts of the six new operation codes – required for every contract code by the
    completeness test – were shortened: 220.1 KiB. The remaining part is what the first screen needs of this feature
    (error texts, navigation entry, request kind); the guard moves from 220 to 221 KiB like KOR-57's step. The 300 KiB
    ceiling of the specification stays.
