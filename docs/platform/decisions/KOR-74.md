# KOR-74 – decisions

Package: sessions read mailboxes through the platform, without a VM and without the password
([ADR-099](../adr/099-mailboxes-read-by-the-platform.md)).

1. **One store, the existing kind.** The kind `mailbox` becomes a kind with several records per user instead of a new
   kind beside it: the single mailbox of the automatic login stays the primary record and every client and runner
   from before keeps reading it unchanged. KOR-73 (an own mailbox per subscription) builds on this store: a
   subscription names its mailbox by `mailbox_id`; until KOR-73 reads it, the login uses the primary mailbox. KOR-73's
   draft put the mailbox body into the subscription record; whichever package merges second aligns to the other
   (package text: "Wer zuerst merged, legt sie an").
2. **Release per workgroup, by the owner.** A mailbox released to a workgroup is readable by every session of that
   workgroup, whoever owns the task: that is what the owner switched on (E-31). The runner trusts only the sealed
   release and checks the owner's explicit access on the verified membership log at every call; the hub's mirror only
   finds candidates in one read. The hub refuses a mirror naming a workgroup the owner has no access to (`403`), so a
   wrong release is visible at once instead of silently doing nothing.
3. **An operation, no MCP tool family.** `mail.find` is an operation type of class `read`: it has a hub record, an
   operation audit, the profile check of ADR-094 in guest, runner and hub, and a sealed result in the task's events
   without anything new. A profile without `mail.find` forbids mailboxes.
4. **The wait lives on the device.** The executor looks every 5 s; the guest's `request_operation` waits up to
   `wait_seconds` + 20 s (at most 320 s) for `mail.find` by asking the control socket again for the same pending call
   in steps of 30 s, and the `/mcp` route allows 330 s. Should an MCP client end the call earlier, the operation still
   finishes on the device and `get_operation` reads its result. A guest before 0.7.0 cannot request the type at all.
5. **"New mail" without a clock in the session.** Without `since`, a wait counts mails that arrived at most two
   minutes before the request: a session that just triggered a sign-in mail and then asks finds it even when it
   arrived first; a session that wants only later mails passes `since`.
6. **Only TLS.** The reader connects with implicit TLS on the mailbox's port (like the login automation and
   `mailbox.check` before); a plain IMAP connection would send the password in clear text.
7. **Checking a mailbox** sends its body sealed to the device inside `mailbox.check` (`{mailbox}`), so a mailbox of
   the store can be checked before and after it is saved without a new device route for single records.
8. **Audit by the runner.** The hub cannot see what was read (zero knowledge), so the runner writes `mail.read`
   through the outbox with the mailbox as target and only counts and times as detail.
9. **No `uniqueItems`.** The guest's schema checker does not implement it; the clients and the hub deduplicate the
   released workgroups instead.
10. **`docs/platform/AGENTS.md`** named in the package text is the agent guide `docs/platform/agents.md` (and its copy
    `src/agent_platform/guide.md`).
11. **No rollback of a release** (review finding): the runner keeps the newest verified version of every mailbox
    record it used (`runner.mailbox_seen`, IDs and versions only) and refuses an older one. A deleted record that a
    dishonest hub keeps serving cannot be told apart from a living one (true for every credential); the clients end a
    release by a new version, and the documentation says so.
12. **Failures that are not the network** (review findings): a refused TLS certificate is `upstream_error` without a
    retry (the remedy is the host, not waiting); any other failure of a look is `upstream_error` and audited; a mail
    the parsers refuse keeps its headers (`text_truncated`) instead of failing every later `mail.find`. A name used by
    two released mailboxes is refused with their IDs, so the hub's order never chooses.
13. **`traceability.json`** gets the row `E2E-74` like the scenario packages before it (the traceability test needs
    every scenario there; the merge tool joins the appended rows). Milestone 3 like the other platform scenarios.
14. **Rights profiles** need no test of their own for `mail.find`: the type is in `classes.json` (class `read`) and
    the generic checks of ADR-094 in guest, runner and hub refuse every type outside a profile; the contract tests
    keep `classes.json`, the hub's copy and `INDEX.json` equal.
