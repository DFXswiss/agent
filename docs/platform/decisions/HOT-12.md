# HOT-12 – decisions

Package: every member has a name, never an ID instead of a person ([ADR-067](../adr/067-names-for-every-member.md)).

1. **Where the admin-given name lives:** an org-scope event `member.name` (sealed with the org key), not a field of
   the `admit` entry – the membership log is plaintext for the hub. One kind serves members and invitations (`ref` =
   user ID or `invite_id`); the header check `subject` requires a user or a user invitation of the org at `lh`.
2. **Precedence:** own `member.profile` > admin-given name of the member (author an admin in the current log, written
   during the current membership) > name of the invitation the membership started with > "Name not known yet".
   Only the own name is the account's name.
3. **Own name without a question** (task goal 2 against ADR-060 "consent"): consent in ADR-060 is about credential
   keys; a display name is none. Reviewed finding "the admitter learns the name on open" accepted as a deliberate
   change of ADR-060's residual-risk sentence (recorded in ADR-067 §3), also when the person opens the org only to
   leave it.
4. **`--name` is required for `org admit`, optional for `org invite`** in the tool (an agent may create a link before
   it knows for whom; the web asks for it). The web requires it in both forms.
5. **Invitations settle by themselves** when the admitting client admits exactly the account the hub names as
   registered with them; the list offers "Close invitation" for the others. The tool command `org invite-revoke` is
   KOR-42's; HOT-12 revokes only settled invitations, with the same log entry and route.
6. **Runners** do not pull `member.name` (`device_pull: false`): an older runner would see an unknown kind; session
   tools keep naming members from own profiles – follow-up.
7. **Class check (E-19):** catalogs without `{id}`/"…{x}" and no `<x>Id.slice(` under `web/src/ui`, with an explicit
   allow-list (operation duplicate-of, session label in the charges table). Device pickers show "Device without a
   name" (two unnamed devices look alike there; the ID is in the tooltip/second line where lists need it).
8. **Notifications** keep "…" as the placeholder of a name that is not loaded yet: it is no ID.
9. **E2E:** E2E-41 is extended (admission with a name, the own name on first open, `org member-name`, no ID fragment
   in the member list, names among the plaintext canaries). A member with no name at all is proven by the web unit
   tests and the class check; producing one in the hermetic scenario would need an admission without a name, which
   both clients refuse.
