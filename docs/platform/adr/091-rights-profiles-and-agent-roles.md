# ADR-091: Rights profiles and agent roles – work, review and release kept apart, a voluntary setting and never a precondition

Status: Accepted (2026-11-25, after a review of the draft with fresh context; its findings are worked in)

Implements [Idee 42](../KONZEPT.md#k-idee-42) ("Konfigurierbare Rollen und API-Berechtigungen … Development Agents
dürfen etwa Commits/PRs beantragen, aber nicht mergen; Security Agents liefern Scans; Review Agents bewerten Nachweise;
Merge Agents prüfen formelle Freigabekriterien und beantragen einen Merge … Arbeit, Prüfung und Freigabe werden
getrennt"), [§10 Punkt 7](../KONZEPT.md#k-10-b7) ("Die Plattform prüft Aktionen deterministisch und formell, nicht
inhaltlich") and interpretation [A1](../KONZEPT.md#a-1) ("Subagenten dürfen dasselbe wie ihr Hauptagent …
Rechteprofile und Rollen (Idee 42) kommen als freiwillig einstellbare Einschränkung, nicht als Voreinstellung"),
under the rules [E-11](../KONZEPT.md#e-11) (restrictions are voluntary options, never a precondition),
[E-04](../KONZEPT.md#e-04) (agents reach everything people reach), [E-05](../KONZEPT.md#e-05) (a refusal names the
step that resolves it) and [E-19](../KONZEPT.md#e-19) (a class of errors is closed by a lasting check). Closes the open
deviation "A1 mit Idee 26" of [part D](../KONZEPT.md#d-offen). Builds on [ADR-088](088-full-subagents-and-limits-as-settings.md)
(full subagents), [ADR-089](089-sessions-read-like-their-human.md) (reads), [ADR-078](078-github-identities.md)
(identities) and [ADR-080](080-test-runs-as-operations.md) (`test.run`).

## Context
- A session may request every operation type its task owner may (check H2: the owner's `wg:write`); a child has the
  rights of its parent (A1, ADR-088 §1). There is no way to give one session less.
- The hub never reads parameters or results of an operation (zero knowledge): it sees the type, the session, the
  task, the acting human and the identity. Formal checks on parameters (repository, branch, head) are the runner's,
  which executes the operation and holds the GitHub tokens. A session sandbox offers the model only file tools and
  the platform MCP tools (`policy.py` rule 2); an operation exists only through `request_operation`, which the guest,
  then the runner, then the hub check. A work VM holds no credential (ADR-059).
- There is no operation to merge a pull request, to read its reviews or checks, or to record a review result.

## Decision

### 1. A rights profile is an optional restriction of one session
- A **profile** is `{name, operations, reads}`: `operations` the operation types the session may request (a closed
  list, each a type of `contracts/ops/INDEX.json`), `reads` the reach of its read tools: `all` (as the human,
  ADR-089), `workgroup` (only the session's own workgroup) or `task` (only the session's own task).
- **No profile = everything.** A session without a profile may request every type and read like its human, exactly
  as before this ADR (A1, E-11). Nothing is assigned by default, at installation, for a new organization, workgroup,
  task or child. No operation, read, setting or screen needs a profile; a workgroup that never sets one notices
  nothing.
- **Snapshots.** A profile is resolved into its operation list and reach **when it is assigned** and stored with the
  task (for its main sessions) or with a child that was started with a profile of its own (a later change or deletion
  of a definition never changes an assignment; to apply a change, assign the profile again; a type added by a later
  release is missing from an assignment until then, which fails closed).
- **Effective rights** of a session = the intersection of the task's snapshot and every snapshot along its chain of
  parents (itself included): the types all of them allow, the narrowest reach, named after the innermost snapshot.
  The hub computes it at every registration (H2b) and the runner the same way from the device state. A restriction a
  person sets on the task therefore applies at once to every session of the task, also to children started before
  it (review finding: "narrow, then instruct an old child").
- **A child never has more than its parent.** `subagent.request` takes an optional `profile` (name). Without it the
  child has its parent's effective rights (A1: the same rights; it stores no snapshot). With it the child's profile must be a subset of
  the parent's effective rights: every type in the parent's list (a parent without a profile has every type) and a
  reach no wider (`task` ⊂ `workgroup` ⊂ `all`); otherwise the request is refused `rights_denied` ("The profile
  <name> allows more than this session may do."). Since every session's profile is the intersection along its
  chain, a grandchild can never regain what a parent lost.
- **Who assigns.** Only people (and their agents over the API, E-04) assign profiles: a writer of the workgroup sets
  or clears the profile of a task (`PUT /v1/tasks/{task}/agent-profile {profile: name|null}`); a session assigns one
  only to its own child, and only a narrower one. No operation lets a session change its own profile or its parent's,
  so no text in a prompt, file or tool result can widen it; the profile lives in hub rows and in the device's
  desired state, never in text the model controls.

### 2. Built-in roles and own profiles
Every operation type **and every platform MCP tool** has one **class** in `contracts/ops/classes.json` (the one
source; tests refuse a type or a tool without a class – E-19): `read` (reads GitHub or the platform), `write`
(changes a repository, an issue, the task or the platform), `run` (starts compute: work VM, test run, browser),
`delegate` (subagents), `ask` (asks a person), `scan` (`security.report`), `review` (`review.verdict`), `merge`
(`github.merge`). A tool is allowed when its class is allowed by the profile's types: the work VM tools
(`workvm_*`) need `workvm.request`, the browser tools (`browser_*`) `browser.open`; the read tools are `read` and
bounded by the reach. These tools reach the runner as `workvm.call` and `platform.query`, never as an operation, so
the runner checks them there (review finding: a `read_only` session could otherwise run commands in a work VM).
`classes.json` also flags the **head-changing** types (`github.commit`, `github.push`) used by §6. Built-in profiles are **derived from the classes**, so a new
type falls into the roles by its class without a list being edited:

| Profile (role) | Operations | Reads |
|---|---|---|
| `read_only` | `read`, `delegate`, `ask` | `all` |
| `development` | `read`, `write`, `run`, `delegate`, `ask` | `all` |
| `security` | `read`, `run`, `scan`, `delegate`, `ask` (scans through `test.run` kind `security`, the result through `security.report`) | `all` |
| `review` | `read`, `review`, `run`, `delegate`, `ask` | `all` |
| `merge` | `read`, `merge`, `delegate`, `ask` | `all` |

- `delegate` is in every role: a child is bounded by its parent (§1), so starting one never widens anything.
- **Own profiles** are named definitions of an organization (`scope: org`) or of a workgroup (`scope: wg`), created,
  changed and deleted by admins of the organization (org) or writers of the workgroup (wg): `PUT/DELETE
  /v1/agent-profiles/{scope}/{scope_id}/{name}`, listed with the built-ins by `GET /v1/agent-profiles?wg={wg}` (every
  member). Names `[a-z][a-z0-9_-]{0,39}`; the built-in names are reserved. A name is resolved in the order workgroup,
  organization, built-in. Hub table `hub.agent_profile` (plaintext metadata: names and type lists are configuration,
  no content; audited `agent_profile.set` / `agent_profile.deleted`).

### 3. Enforced three times
- **Hub (binding), check H2b** after H2a: the type is in the session's effective operation list (`denied`
  `rights_denied`, audited like every denial); for `subagent.request` the requested child profile (the registration's
  plaintext field `child_profile`, a name) resolves in the task's workgroup and is within the parent's effective
  rights (§1), else `denied` `rights_denied`. The child row stores the resolved snapshot
  (`hub.session.agent_profile`) and the answer returns it (`child_profile`), so the runner starts the child with it
  without waiting for the next desired state. An older runner that does not send `child_profile` gives children the
  parent's rights – never more. The task's profile is changed under the task row lock that every registration takes,
  so a registration sees either the old or the new profile.
- **Runner** (device state section `agent_profiles`: per session of the device its effective profile
  `{name, operations, reads}` or null, and per workgroup the profiles its names resolve to): every request is checked
  right after the formal checks of its parameters, before any pre-check reaches GitHub (`rights_denied` with the sentence "This session works with the profile <name>, which does
  not allow <type>." and, for a main session, "A person can change the task's profile.", for a child "Ask the session
  that started you."), registered as `rejected` and audited; `subagent.request` checks the child profile as the hub
  does; `workvm.call` and `platform.query` are refused by class and reach with the same kind of sentence. The runner
  stores every profile it learned (state and registration answers) and computes a child's effective rights from its
  own snapshot and its parent's; it starts no child before the decision named its snapshot. The `github.merge`
  executor checks the criteria and the separation (§5, §6) again right before the merge call, so a verdict
  `rejected` or a failed check that arrived while the operation waited is not missed.
- **Guest**: the session gets its profile at creation (`sessions-create` `profile`, only sent to a guest that
  announces it, `guest_features`). `request_operation` rejects a type outside the list locally (`rights_denied`,
  forwarded with `local_check:"rejected"` like every local refusal), `get_context` and the harness name the profile,
  its types and reach (the harness line of every session start), and the model is told to ask its parent or a person
  instead of trying a way around.
- Without a profile none of the three changes anything; the runner and the guest skip the check.
- **Older runners.** A runner announces the feature `agent.profiles` (ADR-060 decision 6). Assigning a profile to a
  task (`409 peer_outdated`, "A device of this organization runs an older runner that cannot enforce profiles;
  update it first.") is refused while an active device assigned to the organization has not announced it, because
  only the runner can check reads, work VM and browser tools. This concerns only people who choose profiles (E-11).
  A device that joins later with an older runner can still run such a task: the hub keeps checking the operation types
  (H2b); reads, work VM and browser tools are then unchecked until the runner is updated (stated, not gated).
- **Older runners and the extended settings.** An older runner refuses a `settings.workgroup` body with
  `merge_criteria`/`separate_duties` as invalid and keeps the settings it had before (never wider than those); an older
  web tab shows such settings as invalid until it is reloaded. Runners are updated right after the hub (§8).

### 4. New operation types
| Type | Class | Params | Runner checks | Dedupe | Result |
|---|---|---|---|---|---|
| `github.list_reviews` | read | `{repo, number, github_identity?}` | allow-list, number ≥ 1 | – | `{number, head_sha, reviews:[{id, author, state, commit_id, submitted_at, body}], verdicts:[{verdict_id, session_id, profile, outcome, head_sha, created_at}]}` – GitHub's reviews (newest 100) and the platform verdicts of §5 the device knows for the pull request |
| `github.get_checks` | read | `{repo, ref, github_identity?}` (`ref` a sha or branch) | allow-list, ref syntax | – | `{sha, state: success|failure|pending|none, check_runs:[{name, status, conclusion}], statuses:[{context, state}]}` – check runs and commit statuses of the commit; `state` `failure` when any finished one failed, `pending` when any is not finished, `success` when all succeeded, `none` without any |
| `review.verdict` | review | `{repo, number, head_sha, outcome: passed|rejected, summary, evidence?}` | no GitHub request: a verdict names its head and counts only on the head a merge finds; with separation a session that made or pushed `head_sha` is refused (§6) | repo, number, head_sha, session (the requesting session's ID, as `github.checkout`) | 30 days | `{verdict_id, repo, number, head_sha, outcome, profile}` |
| `security.report` | scan | `{repo, ref, tool, passed, findings:{critical, high, medium, low, info}, run_id?, summary?}` | allow-list; `run_id` a `test.run` of this task | – (idempotency key) | – | `{report_id, repo, ref, tool, passed, findings}` – the structured scan result of a Security session |
| `github.merge` | merge | `{repo, number, head_sha, method?: merge|squash|rebase, github_identity?}` | allow-list; open, head = `head_sha` (`merge_outdated`); the base branch not protected by the workgroup; the workgroup's merge criteria (§5) and separation (§6) | repo, number, head_sha | 24 h | `{merged, sha, number}` – GitHub's merge with `sha` as precondition, so a head that moved is never merged |

- `evidence` is a list of at most 50 `{kind: operation|test_run|security_report|url|note, ref}` (operation, run and
  report IDs, `https:` URLs, notes ≤ 1000 characters); `summary` 1–4000 characters. A verdict is a **platform object**: its operation record
  (type, session, task, acting human; hub) and its sealed result; the runner keeps the verdicts of its device in
  `runner.review_verdict` for the criteria. Readable through `get_operation`/`list_operations`, `github.list_reviews`,
  the web operations tab and `agent-platform review verdicts`.
- One verdict per session and head (dedupe; the session is part of the tag, so two sessions' verdicts on one head
  are two verdicts): a repetition is a `duplicate` of the first. A new head needs a new verdict.
- `github.list_reviews` lists only verdicts within the session's reach (its task for reach `task`, its workgroup
  otherwise).
- GitHub's refusals of a merge are passed on: `405` (not mergeable: GitHub's own rules, a required review, a failed
  required check) → `rights_denied` with GitHub's message; `409` (head changed) → `merge_outdated`.

### 5. Merge criteria are a workgroup setting and default to none
`settings.workgroup` gains `merge_criteria {passed_verdicts, verdict_profile, checks_green, github_approvals}` and
`separate_duties` (contract additive, all optional; defaults `0`, `review`, `false`, `0`, `false`). The runner evaluates them in the pre-check
of `github.merge`, deterministically and formally (§10 point 7), never on content:
- `passed_verdicts` n > 0: at least n verdicts `passed` for exactly this repository, pull request and head, from n
  different sessions other than the merging one, each with profile `verdict_profile` at the time of the verdict, and
  no verdict `rejected` for this head from a session with that profile (a rejection blocks until a new head). Profiles
  are compared by name (a workgroup's own profile of the same name as an organization's counts alike). Verdicts known
  to the device count: a task's sessions run on one device and children on their parent's; combining verdicts of
  other devices is a v1 scope choice not built (the refusal says that only this device's verdicts were counted).
- `checks_green`: `github.get_checks` of the head is `success` (a repository without any check is `none` and blocks;
  the sentence says so).
- `github_approvals` n > 0: at least n GitHub reviews `APPROVED` on this head by distinct accounts (people's
  independent approval, review finding 7).
- A criterion not met refuses `rights_denied` with a sentence that names the criterion, what is there and what is
  missing (E-05: "1 passed verdict of a review session on this head is required, 0 found. Request a review.").
- **Without criteria only GitHub decides** (E-11): its branch protection, required reviews and checks apply through
  the merge call.

### 6. Separation of work, review and release
With `separate_duties: true` a session that made or pushed `head_sha` may not give a verdict on it (checked without
GitHub), and at the merge a session that made or pushed **any commit of the pull request** (the commits GitHub lists
for it, compared with the session's own `github.commit` results and `github.push` shas – the head-changing types of
`classes.json`, merge commits of ADR-084 included) may neither give a verdict on its head nor merge it, and its
verdicts never count for it (`rights_denied`, "This session made the head <sha>; with separate duties another session reviews
and merges it."). The runner decides from its own operation records (session ID and result sha), the same records
the hub has as rows; a refusal is registered `rejected` and audited like every refusal. The merging session's own
verdict never counts (§5), with or without separation.

**No bypass through the default branch.** With any merge rule set, a `github.push` onto the repository's default
branch is refused (`branch_protected`): the default branch then changes only through `github.merge`. (A workgroup that
uses `github.merge` on `main` cannot list `main` as protected, because the merge itself writes it.)

**Limits, stated honestly.** The separation binds the sessions that wrote, as the package asks. One main agent without
a profile can still start a Development child, several Review children and a Merge child; the sessions are separate,
their instructions come from one parent, and a Review child may start review grandchildren of the same name. A
workgroup that wants independent approval sets `github_approvals` (a
person's review at GitHub) or uses `human.approval`.

### 7. Workflows and visibility
- A main agent builds a chain with what exists: `subagent.request {profile: "review", …}` then `{profile: "merge"}`;
  the harness gives the recipe. People see the role of every session: the session rows carry `profile` (name, null
  without), shown in the session tree of the situation view and the sessions tab, and the task's profile in the task
  facts.
- **Agents (E-04):** `agent-platform profile list|set|delete`, `task profile --task --profile|--clear`, `wg settings
  --merge-verdicts --merge-verdict-profile --merge-checks-green --separate-duties`, `review verdicts --task`;
  rows in `agent-parity.md`.

### 8. Rollout
Hub first (types, H2b, routes, columns, the device state section), then runners (pre-checks, executors, the child
profile, the section), then the golden (the guest's `profile`). An older runner never sends the new types (`unknown_type`
locally) and ignores the section; the hub still enforces every profile on it (H2b), and its children inherit the
parent's profile at the hub. An older guest gets no profile and refuses nothing itself; runner and hub still do.

### 9. Specification
03-api.md §§4.10, 8.3, 9 (types, H2b, `child_profile`), 05-runner.md §§12, 13 (pre-check, children, tools),
06-web.md (session tree, situation view, settings), 01-requirements.md K-42 and K-31, [KONZEPT part D](../KONZEPT.md#d-offen) (A1 with Idee 26)
and `agent-parity.md`.

## Consequences
- A workgroup can run development, review and release in separate sessions with a formal merge rule; nobody else
  notices anything.
- The model cannot widen its profile: no operation changes it, children are intersected, and three components check
  every request.
- Verdicts across devices are not combined (stated in the refusal); a task's sessions run on one device in v1.

## Alternatives considered
- A default profile for subagents ("less rights"): contradicts A1 and E-11 (rejected).
- Profiles evaluated by name at each request (live definitions): a change of a definition would widen running
  sessions and a child could outgrow its parent's snapshot; snapshots make "never more" checkable at one moment.
- Merge criteria checked by the hub: the hub cannot read the repository, the pull request or the head (zero knowledge).
- Verdicts as GitHub reviews: they would need a GitHub account per role and are not platform objects bound to a
  session; GitHub's reviews are read and shown beside them.

## Deviation from DESIGN.md
None beyond ADR-001.
