# ADR-084: Pull requests at a glance and "Fix conflicts" – the same session merges the base into the branch with a merge commit, no automatic merge

Status: Accepted (2026-11-24)

## Context
- [Idee 41](../KONZEPT.md#k-idee-41): a button on the pull request starts the AI-supported resolution of merge
  conflicts, preferably by resuming the original session; changes, GitHub actions and optional tests run through
  controlled platform operations; the existing pull-request branch is updated; an automatic merge is not part of it.
- [Idee 40](../KONZEPT.md#k-idee-40): for later change requests, also to an existing pull request, the platform finds
  the earlier task and session and restores the original session.
- [A2](../KONZEPT.md#a-2): the branch is updated **with a merge commit**, nothing is overwritten; a rebase with
  `--force-with-lease` only after an explicit decision (not built). [A17](../KONZEPT.md#a-17): overwritten is
  nothing. [§11 Punkt 7](../KONZEPT.md#k-11-b7) / [§13 Punkt 7](../KONZEPT.md#k-13-7): the detailed pull-request data
  model stays deferred ([ADR-014](014-pull-request-model-deferred.md)); only what the button needs is added.
- Rules: [E-04](../KONZEPT.md#e-04) (everything also for the user's agent), [E-05](../KONZEPT.md#e-05) (a missing
  connection is requested), [E-11](../KONZEPT.md#e-11) (no approval step), [E-19](../KONZEPT.md#e-19) (exclude the
  error class: a commit that still holds conflict markers, a "merge" without the second parent, a forced update).
- Built before: the opaque pull-request tag `hub.pr_link` registered after `github.create_pull_request` and the
  lookup by tag ([ADR-072](072-session-context-resume.md) §8); every follow-up prompt continues the same Claude Code
  conversation, a fresh AI only as the reported emergency path (ADR-072 §§1, 7); GitHub identities
  ([ADR-078](078-github-identities.md)); the situation view and `human.question`
  ([ADR-076](076-situation-view-and-attention-categories.md)). The runner never runs `git`: checkout is a tarball,
  commits are made with the Git Data API ([ADR-015](015-github-app-device-flow-rest-only.md), 05-runner.md §12.1).
  `github.commit` always had exactly one parent and `github.push` is fast-forward only.
- The legacy tool chain resolves conflicts by rebase and forced push (against A2); only its marker detection idea is
  carried over.

## Decision

### 1. A pull-request index the hub cannot read (scope: what the button needs)
- `hub.pr_link` gains `sealed` (text, NULL for links registered by older runners) and `linked_by` (the user who
  linked a pull request by its address; NULL for a link the device registered after `github.create_pull_request`).
- `sealed = b64u(nonce(12) ‖ AES-256-GCM(k_body, pad(canonical({"number":n,"repo":"<owner>/<name>"})), AAD))` with
  `k_body = HKDF(ikm=WK_e, info=P‖"pr-link-body")`, the plaintext padded with zero bytes to 256 bytes (the
  ciphertext does not show the length of the name), and `AAD = P‖"pr-link-body\0"‖wg_id‖"\0"‖epoch‖"\0"‖tag‖"\0"‖
  task_id‖"\0"‖origin` where `origin` is `device:<session_id>` or `user:<linked_by>` (04-crypto.md §4.11a). A reader
  opens it with the row's own `task_id`, `session_id`/`linked_by` and **recomputes the tag** from the opened
  repository and number; a body that does not open or names another pull request than its tag is dropped as
  unreadable. A hub can therefore neither move a link to another task, workgroup or pull request nor turn a device
  link into a person's link. The repository is stored as GitHub returned it (display); the tag stays over the
  lower-case name (ADR-072 §8).
- `GET /v1/workgroups/{wg}/pr-links?task=&cursor=&limit=` (`wg:read`, newest first, at most 200 per page, cursor)
  lists `{epoch, tag, sealed, task_id, session_id, linked_by, created_at}` – the per-workgroup and per-task list.
  Nothing about a pull request is in clear at the hub: no repository, number, title or state.
- `POST /v1/workgroups/{wg}/pr-links` `{epoch, tag, sealed, task_id}` (`wg:write`, the task in that workgroup, the
  workgroup's **current** epoch): "paste a pull-request address" links a foreign pull request to a task. Before it
  the client looks the tag up under every epoch it holds; a pull request linked already is shown with its task
  (idea 40: the earlier task is found, never moved silently), and the hub answers a tag linked under the current
  epoch with `200 {created:false, link}`. A new link answers `201 {created:true, link}`, audit `pr.linked`.
  `DELETE /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` (`wg:write`) removes a link a person made, audit `pr.unlinked`;
  a link the device registered is a fact of the task and stays (`409 id_conflict`); a link by address under an
  older epoch is `400 epoch_stale`.
- The device registration (`POST /v1/device/pr-links`) carries `sealed`; when a person had linked the same pull
  request before, the device's link **replaces** it (audit `pr.linked` with `replaced`), since the creating task is
  the true origin. An older hub refuses the unknown field (`request_invalid` with pointer `/sealed`); the runner then
  registers once more without it, so a new runner works with an old hub (such a link stays "details unavailable").
- The backward query stays in the client (ADR-072 §8).

### 2. A fresh read of the pull-request state (device request `github.pull_status`)
- Repositories, numbers and states are confidential and tokens never leave a device, so the client asks a device:
  device request kind `github.pull_status`, HPKE body `{org_id, wg_id, task_id?, pulls: [{repo, number}] 1..50}`,
  sent to the device assigned to the workgroup (the task's device when known), answered once `{step:"done",
  pulls:[…]}` or `{step:"failed", code}` (05-runner.md §11). The client sends one request at a time (the runner keeps
  one flow per user and kind) and batches every pull request of a view into it.
- **Who may read with which identity.** The device answers only for a workgroup it is assigned to and a requester who
  can read that workgroup in the verified organization log (`request_invalid` / `forbidden` otherwise). A requester
  who can **write** the workgroup reads with the identity ADR-078 resolves for an operation of that task (task choice
  when the device knows the task, workgroup default, organization default, the requester's primary record, the
  operator's account; every candidate must be usable by the requester). A reader reads with the requester's own
  primary record only. A task the device knows must belong to the named workgroup and organization. The workgroup's
  repository list applies when it is set ([ADR-054](054-repository-list-optional.md); `repo_not_allowed` per pull
  request). Nothing usable is `github_not_connected` (E-05: the client offers "Connect GitHub").
- Per pull request: `GET /repos/{r}/pulls/{n}` → `{repo, number, found:true, title, state, draft, merged,
  mergeable, mergeable_state, head_ref, head_sha, head_repo, base_ref, url, updated_at}`; `404` → `{repo, number,
  found:false, code:"not_found"}`; a refused repository `repo_not_accessible`.
- **`mergeable` is computed asynchronously by GitHub** (`null` while unknown; GitHub starts the computation on the
  first read). The device reads an open pull request with `mergeable: null` again after 1 s and 2 s; still `null` →
  passed on as `null`. The client shows `null` as **"being checked"**, never as "no conflict", asks once more after
  10 s by itself, then on "Check again". **Conflict = `mergeable: false`** alone (a draft has `mergeable_state:
  "draft"` even with a conflict; `mergeable_state` is display only). Closed and merged pull requests show their state
  only. A pull request whose head is in another repository (a fork) shows "Fix conflicts" as not available (§4).
- `github.get_pull_request` (the session's read) gains the same `mergeable` and `mergeable_state` with the same
  re-read.

### 3. The button sends a fixed platform instruction to the original task
"Fix conflicts" (in the pull-request view, on the task, and as an action of a situation-view entry whose task has a
linked pull request in conflict – the client reads the state of the entries' linked pull requests with the same
device request) posts **one prompt** to the task the pull request is linked to (`POST /v1/tasks/{task}/prompts`, the
ordinary signed and encrypted prompt of the person who pressed it). Its text is the fixed, versioned platform
instruction (web `core/pulls/instruction.ts`, the same text in `agent_platform/pulls.py`, a test keeps them equal):

> Platform instruction "fix merge conflicts" (v1) for pull request <owner>/<name>#<n>: 1. call `github.merge_base`
> for it; 2. if it reports `up_to_date`, say so and stop; 3. resolve every listed conflict in the workspace keeping
> the intent of both sides, remove every conflict marker, and record a decision for every conflict without markers;
> 4. if the repository has tests, run them with `test.run` on the resolved workspace and fix what the merge broke
> before the commit; 5. `github.commit` with `merge: true` on the pull request's branch, then `github.push` of that
> commit to the same branch; 6. never merge, close or force anything; 7. when a resolution needs a decision of a
> person, ask with `human.question` and wait for the answer.

Because every follow-up continues the original conversation (ADR-072), the AI that wrote the pull request resolves
its conflicts with its full context; when the context cannot be restored, the new AI starts with ADR-072's reported
notice – never silently. A pull request linked by address resumes that task's session the same way. Whoever may
write prompts to the task may press the button (`task:write`); a reader sees the state but no button.

### 4. Controlled operations: merge into the workspace, merge commit, fast-forward push
**`github.merge_base`** `{repo, number, discard?, github_identity?}` (new type, no dedupe slot: every call prepares
anew).
- Pre-check: identity (ADR-078) and write access; `GET /pulls/{n}`: open and its head in the same repository (a fork,
  a closed or merged pull request is `merge_unsupported`); the branch rules of `github.push` on the head branch
  (repository list, protected branches: `repo_not_allowed`, `branch_protected` – a merge that could never be pushed
  does not replace the workspace). The **base tip** is read from `GET /git/ref/heads/{base_ref}` (GitHub's
  `base.sha` of the pull request can lag behind), the merge base from `GET /compare/{base_tip}...{head_sha}`
  (`merge_base_commit`). `up_to_date` iff the merge base is the base tip: the workspace is not touched.
- The three trees O (merge base), A (head), B (base tip) come from `GET /git/trees/{sha}?recursive=1` (path, mode,
  type, blob; truncated → `size_exceeded`) – never from a tarball listing, which skips links, protected names and
  `export-ignore` files and substitutes `export-subst` text.
- **Workspace protection.** The pre-check exports `repos/<owner>/<name>` (when it exists) and compares it with the
  head's tree and with the trees of the session's latest `github.checkout`, preparation or **succeeded** `github.commit`
  of the repository (a committed state only while the head contains that commit); anything the export skips (links,
  protected names the session wrote) is never replaced silently. Equal to one of them → clean. Otherwise `workspace_dirty` with up to 20 paths: commit or push first,
  or, after the person agreed (`human.question`), call again with `discard: true` to replace the directory on purpose. Nothing in the workspace is lost silently.
- Executor: `merge3.merge_entries(O, A, B)` per path, mode and blob (module `agent_runner.github.merge3`): equal on
  both sides → that; changed on one side → that side (also a deletion or a mode change); changed on both sides:
  a line merge for text files (UTF-8, no NUL byte, at most 1 MiB per version), a listed conflict otherwise. File
  contents come from the head's and the base's tarballs, **each member checked against its blob SHA** from the
  tree; a member that is missing or differs is read with `GET /git/blobs/{sha}` (at most 1 000 such reads, else
  `size_exceeded`); the merge base's versions of files changed on both sides are read as blobs. Contents are stored
  once per blob SHA in the scratch directory. Conflicting hunks are written in the diff3 style with the common
  ancestor (`<<<<<<< pull request (<head_ref>)`, `||||||| common ancestor`, `=======`, `>>>>>>> base (<base_ref>)`),
  so the AI sees both sides and what they started from. The merged regular files (without protected names) are
  imported into `repos/<owner>/<name>` with `replace=true`, the directory of `github.checkout`.
- Conflict kinds: `content` (markers in the file), and **without markers**: `binary` (head's version kept),
  `modify_delete` (modified version kept), `type` (file against directory: head's entries kept, the base's entries
  listed), `mode` (added on both sides with different modes: head's mode), `protected` (a protected name, symbolic
  link or submodule changed differently on both sides; a protected text file is line-merged at blob level first and
  only a remaining conflict is listed; the head's entry is kept). Nothing of either side is dropped without a listed
  conflict.
- The **preparation** is stored on the device (`runner.merge_prep`, migration `0027_merge_prep`): session,
  repository, number, head branch, `head_sha`, `base_sha`, `merge_base_sha`, the merge result for every path the
  merge changed against the head (`{path: [mode, sha] | null}`), every conflict with its kind and, for `content`
  conflicts, the marker-like lines the head's and base's versions already contain; state `pending` → `committed`
  (merge commit SHA) → `pushed`, or `ended` (a later checkout or preparation of the repository in the session).
  Result `{repo, number, branch, base, head_sha, base_sha, merge_base_sha, up_to_date, conflicts: [{path, kind}]
  (at most 1 000 listed), conflicts_total, dir, files, bytes}`.

**`github.commit` with `merge: true`** creates the merge commit with **two parents** `[head, base_sha]`.
- It needs the session's preparation of the repository in state `pending` for that branch whose `head_sha` is still
  the branch head at GitHub, else `merge_outdated` (prepare again). `paths` together with `merge` is refused by the
  formal checks (a partial snapshot would revert the base outside the listed paths). An unchanged tree is allowed
  (the merge itself is the change). The dedupe field `tree_sha` of a merge commit is `<tree sha>+<merge parent>`, so it
  never shares a slot with a single-parent commit of the same parent and tree.
- The tree is the head's tree changed by: the workspace snapshot for regular files (as before), the stored merge
  result for every entry the workspace does not hold (protected names, links, submodules), and the merged mode of
  every regular file (a mode the base changed survives).
- **Verification against the preparation** (`conflicts_unresolved`, not retryable; the message names up to 20 paths):
  every path the merge changed cleanly must hold its merge result in the snapshot; every conflict without markers
  needs an entry in `decisions: [{path, choice}]` with `choice` `head`, `base`, `deleted` or `edited`, and the
  snapshot (or, for an entry the workspace does not hold, the chosen side) must match the choice; a cleanly merged
  path that the AI changed deliberately needs a decision as well (`head`, `deleted` or `edited`). So a "merge" whose
  tree silently drops the base's changes – an empty or foreign workspace after a VM change, a skipped binary – is
  refused.
- **Formal marker check** (`conflict_markers`, not retryable): in every `content` conflict file any line that starts
  with `<<<<<<<`, `|||||||` or `>>>>>>>` or is exactly `=======` (CR ignored) refuses the commit unless the same line
  occurs in the head's or the base's version (stored with the preparation); in every other file with new content a
  line starting with `<<<<<<< ` together with one starting with `>>>>>>> ` refuses it unless both occur in that path's
  head or base version (read as blobs only then). Deleting a conflicting file is a resolution (`deleted`).
- Limits (500 files, 10 MiB, 50 MiB), the secret scan and the blob upload apply to **new content only** – a blob that
  the head's or the base's tree already holds is at GitHub and was checked there.
- **A normal commit while a merge is pending or committed but not pushed** in the session for that repository (any
  branch) is `merge_pending`: a single-parent commit would carry the base's changes as the pull request's own.

**`github.push` is unchanged**: fast-forward only (`force: false`). The merge commit's first parent is the head, so
the push is a fast-forward unless the branch moved meanwhile (`non_fast_forward` – prepare again). The push of the
merge commit marks the preparation `pushed`. The pull request is **never** merged by this function; no type here can
merge, close or force.

**Forks** are not supported in this package: the head branch lives in another repository with other rights; the
view says so instead of offering the button (decisions/KOR-48.md).

**Optional tests**: the instruction asks for the repository's tests with `test.run` ([Idee 27](../KONZEPT.md#k-idee-27),
KOR-43) on the resolved workspace before the merge commit, when the repository has tests.

Identity and attribution follow ADR-078 for every one of these operations (`github_identity`, author, committer,
co-authors, signature – the signature covers both parent lines).

### 5. Feedback
- On success the chat shows the operation cards (base merged with n conflicts, merge commit, branch updated) and the
  pull-request panel of the task shows "Conflicts fixed, branch updated" with the merge commit (the newest succeeded
  `github.push` to the pull request's branch of a commit the task made with `merge: true`), followed by a fresh read.
- When the AI needs a decision it asks with `human.question` – the situation view lists it under "needs you"
  (`needs_decision`, ADR-076); nothing new at the hub.

### 6. Agent access (E-04)
`agent-platform pr list [--wg|--task]`, `pr show <url|repo#n>`, `pr link <url> --task`, `pr unlink <url>`,
`pr fix-conflicts <url|repo#n>` use the same routes, the same device request and the same instruction text
(11-agent-access.md, agent-parity.md).

### 7. What the hub sees additionally (04-crypto.md §7)
Per link: an AEAD ciphertext of fixed length, whether a person linked it and who, and when. The device request is
HPKE end to end. The operation type `github.merge_base` and the operation codes are metadata like every operation
type and code; parameters and results stay sealed.

## Consequences
- A pull request can be brought up to date with its base without a rebase and without overwriting anything; the
  pull request's history keeps every earlier commit.
- The runner does the three-way merge itself (no `git` binary): the classic diff3 algorithm on `difflib` matching
  blocks without the junk heuristic; changes that touch without an unchanged line between them conflict, as with
  `git`; only regions anchored by a line that is unique in all three versions are trusted, and a clean merge must come
  out the same when the lines are matched from the end – otherwise the differing part is one conflict. A differential
  test against `git merge-file --diff3` (2 000 generated cases with repeated lines, CRLF and missing final line feeds)
  enforces: whenever `git` reports a conflict, so does the runner; a clean merge equals `git`'s. In 15 000 further
  generated cases one differed: `git`'s own alignment conflicted where the runner kept both sides of an unambiguous
  insertion. The runner may report a conflict where `git` merges cleanly; the AI resolves it then.
- Large merges are bounded by the tarball and tree limits; a merge commit with more than 500 files of new content is
  `size_exceeded` like any commit.
- Review amendments of the design review (fresh context, 2026-11-24): workspace protection (`workspace_dirty`,
  `discard`), the preparation bound to the session and verified at the commit (`conflicts_unresolved`,
  `decisions`), no dedupe for `github.merge_base`, the base tip from its ref, `up_to_date` from the merge base, trees
  from the Trees API with checked tarball contents, merged modes, protected entries line-merged or decided instead
  of failing, `merge_pending` until the push and for every branch, `paths` refused with `merge`, task and origin in
  the link's AAD, padded link bodies, the device read scoped to the workgroup and its rights, conflict =
  `mergeable: false`, the differential test of the line merge, marker lines that the sides already contain, forks
  recorded as not supported, the old-hub fallback on `request_invalid`, a device link replacing a person's.
- A link registered by an older runner has no `sealed` body; it is listed as "pull request (details unavailable)"
  with its task.

## Alternatives considered
- `PUT /pulls/{n}/update-branch` / `POST /merges`: GitHub merges itself, but only without conflicts – no markers for
  the AI, and a merge commit made by GitHub without the platform's checks. Rejected.
- Rebase onto the base with a forced update: forbidden by A2.
- A plaintext pull-request table: violates the zero-knowledge hub (ADR-003). An encrypted per-workgroup event
  stream: more than the button needs (§11 Punkt 7).
- The client reading pull-request results from all task events: does not cover linked foreign pull requests and
  scales with the history instead of the number of links.

## Deviation from DESIGN.md
None (DESIGN §24.5 "pull requests are read, not owned" stays: the platform updates a branch, it does not own the
pull request).
