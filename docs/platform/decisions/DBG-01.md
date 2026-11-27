# DBG-01 – decisions

Package: sessions of agent.dfx.swiss query the DFX production data through the debug endpoint
([ADR-102](../adr/102-dfx-debug-queries-by-the-platform.md)).

1. **A credential kind of its own, one per user.** The debug sign-in is no mailbox: one record of the new kind
   `dfx_debug` per user, sealed under the user's `CK` like every credential and stored through the existing
   `/v1/me/credentials/dfx_debug` endpoints. No new hub route and no hub mirror are needed, because only the owner's
   own tasks use it (decision 2): the device reads the task owner's record by the existing device route.
2. **"Aufgaben des Inhabers und Workgroups, die er freigibt"** is read as both conditions together: a session of a
   task of the owner, in a workgroup the owner released the sign-in to, in a turn the owner instructed. The result of
   every operation is readable by the whole workgroup (sealed under the workgroup key), so the release names the
   audience; the owner's own instruction keeps every query attributable to the person whose Debug account the
   endpoint counts (daily limit of persons read, its own audit log). Tasks of other people in a released workgroup
   would need the hub's release mirror of ADR-099 §3; that is left to a later order.
3. **Stdin only.** `credentials set-dfx-debug` reads the address and the signature from standard input, one per line,
   like `set-mailbox` reads the password; the API URL comes as an option (no default host in the public repository).
4. **The token in memory only.** The device signs in for each query and forgets the token at its end: no token cache
   on disk or in the store; a sign-in per query is cheap compared with the query and keeps the token's lifetime short.
5. **No allowlist copy.** The runner checks the DTO's form and the bounds of the WHERE tree; tables and columns are
   the endpoint's allowlist, whose refusal reaches the session with its message. The tool's description carries the
   allowed tables and the column classes of the skill `dfx-debug`.
6. **The kill switch is the endpoint's.** A `403 Endpoint disabled` is reported as such and never retried; the
   platform has no switch that could bypass it.
7. **No write.** The DTO has no field for a write and the client has no path but the sign-in and the query; `poll`
   would be a type of its own of class `write` on an explicit order.
8. **The web section follows.** The agent tool stores and releases the sign-in; a settings section like "Mailboxes"
   is not part of this package. The web client knows the kind (it lists, checks and rotates it) so a `CK` rotation from
   the browser keeps the record.
9. **`agents.md`.** The workspace of this package does not contain the files named `agents.md`; their content is
   `src/agent_platform/guide.md`, which this package changes – the two copies follow byte for byte.
