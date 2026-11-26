# ADR-099: Sessions read mailboxes through the platform – many mailboxes per member, released to workgroups, read by the device itself with `mail.find`, never in a VM and never with the password in the session

Status: Accepted (2026-11-26)

## Context
- The client (2026-11-26, KOR-74): "wenn ein agent einen unbuntu braucht um ein e-mail postfach abzurufen dann haben
  wir was falsch gemacht". Five agents registering new subscriptions each started a work VM to read one sign-in mail
  with Python and IMAP, with the mailbox password in clear text in their instruction; the four work VM places of the
  device were taken, three agents waited over eight minutes and the sign-in codes expired.
- Rules: [§10 point 3](../KONZEPT.md#k-10-b3) (a session executes nothing itself, everything is a platform
  operation), [E-31](../KONZEPT.md#e-31) (a person's access is used only where the person switches it on),
  [E-04](../KONZEPT.md#e-04)/[E-26](../KONZEPT.md#e-26)/[E-27](../KONZEPT.md#e-27) (the web and the agent tool can do
  the same, efficiently), [E-05](../KONZEPT.md#e-05) (a refusal names its remedy), [E-11](../KONZEPT.md#e-11)
  (restrictions are voluntary), [E-25](../KONZEPT.md#e-25) (no unordered protection layers), E-33 (things run in
  parallel).
- Built before: the single credential kind `mailbox` (`contracts/credentials/mailbox.json`; the mailbox of the
  automatic Claude login, 05-runner.md §11.3, and its check `mailbox.check`); several records of one kind per owner
  with a plaintext mirror of their use (`claude_subscription`, [ADR-090](090-every-subscription-logged-in-centrally.md)
  §§1 and 3; `github`, [ADR-078](078-github-identities.md)); operations executed by the device without a VM
  (`task.set_title`, `human.question`, the `github.*` types); rights profiles over operation types
  ([ADR-094](094-rights-profiles-and-agent-roles.md)). KOR-73 gives each subscription its own mailbox for its login;
  both packages use the one store of this ADR.

## Decision

### 1. General rule: what can be a platform operation needs no VM
A session gets a work VM or a browser VM only for what really needs one: running its own code, a build, a test, a
page in a browser. Everything the device can do itself with a credential the session must not see – reading a
mailbox, a GitHub call, a question to a person – is a platform operation executed by the runner on the device,
recorded and audited like every operation, with the credential opened on the device for that one use. A new need
of sessions is first checked against this rule (05-runner.md §12.2).

### 2. Mailboxes as a platform credential – one store
- The kind `mailbox` becomes a kind with **several records per member** (`records.MULTI_KINDS`, at most
  `MAX_MAILBOXES` = 100 per user – a technical bound, not a product limit). Each record is sealed under the owner's
  `CK` like every credential (04-crypto.md §4.4); the hub never sees host, user, password, address, name or purpose.
- The body (`contracts/credentials/mailbox.json`) keeps `imap_host`, `imap_port`, `imap_user`, `imap_password`,
  `email` and gains three optional fields: `name` (1–64 characters, `[a-z0-9][a-z0-9._-]*`, unique among the owner's
  mailboxes – the clients keep it so), `purpose` (free text, at most 500 characters) and `use {wgs}` (the release,
  §3). A body without them is the mailbox from before.
- The **primary** record is the mailbox of the automatic login (the endpoints `/v1/me/credentials/mailbox` and
  `/v1/device/credentials/{owner}/mailbox` from before keep addressing it, so `mailbox.check`, the login automation
  and older clients work unchanged). A user's first mailbox becomes primary (like a first `github` record); further
  ones never do.
- Browser routes (author the user or the user's own-identity agent, ADR-082 §9.5): `GET /v1/me/mailboxes` (metadata
  of every own mailbox with its release mirror), `GET /v1/mailboxes/{id}`, `PUT /v1/mailboxes/{id}` `{record,
  expected_version, use?: {wgs}}` (0 = create; also the primary record) and `DELETE /v1/mailboxes/{id}`. Device route:
  `GET /v1/device/mailboxes?wg_id=` – the records released to that workgroup (mirror) whose owners this device is an
  eligible `CK` reader of (`credentials.eligibility.holds`), each `{id, owner_id, primary, record}`.
- **The subscription side** (the client's "oder einem Abo"): a subscription record names the mailbox of its login by
  `mailbox_id` (a record of this store of the same owner). Reading it in the login is built by KOR-73 on this store;
  until then the login uses the primary mailbox as before.

### 3. Release to workgroups (E-31)
- A mailbox is usable by sessions only where its owner released it: `use.wgs`, a list of up to 50 workgroup IDs
  where the owner has explicit access. Released to W means: every session of a task in W may read it with
  `mail.find`, whoever started the task – that is what the owner switched on. Nothing is released by default.
- The hub keeps a plaintext mirror (`hub.mailbox_use`: record, owner, workgroups) so a device finds the mailboxes of
  a workgroup in one read; `PUT /v1/mailboxes/{id}` with `use` replaces it, without `use` it stays. The hub sees
  which workgroups a mailbox serves, never the mailbox.
- The runner trusts the **sealed** release only: a record is used for a session of W when its sealed `use.wgs`
  contains W and its owner has explicit `read` or `write` access to W on the verified membership log at the time of
  the call (an owner who left W or the organization ends the release at once). A hub that lies in the mirror can
  only hide a mailbox, never lend one.

### 4. The operation `mail.find`
- Type `mail.find` (class `read`, no dedupe: every find is a new read). Parameters (`contracts/ops/objects.json`):
  - `mailbox` – the name or the ID of a mailbox released to the session's workgroup (required);
  - filters `from`, `subject` (substrings without case, printable ASCII), `since`, `until` (times), `folder`
    (`inbox`, `spam` or `all`, default `all`: the spam folder is found by its `\Junk` flag, else by its name);
  - `limit` (1–20, default 5) – the newest matching mails first;
  - `body` (default false) – with text and links of each mail; `id` – one mail of an earlier answer, with text and
    links;
  - `wait_seconds` (0–300, default 0) – §5.
- Result: `{mailbox: {id, name, email}, messages: [{id, folder, from, to, subject, date, size, text?, links?,
  text_truncated?}], waited_seconds, timed_out}`. `id` is `<folder>:<uid>`; `text` is the plain-text part (HTML
  reduced to text when there is none), at most 12 000 characters; `links` the distinct `http(s)` links of the mail in
  order, at most 20 of at most 2 000 characters each.
- Read only: the folder is opened with `EXAMINE` and bodies are read with `BODY.PEEK`, so nothing is marked read,
  moved, deleted or sent.
- Errors (E-05): `not_found` names the released mailboxes of the workgroup ("No mailbox named X is released to this
  workgroup. Released here: a, b. Its owner releases it in the settings under Mailboxes or with agent-platform
  mailbox release."); `not_found` for an `id` that is gone; `upstream_error` for a refused login ("The mailbox server
  refused the sign-in; its owner checks the mailbox in the settings.") or an IMAP failure; `timeout` when the server
  does not answer within 30 s (retryable). Never the password, never a server answer that could echo it.

### 5. Waiting is built in
- `wait_seconds` > 0 waits for a mail that matches the filters and arrived at or after `since` – without `since`,
  at or after two minutes before the request (a mail that the session triggered just before it asks counts). The
  device looks every `MAIL_POLL_SECONDS` (5 s) over one connection per look; the first match ends the wait. No match
  in time is no failure: `messages: []`, `timed_out: true`.
- The session does not poll: for `mail.find` the guest's `request_operation` waits up to `wait_seconds` + 20 s (at
  most 320 s) for the answer instead of 20 s, and the `/mcp` route allows 330 s. A guest before 0.7.0 answers
  `pending` after 20 s and the result comes with `get_operation` or the `operation.finished` ping, as for every
  operation.

### 6. Bounds
- 30 s per IMAP command and connection (`timeout`), the wait ≤ 300 s, at most 200 newest matching mails looked at per
  folder, bodies only of mails up to 1 MiB (`text_truncated` for a larger one, its headers still listed), the text
  and link bounds of §4 and an answer of at most 200 KiB (texts of the oldest mails in the answer are left out until
  it fits). TLS always (implicit TLS on the mailbox's port; the device's trust store).
- Parallel: every `mail.find` is its own connection in its own thread; many sessions read many mailboxes at
  once, bounded only by the runner's worker threads – no VM, no slot, no queue.

### 7. Audit and records – never the content
- The hub records the operation like every operation (`operation.received`/`accepted`/…, type, session, task, the
  instructing human); its parameters and result are sealed under the workgroup key in the task's events like every
  operation's (the people of the workgroup see what the session read, the hub does not).
- The runner adds the audit row `mail.read` (target kind `credential`, target ID the mailbox): `{operation_id,
  owner_id, folder, count, waited_seconds, timed_out, result}` – mailbox, time and number, never an address, subject,
  text or link. The password appears in no event, log line, audit row or answer (tests search for it).

### 8. Rights profiles
`mail.find` is an operation type: a profile whose operation list does not name it refuses it at the guest, the
runner and the hub (`rights_denied`, ADR-094 §3). It has the class `read`, so the built-in roles keep it; an own
profile leaves it out to forbid mailboxes.

### 9. People and agents (E-04)
- Web: the settings section **Mailboxes** lists every own mailbox (name, address, purpose, released workgroups,
  "login" for the primary one), adds and changes one (password write-only), checks it on a device (`mailbox.check`
  with the mailbox body, sealed to the device) and releases it to workgroups; deleting asks once.
- Agent tool: `agent-platform mailbox list|add|change|release|check|delete` (password from standard input).
- Sessions: `request_operation {type: "mail.find", …}`; the agent guide says: "Read a mailbox: the operation
  mail.find, never a VM".
- `mailbox.check` takes an optional `mailbox` (a mailbox body sealed to the device with the request; without it:
  the primary mailbox, as before).

## Consequences
- The hub learns how many mailboxes a member keeps, which workgroups each is released to and when a session read
  one (audit), never a mailbox's content or access data.
- A guest image before 0.7.0 knows no `mail.find` (its bundled contracts) and refuses it locally with `unknown_type`;
  nothing of the operation reaches an older guest, so the runner needs no version gate. Roll-out: hub, runners, then
  the web; the golden follows.
- A web client from before refuses a mailbox body with the new fields (its closed field list) only in the old
  single-mailbox form; web and hub are deployed together.

## Alternatives considered
- Mail tools in the work VM with the password injected: what the client called wrong.
- A mailbox kind of its own beside `mailbox`: two stores for one thing (KOR-73 and this package agreed on one).
- An IMAP IDLE push instead of looking every 5 s: not every server offers it, and a 5 s look is cheap for minutes of
  waiting (E-25).
- Session MCP tools `mail_*` beside the operation: one more tool family in every profile check; the operation has
  records, audit and profiles already.

## Deviation from DESIGN.md
None.
