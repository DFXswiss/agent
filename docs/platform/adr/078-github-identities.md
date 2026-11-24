# ADR-078: GitHub identities – several accounts, choice per operation, session and task, defaults per organization and workgroup, author, committer, signature and co-authors

Status: Accepted (2026-11-24)

## Context
- [Idee 37](../KONZEPT.md#k-idee-37): a connected GitHub account can be chosen for a pull request; defaults per
  organization and workgroup; commits can carry the company identity, individual employees or several contributors;
  author, committer, signature and co-authors are distinct; the actual trigger stays traceable.
  [Idee 31](../KONZEPT.md#k-idee-31) and [Idee 42](../KONZEPT.md#k-idee-42): main agents and subagents may use
  different identities. [§16 Punkt 7](../KONZEPT.md#k-16-b7) lists GitHub identities as still to be checked.
- Rules: [E-06](../KONZEPT.md#e-06) (approved users work with the operator's account at once – stays the default),
  [E-05](../KONZEPT.md#e-05) (a missing connection is requested, not explained), [E-11](../KONZEPT.md#e-11) (nothing
  must be approved first), [E-04](../KONZEPT.md#e-04) (everything also without a browser),
  [A15](../KONZEPT.md#a-15) (GitHub actions only with identities the instructing human may use).
- Built before: one `github` credential per user (`hub.credential UNIQUE (owner_id, kind)`), every operation uses the
  task owner's credential ([ADR-015](015-github-app-device-flow-rest-only.md)), the operator's token is copied into
  the credential of every approved user ([ADR-053](053-operator-github-account.md)), and `github.commit` sends only
  `message`, `tree` and `parents`, so GitHub sets author and committer from the token and nothing is signed.

## Pre-study: what GitHub does with commits made through the API (stage a)
From GitHub's public documentation (REST "Git database / commits", "About commit signature verification", GraphQL
"commits", REST "SSH signing keys" and "GPG keys"; read 2026-11-24) and the GitHub double:

1. **No GitHub signature for REST commits made with a user token.** GitHub signs commits made in its web interface,
   commits of the GraphQL mutation `createCommitOnBranch` ("automatically signed by GitHub … marked as verified") and
   bot commits of a GitHub App, the latter only "if the request … contains no custom author information, custom
   committer information, and no custom signature information". A REST commit (`POST /repos/{r}/git/commits`) with an
   OAuth, App user or personal token and no `signature` is `verification.reason = "unsigned"`. `createCommitOnBranch`
   "does not support specifying the author or committer" – a GitHub signature and a chosen author exclude each other.
2. **Author and committer are free fields of the REST call.** `author {name, email, date?}` defaults to the
   authenticated user, `committer` to the author. GitHub stores any values; it links a commit to an account by the
   email address (the account's verified addresses, including its `<id>+<login>@users.noreply.github.com` address).
   `Co-authored-by:` trailers in the message are GitHub's way to name further authors (GraphQL `Commit.authors`:
   "based on the git author and the Co-authored-by message trailer").
3. **An own signature travels in the `signature` field** ("added to the gpgsig header of the created commit", computed
   over the commit object exactly as it is written – tree, parents, author and committer lines with dates, empty line,
   message). The REST page documents an ASCII-armoured PGP signature; GitHub verifies SSH signatures of commits too
   (signing keys of type SSH) and lists `unknown_signature_type` as a reason, so whether the REST field takes an SSH
   signature is the one point only the real service can prove. It shows `verified: true` when the key is a signing key
   of the account whose verified email is the committer email. Registering a key: `POST /user/ssh_signing_keys`
   (classic scope `write:ssh_signing_key`) or `POST /user/gpg_keys` (`write:gpg_key`); neither the App of ADR-015
   nor a token with only `repo` has these scopes.

The proof against the real service is the script `scripts/probes/probe_github_identity.py` (run by the operator after the
merge in a test repository; this package uses no real account): it creates commits (a) without signature, (b) with
author ≠ committer and two `Co-authored-by` trailers, (c) with an SSH signature of a key it registers and deletes
again, and prints GitHub's `verification`, the attribution of each and whether GitHub's commit ID equals the one the
runner computes for the signed object (the byte-exact form of the signed commit).

## Decision

### 1. An identity is a `github` credential record
- A **GitHub identity** is one `github` credential record (04-crypto §4.4), named by its record ID. An owner may hold
  several (`UNIQUE (owner_id, kind)` stays for every other kind). The owner is a **user** (a personal account) or an
  **organization** (a company account, e.g. the operator's). The hub sees what it saw before – ID, owner, kind,
  version, epoch, times – plus which record is a user's primary one; never a login or token.
- **Primary record.** Each user has at most one primary `github` record (`credential.is_primary`). The migration
  marks every existing record primary, so nothing changes for anybody. A user's first record becomes primary; another
  one only when the user says so (`PUT /v1/github-identities/{id}/primary`). Deleting the primary record promotes
  none. Every endpoint of before (`/v1/me/credentials/{kind}`, `/v1/device/credentials/{owner}/{kind}`, the rotation)
  addresses the primary record only, so an old browser or runner keeps working with exactly the account it knew;
  after its deletion they answer `404` as after a disconnect. Further accounts and company accounts live behind
  `/v1/github-identities/{id}` and `/v1/device/github-identities/{id}` (03-api §4.8).
- **Company accounts** live in the `cred` scope of their **organization** (`scope_id` = the organization; format
  unchanged, `k_cred = HKDF(CK_e, salt, P‖"credential")` with the organization's credential key): its readers are
  **devices only** – every active device that is a member of the organization and assigned to one of its workgroups
  (key model KM-35: never every member, and not one user's `CK`, which would not survive that user). They are
  **authored only by an admin** of the organization (browser or agent tool). An admin writes each version under a
  **fresh epoch**: it creates the key, wraps it for every reader device, seals the record and forgets the key, so it
  cannot read the record back; the hub stores epoch, wraps and record in one transaction (`PUT
  /v1/github-identities/{id}` with `key {epoch, wraps}`). Devices holding an epoch wrap it to devices assigned later
  (catch-up, 04-crypto §5.11). A device that stops being a reader keeps what it already held, as for every key; each
  later version uses a new epoch it does not get. A `github.connect` for an organization (`org_id` in the plaintext
  of the request, accepted from admins only) runs on the device as for a user, but the device stores nothing: its
  final answer – end-to-end encrypted to the requesting admin – carries the credential body, which the admin's client
  seals as above. Readers accept a company record only when its author is an active admin of the organization in the
  verified log. Browsers see company accounts by metadata only (record ID, organization, times).
- **Reconnecting updates the record** (same ID, version + 1): a `github.connect` may name the record to fill
  (`identity_id`, the "reconnect" of a listed account); without it the device updates the primary record when it holds
  the same GitHub account, creates the primary record when there is none and otherwise adds a record. For a company
  account the admin's client does the same with the organization's records.
- Body (`contracts/credentials/github.json`, additive): `name` (display name, optional), `source` adds `operator`,
  `signing_key {alg:"ssh-ed25519", seed, public, github_key_id?}` (optional, §5).
- **Rotation of a user's `CK`.** The rotation must contain the primary records as before; non-primary `github`
  records that an old browser does not know may be left out and keep their epoch, and a current client re-seals them
  under the new epoch at once (rewrap job). A removed device can thus read such a record until it is re-sealed –
  the same window as for every record that was readable before the rotation.

### 2. Who may use an identity
For an operation of a task in organization O, an identity is **usable by** the acting human H when it is
- H's own (owner = H), or
- a company account of O (H is an active member of O, verified log), or
- the operator's account (§7), when H is an active member of the operator's organization.

Never another person's personal account and never the account of an organization other than O or the operator's.
The **acting human** H is the acting user of [ADR-070](070-personal-connections-follow-the-instruction.md): the
verified author of the prompt that started the session's current turn, stamped on the operation when it is received
(a child session takes the acting user of its request). Without an acting user the operation uses no identity at all
and is rejected with `github_not_connected` (ADR-070); it never falls back to the task owner. `credentials/acting.py`
stays the only module that opens a personal record for an operation. The runner decides from its verified logs; the
hub checks the same rule leniently where it stores a choice (`identity_forbidden`).

### 3. Choice and defaults
For each `github.*` operation the runner takes the first of:

1. the operation's parameter `github_identity`;
2. the session's identity (set by `subagent.request` with `github_identity`; a subagent without one inherits its
   parent's at creation);
3. the task's identity (chosen at creation or changed later by a writer of the workgroup);
4. the workgroup's default, 5. the organization's default (company accounts of that organization only);
6. H's own primary identity;
7. the operator's account (§7).

An explicit choice (1–3) that H may not use is rejected (`identity_forbidden`, not held); one whose record is gone or
does not open is `github_not_connected` and held like a missing connection (ADR-066), and so is a default whose
record cannot be opened yet (for example before the device received the key's epoch): reconnecting the account
restores the same record ID (§1), or a writer changes the task's choice. A default that H may not use is skipped. The
hub deletes defaults together with their record. Without any usable identity the operation is held for the
connection as before (E-05).

Choices and defaults are hub columns (`task.github_identity`, `session.github_identity`, `github_default (scope,
scope_id)`), set through `POST /v1/tasks`, `PUT /v1/tasks/{task}/github-identity` (writers of the workgroup),
`PUT /v1/github-defaults/org/{org}` (org admins) and `PUT /v1/github-defaults/wg/{wg}` (org admins and writers of the
workgroup). The desired state of a device carries them (`sessions[].github_identity`, `.task_github_identity`,
`github_defaults`); the runner keeps the last answer in its store, so a restart without the hub never falls back to
another account. A new operation type `github.list_identities` tells the session which identities H may use, which one
applies now and which humans may be co-authors.

**Old runners.** A runner announces the feature `github.identities` (ADR-060 decision 6). The hub refuses to store a
task or session choice or a default (`409 peer_outdated`) while an active device assigned to the organization has
not announced it, and denies the registration of a `github.*` operation without `github_identity` (an old runner) with
`rights_denied` when a choice or default applies to it. An old runner therefore never acts with another account than
the one chosen.

### 4. Commit identity
`github.commit` takes `author`, `committer` (identity IDs, never free text) and `co_authors` (user IDs):
- `committer` defaults to the acting identity of §3, `author` to the committer; each must be usable by H (§2). The
  commit carries the account's login and its `<id>+<login>@users.noreply.github.com` address, so GitHub attributes it
  to the account. Dates are set by the runner (UTC, seconds).
- `co_authors` are humans only: the humans who gave the task's delivered prompts (the task owner included). Each is
  written as `Co-authored-by: <login> <noreply address>` of that human's own primary identity; a human without one, or
  whose primary record is a copy of the operator's account (`source:"operator"`, or on a platform device the
  operator's GitHub account ID), is `identity_unavailable`.
- A message with a trailer-like line `co-authored-by:`, `signed-off-by:` or `committed-by:` (any case, leading spaces
  allowed) is `params_invalid` (pointer `/params/message`): no free text, tool or model name can become an author or
  co-author (the repositories' rule "no AI attribution").
- `signing`: `none` or `key` (default: `key` when the committer identity holds a signing key, else `none`). GitHub
  signatures are not offered: REST commits are never signed by GitHub (pre-study 1).

### 5. Own signing key per identity
`github.connect` takes `signing_key: true`: after the account is checked the device creates an Ed25519 key, registers
it with `POST /user/ssh_signing_keys` and puts it into the record (`signing:"registered"`); without the scope GitHub
refuses and the record is stored without a key (`signing:"scope_missing"`). A commit with `signing:"key"` is signed in
the SSH signature format (namespace `git`, SHA-512) over the commit object the runner builds; GitHub's answer
`verification` is passed on. Whoever can open the record can read the key – the same reach as the token: the owner and
its eligible devices for a personal account, the organization's devices (and, once, the admin who stores it) for a
company account. A key in the operator's account is only created
when an admin of its organization asks for it (question F4 of the owner).

### 6. Traceability
The registration of every operation carries `triggered_by` (H) and, for GitHub types, `github_identity` (the acting
identity). The hub stores both on the operation, writes them into its audit and returns them with the task's
operations, so the task shows "PR #12 as <login>, triggered by <human>" (the web resolves the login from the records it
can open). Commit author, committer and co-authors are in the sealed parameters of the operation. KOR-52 shows the
identity of each main agent from the same fields.

### 7. The operator's account becomes an organization's account
On a platform device `OPERATOR_GITHUB_ORG` (with `OPERATOR_GITHUB_TOKEN_FILE`) names the organization that owns the
operator's account. A `github.connect` of an admin of that organization for it answers with the operator's token
as that organization's credential body (`source:"operator"`), which the admin's client seals and stores; its members use it without being
listed (step 7 of §3, E-06), on every device of that organization – also for their tasks in other organizations,
as long as the device that runs them is assigned in the operator's organization (otherwise it cannot open the record
and the operation is held for a connection; a root-signed list of platform devices for other organizations is
KOR-49's). `OPERATOR_GITHUB_USERS` stays the setting for users outside
that organization until the owner decides F2; their copies are now stored with `source:"operator"`, and the runner
audits every change of the list (`runner.operator_github_users`).

## Consequences
- A company account is readable by the devices of its organization only; the admin who stores a version sees its body
  once. The boundaries are the formal checks, the optional repository list, the audit and the usage rules of §2. A
  device that left the organization may have read the token: revoke or replace it at GitHub.
- The hub additionally sees organizations as credential owners, the number of GitHub records per owner, the primary
  mark, which record a task, session, default and operation uses, and who triggered an operation (04-crypto §7). A
  hub that swaps a choice can only pick another identity that H may use (the runner enforces §2); the attribution
  would be wrong, never the rights.
- Roll-out: hub, runners, goldens, web and agent tool. The hub accepts registrations and requests without the new
  fields; choices and defaults wait for the runners (§3). Copies of the operator's token made before this package on
  other devices than the platform device are not recognised as such (they stay the user's own account, as today).
- A personal account is never a default; a workgroup or organization that wants a shared account connects one for
  the organization.
- Signing needs a scope that tokens of the GitHub App do not have; commits stay unsigned unless the account's
  connection has `write:ssh_signing_key`.

## Alternatives considered
- One identity per user plus an "org flag": cannot model several accounts per person or a company account that no
  member owns.
- Login in plaintext at the hub for defaults: unnecessary (record IDs suffice) and would widen what the hub sees.
- GitHub-signed commits through `createCommitOnBranch`: no author or committer choice, and a second commit path
  that also moves the branch (no separate push operation).
- A personal account as workgroup default: would let other members act as that person (A15).

## Deviation from DESIGN.md
None beyond [ADR-015](015-github-app-device-flow-rest-only.md) (`docs/github-accounts.md` knew several named
accounts with signing keys on the operator's machine; here they are platform records).
