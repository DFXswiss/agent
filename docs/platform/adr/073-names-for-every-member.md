# ADR-073: Every member has a name – an admin-given name (`member.name`) and the own name set on first open

Status: Accepted (2026-11-24; draft reviewed with fresh context before it was built)

## Context
A member's display name existed only as the member's own `member.profile` event in the org scope (`ref == author`,
04-crypto.md §4.3). A person admitted directly ([E-18](../KONZEPT.md#e-18), ADR-060) never writes one: nothing set it,
not even the first open. The member list then showed "Member …d405bb" – the owner did not recognise the three
colleagues admitted the day before. The same fallback (a fragment of an ID as the main line) was used for whiteboard
authors, audit actors, devices, workgroups, tasks and work VMs. Rules: [E-18](../KONZEPT.md#e-18) (no action of the
admitted person), [E-05](../KONZEPT.md#e-05) (what is missing is started, not explained), [E-04](../KONZEPT.md#e-04)
(everything through the agent too), [E-19](../KONZEPT.md#e-19) (exclude the class). Names are content: the hub never
sees them in plaintext ([§5 point 2](../KONZEPT.md#k-5-b2)).

## Decision
1. **New event kind `member.name`** (org scope, author a user, right `org_admin`, header `wg` empty, `ref` = the
   **subject**: a user of the org – active or not – or a **user** invitation of its log, both in the state at `lh`;
   new header check `subject`), body `{display_name}` (1–100 characters, as `member.profile`; clients write 1–80).
   It is sealed with the org key like every org-scope event: every member and every device holding the org key can
   read it, the hub cannot. It is the name an admin gives to a person – when admitting an account, when inviting
   someone (the invitation's ID as `ref`), or later for a member without an own name. It is an event, not a log entry:
   the membership log is plaintext for the hub. Runners do not pull it (`device_pull: false`).
2. **Which name is shown** (web and agent tool alike, from events that pass the reader checks E1–E5 only):
   1. the member's own `member.profile` (`ref == author`) – always first, whatever its time;
   2. else the `member.name` with `ref` = the member with the highest `scope_seq` among those whose author is an admin
      **in the current verified log** and that were written during the member's **current membership** (`lh` ≥ its
      `joined_seq`);
   3. else, for a member whose current membership started with `accept` of an invitation, the invitation's name by the
      same rule (an admin now);
   4. else no name: the interface says "Name not known yet" and that the name appears as soon as the person opens the
      organization, offers admins "Enter name", and keeps the ID behind "Show ID" (tooltip elsewhere), never as the
      main line.
   Names from 2 and 3 are marked: in the member list with who gave them, in running text as "<name> (provisional)".
   Only the **own** name (1) is the account's name (the proposal when joining another org, the source of point 3):
   an admin-given name never travels into another org.
3. **The own name comes by itself.** When a member opens an org (web: any screen under `/o/:org`; agent tool:
   `org show`) and the org has no own `member.profile` of the member, the client writes one with the own name of
   another org of the account – after the org's profiles were pulled completely, only with the current org key, at
   most once per org and sign-in. No dialog and no question: a display name is no access credential. The consent rule
   of ADR-060 (point 3) protects the user's credential keys (GitHub token, model login) from devices of an unknown
   admin's org; it stays untouched. The display name only tells the org's members who the member is; the admin who
   admitted the account chose it by its account code or user ID and gave it a name already. This supersedes the
   sentence of ADR-060 ("the admitter learns its display name only once it opens the org") in that opening now
   writes it – also when the person opens the org only to leave it (the owner's rule: the admitted person does
   nothing). If no other org has an own name or the org key is not held, nothing is written; the next open tries
   again.
4. **Invitations carry the invitee's name** (`member.name` with `ref` = `invite_id`; asked for by the web, `--name`
   in the tool). The invitation list shows "Invitation for <name>" ("Invitation without a name" with "Enter name" for
   older ones). The hub's invitation list also returns `registered_user` (the account that registered with the
   invitation; the hub stores it already; admins only). An open invitation whose registered account is a member of
   the org now is shown as settled ("<name> is a member now – this invitation is no longer needed") with "Close
   invitation", and the admitting client closes the invitations the hub names for **exactly the admitted account**
   right after the `admit` (web and agent tool), so it settles itself.
5. **Agent tool** ([E-04](../KONZEPT.md#e-04)): `org admit --name` (required), `org invite --name`, `org member-name
   --org --user (user ID | account code | invitation ID) --name`; `org show` lists `name`, `name_source`
   (`own|admin|invitation|none`) and `name_given_by` per member, the name of each invitation, and `own_name`
   (`present|written|unknown|no_key`). The tool reads names with the full reader check (`check_event`) like the web.
6. **The class (E-19).** No message of the web catalogs carries `{id}` or "…{x}" and no file under `web/src/ui` cuts
   an ID for display, except listed technical exceptions (`web/tests/core/names-class.test.ts`); fallbacks use
   `web/src/ui/names/names.tsx` (a sentence, the ID on request).

## What the hub sees, what a malicious admin, member or hub can do
The hub sees that an admin wrote a `member.name` event about a subject (author, `ref`, size, time) – the same metadata
as `device.profile` – and, for invitations, which account registered with them (already stored). It never sees a
name; the ciphertext size reveals the name's length as for `member.profile`.

- **Admin.** An admin can give a member a false name. Bounded and acceptable: an admin already holds the rights that
  matter more (remove members, read every workgroup by ADR-058, rotate keys); the name is marked as provisional and
  shows who gave it; it never replaces a member's own name (2.1), never becomes the member's own name elsewhere (2),
  and lasts only until the member opens the org (3). A name loses its effect when its author stops being an admin.
  Invitation names are readable by every member and device of the org, not only by admins.
- **Member.** A non-admin cannot write `member.name` (E5). It can already set its own profile to any name (unchanged).
- **Hub.** It cannot forge a name (signature, E5, `subject` at `lh`). It can withhold events (the list then says "Name
  not known yet" or shows an older provisional name of a current admin – a denial or a stale name, never an invented
  one), and it can withhold the member's own profile so that opening the org writes the own name again (copied from
  another org). `registered_user` is the hub's word: a lying hub can make the admitting client revoke another open
  invitation (a denial it can cause anyway) or show a false "settled" line; it cannot admit anyone.

## Consequences
- Contracts: kind `member.name` (matrix, body schema, header check `subject`), audit detail kind, optional
  `registered_user` in `GET /v1/orgs/{org}/invites`; vectors for hub, runner copy and web (`contracts/changes/HOT-12.md`).
- Introduction: hub with web first, then the agent tool (an older hub refuses the unknown kind with
  `kind_not_allowed`; the tool then reports `name_saved: false` and the admission stands). Older clients pull profile
  kinds by an explicit list and never see the new kind; an unknown kind would make only that event unverifiable.
- Runners keep naming members in sessions (`list_members`, the context block) from own profiles only – a follow-up.
