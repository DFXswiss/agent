# Decisions KOR-36 – accept an organization invitation with an existing account in one clear action

Context: people with an existing account stopped on the invitation page (production, 2026-11-23): the page offered
"Create passkey" as the main button, a signed-in browser needed "Sign in with passkey", then an empty required name
field, then "Join"; nothing said "not joined yet". Nothing in the cryptography of the join changes (`04-crypto.md`
§5.2a): the same `accept` batch is built and sent; only the order of the steps in the browser changed.

1. **The step waits for the stored session.** `org_verify` used the session phase at the moment the verification
   finished; a browser still restoring its session (`starting`) landed in the new-user path – a second identity by a
   race. The step now waits until the phase is settled (`settledPhase`); the platform invitation shows nothing but
   "Checking invitation…" while the phase is `starting`.
2. **One action for an existing account.** "Join <org> with my account" (locked) runs the login ceremony, the unlock
   (PRF, else this browser's device key, else the recovery code of WEB-14) and the `accept` in one go; "Join <org> as
   <name>" (unlocked) sends the `accept` directly. The user's will is the click on a button that says "join"; a sign-in
   alone never joins (the button that signs in says "… join"), so nothing is joined without the user's will.
3. **The name.** The display name is sealed per org (`member.profile`); the hub cannot provide it (ADR-003). The
   unlocked browser reads the account's own `member.profile` from its orgs (landing org first, profiles pulled once)
   and uses it. While the keys are locked the button cannot show the name ("Join <org> with my account"). The name
   field is optional ("Use another name in this organization"); it is asked for only when no org of the account has
   a name (then the screen says "Almost done … You have not joined yet"). No name is stored in `localStorage`
   (`core/account/prefs.ts` holds IDs only). If "Use another account" signs in a different account, nothing typed or
   read for the previous account is used. In the locked case the name is not shown before the gesture (it cannot be
   read yet); a user who wants another name in the new org uses "Use another name" first – accepted as the price of
   one action (review finding, low).
4. **Not joined until joined.** Every step before the join says "You have not joined <org> yet"; failures add "You have
   not joined yet – try again". After the join the header shows "You are now a member of <org>" (slice `joined`,
   header item `joined`) while the user works in that org, until closed.
5. **Retry without the link.** The code stays in the screen's memory; every failure keeps "Try again". An `accept`
   whose answer was lost (hub restart) is recognized in `joinOrg` after any failure, not only after `seq_conflict`: if
   `GET /v1/me` lists the org, the member log is read and verified against the invitation's founder and must hold this
   invitation accepted by this user (`joinedLog`, as before for `seq_conflict`); then the org is installed and the
   self-wraps are queued like after a normal join. A failing membership check leaves the original failure. A reload
   loses the code (it left the address on the first read, `06-web.md` §2.4, kept): the new state `missing` says so and
   that nothing was joined, instead of "Check the code". Keeping the code in `sessionStorage` across reloads was
   rejected: the code is a secret and the rule "never stored" stays.
6. **Platform invitations.** A browser with a stored session is told that the link creates a second, separate account
   ("Go to my account", "Create a second account anyway"); without a session the intro offers "Sign in" for people
   who already have an account. Registering stays possible (two accounts can be wanted).
7. **State for the inviter.** `GET /v1/orgs/{org}/invites` adds `opened_at`, the first answered lookup of the
   invitation, read from the audit rows `invite.lookup` the hub writes anyway (no new column, no migration; the hub
   learns nothing new). The members screen shows "Not opened yet", "Link opened – not joined yet", "Account created –
   not joined yet" and "Accepted – <name> is a member"; accepted comes from the verified log, the hub's list is a hint
   for the rest. Accepted invitations stay listed until their expiry (the log keeps them forever). Without a browser:
   `agent org show` lists the same states under `invites` for admins (`open_invites` unchanged); non-admins get no
   `invites` key (the hub list needs `org:admin`).
8. **Test IDs.** `invite.sign-in`, `invite.back` and the separate `ready` form are gone; `invite.join` is the one main
   button of `existing_account`; new: `invite.not-joined`, `invite.one-account`, `invite.rename`, `invite.need-name`,
   `invite.other-account`, `invite.new-account`, `invite.missing`, `invite.has-account`, `invite.to-account`,
   `invite.new-anyway`, `invite.sign-in-instead`, `shell.joined`, `members.invites.status`,
   `members.invites.accepted` (accepted rows; `members.invites.item` stays for open ones).
9. **E2E.** E2E-39 gains the scenario of a further invitation opened in Bob's open session (stored session, locked
   keys – the state of a new tab; a passkey copied into a second tab's virtual authenticator gives no PRF output, so the
   original page navigates to the link),
   one passkey gesture, a hub restart in the middle of the join (`POST /hub/restart` of the orchestrator), "Try again"
   in the same tab, the inviter's states open → link opened → accepted. T2 is not available (INF-05); the scenario
   runs on T1.
10. **Review findings settled otherwise.** `opened_at` is read with the org index of `hub.audit`; the cost grows with
    the org's audit rows (acceptable for the list of one org; an index on `(action, target_id)` is a later option).
    `agent org show` reports a failing hub list like every other failing call (no partial answer). The new slice and
    header item are named `joined` (what they hold) rather than after the invitation area; both are discovered points
    of ADR-027 §4. The shared documentation changes (`spec/03-api.md`, `spec/06-web.md`) are identical in both
    repositories (`test_docs.py`); the agent commit also carries the synchronised `contracts/` (the merge would sync them).
