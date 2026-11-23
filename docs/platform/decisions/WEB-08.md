# WEB-08 – decisions

Accepting org invitations on the invitation screen, joining with an existing account, self-wraps (06-web.md §3.1
states `org_verify`, `intro`, `existing_account`, `accept`, `done`, §3.1.1, §11 job 6, §13; 04-crypto.md §4.6, §5.1
steps 1–2, §5.2, §5.2a, §6.1, §6.3 W1; 03-api.md §4.1 lookup, §4.3; ADR-004, ADR-005).

1. **Hook point.** WEB-03's state machine already branches org invitations into the step `org` and renders
   `screens/invite/org/OrgInvite.tsx` with the code and the lookup answer. This package replaces that placeholder and
   adds its own state machine `screens/invite/org/machine.ts`; WEB-03's screen is unchanged. The org path reuses
   WEB-03's registration (`register`, `signIn`, `RecoveryCodeStep`) and its test IDs where the step is the same
   (`invite.display-name`, `invite.create-passkey`, `invite.unlock`, `invite.notice`, `invite.accept-retry`, …), so
   the shared E2E steps drive both paths. New IDs: `invite.org`, `invite.verifying`, `invite.blocked`,
   `invite.org-intro`, `invite.org-name`, `invite.role`, `invite.workgroup`, `invite.existing-account`,
   `invite.org-summary`, `invite.existing` (`data-mode` `sign_in|recovery|ready`), `invite.sign-in`, `invite.prf-missing`,
   `invite.recovery.input|submit`, `invite.join`, `invite.back`. The catalog is `invite-org.<locale>.json`; the
   shared texts stay in WEB-03's `invite` catalog (its unused `invite.org.*` placeholder keys are left to WEB-03).
2. **Core flow in `core/account/join.ts`** (new file in WEB-03's directory, covered by Vitest alone):
   `openInvitation` (package with `package_key`, lookup log against `founder`/`head`), `completeInvitation`,
   `refreshInvitation`, `prepareAccept`, `sendAccept`, `joinOrg`, `loadPlatform`.
3. **What `org_verify` checks** and with which code: code ≠ lookup ID, missing package or one the code does not open →
   `invite_code_invalid`; no log → `log_empty`; the log must contain the package head (`log_rollback`), verify from
   `seq` 0 against the package's founder (L1–L8, `log_*`), belong to the package's org (`log_genesis`) and end at the
   served `log_head` (`log_chain`); the `invite` entry with this ID must exist with the code's `pk_inv` and be a user
   invitation (`invite_not_in_log`; that an admin appended it is L5 of the verification), not revoked
   (`invite_revoked`) and not accepted (`invite_consumed`). Refusals of the package or the log block the screen
   ("This invitation cannot be trusted … The invitation was not accepted"; before the sign-in nothing is sent, after
   a registration the account exists but the invitation is not accepted); `invite_revoked`/`invite_consumed` show the one invalid
   message of `invite_unknown` (06-web.md §3.1 `invalid`); `non_ascii` blocks like `header_invalid`. The expiry is
   not checked against the browser clock: the hub answers expired invitations with `invite_unknown` and refuses an
   expired `accept`.
4. **Logs with platform devices.** The invitee is not signed in and cannot read the platform log, which L5 needs for
   `device_assign` with `platform_seq`. Before the registration such a log is anchored without signatures: gapless
   `seq` and `prev` links over all entries (L1, L2) ending at the served head (`log_chain`), the package head on the
   chain (`log_rollback`), genesis naming the package's founder and org (`log_genesis`), and the `invite` entry of
   this code at or before the package head (`invite_not_in_log`). The package head is the hash the inviting admin
   verified, so every entry up to it – including the `invite` entry the intro shows – is authentic without the
   signatures; a hub that injects a platform entry gains nothing. After the sign-in and before the `accept` the log is
   verified completely (`completeInvitation` with the verified platform log of the store, or `loadPlatform`:
   `GET /v1/platform/log` verified against the compiled root key, which `boot` checked against `/v1/config`). The
   platform head is not compared with a stored one here (the log job does that for every sign-in); a rolled-back
   platform log could only hide a later decertification, which the hub enforces anyway (review finding, accepted).
5. **Intro.** Org and workgroup names from the package (`org_name`, `wg_names`; empty names show "an organization" /
   "Unnamed workgroup"); role, workgroups with access (default workgroup always read and write) and expiry from the
   verified `invite` entry. The display name (1–80 characters, `displayName` of WEB-03) is asked on the intro (new
   user) or in `existing_account` (`ready`), since every org has its own `member.profile`.
6. **Existing account.** `unlocked` goes to `existing_account` ready; a `locked` session (identity not in memory)
   goes to `existing_account` with the passkey sign-in; anonymous users get the intro with "I already have an
   account". The sign-in is the login ceremony of §3.4 in place (the fragment cannot survive a detour via `/login`);
   without PRF output or with a slot that does not open (`decrypt_failed`) the recovery code unlocks once and a
   missing passkey slot is uploaded afterwards, as on `/login`. The full `prf_missing` guidance is WEB-14's. "Back"
   returns to the intro while not unlocked. A user the verified log already lists as a member gets the org opened
   without an `accept` (the batch would be refused with `log_accept`).
7. **The `accept` batch** is built by `buildBatch` with the package keys as key ring: `accept{invite_id, subject,
   kind:"user", pub}` with `sig` and `inv_sig`, the W1 self-wraps of the current epochs of every package scope the
   user reads after the `accept` (atomic with the `accept`, so a second browser can unlock immediately), and the
   `member.profile` event sealed with the package's org key at `lh` = the `accept`'s `seq`. The batch passes the
   client-side batch rules before it is sent (an existing member is refused with `log_accept`).
8. **Org key rotated after the invitation.** The package then lacks the current org key: the `accept` is sent without
   `member.profile` (it cannot be sealed). The display name is queued with the self-wraps (decision 9) and the job
   sends the event as soon as the user holds the current org key (catch-up by another member, W2).
9. **Self-wraps job (`core/jobs/self-wraps.ts`, job 6, order 35, triggers `login`/`log`/`interval`).** The screen
   queues the package keys (and a pending display name, decision 8) after the `accept` for the joining user
   (module-level queue per store, like the platform lock of the log job; it never leaves memory). A queue of another
   user (sign-out and another sign-in in the same tab) is dropped unused, so no key reaches another account. A run
   with the verified log containing the user lists the user's wraps per scope (`GET /v1/wraps?scope=&scope_id=`),
   wraps every queued key without a wrap of the user that W1 allows (`mayWrap` against the verified log: the user
   reads the scope now, the epoch exists, excluded devices respected) and sends them with `POST /v1/wraps`; then the
   `member.profile` if the current org key is held (`POST /v1/events`). The queue is then cleared (a queue replaced
   during the run stays; a profile without key stays). In practice the job stores the older package epochs; the
   current ones are already in the batch. The screen triggers the `log` jobs after installing the org
   (`app.tasks.refreshLog`).
10. **Retries.** `seq_conflict` (an admin appended in between): if `GET /v1/me` already lists the org, the `accept` was
    stored and its answer lost – the log is read as a member, verified like the lookup log and must contain this
    invitation accepted by the user (`log_accept` otherwise); else a new lookup (the invitee cannot read the org log)
    whose log must extend the verified one (`log_rollback`), verification and a rebuild, three builds in total. Other
    failures show "Try again"; `invite_registered` and invalid invitations end in the invalid state.
11. **Installing the org (change in `core/account/session.ts`, WEB-03).** `installOrg` pinned the user's own signing
    key as founder, correct only for an org the user founded. It now pins the founder of the verified log
    (identical for genesis, the package's founder for a joined org) and accepts any object with `org`, `log` and
    `keys`. Without this one-line change the next sign-in would refuse the joined org (`log_genesis`). The org becomes
    the last org (`rememberOrg`), `/o/:org` follows.
12. **A registration whose `accept` did not happen** (tab closed after the passkey was created) leaves the invitation
    `registered`. The org path does not stop there (unlike the platform path): the same user can resume with "I
    already have an account" (the hub accepts its own registered user); anyone else gets `invite_registered` from the
    hub.
13. **Tests.** Vitest: `tests/core/account/join.test.ts` (package and log checks against the simulated org of the core
    test world, manipulated lookup answers: another founder, dropped/forged/foreign `invite` entries, forged
    signatures, wrong served head, revoked/consumed invitations, package of another org, platform-device logs;
    `accept` batch checked by the simulated hub's batch rules; retries), `tests/core/jobs/self-wraps.test.ts`,
    `tests/ui/invite-org.test.tsx` (the screen in jsdom against the account sim hub wired to the core test org).
    Playwright `web/e2e/invite-org.spec.ts` against the real hub: new user (role, workgroups, `inv_sig`, own wraps),
    existing user with a locked session and from the intro after signing out (cancelled sign-in), used and expired
    (3 s) invitations, cancelled passkey creation, and a lookup answer forged in the browser (`page.route`) that blocks
    the screen with nothing sent. The jsdom harness answers the log job's incremental read itself because the account
    sim hub ignores `?after=`.
14. **E2E-02, E2E-03, E2E-39** (`e2e/platform/specs`) do not exist yet; they are written by SCN-03. The web-level
    spec covers their browser part.
15. **Review findings left as they are.** A device-invitation code opened in the browser shows the blocking message
    (`invite_not_in_log`) rather than the invalid one – safe, and such codes are not links. The lost-answer path
    checks the member log against the package head, not against the newer lookup head (a fork in between needs
    entries signed by an admin, which L4 checks). The browser expiry test uses a real 3-second expiry: the hub test
    server has no fake clock (07-testing.md §5 fake clocks are used in the hub's own invitation tests).
