# Record protocol

This document fixes the team record of the third build step: the signed events, the objects
task and security issue, the rules that compute their state, the endpoints under
`/api/key/record/`, the release rule over the record, the storage in PostgreSQL, verification,
backup, and the tests the step must carry.

**Status:** specified, not built. The rules behind it are in
[DESIGN.md §24 and §25](https://github.com/DFXswiss/agent/blob/develop/DESIGN.md) of the client
repository; if this document and DESIGN.md disagree, DESIGN.md wins and this document is fixed.
Signed requests, signed answers, the keys file and the release query are specified in
[KEY-PROTOCOL.md](KEY-PROTOCOL.md); this document builds on them and does not repeat them.
[PROTOCOL.md](PROTOCOL.md) stays the contract for the session store, which this step does not
change.

The key words of this document are rules. "Must", "is" and "never" state a rule; there is no
optional behaviour unless a sentence says "may".

## 1. Scope of what is built in this step

Built in this step, in three changes whose order section 16 fixes:

- the event: its canonical signed text, its hash, the chain of each key and the hub's record
  hash over arrival order (section 4)
- the objects task (with the kinds `work` and `security`; a security issue is a task of kind
  `security`) and comment (section 5)
- the reducer: the deterministic rules that compute the state of every object from the events
  and refuse an invalid event at write time (section 6)
- the endpoints under `/api/key/record/` (section 7)
- the interface of the watcher, an external program that writes status events (section 8)
- the release rule over the record, replacing `release_answer` (section 9)
- storage in PostgreSQL: a separate `migrate` command that alone uses the owner role, and a hub
  that holds only a writer role that can only insert and read (section 11)
- verification at start, at a fixed interval and on request, signed checkpoints of the record
  hash, export, offline verification and restore (section 12)
- two new scopes in the keys file, `record:security` and `record:watch` (section 3)

Not built in this step, and named here only so that nothing below is read as building it:

- session keys of staff members and their certificates (DESIGN.md §25.6). Section 10 fixes how
  events of staff members will be accepted once they exist. Until then, no staff member can
  write to the record, because a `webauthn` key never signs a request (KEY-PROTOCOL.md section
  5, check 5).
- discussions and messages (DESIGN.md §24.4), orders and questions (DESIGN.md §25)
- the dashboard and the state of the record at a past date (DESIGN.md §24.6). The rules of this
  document make it computable (`received_at` never decreases, section 4.6); no endpoint answers
  it yet.
- reading pull requests from the code host (DESIGN.md §24.5). The hub does not call the code
  host in this step.
- the client commands for the record. They are specified in the client repository.

No existing endpoint changes, with two exceptions: the release query computes its answer from
the record (section 9), and it can answer `503` (section 7.1). The session store stays in SQLite
(`AGENT_CORE_DATABASE`); the nonce store of KEY-PROTOCOL.md stays there too.

Change 3 of section 16, the change that makes the hub use the record, also updates every sentence
of KEY-PROTOCOL.md that it makes false, in the same diff:

- the scope table and the loader rules of KEY-PROTOCOL.md section 3 (section 3 here), including
  the new loader rule that the `team` of an entry is at most 100 bytes
- in KEY-PROTOCOL.md section 5, "Every request of a `service` key that passed check 8 writes
  exactly one log line with the key id, method, raw path without the query and final status",
  and its test in KEY-PROTOCOL.md section 10, "exactly one log line per request of a `service`
  key that passed check 8": they become "exactly one request log line with the key id, method,
  raw path without the query and final status; under `/api/key/record/` that line has the form
  of RECORD-PROTOCOL.md section 13 and is written for every key, not only a `service` key, and a
  request to `GET /api/key/release` may write in addition one record line of RECORD-PROTOCOL.md
  section 13 (`record verify failed` or `record database error`)"
- in the status paragraph at the top of KEY-PROTOCOL.md, "the team record and the
  security-issue object": it becomes "the team record with tasks and security issues"
- in KEY-PROTOCOL.md section 5, "Today only `GET /api/key/release` takes a query; every other
  endpoint ignores its query": the endpoints of section 7 take and validate their own queries
- in KEY-PROTOCOL.md section 8, "Until it is, no condition can fail, and the hub answers
  `"released":true,"failed":null` for every valid query": the answer is computed by section 9
  and can be `503`
- in KEY-PROTOCOL.md section 8, "The wire format is final: a later build step changes only how
  the answer is computed", and in its section 1, "with its final wire format": they become "the
  format of the `200` answer is final; a later build step may add refusals", because this step
  adds the signed `503` `record_unverified` (section 7.1)
- the lines of KEY-PROTOCOL.md sections 1 and 8 and of the README that name the team record as
  not built

## 2. Encodings and terms

Everything in KEY-PROTOCOL.md section 2 applies unchanged: base64url without padding and
canonical, SHA-256 as 64 lowercase hex characters, times as `YYYY-MM-DDTHH:MM:SSZ` in whole
seconds, signed text as UTF-8 lines joined by one `\n` without a trailing line feed, plain
Ed25519. In addition:

| Thing | Form |
|---|---|
| UUID | 36 characters, lowercase hex in groups 8-4-4-4-12 joined by `-`, any version, not the nil UUID (all zeros). Compared as text. |
| Handle | The handle rule of KEY-PROTOCOL.md section 3. |
| Repository | The subject rule of KEY-PROTOCOL.md section 3: `owner/name`, lowercase. |
| Pull-request reference | `<repository>#<number>`, where `number` is decimal 1 to 2147483647 without sign and without leading zero. Example: `example-org/example-repo#42`. |
| Commit reference | `<repository>@<commit id>`, where the commit id is exactly 40 or exactly 64 lowercase hex characters (SHA-1 or SHA-256 repositories). |
| Single-line text | Valid UTF-8 (no surrogates, no overlong forms) of Unicode scalar values other than U+0000 to U+001F, U+007F to U+009F, U+2028 and U+2029, not starting or ending with U+0020. |
| Multi-line text | As single-line text, but U+000A (line feed) and U+0009 (tab) are allowed and the rule about U+0020 at the start and end does not apply. U+000D is never allowed. |
| Position | A decimal integer from 1 to 9223372036854775807, without sign and without leading zero. |

Text is never normalised. Two texts are equal when their bytes are equal. Every length limit in
this document counts UTF-8 bytes.

Terms:

- **Actor**: the entry of the keys file whose `id` is the event's `actor_key_id`.
- **Writer**: an actor with principal `agent` (and, once section 10 is built, `staff`) whose key
  holds `record:write`.
- **Watcher**: an actor with principal `service` whose key holds `record:watch` (section 8).
- **Now**: the hub's clock cut to whole seconds, as in KEY-PROTOCOL.md section 2.

## 3. Scopes and visibility

### New scopes

The scope table of KEY-PROTOCOL.md section 3 gains two rows. Change 3 of section 16 adds them to
that table and to the loader in the same diff.

| Scope | Allows | May be held by |
|---|---|---|
| `record:security` | Read, and with `record:write` or `record:watch` write, tasks of kind `security` and their comments | `staff`, `agent`, `service` |
| `record:watch` | Write status events as a watcher through `POST /api/key/record/watch` (section 8) | `service` |

Loader rules added to KEY-PROTOCOL.md section 3; a file that breaks one does not start the hub:

- An entry that holds `record:write`, `record:watch` or `record:security` also holds
  `record:read`.
- An entry that holds `record:watch` holds `subjects` (every `service` entry already does).
- The `team` of every entry, whatever its scopes, is at most 100 bytes (section 5.2).

The existing scopes keep their meaning: `record:read` reads the record, `record:write` writes
events through `POST /api/key/record/events`.

### Which key sees which object

A key **sees** a task when all of these hold:

1. the key holds `record:read`
2. the key's `team` equals the task's `team`
3. the task's `kind` is `work`, or the key holds `record:security`
4. the key's principal is not `service`, or at least one repository of the task (the repository
   of each pull-request reference in `links`, and `repository` for kind `security`) is in the
   key's `subjects`

A key sees a comment when it sees the comment's task. A key sees an event when it sees the
object of the event; for an event of a comment, that is the comment's task. Visibility is always
computed from the current state of the object and the current keys file.

A key never learns anything about an object it does not see, except through the three
exceptions below: such an object is answered exactly like an object that does not exist
(`object_not_found`), it is missing from every list, and its events are withheld (section 7.4).
A key with `record:read` of one team never sees anything of another team, with the same three
exceptions. There is no view without a registered key (DESIGN.md §24.7).

The three exceptions, and what they reveal:

- Record-wide positions and hashes. A withheld entry gives every key with `record:read` the
  position, `received_at`, event hash and record hash of an event it may not read. The `head` of
  section 7.4, the record head of sections 7.8 and 7.9 and every checkpoint of section 7.10 give
  the position and record hash of the head of the whole record, which includes the events of
  every team. Together they reveal that such events exist, how many there are and when they were
  received; they reveal no team, no object, no actor and no value. This is what lets every reader
  check the whole record hash chain.
- The release rule reads the tasks of every team and of both kinds. Its yes/no can therefore
  tell a `service` key with `release:query` that a pull request in its `subjects` has a linked
  task it does not see, for example a security issue or a task of another team (`task_not_ready`
  instead of `task_missing`, or `released` true). It never names the task.
- An id the caller already holds. An event whose `object_id` is the id of an existing object the
  actor does not see is an update of that object and gets `object_not_found` (check 12 (d) of
  section 6.7), where an unused id creates the object; an event whose `event_id` a stored event
  already uses, whichever key signed it, gets `event_id_taken` (check 8 of section 6.7). Both
  answers only confirm that an id the caller already holds is in use. They reveal nothing about
  the object or event behind it, and an id that its writer chose at random cannot be found by
  trying.

All three are decisions of section 17.

A key that is revoked or expired reads and writes nothing, because the request checks of
KEY-PROTOCOL.md section 5 refuse it. The events it signed earlier stay in the record and stay
attributed to it.

## 4. The event

### 4.1 Fields

Each event has these fields. All of them except `received_at` are part of the signed text.

| Field | Type and form | Meaning |
|---|---|---|
| `event_id` | UUID | Chosen by the writer. Unique in the record. |
| `object_type` | `task` or `comment` | The type of the object (section 5). No other value exists in this step. |
| `object_id` | UUID | The object. An event for an `object_id` that does not exist yet creates the object. |
| `actor_key_id` | The `id` rule of KEY-PROTOCOL.md section 3 | The key that signed. In this step a key of the keys file of kind `ed25519`. |
| `signed_at` | Time | The writer's clock when it signed. |
| `prev_hash` | 64 lowercase hex characters, or `-` | The event hash (section 4.4) of the previous event of the same key that the hub accepted; `-` for the first event of a key. |
| `key_certificate` | A value token (below) | The certificate of a session key (section 10). In this step always `-`; any other value token is refused with `certificate_not_supported` (section 6.7, check 5). |
| `changes` | 1 to 15 changes | Each change names one field with its old and its new value. |
| `signature` | 64 bytes, base64url | Ed25519 signature of the actor's key over the event text (section 4.2). |
| `received_at` | Time | The hub's clock when it appended the event (section 4.6). Not signed. |

A change has three parts:

| Part | Form |
|---|---|
| `field` | A field name of section 5. Each field at most once per event. |
| `old` | A value token: the value the writer saw before the event. `-` when the field had no value, which is always the case when the event creates the object. |
| `new` | A value token: the value after the event. `-` removes the value. |

A **value token** is either `-` (no value) or `=` followed by the base64url encoding of the
value's UTF-8 bytes. A value is never empty, so `=` alone is invalid. Every value is text; a
number such as `urgency` is written as its decimal text, and a list such as `links` as its
canonical text (section 5.2).

### 4.2 The event text

The signed bytes are the **event text**: `9 + 3 × n` lines for `n` changes.

```text
agent-event-v1
<event_id>
<object_type>
<object_id>
<actor_key_id>
<signed_at>
<prev_hash>
<key_certificate>
<n>
<field 1>
<old 1>
<new 1>
...
<field n>
<old n>
<new n>
```

Rules; an event text that breaks one is refused with `event_invalid`:

- The first line is exactly `agent-event-v1`. A later version gets a new label; a hub refuses a
  label it does not know.
- Lines are joined by one `\n`. There is no `\r`, no trailing line feed, and no empty line.
  Every line consists of visible ASCII characters (0x21 to 0x7E) only.
- `n` is decimal 1 to 15 without leading zero, and the number of lines is exactly `9 + 3 × n`.
- Every line from `event_id` to `key_certificate` has the form of its field in section 4.1:
  `event_id` and `object_id` are UUIDs (not the nil UUID), `object_type` is `task` or `comment`,
  `actor_key_id` follows the `id` rule, `signed_at` is a time, `prev_hash` is 64 lowercase hex
  characters or `-`, and `key_certificate` is a value token.
- The fields of the changes are in strictly ascending byte order, which makes each field appear
  at most once.
- `old` and `new` of one change differ. A change that changes nothing is invalid.
- Every value token is `-` or `=` with canonical base64url (KEY-PROTOCOL.md section 2) whose
  decoded bytes are not empty.
- The whole event text is at most 65536 bytes.

The limit of 65536 bytes lies above the largest event that the field limits of sections 5.2 and
5.3 allow, `team` included (at most 100 bytes). The largest valid event is an update of a
security issue that changes every changeable field except `verification`, each at its limit in
both `old` and `new`; `links` and `verification` never change in one event, because `links`
changes only outside `merged` and `done` (section 6.6) and `verification` only by row 11 of
section 6.4, which starts from `merged`. With an `actor_key_id` of 63 characters, that event is
at most 54461 bytes; every create, of a task or of a comment, is smaller, because every `old` of
a create is `-`. An event text that has the form of this section and is longer than every valid
event is therefore refused by a content check of section 6.7 (for example `value_invalid` at
check 14, or `field_not_allowed` at check 16 for an oversized value of a field the object type
does not have), never by its size. Check 2 of section 6.7 refuses by size alone only an event
text of more than 65536 bytes, which no valid event reaches.

A value whose decoded form breaks the form of its field (section 5) is refused with
`value_invalid`. A field name that the object type or the task kind does not have is refused with
`field_not_allowed` (section 6).

Example: a writer creates a task of kind `work` with the title `Fix login` for the team
`example`. The values are `work`, `example` and `Fix login`.

```text
agent-event-v1
0b6f3c1e-8d2a-4c1b-9f3e-2a7d5c9e1f40
task
5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d
example-staff-laptop-1
2026-11-17T10:00:00Z
-
-
3
kind
-
=d29yaw
team
-
=ZXhhbXBsZQ
title
-
=Rml4IGxvZ2lu
```

The next event of the same key assigns `example-staff` and starts the work:

```text
agent-event-v1
6a0e9d4c-2b7f-4e1a-8c3d-5f2b1a0e9d8c
task
5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d
example-staff-laptop-1
2026-11-17T10:05:00Z
<event hash of the first event>
-
2
assignee
-
=ZXhhbXBsZS1zdGFmZg
status
=b3Blbg
=aW5fcHJvZ3Jlc3M
```

Every example answer of sections 7 and 12 follows this one story, on a hub with the key id
`hub-1`:

| Position | `received_at` | What |
|---|---|---|
| 1 to 15 | before `2026-11-17T10:00:00Z` | earlier events of other keys |
| 16 | `2026-11-17T10:00:01Z` | the first event above: `example-staff-laptop-1` creates the task `5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d` of kind `work` |
| 17 | `2026-11-17T10:05:01Z` | the second event above: the same key assigns `example-staff` and sets `in_progress` |
| 18 | `2026-11-17T10:06:00Z` | an event of another team, withheld from the keys of the team `example` |
| 19 | `2026-11-17T10:30:00Z` | `example-staff-laptop-1` writes the comment `8e2f4a6c-1b3d-4e5f-9a7b-0c1d2e3f4a5b` on the task |
| 20 | `2026-11-17T10:45:00Z` | `example-staff-laptop-1` writes a second comment on the task |
| 21 | `2026-11-17T11:20:00Z` | `example-staff-laptop-1`, signed at `2026-11-17T11:20:00Z`, sets `links` of the task to `example-org/example-repo#42` |

Nothing is appended after position 21. The hub writes a checkpoint of position 20 at
`2026-11-17T11:00:00Z` and one of position 21 at `2026-11-17T12:00:00Z` (section 12.2), and the
export of `2026-11-18T02:00:00Z` ends at position 21.

### 4.3 The signature

`signature` is the plain Ed25519 signature of the actor's key over the event text bytes. The hub
verifies it with the `public_key` of the actor's entry in the keys file. The request that carries
the event is signed separately (KEY-PROTOCOL.md section 5); that request signature is not stored
and does not replace the event signature.

The event text names no hub and no environment. This is acceptable: an event is accepted only
through a request signed by the actor's own key (check 4 of section 6.7 needs `actor_key_id` to
equal the request's `Agent-Key`), so only the key holder can send its event to another hub, and
only to a hub whose keys file registers that key. There the event enters that key's own chain
(section 4.5), where the key holder sees it through `GET /api/key/record/head` (section 7.8).

### 4.4 The event hash

The **event hash** is the SHA-256 of the event text bytes, as 64 lowercase hex characters. It
identifies the event in the chain of its key and in the record hash.

### 4.5 The chain of a key

Every key has its own chain (DESIGN.md §24.3):

- The first event the hub accepts from a key has `prev_hash` `-`.
- Every later event of that key has as `prev_hash` the event hash of the last event of that key
  that the hub accepted.
- `signed_at` of an event is not earlier than `signed_at` of the previous accepted event of the
  same key.

A writer can sign while the hub is unreachable: it chains each new event to its own previous
event. When the hub refuses an event, that event stays in the writer's local log together with
the refusal; the writer signs its later events again, each against the last event of that key
that the hub accepted (DESIGN.md §24.3). `GET /api/key/record/head` tells a writer that last
event (section 7.8).

### 4.6 Positions, `received_at` and the record hash

The hub keeps one record. It appends accepted events in the order it accepts them:

- **Position**: the first event has position 1; every further event has the position of the
  previous one plus 1. There is no gap.
- **`received_at`**: the later of now and the `received_at` of the event at the previous
  position. It therefore never decreases along the positions, so the state at a past moment is
  the fold of a prefix of the record (DESIGN.md §24.6).
- **Record hash**: the SHA-256 of this text, as 64 lowercase hex characters:

```text
agent-record-v1
<position, decimal>
<received_at>
<event hash>
<record hash of the previous position, or - at position 1>
```

Position, `received_at` and record hash are computed values. Anyone who holds the events can
compute them again; they are not events and the hub states nothing with them. The only statement
the hub signs about the record is the checkpoint (section 12.2).

The **head** of the record is the position and record hash of its last event; an empty record
has position 0 and no record hash.

## 5. Objects

### 5.1 Object types

| `object_type` | What it is |
|---|---|
| `task` | A unit of work. Kind `work` is an ordinary task; kind `security` is a security issue (DESIGN.md §24.4). |
| `comment` | A text attached to one task. A comment is written once and never changed; a correction is a new comment. |

Discussions, messages, orders and questions are later build steps. An event with any other
`object_type` is refused with `event_invalid`.

### 5.2 Fields of a task

"Create" means the event that creates the task. "Required" means the field has a value after
every event; "allowed" means it may have one.

| Field | Form | Kind `work` | Kind `security` | Changeable after create |
|---|---|---|---|---|
| `kind` | `work` or `security` | required | required | no |
| `team` | Valid UTF-8, 1 to 100 bytes, with no further form: it is a team name of `teams.yaml`, and the keys-file loader refuses an entry whose `team` is longer (section 3), so no key and no task has a longer team. At create equal to the actor's `team` (section 6.5, rule 1). | required | required | no |
| `title` | Single-line text, 1 to 200 bytes | required | required | yes |
| `description` | Multi-line text, 1 to 16384 bytes | allowed | allowed | yes |
| `assignee` | Handle | allowed | allowed | yes |
| `status` | `open`, `in_progress`, `ready_for_review`, `merged`, `done`, `dropped` | required | required | yes, only by the transitions of section 6.4 |
| `completion` | `merged` or `verified` (DESIGN.md §24.11) | required | required | yes |
| `links` | 1 to 20 pull-request references, joined by one space, in strictly ascending byte order | allowed | allowed | yes, not in `merged` or `done` |
| `repository` | Repository | refused | required | yes |
| `short_name` | 1 to 30 characters of `a-z`, `0-9` and `-`, starting and ending with a letter or digit, without `--`. Unique among the tasks of kind `security` of the same team. | refused | required | yes |
| `severity` | `low`, `medium`, `high`, `critical` | refused | required | yes |
| `urgency` | Decimal 1 to 10 without leading zero | refused | required | yes |
| `source` | 1 to 200 visible ASCII characters (0x21 to 0x7E): an external reference to where the issue was found | refused | allowed | yes |
| `fix` | Pull-request reference or commit reference | refused | allowed; required in `done` | yes |
| `verification` | Single-line text, 1 to 2000 bytes: what a staff member checked in production | allowed | allowed | only by row 11 of section 6.4, which sets a value; never removed |

Defaults at create: when the create event has no `status`, the status is `open`; when it has no
`completion`, the completion is `merged` for kind `work` and `verified` for kind `security`
(DESIGN.md §24.11). The default is part of the computed state; it is not written into the event.

Status names on the wire use `_` instead of the spaces of DESIGN.md: `in_progress` is "in
progress", `ready_for_review` is "ready for review". `completion` `verified` is the rule
"verified in production".

The reducer also keeps, per task, the derived value **`completed_links`**: the value of `links`
when row 14 of section 6.4 last set the task from `merged` or `done` to `in_progress`; none
before that. Rows 11, 12 and 13, which take a task from `merged` to `done`, leave it unchanged.
It is not a field and is never written by an event.

### 5.3 Fields of a comment

| Field | Form | Rule |
|---|---|---|
| `task` | UUID | Required at create. A task the actor sees. |
| `body` | Multi-line text, 1 to 16384 bytes | Required at create. |

A comment has no other field. Every event for an existing comment is refused: by the first
earlier check of section 6.7 that fails (for example `object_not_found` when the actor does not
see the comment's task, `state_mismatch` for a stale `old` value or another `object_type`), and
otherwise, with matching `old` values, with `field_immutable`.

### 5.4 What never enters the record

Nothing is ever deleted from the record (DESIGN.md §24.3). Personal data, secrets, credentials
and customer data therefore never enter any field. `source` and `fix` are references that reveal
nothing by themselves; a description names what is wrong, not whose data was affected. The hub
cannot check this; a review treats a field or a client form that invites such data as a defect.
The only names of people in the record are handles, which are already public in the keys file.

## 6. The reducer

The state of the record is the fold of its events in position order. The reducer applies one
event to the state. It is deterministic: it reads only the state before the event and the event,
and for the actor rules (section 6.5) the keys file and `received_at`.

### 6.1 Two kinds of rule

- **State rules** (sections 6.2 to 6.4 and 6.6) depend only on the state and the event. The hub
  checks them at write time, and every fold checks them again: at start, in the periodic
  verification (section 12.1), in `GET /api/key/record/verify`, in the incremental fold of a
  request or a checkpoint attempt (sections 11.6 and 12.2), and in offline verification.
- **Actor rules** (section 6.5) depend on the actor's entry in the keys file, on the endpoint
  that received the event, and on `received_at`. The hub checks them only at write time. A later
  change of the keys file, such as a revocation or a changed scope, never makes an accepted
  event invalid (DESIGN.md §24.2: events a key already signed stay valid).

An event that breaks a rule is refused with the error value of that rule. It is never stored and
changes nothing.

### 6.2 Create and update

- An event whose `object_id` does not exist creates the object. Every `old` of the event is `-`.
- An event whose `object_id` exists updates it. Every `old` equals the current value of its
  field, `-` for a field without value.
- `object_type` of an update equals the type of the existing object.

A mismatch of an `old` value is refused with `state_mismatch`; this makes concurrent edits
explicit instead of losing one (DESIGN.md §24.3). A mismatch of `object_type` is refused with
`state_mismatch` too.

### 6.3 Fields

- A field that the object type does not have, or that the kind does not have (the column
  "refused" of section 5.2), is refused with `field_not_allowed`. The kind is the `new` value of
  `kind` for a create and the current kind for an update.
- `kind` and `team` of an existing task, and any field of an existing comment, are refused with
  `field_immutable`.
- A value outside the form of its field is refused with `value_invalid`.

### 6.4 Status transitions

A status change is valid when one row of this table matches. "Before" is the state before the
event; "after" is the state after it. The column "Who" is an actor rule (section 6.5); every
other column is a state rule.

| # | From | To | Who | Further condition |
|---|---|---|---|---|
| 1 | (create) | `open` | writer | none |
| 2 | (create) or `open` | `in_progress` | writer; for kind `security` only the writer whose `handle` is the `assignee` after the event | `assignee` has a value after the event |
| 3 | `in_progress` | `ready_for_review` | writer | none |
| 4 | `ready_for_review` | `in_progress` | writer | none |
| 5 | `in_progress`, `ready_for_review` | `open` | writer | none |
| 6 | `open`, `in_progress`, `ready_for_review` | `dropped` | writer | none |
| 7 | `dropped` | `open` | writer | none |
| 8 | `open`, `in_progress`, `ready_for_review` | `done` | watcher | `completion` is `merged` before and after; `links` has a value |
| 9 | `open`, `in_progress`, `ready_for_review` | `merged` | watcher | `completion` is `verified` before and after; `links` has a value |
| 10 | `in_progress`, `ready_for_review` | `done` under `completion` `merged`, or `merged` under `completion` `verified` | assignee | `completion` unchanged by the event; `links` has no value, or equals `completed_links` |
| 11 | `merged` | `done` | staff | `completion` is `verified` before and after; the event changes `verification` to a value (its `new` is not `-`) |
| 12 | `merged` | `done` | assignee | `completion` is `merged` before and after; `links` has no value |
| 13 | `merged` | `done` | writer | The same event changes `completion` from `verified` to `merged`; `links` has a value |
| 14 | `merged`, `done` | `in_progress` | writer | `assignee` has a value after the event. The reducer sets `completed_links` to the value of `links`. |

- "Writer", "watcher" and "staff" are the actors of section 2. "Assignee" is an actor with
  principal `agent` (or `staff`, section 10) whose `handle` equals the task's `assignee`, where
  the event does not change `assignee`.
- Row 13 is DESIGN.md §24.11 "changing the rule of a task in status `merged` to `merged` sets
  the task to `done` in the same event". The condition of the rule `merged` held when the task
  reached `merged`, and `links` cannot change in `merged` (section 6.6), so it still holds. An
  event that changes `completion` from `verified` to `merged` on a task in `merged` with `links`
  and does not set `done` is refused with `transition_invalid`. A task in `merged` without
  `links` keeps its status; its assignee then sets `done` by row 12.
- An event that changes `completion` of a task in `done` without changing `status` is valid and
  leaves the status `done`.
- Row 10 is DESIGN.md §24.11 for a task without a linked pull request, and for a task that was
  set back while the condition of its rule already held: the hub cannot ask the code host, so
  "already true and no further pull request linked" is decided by `links` being equal to
  `completed_links`, the value it had when row 14 last set the task back. Row 10 starts only
  from `in_progress` or `ready_for_review`: a task in `open` reaches `done` or `merged` through
  its assignee only by row 2 first and then row 10, two events. This is narrower than DESIGN.md §24.11 and a decision of section 17.
- A change of `status` that matches no row by its state conditions is refused with
  `transition_invalid`. A change that matches a row by its state conditions, but no such row
  allows the actor, is refused with `change_not_allowed`.
- An event that changes `verification`, whether it sets a value or removes it (`new` is `-`),
  without matching row 11 is refused with `transition_invalid`. Row 11 sets a value; no row
  removes one, so a value of `verification` is never removed.

### 6.5 Actor rules

Checked at write time only. Rule 1 is checked at check 12 (c) of section 6.7, before every check
that reads the state of a team, so that a create for another team never reaches a check that can
reveal that team's state (such as `short_name_taken`). Rules 2 to 6 are checked at check 18 of
section 6.7, in this order:

| # | Rule | Status | `error` |
|---|---|---|---|
| 1 | At create of a task with a change of `team`, the new value of `team` equals the actor's `team`. | 403 | `change_not_allowed` |
| 2 | The actor is allowed the status change by the column "Who" of section 6.4. | 403 | `change_not_allowed` |
| 3 | Through `POST /api/key/record/events`, the actor is a writer. Through `POST /api/key/record/watch`, the event changes `status`, and it changes no other field except `fix` of a task of kind `security` to a value that is one of the task's `links` (section 8). | 403 | `change_not_allowed` |
| 4 | For kind `security`, a change of `assignee` is allowed in exactly three cases: (a) `assignee` had no value and its new value is the actor's `handle` (whoever starts takes it themselves); (b) `assignee` had a value and the actor's `handle` equals that value (the assignee hands it over or gives it up); (c) `assignee` had a value that is **orphaned**, and its new value is the actor's `handle`. Otherwise nobody takes over an assigned security issue (DESIGN.md §24.4). | 403 | `change_not_allowed` |
| 5 | A new value of `assignee` other than `-` is, for a task of kind `work`, the `handle` of at least one entry of the keys file with principal `staff` or `agent`, whose `team` equals the task's `team`, and that is neither revoked nor expired at `received_at`; for a task of kind `security`, the `handle` of at least one entry that **can act on** the task at `received_at` (below). | 409 | `assignee_invalid` |
| 6 | Through `POST /api/key/record/watch`, the repository of every reference in the task's `links` is in the watcher's `subjects`. | 403 | `subject_not_allowed` |

An entry **can act on** a task of kind `security` at a time when all of these hold: its principal
is `agent`; its `team` equals the task's `team`; it is neither revoked nor expired at that time;
and it holds `record:read`, `record:write` and `record:security`. These are exactly the entries
that can sign an event which changes the task through `POST /api/key/record/events` (a watcher
changes it only through its own endpoint and is never an assignee): a `staff` entry is a passkey
and signs no event in this step (section 1), a key without `record:read` or `record:security` does not see the task
(check 12 (d) of section 6.7), and a key without `record:write` is not a writer (rule 3). A later
step adds the session keys of staff members (section 10).

An `assignee` value of a task of kind `security` is **orphaned** when the keys file has no entry
whose `handle` is that value and that can act on the task at `received_at`. Rule 5 and
"orphaned" use the same condition: a security issue is handed only to a handle that can act on
it at that moment, so handing it over (case (b)) to a handle with only a `staff` entry, with only
revoked or expired entries, or whose `agent` entries lack `record:write` or `record:security`, is
refused with `assignee_invalid`; and an assignee that later loses that condition, by revocation,
expiry or a removed scope, is orphaned at once.

Without case (c) of rule 4, a security issue whose assignee can no longer act on it could never be
reassigned, because only the assignee may change `assignee`. With it, any writer of the same team
that holds `record:security` takes the issue over by setting `assignee` to its own `handle`. The
actor's team and `record:security` already follow from check 12 (d) of section 6.7 (an update
needs an actor that sees the task), `record:write` from rule 3, and its own validity at
`received_at` from check 11, so the actor's own handle always passes rule 5. Case (c) never allows
setting a handle other than the actor's own. When no key of the team can act on the task, the
issue keeps its orphaned assignee until the keys file gives one such key.

### 6.6 Invariants after the event

State rules on the state after the event:

| Rule | Status | `error` |
|---|---|---|
| Every required field of section 5.2 or 5.3 has a value. | 409 | `field_required` |
| In `in_progress` and `ready_for_review`, `assignee` has a value. | 409 | `field_required` |
| A task of kind `security` in `done` has a value in `fix`. | 409 | `field_required` |
| `short_name` is unique among the tasks of kind `security` of the same team. | 409 | `short_name_taken` |
| An event that changes `links` has neither before nor after it the status `merged` or `done`. | 409 | `links_frozen` |
| `task` of a comment names a task. | 404 | `object_not_found` |

### 6.7 Order of the checks of one event

For an event that arrives at `POST /api/key/record/events` or `POST /api/key/record/watch`, after
checks 1 to 9 of KEY-PROTOCOL.md section 5, the hub checks in this order and stops at the first
failure. Checks 1 to 6 need no lock; checks 7 to 18 run inside the append transaction (section
11.5).

| # | Check | Status | `error` |
|---|---|---|---|
| 1 | The query is empty. | 400 | `query_invalid` |
| 2 | The body is one JSON object with exactly the members `event` and `signature`, each a string, no member twice. `event` is canonical base64url of 1 to 65536 bytes; `signature` is canonical base64url of 64 bytes. | 400 | `body_invalid` |
| 3 | The event text has the form of section 4.2. | 400 | `event_invalid` |
| 4 | `actor_key_id` equals the request's `Agent-Key`. | 403 | `actor_mismatch` |
| 5 | `key_certificate` is `-`. | 400 | `certificate_not_supported` |
| 6 | The signature verifies with the actor's public key over the event text. | 400 | `event_signature_invalid` |
| 7 | No stored event has this event hash. | 409 | `event_duplicate` |
| 8 | No stored event has this `event_id`. | 409 | `event_id_taken` |
| 9 | `prev_hash` is the event hash of the last accepted event of this key, or `-` when there is none. | 409 | `chain_broken` |
| 10 | `signed_at` is at most 120 seconds after now and not earlier than `signed_at` of the last accepted event of this key. | 400 | `event_time_invalid` |
| 11 | The actor's key is neither revoked nor expired at `received_at`. | 409 | `key_not_valid_at_receipt` |
| 12 | The object, in this order: (a) through the watch endpoint, an object that does not exist or is not a task, such as a comment (404 `object_not_found`); (b) a create of a task without a change of `kind`, or a create of a comment without a change of `task` (409 `field_required`); (c) a create of a task with a change of `team` whose new value differs from the actor's `team`, actor rule 1 of section 6.5 (403 `change_not_allowed`); (d) an update of an object the actor does not see, or a create of a comment whose `task` value is not a UUID, names no task or names a task the actor does not see (404 `object_not_found`). | 403 / 404 / 409 | `change_not_allowed` / `object_not_found` / `field_required` |
| 13 | A create of a task of kind `security` by an actor without `record:security`. | 403 | `scope_missing` |
| 14 | Every value of a field that the object type has is in the form of that field (section 5). | 400 | `value_invalid` |
| 15 | Section 6.2. | 409 | `state_mismatch` |
| 16 | Section 6.3. | 400 / 409 | `field_not_allowed` / `field_immutable` |
| 17 | Section 6.4 (state conditions) and section 6.6, in the order of their tables. | 404 / 409 | as given there |
| 18 | Section 6.5, rules 2 to 6, in the order of its table. | 403 / 409 | as given there |

Check 11 can fail only when `received_at` is later than now (section 4.6); otherwise checks 3 and
4 of KEY-PROTOCOL.md section 5 have already refused the request. It is a different situation from
those checks: the key that signed the request is valid now, but not at the later `received_at`
that the event would get. It therefore has its own error value; `key_revoked` and `key_expired`
keep the status `401` of KEY-PROTOCOL.md and are never answered by a check of this table. An event
that a key signed before its revocation and that reaches the hub only afterwards is always
refused, by check 3 or 4 of KEY-PROTOCOL.md section 5 (DESIGN.md §24.3); it stays in the writer's
local log.

Because of check 12 (b), checks 13 and 16 always know the kind of a task create, and check 12 (d)
always knows the task of a comment create. A `task` value of a comment create that is not a UUID
therefore gets `object_not_found` at check 12 (d), before check 14 could give `value_invalid`.

Because of check 12 (c), a task create for another team is refused before checks 15 to 17, which
read the state; in particular `short_name_taken` (section 6.6) is only ever answered for a short
name of the actor's own team. A create of a task without a change of `team` passes check 12 (c)
and is refused at check 17 with `field_required`, which reveals no state.

While the hub process is in the state `record_unverified` (section 12.1), the `503`
`record_unverified` of section 7.1 comes after check 2 and before check 3. When the process
enters that state in the fold of step 2 of the append transaction (section 11.5), which runs
after check 6, the `503` comes after check 6 and no check from 7 on runs.

A refusal changes nothing: no row is written, the chain of the key does not advance, and the
state is unchanged.

## 7. Endpoints

### 7.1 Common rules

Every endpoint of this section:

- needs a signed request (KEY-PROTOCOL.md section 5) and has its answer signed (section 6
  there), whatever the status, for every request that passed check 8 of KEY-PROTOCOL.md section
  5; a refusal at checks 1 to 8 there stays unsigned, as KEY-PROTOCOL.md section 6 requires
- names no subject, so check 10 of KEY-PROTOCOL.md section 5 does not apply; the subjects of a
  `service` key act through visibility (section 3) and actor rule 6 (section 6.5)
- answers a refusal with exactly `{"error":"<value>"}`, without whitespace and without any data
  of the record
- answers JSON written without whitespace, with the members in the order shown in this document
- runs the incremental fold of section 11.6 before it reads the state, at the point section 11.6
  gives for it: a `GET` endpoint after the `503` check below, an append endpoint only in step 2
  of the append transaction, after checks 1 to 6 of section 6.7, so that an append refused at
  checks 1 to 6 folds nothing; `GET /api/key/record/verify` reads the whole record instead
  (section 7.9)
- answers `503` with `{"error":"record_unverified"}`, signed, while the hub process that receives
  the request is in the state `record_unverified` (section 12.1); see "Order of the `503`" below
- answers `500` with `{"error":"internal"}` when the database fails, signed, as KEY-PROTOCOL.md
  section 6 requires for a `500` that occurs after check 8 of KEY-PROTOCOL.md section 5

**Order of the `503`.** In a process that is already in the state `record_unverified`, the
refusal comes after checks 1 to 10 of KEY-PROTOCOL.md section 5 (check 10 applies only to the
release query) and after the validation of the endpoint's query and body, and before any read of
the record. A key without the scope
therefore still gets `scope_missing`, an invalid query `query_invalid` and an invalid body
`body_invalid`. In detail:

| Endpoint | The `503` comes |
|---|---|
| `POST /api/key/record/events`, `POST /api/key/record/watch` | after checks 1 and 2 of section 6.7, before check 3; when the fold of the append transaction fails, after check 6 (section 6.7) |
| every `GET` endpoint of this section | after the query validation, before anything else; for `GET /api/key/record/verify` also before the `429` of section 7.9 |
| `GET /api/key/release` | after step 3 (check 10, `subject_not_allowed`) of the order of checks of KEY-PROTOCOL.md section 8, before the release rule of section 9 |

The table applies to a process that is already in the state when the request arrives. When the
incremental fold that a request runs (section 11.6) fails, the process enters this state and
answers that request with the same signed `503`, at the point where that fold runs.

**Query.** An endpoint takes the query parameters named for it and nothing else. The query is
read from the raw request target that the signature covers, like the release query
(KEY-PROTOCOL.md section 8): pairs `name=value` joined by `&`, each name at most once, no unknown
name, no empty value, no parameter without `=`, no empty pair, no `+`, and no `%` except `%2F` or
`%2f` for the `/` in a `repository` value. An endpoint without parameters takes only the empty
query. An invalid query gets `400` with `{"error":"query_invalid"}` after check 9 of
KEY-PROTOCOL.md section 5 (the scope) and before anything else. A `GET` endpoint ignores a
request body.

**Paging.** A list is read in pages. `limit` is decimal without leading zero, within the range
given for the endpoint; without `limit`, the default given there applies. A page holds at most
`limit` items; the cursor of the next page is in the answer, or `null` when there is no further
item.

| Endpoint | Method and path | Scope |
|---|---|---|
| 7.2 | `POST /api/key/record/events` | `record:write` |
| 7.3 | `POST /api/key/record/watch` | `record:watch` |
| 7.4 | `GET /api/key/record/events` | `record:read` |
| 7.5 | `GET /api/key/record/object` | `record:read` |
| 7.6 | `GET /api/key/record/tasks` | `record:read` |
| 7.7 | `GET /api/key/record/comments` | `record:read` |
| 7.8 | `GET /api/key/record/head` | `record:read` |
| 7.9 | `GET /api/key/record/verify` | `record:read` |
| 7.10 | `GET /api/key/record/checkpoints` | `record:read` |

Any other method or path under `/api/key/record/`, a trailing slash and `HEAD` get the signed
`404` `not_found` of KEY-PROTOCOL.md section 5.

### 7.2 `POST /api/key/record/events`

Appends one event of a writer. Body:

```json
{"event":"<base64url of the event text>","signature":"<base64url of 64 bytes>"}
```

The checks are those of section 6.7. A request may be sent with `Content-Encoding: gzip`
(KEY-PROTOCOL.md section 5). Answer `201`, here to the second event of the story of section 4.2:

```json
{"position":17,"event_hash":"<64 hex>","record_hash":"<64 hex>","received_at":"2026-11-17T10:05:01Z","hub_key":"hub-1"}
```

The answer is the hub's signed acknowledgement that the event is stored at that position. A
writer that receives no answer reads `GET /api/key/record/head` before it sends anything else; a
second send of the same event is refused with `event_duplicate`.

### 7.3 `POST /api/key/record/watch`

Appends one event of a watcher (section 8). Body, checks and answer as in section 7.2.

### 7.4 `GET /api/key/record/events`

Reads the record in position order, for replication and audit.

| Parameter | Form | Default |
|---|---|---|
| `after` | `0` or a position | `0` |
| `limit` | 1 to 100 | 100 |
| `object` | UUID | none |

Without `object`, the answer holds every event with a position greater than `after`, in
ascending position, at most `limit`. An event the key sees is given in full; an event it does not
see is **withheld**: its position, `received_at`, event hash and record hash are given, its text
and signature are `null`. A withheld entry does not say whether the event belongs to another
team, to a security issue or to a task outside a `service` key's subjects. The record hash chain
can be checked over all entries, withheld or not.

With `object`, the answer holds only the events of that object, in ascending position, after
`after`; for a task this does not include the events of its comments. When the key does not see
the object, or it does not exist, the answer is `404` `object_not_found`.

Answer of the story of section 4.2 to `?after=16&limit=2` for `example-staff-laptop-1`:

```json
{"events":[{"position":17,"received_at":"2026-11-17T10:05:01Z","event_hash":"<64 hex>","record_hash":"<64 hex>","event":"<base64url>","signature":"<base64url>"},{"position":18,"received_at":"2026-11-17T10:06:00Z","event_hash":"<64 hex>","record_hash":"<64 hex>","event":null,"signature":null}],"next_after":18,"head":21,"hub_key":"hub-1"}
```

- `next_after` is the position of the last entry of this page when a further entry exists for
  the same request, otherwise `null`.
- `head` is the position of the record head when the page was read, `0` for an empty record.

### 7.5 `GET /api/key/record/object`

Reads the current state of one object. One parameter, `id`, a UUID, required. When the key does
not see the object, or it does not exist, the answer is `404` `object_not_found`. Answer `200`:

```json
{"object":{...},"hub_key":"hub-1"}
```

A task has exactly these members in this order. A field without value is `null`; `links` and
`completed_links` are arrays, empty when there is no value; `urgency` is a JSON number, or `null`
like every other field without value. The task of the story of section 4.2, after position 21:

```json
{"type":"task","id":"5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d","team":"example","kind":"work","title":"Fix login","description":null,"assignee":"example-staff","status":"in_progress","completion":"merged","links":["example-org/example-repo#42"],"completed_links":[],"repository":null,"short_name":null,"severity":null,"urgency":null,"source":null,"fix":null,"verification":null,"comments":2,"created":{"position":16,"received_at":"2026-11-17T10:00:01Z","key":"example-staff-laptop-1","handle":"example-staff"},"changed":{"position":21,"received_at":"2026-11-17T11:20:00Z","key":"example-staff-laptop-1","handle":"example-staff"}}
```

A task of kind `security` has the same members; there `repository`, `short_name`, `severity` and
`urgency` always have a value, for example
`"repository":"example-org/example-repo","short_name":"login-bypass","severity":"high","urgency":8`.

- `comments` is the number of comments of the task.
- `created` names the event that created the object, `changed` the last event of the object;
  `key` is its `actor_key_id` and `handle` the `handle` of that key's entry.

A comment:

```json
{"type":"comment","id":"8e2f4a6c-1b3d-4e5f-9a7b-0c1d2e3f4a5b","task":"5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d","body":"Reproduced on the example build.","created":{"position":19,"received_at":"2026-11-17T10:30:00Z","key":"example-staff-laptop-1","handle":"example-staff"}}
```

### 7.6 `GET /api/key/record/tasks`

Lists the tasks the key sees, newest first: by the position of `changed` descending. Each task
has its own `changed` position, so the order is total.

| Parameter | Form | Default | Selects |
|---|---|---|---|
| `kind` | `work` or `security` | none | tasks of that kind |
| `status` | a status of section 5.2 | none | tasks in that status |
| `assignee` | a handle, or `-` | none | tasks with that assignee; `-` selects tasks without assignee |
| `repository` | repository | none | tasks with a link into that repository, or kind `security` with that `repository` |
| `before` | position | none | tasks whose `changed` position is lower |
| `limit` | 1 to 200 | 50 | |

All given filters must hold. A key without `record:security` that asks for `kind=security` gets
an empty list.

```json
{"tasks":[{...}],"next_before":null,"hub_key":"hub-1"}
```

Each item has the form of section 7.5. `next_before` is the `changed` position of the last task
of this page when a further task matches, otherwise `null`. Pages are not a snapshot: a task that
changes between two pages moves to the front and can be missed or appear twice. A reader that
needs a consistent set reads the events (section 7.4).

### 7.7 `GET /api/key/record/comments`

Lists the comments of one task, newest first by position.

| Parameter | Form | Default |
|---|---|---|
| `task` | UUID, required | |
| `before` | position | none |
| `limit` | 1 to 200 | 50 |

When the key does not see the task, or it does not exist, the answer is `404`
`object_not_found`.

```json
{"comments":[{...}],"next_before":null,"hub_key":"hub-1"}
```

Each item has the comment form of section 7.5; `next_before` is the position of the last comment
of this page when a further comment exists, otherwise `null`.

### 7.8 `GET /api/key/record/head`

No parameters. Answers the head of the record and of the caller's own chain:

```json
{"position":21,"record_hash":"<64 hex>","key":"example-staff-laptop-1","key_position":21,"key_event_hash":"<64 hex>","key_signed_at":"2026-11-17T11:20:00Z","hub_key":"hub-1"}
```

- `position` and `record_hash` are the record head; `0` and `null` for an empty record.
- `key_position`, `key_event_hash` and `key_signed_at` describe the last accepted event of the
  calling key; all three are `null` when the key has none. A writer uses `key_event_hash` as its
  next `prev_hash`.

The answer names no other key's chain.

### 7.9 `GET /api/key/record/verify`

No parameters. The hub reads every stored event and checkpoint from the database, not from its
state in memory, in the one snapshot of section 12.1, and verifies them as in section 12.1.

**Limit.** Each hub process starts at most one verification on request within any 60 seconds,
measured on a monotonic clock, so that a hub clock that is set back never blocks verification. A
request that arrives while a verification on request runs, or less than 60 seconds after the
previous one started, is answered with a signed `429` and `{"error":"verify_busy"}`, with no
further detail and no header that names a waiting time. The limit counts runs on request only;
the verification at start (section 11.4) and the periodic verification (section 12.1) do not
count. The order of checks of this endpoint is: checks 1 to 9 of KEY-PROTOCOL.md section 5, the
query validation (`query_invalid`), the `503` of section 7.1, then this `429`.

Answer `200`, for the story of section 4.2:

```json
{"verified":true,"position":21,"record_hash":"<64 hex>","failure":null,"hub_key":"hub-1"}
```

For an empty record, `position` is `0` and `record_hash` is `null`, as in section 7.8:
`{"verified":true,"position":0,"record_hash":null,"failure":null,"hub_key":"hub-1"}`.

When verification fails, `verified` is `false`, `position` is the position of the failure as
section 12.1 defines it, `record_hash` is `null` and `failure` is one value of section 12.1 other
than `file_invalid`. The status is `200` in both cases. After a failure the process that answered
enters the state `record_unverified` of section 12.1: it answers `503` `record_unverified` to every
record endpoint and to the release query until it is restarted, and a restart verifies again
(section 11.4). Other hub processes on the same database enter that state at the latest with
their next periodic verification (section 12.1).

### 7.10 `GET /api/key/record/checkpoints`

Lists the checkpoints (section 12.2) of every hub key id, newest first: by position descending,
and at the same position by hub key id descending in byte order. The pair of position and hub
key id is unique (section 11.3), so the order is total.

| Parameter | Form | Default |
|---|---|---|
| `before` | position | none |
| `before_key` | the `id` rule of KEY-PROTOCOL.md section 3 | none |
| `limit` | 1 to 200 | 50 |

`before` and `before_key` are given together or not at all; one without the other is
`query_invalid`. With them, the page holds only checkpoints whose position is lower than
`before`, or whose position equals `before` and whose hub key id is lower than `before_key` in
byte order.

Answer of the story of section 4.2 to `?limit=1`:

```json
{"checkpoints":[{"position":21,"record_hash":"<64 hex>","published_at":"2026-11-17T12:00:00Z","signed_by":"hub-1","signature":"<base64url>"}],"next_before":21,"next_before_key":"hub-1","hub_key":"hub-1"}
```

`next_before` and `next_before_key` are the position and the hub key id of the last checkpoint of
this page when a further one exists, otherwise both `null`. A checkpoint reveals only a position
and a hash of the whole record, so every key with `record:read` reads all of them; this is part of
the first exception of section 3.

## 8. The watcher

The watcher is an external program with its own `service` key (DESIGN.md §24.11). It observes
merges and closes of linked pull requests and changes of links on the code host, and writes the
status change when the condition of a task's rule becomes true. The hub authors nothing; it
checks the watcher's event like any other.

Its keys file entry is an ordinary `service` entry (KEY-PROTOCOL.md section 3) with the scopes
`record:read` and `record:watch`, `record:security` when it serves security issues, and the
repositories it observes as `subjects`. Example:

```yaml
  - id: example-watcher-1
    kind: ed25519
    principal: service
    handle: example-watcher
    team: example
    device: runner-1
    public_key: "<base64url, 32 bytes>"
    scopes: [record:read, record:watch, record:security]
    subjects: [example-org/example-repo]
    expires: "2027-06-30T00:00:00Z"
```

A watcher event is sent to `POST /api/key/record/watch` and is valid only when:

- its object is an existing task the watcher sees (otherwise `object_not_found`)
- it changes `status` by row 8 or row 9 of section 6.4 (otherwise `transition_invalid` or
  `change_not_allowed`)
- it changes no other field, except `fix` of a task of kind `security`, whose new value is one of
  the task's `links` (otherwise `change_not_allowed`)
- every repository of the task's `links` is in the watcher's `subjects` (otherwise
  `subject_not_allowed`)

The watcher writes a task's next status only after a change of its linked pull requests or its
links (DESIGN.md §24.11); the hub cannot observe the code host and does not check this. A watcher
key has one team; a watcher that serves several teams has one key per team. When those keys share
one handle and one `device`, each entry carries its own `program` label (for example
`program: watch-example`), because KEY-PROTOCOL.md section 3 requires the pair of `device` and
`program` to be unique among the `agent` and `service` entries of one handle that are not
revoked; without it the keys file does not load. A watcher never writes through
`POST /api/key/record/events`: a `service` key cannot hold `record:write`.

## 9. The release rule

`release_answer(repository, pull_request)` of `GET /api/key/release` (KEY-PROTOCOL.md section 8)
is replaced by this rule over the current state. The query, the order of checks and the format
of the `200` answer stay as specified there; the only addition is the signed `503`
`record_unverified` of section 7.1, which comes after step 3 of the order of checks of
KEY-PROTOCOL.md section 8 (after `scope_missing`, `query_invalid` and `subject_not_allowed`) and
before the rule below reads the record.

The **linked tasks** of a pull request are the tasks of every team and of both kinds whose
`links` contain the reference `<repository>#<pull_request>` and whose status is not `dropped`.
The conditions are checked in this order:

| `failed` | Condition | After this step |
|---|---|---|
| `task_missing` | The pull request has at least one linked task. | checked |
| `task_not_ready` | Every linked task has an `assignee` and the status `in_progress` or `ready_for_review`. | checked |
| `question_unanswered` | No question of an order for a linked task is unanswered. | always met: orders and questions are not built (DESIGN.md §24.11) |

When all hold, the answer is `"released":true,"failed":null`; otherwise `"released":false` and
`failed` names the first that failed. The visibility of section 3 does not limit what the rule
reads: the answer is a yes or no and names no task. A security issue counts as a linked task
(DESIGN.md §24.11). Because the rule reads every team and both kinds, its yes/no can reveal to
the asking key that a linked task exists that the key does not see, such as a security issue or
a task of another team; this is the second exception of section 3 and a decision of section 17.

From this step on, a pull request that no task links gets `task_missing`. That is the intended
effect of DESIGN.md §24.11; the answer stays one input for a guard and replaces no review rule.
Section 16 fixes when a guard may start to rely on it.

## 10. Events of staff members (not built)

A staff member's passkey never signs a request or an event directly. A later build step adds the
session key of DESIGN.md §25.6, which a passkey certifies and which signs every write from the
website. This section fixes how its events are accepted, so that the record format of this step
stays valid; nothing here is built now.

- The event text keeps the form of section 4.2 and the label `agent-event-v1`. `actor_key_id` is
  the id of the session key; the later step defines its form and keeps it disjoint from every
  `id` of the keys file. `key_certificate` is `=` followed by the base64url of the certificate
  bytes that DESIGN.md §25.6 defines; until that step, any value other than `-` is refused with
  `certificate_not_supported`.
- The hub then accepts such an event only when the certificate verifies against a `webauthn`
  entry of the keys file that is neither revoked nor expired at `received_at`, the certificate
  binds the session key's public key and that key verifies the event signature, the
  certificate's expiry has not passed at `received_at`, and that expiry lies no further than the
  longest session-key lifetime of DESIGN.md §25.6 after the time at which the hub issued the
  fresh value named in the certificate (DESIGN.md §24.3). These checks take the place of check 6
  of section 6.7, with error values the later step adds.
- A session key has its own chain (section 4.5), starting with `-`.
- For every rule of this document, the actor of such an event is the passkey's entry: principal
  `staff`, its `handle`, `team` and scopes. Only then can row 11 of section 6.4 and the
  confirmation of DESIGN.md §24.11 be written.
- The later step adds to the entries that can act on a security issue (section 6.5), and so to
  rule 5 for kind `security` and to "orphaned", a `staff` entry from whose passkey a session key
  can be certified at `received_at` and that holds the same three scopes. Until then only `agent`
  entries count there.
- Fold and offline verification verify the certificate against the passkey's public key, but
  check neither revocation nor expiry, as for every other key (section 6.1).

Until that step, a task with `completion` `verified` reaches `merged` but not `done`, because
row 11 needs a staff member.

## 11. Storage in PostgreSQL

### 11.1 Database and version

The record is stored in one PostgreSQL database of its own, version 16 or later, separate from
the SQLite store of the session bus (DESIGN.md §24.8). The hub accesses it with the `psycopg`
package (version 3) and plain SQL.

### 11.2 Roles

| Role | Used for | Rights |
|---|---|---|
| Owner | Only the `migrate` command (section 11.4). Never the hub process, `export` or `restore`. | Owns the database, and through the migration the schema `record` and its tables, function and triggers. Not a superuser. |
| Writer | The hub process, `export` and `restore` (sections 11.4 and 12.3) | `CONNECT` on the database, `USAGE` on the schema `record`, `SELECT` on `record.schema_version`, `SELECT` and `INSERT` on `record.event` and `record.checkpoint`, each granted to the role by name by the migration, without grant option. Nothing else: the role is named in no other privilege of any object in any database of the server (no other table, column, sequence, schema, function, database or default privilege); through `PUBLIC` it holds no `CREATE` or `TEMPORARY` on the database, no `CREATE` on any schema and no `EXECUTE` on `record.refuse_change()`; it owns no object; it is a member of no role, directly or indirectly, also not of the owner role or of a predefined role of PostgreSQL such as `pg_execute_server_program`, `pg_write_server_files`, `pg_write_all_data` or `pg_read_all_data`; and it has the role attribute `LOGIN` and none of `SUPERUSER`, `BYPASSRLS`, `CREATEROLE`, `CREATEDB` and `REPLICATION`. |

The deployment creates both roles and the database, with the owner role as the owner of the
database (`CREATE DATABASE <database> OWNER <owner>`), so that the owner holds the `CREATE` right
on the database that `CREATE SCHEMA` needs. It creates the writer role with `LOGIN` and none of
`SUPERUSER`, `BYPASSRLS`, `CREATEROLE`, `CREATEDB` and `REPLICATION`, grants it no role and
grants it no right; every right of the writer comes from the migration. The passwords of both roles are created by the deployment's secret tooling, never by a
person (section 15). The migration (section 11.3) grants the writer exactly the rights of the
table above and nothing else: no role membership, no attribute and no right on any other object.
It also revokes every right of `PUBLIC` on the database and on `record.refuse_change()`, so that
the writer holds exactly the rights of the table above, and the hub checks them at every start
(section 11.4).

Rights that every role holds through `PUBLIC` by the defaults of PostgreSQL, such as `EXECUTE` on
the built-in functions or `USAGE` on the schema `public`, are not rights of the writer in the
sense of this table, with one exception: the four rights through `PUBLIC` that the table names
(`CREATE` and `TEMPORARY` on the database, `CREATE` on any schema, `EXECUTE` on
`record.refuse_change()`) are refused, because each of them would let the role create or run
something in the database.

The hub process never holds the owner's connection string (DESIGN.md §24.8: the role the hub uses
may only insert and read, and the no-delete rule is enforced by the database, not only by the
hub's code). The no-delete rule is enforced by the database twice: the writer role has no
`UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` or `TRIGGER` right on either table, and a trigger
refuses `UPDATE`, `DELETE` and `TRUNCATE` on both tables for every role. The owner and a database
superuser can still disable that trigger or drop a table; neither is available to the hub. That
is why the record is verified at every start and every 10 minutes, checkpoints are signed, and
backups are kept off-site (section 12).

### 11.3 Schema

Schema version 1. `<owner>` and `<writer>` are the role names, and `<database>` the database
name, of the two connection strings that the `migrate` command reads (section 11.4).

```sql
REVOKE ALL ON DATABASE <database> FROM PUBLIC;
GRANT CONNECT ON DATABASE <database> TO <writer>;

CREATE SCHEMA record AUTHORIZATION <owner>;
REVOKE ALL ON SCHEMA record FROM PUBLIC;
GRANT USAGE ON SCHEMA record TO <writer>;

CREATE TABLE record.schema_version (
  version    integer     PRIMARY KEY,
  applied_at timestamptz NOT NULL
);

CREATE TABLE record.event (
  position     bigint      PRIMARY KEY CHECK (position >= 1),
  event_id     uuid        NOT NULL UNIQUE,
  actor_key_id text        NOT NULL,
  prev_hash    text        NOT NULL CHECK (prev_hash = '-' OR prev_hash ~ '^[0-9a-f]{64}$'),
  event_hash   text        NOT NULL UNIQUE CHECK (event_hash ~ '^[0-9a-f]{64}$'),
  received_at  timestamptz NOT NULL
               CHECK (date_trunc('second', received_at AT TIME ZONE 'UTC') = received_at AT TIME ZONE 'UTC'),
  event_text   bytea       NOT NULL CHECK (octet_length(event_text) BETWEEN 1 AND 65536),
  signature    bytea       NOT NULL CHECK (octet_length(signature) = 64),
  record_hash  text        NOT NULL UNIQUE CHECK (record_hash ~ '^[0-9a-f]{64}$'),
  UNIQUE (actor_key_id, prev_hash)
);

CREATE TABLE record.checkpoint (
  hub_key_id   text        NOT NULL,
  position     bigint      NOT NULL CHECK (position >= 1),
  record_hash  text        NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
  published_at timestamptz NOT NULL
               CHECK (date_trunc('second', published_at AT TIME ZONE 'UTC') = published_at AT TIME ZONE 'UTC'),
  signature    bytea       NOT NULL CHECK (octet_length(signature) = 64),
  PRIMARY KEY (hub_key_id, position)
);

CREATE FUNCTION record.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'record rows are never changed or removed' USING ERRCODE = 'P0001';
END
$$;
REVOKE ALL ON FUNCTION record.refuse_change() FROM PUBLIC;

CREATE TRIGGER event_no_change BEFORE UPDATE OR DELETE ON record.event
  FOR EACH ROW EXECUTE FUNCTION record.refuse_change();
CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON record.event
  FOR EACH STATEMENT EXECUTE FUNCTION record.refuse_change();
CREATE TRIGGER checkpoint_no_change BEFORE UPDATE OR DELETE ON record.checkpoint
  FOR EACH ROW EXECUTE FUNCTION record.refuse_change();
CREATE TRIGGER checkpoint_no_truncate BEFORE TRUNCATE ON record.checkpoint
  FOR EACH STATEMENT EXECUTE FUNCTION record.refuse_change();

GRANT SELECT, INSERT ON record.event, record.checkpoint TO <writer>;
GRANT SELECT ON record.schema_version TO <writer>;
INSERT INTO record.schema_version (version, applied_at) VALUES (1, now());
```

- `event_text` and `signature` are the authoritative data. `event_id`, `actor_key_id`,
  `prev_hash` and `event_hash` are copies taken from the event text for the unique constraints;
  verification checks that they agree (section 12.1).
- `UNIQUE (actor_key_id, prev_hash)` makes a fork of a key's chain impossible in the database,
  also for a second first event (`prev_hash` `-`).
- `received_at` and `published_at` are stored in whole seconds; the `CHECK` on each refuses a
  value with a fraction of a second (SQLSTATE `23514`). The comparison is made in UTC, so it does
  not depend on the session's time zone.
- There is no table of materialised state: the state is computed in the hub's memory (section
  11.6), so nothing in the database is ever updated.

### 11.4 Migration and start

The migration and the hub are two programs. Only the migration holds the owner's connection
string; the hub holds only the writer's.

**The `migrate` command.** `python -m agent_core.record migrate` applies the schema. The
deployment runs it to completion before every start of the hub: in the compose file it is a
one-shot service, and the hub service depends on it with the condition
`service_completed_successfully` (section 15). It reads `AGENT_CORE_RECORD_OWNER_DATABASE` and
`AGENT_CORE_RECORD_DATABASE`; it connects only with the owner's string and uses the writer's
string only for the writer role name and the comparison of section 15. It runs these steps and
stops at the first failure:

1. `config`: both connection strings are present and have the form of section 15, name different
   roles, and name the same host, port, database and `sslmode`; and the libpq that `psycopg`
   uses is version 16 or later (section 15).
2. `owner`: it connects with the owner role and refuses an owner role that is a superuser or that
   does not own the database.
3. `migrate`: in one transaction that first takes `pg_advisory_xact_lock(742180002)`, so that two
   runs never migrate at once, it applies the schema of section 11.3 when `record.schema_version`
   does not exist, does nothing when it holds exactly the versions the code knows, and fails when
   it holds a version the code does not know or lacks one the code knows.

It prints one line and exits: `migrated <version>` (exit 0) when it applied the schema,
`unchanged <version>` (exit 0) when the schema was already current, or `failed <step>` (exit 1),
where `<step>` is `config`, `owner` or `migrate`. It prints no value from the configuration and
no message of a database error.

**The start of the hub.** The hub does not start, and logs one line `record start failed <step>`
without any value from the configuration, when a step of this sequence fails:

1. `config`: `AGENT_CORE_RECORD_DATABASE` is present and has the form of section 15,
   `AGENT_CORE_RECORD_OWNER_DATABASE` is not set in the hub's environment, also not empty, and
   the libpq that `psycopg` uses is version 16 or later (section 15).
2. `schema`: the hub connects with the writer role. `record.schema_version` exists and holds
   exactly the versions this code expects (in this version: exactly one row, version 1). A missing
   schema, a missing version or an unknown version fails this step. The hub never creates or
   changes the schema.
3. `writer`: the role has exactly the writer rights of section 11.2 and nothing else. The hub
   reads the catalog of the server and fails this step unless all of these hold:
   - `current_user` is the role of the connection string, and in `pg_roles` its `rolsuper`,
     `rolbypassrls`, `rolcreaterole`, `rolcreatedb` and `rolreplication` are all false.
   - `pg_auth_members` has no row whose `member` is the role, so it is a member of no role,
     directly or indirectly, including the owner role and every predefined role of PostgreSQL.
   - It owns no object: no database (`pg_database.datdba`), no tablespace
     (`pg_tablespace.spcowner`), and `pg_shdepend` has no row of type `o` that names it, in any
     database of the server.
   - The rows of type `a` of `pg_shdepend` that name it, which cover the privileges and default
     privileges of every database of the server, name exactly five objects, each with `objsubid`
     0: this database, the schema `record` and the tables `record.schema_version`, `record.event`
     and `record.checkpoint`. `aclexplode` on the privileges of each gives for the role exactly
     the rights of section 11.2 (`CONNECT`; `USAGE`; `SELECT`; `SELECT` and `INSERT`; `SELECT`
     and `INSERT`), each without grant option.
   - Through `PUBLIC` it has neither `CREATE` nor `TEMPORARY` on the database, no `CREATE` on any
     schema of the database, and no `EXECUTE` on `record.refuse_change()`.
   - The four triggers of section 11.3 exist and are enabled (`tgenabled` is `O`).

   The check of `pg_auth_members` does not rely on how `has_table_privilege` reports the rights
   of a predefined role: a membership fails this step whatever role it names.
4. `verify`: the hub reads the whole record in the snapshot of section 12.1 and verifies it as
   there. On success it logs `record verified <position> <record hash>` (`0 -` for an empty
   record), and this run is the first run of the periodic verification of section 12.1.

### 11.5 The append transaction

One event is appended in one transaction of the writer role, which starts only after checks 1 to
6 of section 6.7 passed:

1. `SELECT pg_advisory_xact_lock(742180001)`; the number is fixed and serialises every append and
   every checkpoint, also across several hub processes on one database.
2. The hub runs the incremental fold of section 11.6. When it fails, the hub rolls the
   transaction back and answers the signed `503` `record_unverified` (section 7.1).
3. Checks 7 to 18 of section 6.7.
4. `INSERT` of the row with the next position, `received_at` and record hash.
5. `COMMIT`. Only after the commit succeeded does the hub apply the event to its state and
   answer `201`.

A failure of the database in any step answers `500` `internal` and leaves the state in memory
unchanged. When the outcome of the commit is unknown, the next incremental fold of the process
(section 11.6) finds the row and folds it.

### 11.6 State in memory

The hub keeps the state in memory: every object, the head of every key's chain, the index of
`short_name` per team, the index from pull-request references to tasks, and the record head.
At start it is built by the fold of section 11.4. After that the hub brings it up to date by the
**incremental fold**: it reads the stored events with a position greater than the head of its
state, in position order, and folds them. The incremental fold runs at exactly these points:

- in every `GET` endpoint of section 7 except `GET /api/key/record/verify`, after the `503` check
  of section 7.1 and before the endpoint reads the state
- in `GET /api/key/release`, after the `503` check of section 7.1 and before the release rule of
  section 9
- in step 2 of the append transaction (section 11.5), which starts only after checks 1 to 6 of
  section 6.7; an append refused at checks 1 to 6 runs no fold
- in step 2 of every checkpoint attempt (section 12.2)

`GET /api/key/record/verify` runs no incremental fold; it reads the whole record (section 7.9).

The incremental fold checks each event it reads against every row of the table of section 12.1
from `position_gap` to `event_refused`, in the order of that table, exactly as a full
verification does, with the state of the process as the state before the first event it reads.
In particular the first event it reads has the position of the head plus 1 and every further one
the position after it (`position_gap`); the copied columns of each row agree with its event text
(`row_mismatch`); and `received_at` is not earlier than at the previous position, the head of the
state for the first event (`event_time_invalid`). A failure has the effect of a failed
verification: the process enters the state `record_unverified` of section 12.1, logs the failure
as section 13 gives, and answers the request that ran the fold with the signed `503`
`record_unverified`. The state in memory keeps the events before the failing one.

This fold reads only events after the process's state. A row at or before that state that is
changed later is found by the next full verification: at start, periodically or on request
(section 12.1).

**The keys file of a process.** Every process checks against the keys file it loaded at start
(KEY-PROTOCOL.md section 3). When the processes on one database run with different keys files, as
during a rolling deployment of a changed keys file, a process with the older file fails its
incremental fold with `key_unknown` at the first stored event of a key that only the newer file
has. That process then answers `503` `record_unverified` until it is restarted, and the restart
with the newer file verifies the record again (section 11.4). This is intended: a process never
treats as sound an event whose key it cannot check. A revocation or a changed scope in the newer
file never has this effect, because the fold checks neither (section 6.1).

## 12. Integrity, checkpoints and backup

### 12.1 Verification

Verification of a sequence of stored events and checkpoints, in position order, against the keys
file, loaded with the teams file by the loader of KEY-PROTOCOL.md section 3:

| `failure` | Fails when |
|---|---|
| `file_invalid` | Only in `verify` and `restore` (section 12.3): the export file breaks the file format of section 12.3. |
| `position_gap` | The positions are not 1, 2, 3, … without gap. |
| `event_invalid` | An event text breaks section 4.2, or a value breaks the form of its field. |
| `row_mismatch` | A stored `event_id`, `actor_key_id`, `prev_hash` or `event_hash` differs from the value taken from the event text. |
| `key_unknown` | `actor_key_id` is not an `ed25519` entry of the keys file. |
| `event_signature_invalid` | The signature does not verify. |
| `chain_broken` | `prev_hash` is not the event hash of the previous event of the same key in position order, or `-` for its first. |
| `event_time_invalid` | `received_at` is earlier than at the previous position, `signed_at` is later than `received_at` plus 120 seconds, or `signed_at` is earlier than that of the previous event of the same key. |
| `record_hash_mismatch` | A stored record hash differs from the one computed by section 4.6. |
| `event_refused` | The state rules of section 6 refuse the event. |
| `checkpoint_invalid` | A checkpoint signed with the hub's current key id has a position beyond the head, a record hash that differs from the record hash at its position, or a signature that does not verify with the hub's public key. |

Revocation, expiry, scopes and the other actor rules are not checked (section 6.1). Checkpoints of
another hub key id are skipped by the hub; offline verification checks them when it is given that
key.

`file_invalid` is checked first, over the whole file. Then the events are checked in position
order, each against the rows of the table from `position_gap` to `event_refused` in the order of
the table, and then the checkpoints in the order of the file or of the database (hub key id, then
position). The first failure ends the run. The **position of a failure**, printed by `verify` and
`restore` and answered by section 7.9, is:

- for `file_invalid`, as section 12.3 defines it
- for `position_gap`, the first missing position
- for `checkpoint_invalid`, the position the failing checkpoint names
- for every other value, the position of the failing event

**State per process.** The state `record_unverified` belongs to one hub process, not to the hub
as a whole; several processes on one database each hold their own. A process enters it when a
verification it runs fails: on request (section 7.9), periodically (below), in the incremental
fold of a request (section 11.6), or in a checkpoint attempt (section 12.2); a failure at start
stops the start instead (section 11.4). It logs the position and the failure: in the one line of
the request when a request under `/api/key/record/` ran the verification, otherwise in the line
`record verify failed <position> <failure>` (section 13). From then until it is restarted, the
process answers the signed `503` `record_unverified` of section 7.1 on every record endpoint and
on the release query, makes no checkpoint attempt and runs no periodic verification. A restart verifies again (section 11.4). The rest of the hub in that process keeps
working: the session store, pairing, sync and `GET /api/key/whoami` answer as before.

**Periodic verification.** Every hub process runs a full verification of the stored record, in
the one snapshot below: first at start (step `verify` of section 11.4), then again 10 minutes
after the previous run started, or when the previous run ends if that is later. The 10 minutes
are measured on a monotonic clock. A periodic run reads beside the requests; the process keeps
answering from its state while it runs. It does not count for the limit of section 7.9. A
database failure in a periodic run is not a broken record: the process logs
`record database error read <SQLSTATE or ->` and runs again at the next interval.

When one process finds the stored record broken, every other process on the same database finds
it at the latest at the end of its next periodic run. As long as one run takes less than 10
minutes, two processes therefore disagree about whether the record is sound for at most 10
minutes plus the duration of one verification run; when one run takes longer, for at most twice
the duration of a run.

**One snapshot.** The verification at start (section 11.4), the periodic verification (above),
the verification on request (section 7.9) and `export` (section 12.3) read every event, every
checkpoint and, for `export`, the head written into the header, inside one read-only transaction
at isolation level `REPEATABLE READ`
(`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`). Every read of that run sees the same
snapshot: an append or a checkpoint that commits while the run reads is either wholly in the
snapshot or wholly outside it. A checkpoint beyond the head of the snapshot is therefore a real
`checkpoint_invalid`, never the effect of a concurrent append, and the header of an export always
matches its last event line.

### 12.2 Checkpoints

The hub publishes the record hash at a fixed interval, signed with its own key (DESIGN.md
§24.8). A **full hour** is a time whose seconds since 1970-01-01T00:00:00Z are divisible by 3600.
Each hub process makes one checkpoint attempt at the first moment at or after each full hour `H`
of its clock; when the process was paused or busy across several full hours, it makes one attempt
for the latest of them. A process in the state `record_unverified` (section 12.1) makes no attempt
and writes no checkpoint. An attempt is one transaction of the writer role:

1. `SELECT pg_advisory_xact_lock(742180001)`, the lock of section 11.5.
2. It runs the incremental fold of section 11.6, with every check of that fold. When that fold
   fails, it writes nothing, rolls the transaction back and enters the state
   `record_unverified`. Because every append holds the same lock, the head of its state after
   this fold is the head of the database.
3. It reads the newest checkpoint of its own hub key id (the one with the highest position) from
   the database and checks it as the row `checkpoint_invalid` of section 12.1 does: its position
   is not beyond the head, its record hash equals the stored record hash of the event at its
   position, and its signature verifies with the hub's public key. When that check fails, it
   writes nothing, rolls the transaction back, enters the state `record_unverified` and logs
   `record verify failed <position> checkpoint_invalid`.
4. It writes nothing when the head of its state is position 0 (empty record), when the head
   position equals the position of that newest checkpoint (the head did not move since the last
   checkpoint), or when that checkpoint has a `published_at` at or after `H` (another process
   already wrote the checkpoint of this hour).
5. Otherwise it inserts the checkpoint of the head of its state, that is the position and record
   hash that the fold of step 2 or an earlier fold of this process checked, with `published_at`
   equal to now at that moment, with `ON CONFLICT DO NOTHING`, and commits.

The hub therefore signs only a record hash it has checked. A row at or before the state of the
process that is changed after the process folded it does not change the signed record hash; the
next full verification (section 12.1) finds the change, and the checkpoint makes it detectable to
everyone who holds a copy.

Because every attempt holds the lock while it reads and inserts, the hub writes at most one
checkpoint per hub key id and full hour, and at most one per hub key id and head position, also
with several hub processes on one database. The checkpoint text is:

```text
agent-record-head-v1
<hub key id>
<position>
<record hash>
<published_at>
```

`signature` is the hub's Ed25519 signature over that text. When the attempt inserted a row, the
hub logs `record checkpoint <position> <record hash>`. A later rewrite of history is detectable
from any checkpoint stored outside the hub, in a reader's copy, a log store or a backup. A checkpoint is a
statement of the hub, not an event: the hub still authors no event.

### 12.3 Export, offline verification and restore

Besides `migrate` (section 11.4), the hub package provides three commands. None of them needs the
hub to run, and none of them reads `AGENT_CORE_RECORD_OWNER_DATABASE`.

| Command | Does |
|---|---|
| `python -m agent_core.record export --out FILE` | Connects with `AGENT_CORE_RECORD_DATABASE` and writes the whole record, read in the one snapshot of section 12.1, to `FILE` with mode `0600`. Refuses an existing `FILE`. |
| `python -m agent_core.record verify --keys KEYS --teams TEAMS --in FILE [--hub-key ID=PUBLIC_KEY]...` | Verifies `FILE` as in section 12.1 against the keys file `KEYS`, loaded with the teams file `TEAMS` by the loader of KEY-PROTOCOL.md section 3, offline, and checks every checkpoint whose hub key id is given. |
| `python -m agent_core.record restore --in FILE [--hub-key ID=PUBLIC_KEY]...` | Restores `FILE` into a migrated, empty database, in the order below. |

`ID=PUBLIC_KEY` is a hub key id by the `id` rule of KEY-PROTOCOL.md section 3, `=`, and the hub's
Ed25519 public key as in KEY-PROTOCOL.md section 2. Each `ID` is given at most once. `--out`,
`--keys`, `--teams` and `--in` are given exactly once where the command names them; no other
argument exists.

`export` checks its configuration, then refuses an existing `FILE` before it connects, then
creates `FILE` so that it fails when `FILE` exists, reads the snapshot and writes the file. When
it fails after it created `FILE`, it removes `FILE`, so that no partial export remains. It prints
one line and exits:

| Line | Exit | When |
|---|---|---|
| `exported <position> <record hash or ->` | 0 | the file was written; the head of the snapshot, `0 -` for an empty record |
| `refused config` | 1 | `--out` is missing, an argument is unknown or given twice, `AGENT_CORE_RECORD_DATABASE` is missing, empty or outside the form of section 15, or the libpq that `psycopg` uses is older than version 16 |
| `refused file_exists` | 1 | `FILE` exists; nothing is written |
| `refused file_write` | 1 | `FILE` cannot be created or written |
| `refused database` | 1 | a database operation failed |

`verify` prints one line and exits:

| Line | Exit | When |
|---|---|---|
| `verified <position> <record hash or ->` | 0 | the file passed verification; the head, `0 -` for an empty record |
| `refused config` | 1 | `--keys`, `--teams` or `--in` is missing, an argument is unknown or given twice, a `--hub-key` value breaks its form or names an `ID` twice, the teams file or the keys file does not load, or `FILE` cannot be read |
| `failed <position> <failure>` | 1 | the file fails verification (section 12.1) |

`restore` runs these steps in this order and stops at the first failure; it writes nothing unless
step 4 commits:

1. It checks `FILE`, in this order: (a) the file format of this section (`failed <position>
   file_invalid`); (b) every checkpoint line names a hub key id given with `--hub-key`
   (`refused hub_key_missing`), so that it never inserts a checkpoint whose signature it did not
   check; (c) the rest of the verification of section 12.1, as `verify` does, against the keys
   file of `AGENT_CORE_KEYS` loaded with the teams file of `AGENT_CORE_TEAMS`, with every
   checkpoint checked with the public key given for its hub key id (`failed <position>
   <failure>`).
2. It connects with `AGENT_CORE_RECORD_DATABASE`, the writer role, and runs the steps `schema`
   and `writer` of the hub start (section 11.4). It never creates the schema: the operator runs
   `migrate` first. A database without the schema `record` is refused with a message that says
   so.
3. In one transaction that takes `pg_advisory_xact_lock(742180001)`, it refuses a database whose
   `record.event` or `record.checkpoint` holds a row.
4. In the same transaction it inserts every event and checkpoint with their stored positions,
   times and hashes, and commits.

`restore` prints one line and exits:

| Line | Exit | When |
|---|---|---|
| `restored <position> <record hash or ->` | 0 | the record was inserted |
| `refused config` | 1 | before step 1: `--in` is missing, an argument is unknown or given twice, a `--hub-key` value breaks its form or names an `ID` twice, `AGENT_CORE_KEYS`, `AGENT_CORE_TEAMS` or `AGENT_CORE_RECORD_DATABASE` is missing, empty or invalid, the libpq that `psycopg` uses is older than version 16, the teams file or the keys file does not load, or `FILE` cannot be read |
| `failed <position> file_invalid` | 1 | step 1 (a): the file breaks the file format |
| `refused hub_key_missing` | 1 | step 1 (b): a checkpoint's hub key id was not given |
| `failed <position> <failure>` | 1 | step 1 (c): the file fails the rest of the verification; as for `verify` |
| `refused schema_missing (run python -m agent_core.record migrate first)` | 1 | step 2: the schema `record` does not exist |
| `refused schema_version` | 1 | step 2: the schema holds another version than this code expects |
| `refused writer_rights` | 1 | step 2: the role does not have exactly the writer rights |
| `refused not_empty` | 1 | step 3 |
| `refused database` | 1 | a database operation failed |

The export file is JSON Lines, UTF-8, one JSON object per line, written without whitespace:

1. A header with the head of the snapshot, here for the story of section 4.2:
   `{"format":"agent-record-export-v1","position":21,"record_hash":"<64 hex>","exported_at":"2026-11-18T02:00:00Z"}`.
   For an empty record `position` is `0` and `record_hash` is `null`, as in section 7.8, and no
   event line and no checkpoint line follows.
2. One line per event in position order:
   `{"type":"event","position":1,"received_at":"…","event_hash":"…","record_hash":"…","event":"<base64url>","signature":"<base64url>"}`
3. One line per checkpoint, ordered by hub key id and position:
   `{"type":"checkpoint","position":21,"record_hash":"…","published_at":"…","signed_by":"hub-1","signature":"<base64url>"}`

Every line ends with one `\n`. `verify` and `restore` refuse a file with `failed <position>
file_invalid` when any of these holds:

- the file is not valid UTF-8, is empty, does not end with `\n`, or holds an empty line
- a line is not exactly one JSON object, or an object has a member twice
- the first line is not a header: an object with exactly the members `format`, `position`,
  `record_hash` and `exported_at`, where `format` is `agent-record-export-v1`, `position` is a
  JSON number `0` or a position, `record_hash` is `null` or 64 lowercase hex characters, and
  `exported_at` is a time
- a later line has a `type` other than `event` or `checkpoint`, a missing or unknown member, or a
  member outside its form: positions as JSON numbers, times as in section 2, hashes as 64
  lowercase hex characters, `event` and `signature` as canonical base64url (`signature` of 64
  bytes), `signed_by` by the `id` rule of KEY-PROTOCOL.md section 3
- an event line follows a checkpoint line; the checkpoint lines are not in strictly ascending
  order of hub key id in byte order and then of position, which also refuses the same pair twice
- the header's `position` and `record_hash` differ from the `position` and `record_hash` of the
  last event line, or are not `0` and `null` when the file has no event line

`<position>` is `0` when the fault is in the whole file (it is not valid UTF-8, is empty or does
not end with `\n`; an empty line is a fault of a line), in the header, or in the header's
agreement with the event lines (the last item of the list); for every other fault it is the
number of event lines before the faulty line plus 1.

The export is the full event list and is verifiable offline with the public keys file and the
public teams file alone (DESIGN.md §24.8).

### 12.4 Backup

The record's data class is unique (DESIGN.md §24.8). The deployment of every environment:

- runs the export at least once every 24 hours
- keeps three copies of the latest export, at least one of them off-site
- encrypts every copy at rest and limits access to it, because an export contains security
  issues; an export never enters a repository, an issue, a pull request or a log
- at least once every 30 days restores the latest export into an empty database, in this order:
  it creates the database and both roles as in section 11.2; runs `migrate` (section 11.4);
  runs `restore` with the keys file, the teams file and the public key of every hub key id in the
  file, which verifies the whole record before it inserts anything (section 12.3); and starts a
  hub against it, which verifies again before it serves (section 11.4)

## 13. Logs

Record data never appears in a log line or an error message (DESIGN.md §24.10). A log line of the
record part of the hub contains only: the fixed words of the lines below, key ids, the hub key
id, the method, the raw path without the query, a status code, an error value or `failure` value
of this document, positions, record hashes, numbers of events, and the step and SQLSTATE of a
database failure. It never contains an event text, a value of a field (such as a title, a
description, a comment body, a short name, a link, a source, a fix or an assignee), a field name,
an object id, an event id, a handle as a field of its own, a connection string, a password, or
the message, detail or hint of a database error. A key id may contain the handle of its holder,
as `example-staff-laptop-1` contains `example-staff`; it is written as a key id, never as a
handle.

**The request line.** Every request under `/api/key/record/` that passed check 8 of
KEY-PROTOCOL.md section 5 writes exactly one log line, whatever the principal of its key, and no
other line of the record part:

```text
record request <key id> <method> <raw path without the query> <status> <result>
```

`<result>` is exactly one of:

| `<result>` | When |
|---|---|
| `appended <position>` | status `201` of an append endpoint; the position of the stored event |
| `ok` | any other status `200` |
| `verify_failed <position> <failure>` | the incremental fold of this request (section 11.6) or this verification on request (section 7.9) failed; status `503`, or `200` for section 7.9 |
| `database_error <step> <SQLSTATE or ->` | a database operation of this request failed; status `500`; `<step>` is one of `connect`, `read`, `append` |
| `<error value>` | every other answer: the value of `error` in its body, such as `scope_missing`, `query_invalid`, `chain_broken`, `verify_busy`, `not_found`, `internal`, and `record_unverified` when the process was already in that state |

For a `service` key this line is the log line of KEY-PROTOCOL.md section 5: it holds, in this
order, the key id, the method, the raw path without the query and the final status that
KEY-PROTOCOL.md requires, and adds `<result>`. No second line is written for such a request.
Change 3 of section 16 updates the sentence of KEY-PROTOCOL.md section 5 and its test (section
1). A request refused at checks 1 to 8 of KEY-PROTOCOL.md section 5 writes no line of the record
part.

**Other lines.** Outside the requests under `/api/key/record/`, the record part writes these
lines and no others:

| Line | When |
|---|---|
| `record verified <position> <record hash or ->` | after the verification at start |
| `record start failed <step>` | a start step of the hub in section 11.4 failed; `<step>` is one of `config`, `schema`, `writer`, `verify` |
| `record checkpoint <position> <record hash>` | a checkpoint was written |
| `record verify failed <position> <failure>` | a verification failed at start, in a periodic run, in a checkpoint attempt, or in the incremental fold of a request to `GET /api/key/release` (section 12.1) |
| `record database error <step> <SQLSTATE or ->` | a database operation failed in a periodic verification (`read`), in a checkpoint attempt (`checkpoint`), or in a request to `GET /api/key/release` (`connect` or `read`) |

A request to `GET /api/key/release` keeps the log line of KEY-PROTOCOL.md section 5 unchanged;
when its incremental fold or a database operation fails, the record part writes in addition
exactly one of the two lines `record verify failed` or `record database error`.

A failure in a record endpoint is never logged with its exception message or a traceback, because
a database exception can carry stored values. The commands `migrate`, `export`, `verify` and
`restore` print only the lines given in sections 11.4 and 12.3, under the same rules: no value,
no connection string, no message of a database error.

## 14. Tests this step must carry

Every test that touches the record runs against a real PostgreSQL 16, never a replacement of it.
The tests read the administrator connection string of a test server from
`AGENT_CORE_TEST_POSTGRES`. Each test session creates a fresh database with a random name and the
owner and writer roles with random passwords, and drops them at the end. In CI, the workflow runs
a `postgres:16` service container and sets the variable; locally, the developer runs a local
PostgreSQL 16. When the variable is missing, the record tests fail with a message that names it;
they are never skipped. Every endpoint case asserts the exact status, the exact body bytes and the
hub signature, with a fixed clock.

The database and the start:

- the writer role gets SQLSTATE `42501` for `UPDATE`, `DELETE` and `TRUNCATE` on `record.event`
  and on `record.checkpoint`
- the owner role gets SQLSTATE `P0001` from the trigger for `UPDATE`, `DELETE` and `TRUNCATE` on
  both tables
- an `INSERT` of a second row with the same `actor_key_id` and `prev_hash` is refused by the
  unique constraint, also for `prev_hash` `-`
- an `INSERT` with a `received_at` or a `published_at` that has a fraction of a second is refused
  with SQLSTATE `23514`, also in a session whose time zone is not UTC
- the `migrate` command: on an empty database it creates exactly the schema and rights of section
  11.3 and prints `migrated 1`; a second run changes nothing and prints `unchanged 1`; two runs at
  once create the schema once; each of its failures prints `failed <step>` and exits 1: a
  variable missing or empty, each malformed connection string of section 15, the same role in
  both, different hosts, ports, databases or `sslmode`, a reported libpq version below 16
  (`config`), an owner that is a superuser, an owner that does not own the database, and an
  unknown schema version
- each start failure of the hub in section 11.4 logs `record start failed <step>` and the hub does
  not start: `AGENT_CORE_RECORD_DATABASE` missing, empty or malformed (`config`);
  `AGENT_CORE_RECORD_OWNER_DATABASE` set in the hub's environment, also empty (`config`); a
  reported libpq version below 16 (`config`); a database on which `migrate` never ran, a schema
  with an unknown version and one without a version row (`schema`); a writer with each of these,
  one at a time (`writer`): `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` or `TRIGGER` on either
  table, `INSERT` on `record.schema_version`, a right on one column of `record.event`, a right of
  section 11.2 held with grant option, `SELECT` on a table and `USAGE` on a sequence in the schema
  `public`, a default privilege that names the writer, `CONNECT` on another database of the
  server, `CREATE` or `TEMPORARY` on the database (also through `PUBLIC`), `CREATE` on the schema
  `record` or on the schema `public`, `EXECUTE` on `record.refuse_change()` (also through
  `PUBLIC`), ownership of a table, ownership of a table in another database of the server,
  membership in the owner role, membership in each of the predefined roles
  `pg_execute_server_program`, `pg_write_server_files`, `pg_write_all_data` and
  `pg_read_all_data`, membership in a role that holds no right at all, `CREATEDB`, `CREATEROLE`,
  `BYPASSRLS`, `REPLICATION`, superuser; a disabled trigger (`writer`); and a stored record that
  fails verification (`verify`)
- the hub never connects with the owner role: a start, every endpoint and a checkpoint against a
  test server that logs connections show only the writer role
- the configuration test reads the compose file: the hub service's environment does not contain
  `AGENT_CORE_RECORD_OWNER_DATABASE`, the hub service depends on the migration service with the
  condition `service_completed_successfully`, and both connection strings that the compose file
  gives use `sslmode=require` or `sslmode=verify-full`
- `sslmode` missing (refused); `disable` with `localhost`, `127.0.0.1`, `127.1.2.3` and `[::1]`
  (accepted); `disable` with a host name without a dot (such as `postgres`), with a host name
  that contains a dot, with a name that only starts with `localhost` (such as `localhost2`), and
  with any other IP address (refused); `require` and `verify-full` with any host (accepted); for
  `verify-full` the program passes `sslrootcert=system` to the driver, also when `PGSSLROOTCERT`
  is set; a connection string with `sslrootcert` or any other query parameter besides `sslmode`
  (refused)
- the start log line, and no password or connection string in any log line or printed line

The event:

- an event text that is accepted, and its event hash and record hash compared with fixed vectors
  that the test file carries and with an independent recomputation in the test
- `event_invalid` for: another label, a `\r`, a trailing line feed, an empty line, a byte outside
  0x21 to 0x7E, `n` of 0, 16 and with a leading zero, a wrong number of lines, fields out of
  order, a field twice, `old` equal to `new`, `=` without value, padded and non-canonical
  base64url, an unknown `object_type`, a malformed UUID in `event_id` and in `object_id`, the
  nil UUID, an `actor_key_id` outside the `id` rule, a malformed `signed_at`, a malformed
  `prev_hash`, and a `key_certificate` that is not a value token
- an event text of exactly 65536 bytes, correctly signed and chained, with an oversized value:
  it passes checks 2 and 3 of section 6.7 and is then refused with `value_invalid`; an event text
  of 65537 bytes is refused with `body_invalid`
- a commit reference in `fix` with 40 and with 64 lowercase hex characters (accepted), and with
  39, 41, 63, 65 or uppercase hex characters (`value_invalid`)
- `value_invalid` for every field form of section 5 at each of its limits, including a title of
  200 and 201 bytes, an invalid UTF-8 sequence, a control character, a `links` value of 20 and 21
  references, out of order or repeated, `urgency` 0, 11 and `07`, and a `short_name` of 31
  characters
- the largest valid event of section 4.2 (an update of a security issue, every field at its
  limit, `actor_key_id` of 63 characters) is accepted and is at most 54461 bytes
- `team`: a task create by a key whose `team` has 100 bytes (accepted); a task create whose
  `team` has 101 bytes gets `change_not_allowed` at check 12 (c), because no key can hold such a
  team, and the same event in the fold directly fails with `event_invalid`
- `field_not_allowed` for a field name that no object type has (such as `priority`), on a task
  and on a comment
- `body_invalid` for a body that is not JSON, has an extra or missing member, a member twice, or
  a signature of 63 bytes
- `actor_mismatch`, `certificate_not_supported`, and `event_signature_invalid` for a signature by
  another key and over another text

The chain:

- a wrong `prev_hash`, a second first event of a key, and a fork (two events with the same
  `prev_hash`; the second is refused with `chain_broken`, also when both are sent in parallel)
- the same event sent twice (`event_duplicate`), and another event with a used `event_id`
  (`event_id_taken`)
- `signed_at` 120 seconds after now (accepted), 121 seconds (refused), and earlier than the
  previous event of the key (refused)
- an event of a revoked key and of an expired `service` key (refused at the request checks,
  unsigned); an event signed before the key's `revoked` time and sent after it (refused, and not
  in the record); check 11 of section 6.7 with `received_at` later than now and the key's
  `revoked` time between now and `received_at` (`409` `key_not_valid_at_receipt`, signed)
- after a refusal, the next event chained to the last accepted event is accepted
- two parallel appends of two keys get consecutive positions and both are accepted
- two hub instances on one database: each folds the other's events before it answers
- the incremental fold checks every row of section 12.1: after the state of instance A, a row is
  inserted as database superuser, once with a gap in the positions, once with a `received_at`
  earlier than at the previous position, once with a `signed_at` more than 120 seconds after its
  `received_at`, and once with an `event_id` column that differs from its event text; each time
  A's next checkpoint attempt writes nothing, A's next `GET /api/key/record/head` answers the
  signed `503`, and A logs the failure (`position_gap`, `event_time_invalid`, `event_time_invalid`,
  `row_mismatch`) with its position
- an append refused at checks 1 to 6 of section 6.7 runs no fold: with a broken row after A's
  state, an append with an invalid event text gets `event_invalid` and A stays out of the state
  `record_unverified`; a valid append then gets the signed `503` after check 6
- two hub instances with different keys files: B, without a key that A's file adds, gets
  `key_unknown` in its incremental fold at the first event of that key, answers `503` and logs it;
  restarted with A's keys file, B verifies and answers again

The reducer, every rule through the endpoint and the fold directly:

- each row of section 6.4 accepted, and for each status every other target refused with
  `transition_invalid`; each row's "Who" refused for every other actor with
  `change_not_allowed`
- the defaults of `status` and `completion` per kind; a create with `status` `merged`, and one
  with `status` `done` (both `transition_invalid`)
- row 10 from `open` (`transition_invalid`), and the same task set by its assignee to
  `in_progress` and then to `done` (both accepted)
- a security issue started by an event that sets the actor as assignee (accepted), that sets
  another handle (refused), and taken over by another handle (refused); handed over by its
  assignee (accepted)
- actor rule 4 case (c): when every `agent` key of the assignee's handle in the task's team is
  revoked at `received_at`, a writer of the same team with `record:security` sets `assignee` to
  its own handle (accepted) and to a third handle (`change_not_allowed`); while the assignee
  keeps one `agent` key that is not revoked, also one whose `revoked` time lies after
  `received_at`, the takeover is refused with `change_not_allowed`; an assignee whose only
  remaining valid entry in the team is a `staff` entry is orphaned, and the takeover is accepted;
  an assignee whose only valid `agent` key lacks `record:security`, and one whose only valid
  `agent` key lacks `record:write`, is orphaned, and the takeover is accepted; an assignee whose
  `agent` keys are all expired at `received_at` is orphaned; a writer without `record:security`
  gets `object_not_found`
- actor rule 5 for kind `security`: the assignee hands the issue over to a handle that can act on
  it (accepted), and to a handle whose only valid `agent` key lacks `record:security`, one whose
  only valid `agent` key lacks `record:write`, one with only a `staff` entry, and one whose
  `agent` keys are all expired (each `assignee_invalid`)
- check 12 (b) of section 6.7: a task create without `kind` and a comment create without `task`
  get `409` `field_required`; through the watch endpoint, an event for an object that does not
  exist gets `object_not_found` first
- check 12 (c) of section 6.7 (actor rule 1): a task create whose `team` is another team gets
  `403` `change_not_allowed`; a security-task create by a key with `record:security` whose `team`
  is another team and whose `short_name` that team already uses gets `change_not_allowed`, not
  `short_name_taken`; a task create without `team` gets `field_required`
- check 12 (d) of section 6.7: a comment create whose `task` value is not a UUID gets
  `object_not_found`, not `value_invalid`; a create whose `object_id` is that of an existing
  object the actor does not see gets `object_not_found`
- row 13 with `done` in the same event (accepted) and without it (refused); row 12 on a task
  without links; a rule change on a `done` task keeps `done`
- row 14 sets `completed_links`; row 10 with links equal to `completed_links` (accepted) and
  after a change of links (refused); rows 11, 12 and 13 leave `completed_links` unchanged, so a
  task that never went through row 14 answers `"completed_links":[]` after each of them
- `links_frozen` in `merged` and `done`; `field_required` for each required field, for
  `in_progress` without assignee, and for a security issue set to `done` without `fix`
- `field_not_allowed` for each security field on kind `work`; `field_immutable` for `kind`,
  `team` and every comment field; `state_mismatch` for a stale `old` value, for a create with
  an `old` value, and for an update whose `object_type` differs from that of the stored object
  (a `comment` event for a task id and a `task` event for a comment id)
- `short_name_taken` in the same team, and the same short name accepted in another team
- `assignee_invalid` for an unknown handle, a handle of another team, a handle whose keys are all
  revoked, a handle whose keys are all expired at `received_at`, and a `service` handle; on a
  task of kind `work`, a handle with only a valid `staff` entry is accepted
- `verification` written by an `agent` key (refused with `change_not_allowed`), and row 11
  applied by the fold directly, which checks no actor (section 6.1)
- `verification` set outside row 11, and removed (`new` `-`) on a task that holds a value, both
  through the endpoint and in the fold directly (`transition_invalid`); row 11 with `new` `-`
  for `verification`, in the fold directly (`transition_invalid`)
- a comment on a task the actor does not see (`object_not_found`) and an event on an existing
  comment (`field_immutable`)
- the fold of an exported record gives the same state as the hub

Visibility:

- a key of another team gets `object_not_found` for a task, a comment and the events of an
  object; the task is missing from every list; its events are withheld
- a key without `record:security` the same for a security issue, and an empty list for
  `kind=security`; a create of a security issue gets `scope_missing`
- a `service` key sees a task only with a link or repository in its `subjects`; a link change
  that makes it visible shows all events of the task
- a withheld entry has exactly the members of section 7.4 and `null` text and signature, and the
  record hash chain checks over a page of withheld and full entries
- the loader refuses `record:write`, `record:watch` and `record:security` without `record:read`,
  `record:watch` on an `agent` or `staff` entry, `record:write` on a `service` entry, and an
  entry whose `team` has 101 bytes (also when that team is in the teams file); it loads an entry
  whose `team` has 100 bytes

The watcher:

- rows 8 and 9 accepted; `fix` set to one of the links (accepted) and to another reference
  (refused); any other field (refused)
- a link outside its `subjects` (`subject_not_allowed`, not `change_not_allowed`); a task that
  does not exist and the id of an existing comment (both `object_not_found`)
- an event without a change of `status` (`change_not_allowed`)
- a writer key on `/watch` and a watcher key on `/events` get `scope_missing`
- two watcher keys of one handle on one `device` for two teams: the keys file loads with
  distinct `program` labels and does not load without them

Paging and queries:

- for each list: `limit` 1, the maximum, the maximum plus one and 0 (`query_invalid`); a page
  with exactly `limit` remaining items has a `null` cursor, one more item gives a cursor; the
  cursor at the head gives an empty page; the order is as specified
- `after` with a leading zero, negative and above the maximum; an unknown, repeated and empty
  parameter; a query on an endpoint without parameters (all `query_invalid`)
- the filters of section 7.6, each alone and against tasks that do and do not match: `kind`
  `work` and `security` (with `record:security`), each `status` value, `assignee` with a handle
  and with `-`, `repository` matching a link of a task of kind `work` and matching the
  `repository` of a task of kind `security`; `kind`, `status` and `assignee` combined, where only
  tasks matching all three are listed; an invalid value of each filter (`query_invalid`)
- the checkpoint list with two hub key ids that both hold checkpoints at the same positions,
  paged with `limit` 1: every checkpoint appears exactly once, in the order of section 7.10;
  `before` without `before_key` and `before_key` without `before` (`query_invalid`)
- the example answers of sections 7.4, 7.5, 7.8, 7.9 and 7.10 reproduced byte for byte, apart
  from hashes and signatures, from the story of section 4.2
- under `/api/key/record/`: another method on each path, each path with a trailing slash, `HEAD`
  on each path and an unknown path get the signed `404` `not_found`

The release rule, through `GET /api/key/release`:

- no linked task (`task_missing`); a linked task in `open`, `merged`, `done`, and without
  assignee (`task_not_ready`); in `in_progress` or `ready_for_review` with assignee (released)
- two linked tasks, one ready and one not (`task_not_ready`); a `dropped` task alone
  (`task_missing`); a security issue as the linked task; a task of another team as the linked
  task; a link removed again (`task_missing`)
- `503` `record_unverified` after a failed verification

The order of the `503` (section 7.1), with the hub in the state `record_unverified`, for every
record endpoint and for `GET /api/key/release`:

- a key without the endpoint's scope gets `scope_missing`, and an invalid query gets
  `query_invalid`, not `503`
- on the two append endpoints, an invalid body gets `body_invalid`; a valid body with an invalid
  event text gets `503`
- on `GET /api/key/release`, a `service` key outside its `subjects` gets `subject_not_allowed`
- a valid request gets the signed `503`, and the test asserts that the endpoint read no row of the
  record (a test hook counts the reads)
- on `GET /api/key/record/verify`, `503` comes before `429`

Integrity and backup:

- `GET /api/key/record/verify` on a sound record and on an empty record (`0` and `null`); then each
  failure of section 12.1 produced by changing a row as database superuser with the triggers
  bypassed, each giving its `failure` and afterwards `503` on every record endpoint; a restart
  refuses to start
- verification on request: a second request while one runs and one 59 seconds after the start of
  the previous one get the signed `429` `verify_busy`; a request 60 seconds after it runs; the
  verification at start and a periodic run do not count; with a fixed monotonic clock, setting
  the wall clock back does not extend the 60 seconds
- the position of a failure (section 12.1): `position_gap` gives the first missing position,
  `checkpoint_invalid` the position the checkpoint names
- state per process and periodic verification, with two hub instances on one database and a
  fixed monotonic clock: a row changed as database superuser with the triggers bypassed; the
  verification on request of instance A fails and A answers the signed `503` on every record
  endpoint and on the release query; instance B keeps answering until its next periodic run,
  which starts 10 minutes after its previous run started, fails, logs `record verify failed`,
  and from then on B answers `503` too; a periodic run that lasts longer than 10 minutes is
  followed at once by the next one; in both instances `GET /api/key/whoami`, the session store,
  pairing and sync keep answering as before; a database failure in a periodic run logs
  `record database error read` and does not enter the state
- one snapshot (section 12.1): through a test-only pause point between the read of the events and
  the read of the checkpoints, a second connection appends an event and writes a checkpoint of the
  new head and commits; the verification on request, the verification at start and `export` all
  succeed at the earlier head, the export header matches its last event line, and offline
  `verify` and `restore` accept that export
- checkpoints: one at a full hour; none for an empty record; none when the head did not move; an
  attempt that starts 30 minutes after the full hour writes the checkpoint with that later time as
  `published_at`; after a pause across three full hours exactly one attempt; two instances in the
  same hour, also when the head moves between their attempts, write exactly one checkpoint; its
  signature verified with the hub public key
- checkpoints sign only a checked head: between two attempts of instance A, instance B appends an
  event and that row is then changed as database superuser with the triggers bypassed; A's next
  attempt writes no checkpoint, logs `record verify failed`, and A answers `503`; the newest
  checkpoint of A's hub key id changed as database superuser (its record hash, and in a second
  run its signature): A's next attempt writes no checkpoint, logs `record verify failed` with
  `checkpoint_invalid`, and A answers `503`; an instance in the state `record_unverified` writes
  no checkpoint at the next full hour, also when the head moved
- `export`: of an empty record (header `0` and `null`, no further line, prints `exported 0 -`);
  of the story of section 4.2 (prints `exported 21 <record hash>`); `refused config` for `--out`
  missing, `--out` given twice, an unknown argument, `AGENT_CORE_RECORD_DATABASE` missing, empty
  and malformed, and a reported libpq version below 16; `refused file_exists` for an existing
  file, which stays unchanged; `refused file_write` for a directory that cannot be written;
  `refused database` for an unreachable database and for a database without the schema, with no
  file left behind; each asserts the printed line and exit 1
- offline `verify` of an export, with `verified <position> <record hash>` and exit 0; `refused
  config` for `--keys`, `--teams` or `--in` missing, each of them given twice, an unknown
  argument, a keys file that does not load, a teams file that does not load, a keys file whose
  `team` is not in the given teams file, an unreadable `FILE`, and a malformed or repeated
  `--hub-key`; each asserts the printed line and exit 1
- `failed <position> file_invalid` from `verify` and from `restore`, with the position of section
  12.3, for: a file that is not UTF-8 (`0`), an empty file (`0`), a missing final `\n` (`0`), an
  empty line, a line that is not JSON, a member twice, a header with an unknown `format` (`0`),
  an unknown `type`, a missing and an unknown member, an event line after a checkpoint line,
  checkpoint lines out of order and the same checkpoint twice, a header whose `position` or
  `record_hash` differs from the last event line (`0`), and a header that is not `0` and `null`
  without event lines (`0`)
- `restore` after `migrate` into an empty database, and a start against it; then each refusal of
  section 12.3, each asserting the printed line, exit 1, and that `record.event` and
  `record.checkpoint` stay empty and no schema is created: a database on which `migrate` never
  ran (the `schema_missing` line); a schema with an unknown version (`refused schema_version`); a
  writer with a right beyond section 11.2 (`refused writer_rights`); a non-empty database
  (`refused not_empty`, and its rows stay unchanged); an unreachable database (`refused
  database`); a changed file (`failed <position> <failure>`); a checkpoint whose hub key id is not
  given (`refused hub_key_missing`), also when the file fails the rest of the verification, which
  shows that step 1 (b) comes before step 1 (c); and `refused config` for `--in` missing, `--in`
  given twice, an unknown argument, a malformed or repeated `--hub-key`, `AGENT_CORE_KEYS`,
  `AGENT_CORE_TEAMS` or `AGENT_CORE_RECORD_DATABASE` missing, empty or malformed, a keys file or
  teams file that does not load, an unreadable `FILE`, and a reported libpq version below 16

Logs:

- every request under `/api/key/record/` that passed check 8 of KEY-PROTOCOL.md section 5 writes
  exactly one line, the request line of section 13 with its exact form, for an `agent` key and
  for a `service` key; for a `service` key no second line is written; each `<result>` of section
  13 is produced once: `appended`, `ok`, an error value, `record_unverified`, `verify_failed`
  from a failing fold and from a failing verification on request, and `database_error`; a
  request refused at checks 1 to 8 writes no line of the record part
- `GET /api/key/release` writes the unchanged line of KEY-PROTOCOL.md section 5, and in addition
  exactly one `record verify failed` line when its fold fails and exactly one `record database
  error` line when the database fails
- after a run that uses every endpoint with marker strings in every text value (title,
  description, comment body, short name, source, verification, and the repository names of links
  and `fix`), no log line contains a marker, an object id, an event id, a password or a
  connection string, and no field of a line other than a key id holds a handle; the key ids of
  the run follow the convention of KEY-PROTOCOL.md section 3 and contain their handles
- a database failure in an endpoint writes only the request line with `database_error` and
  answers `500` `internal`

## 15. Dependencies and configuration

- New dependency: `psycopg` version 3 with its binary package (`psycopg[binary]>=3.2`). The
  libpq that `psycopg` uses is version 16 or later, because `sslrootcert=system` needs it. Every
  program of this document that connects to the database (the hub, `migrate`, `export` and
  `restore`) checks the version that `psycopg` reports before it connects, as part of its
  configuration check (sections 11.4 and 12.3).
- New environment variables, and which program reads which:

| Variable | Form | Read by |
|---|---|---|
| `AGENT_CORE_RECORD_DATABASE` | Connection string of the writer role | the hub (required), `migrate` (required, only for the role name and the comparison below), `export`, `restore` |
| `AGENT_CORE_RECORD_OWNER_DATABASE` | Connection string of the owner role | only `migrate` (required). The hub refuses to start when it is set in its environment, also when empty (section 11.4). |

A connection string is
`postgresql://<role>:<password>@<host>[:<port>]/<database>?sslmode=<mode>`:

- `role` and `database`: 1 to 63 characters of `a-z`, `0-9` and `_`, starting with a letter or `_`
- `password`: 64 lowercase hex characters, created by the deployment's secret tooling; no person
  creates, sees or copies it
- `host`: a lowercase host name in ASCII form, an IPv4 address, or an IPv6 address in brackets
- `port`: decimal 1 to 65535 without leading zero; without it, 5432
- `mode`: `disable`, `require` or `verify-full`. The parameter `sslmode` is required, and no other
  query parameter is allowed. `disable` is allowed only when `host` is a loopback address (an
  IPv4 address in `127.0.0.0/8` or `[::1]`) or exactly the name `localhost`. With any other host,
  also a host name without a dot such as the name of a compose service, `disable` is refused by
  the configuration check of every program that reads the string (sections 11.4 and 12.3), and
  `require` or `verify-full` is needed. A name other than `localhost` can resolve to a remote
  host, for example through a search domain of the resolver, so only these hosts are accepted
  without encryption. With `verify-full`, the server certificate is checked against the system
  trust store, that is the certificate authorities that the operating system of the program's
  image trusts: the program passes `sslrootcert=system` to the driver itself, which takes
  precedence over the variable `PGSSLROOTCERT`. A private certificate authority is not supported in this step: a server whose
  certificate does not chain to the system trust store fails to connect with `verify-full`.

`migrate` checks that the two strings name different roles and the same host, port, database and
`sslmode`. Like every `AGENT_CORE_*` variable, a missing or empty value stops a program that
requires it; so does any value outside this form. Both values are secrets: they never appear in a
log, a printed line, an error message or an answer.

Change 1 of section 16 updates `pyproject.toml` and the test workflow (a `postgres:16` service
container and `AGENT_CORE_TEST_POSTGRES`), and the README names PostgreSQL 16 as a requirement for
running the tests. Change 3 updates the list of required variables in the README, the compose
file of this repository and the configuration test. After change 3 the compose file holds:

- a `postgres:16` service with a persistent volume and an initialisation step that creates the
  owner and writer roles from passwords given as secrets, the writer with `LOGIN`, none of the
  other attributes of section 11.2, no membership and no right, and the database owned by the
  owner role (section 11.2); the service serves TLS, because the other services reach it by its service name, which
  `disable` does not allow, and both connection strings use `sslmode=require` or
  `sslmode=verify-full`
- a one-shot migration service that runs `python -m agent_core.record migrate` with
  `AGENT_CORE_RECORD_OWNER_DATABASE` and `AGENT_CORE_RECORD_DATABASE`
- the hub service, which depends on the migration service with the condition
  `service_completed_successfully` and whose environment does not contain
  `AGENT_CORE_RECORD_OWNER_DATABASE`

The deployment of each environment provides the same three pieces in change 2 (section 16).

## 16. Rollout order

After change 3 below, the hub does not start without its new variable, a migrated schema and a
reachable database, and a hub is deployed automatically after a merge. The implementation is
therefore split into three changes:

1. **Change 1, this repository: the migration and the library.** It adds the command
   `python -m agent_core.record migrate` (section 11.4), the schema of section 11.3, and the
   record library: the event text, the event hash and the record hash (section 4), and the form
   of a connection string (section 15). It adds the dependency and the test workflow of section
   15, and carries the tests of section 14 for `migrate`, the rights, triggers and constraints of
   the database, the connection string, and the event text and its hashes. The hub process does
   not use the database yet, reads no new variable, and starts and answers exactly as before.
2. **Change 2, the deployment of each environment.** It provides a PostgreSQL 16 database with
   persistent storage, which serves TLS whenever the hub reaches it by a host other than a
   loopback address or `localhost` (section 15); creates the owner and writer roles with
   passwords from its secret tooling, the writer with `LOGIN`, none of the other attributes of
   section 11.2, no membership and no right, and the database owned by the owner role (section
   11.2); declares `AGENT_CORE_RECORD_DATABASE`
   for the hub and the migration service and `AGENT_CORE_RECORD_OWNER_DATABASE` for the migration
   service only, never for the hub (section 15); and adds the one-shot migration service, which
   runs `python -m agent_core.record migrate` of change 1 to completion before the hub starts. The
   hub of change 1 ignores `AGENT_CORE_RECORD_DATABASE`, because a variable the running version
   does not know is harmless.
3. **Change 3, this repository: the record in the hub.** It makes the hub require
   `AGENT_CORE_RECORD_DATABASE` and run the start of section 11.4, and adds the scopes of section
   3, the reducer, the endpoints of section 7, the release rule of section 9, checkpoints, the
   periodic verification, `export`, `verify` and `restore`, the updates of the compose file, the
   configuration test and the README of section 15, the updates of KEY-PROTOCOL.md named in
   section 1, and every remaining test of section 14.

Change 2 is rolled out in an environment only after change 1 is deployed there, because its
migration service runs the command of change 1. Change 3 is merged to a branch only after change 2
is rolled out in every environment that deploys from that branch, and its body states this. The
same holds for every later merge that brings change 3 to another deploying branch: a release pull
request to the production branch is merged only after change 2 is rolled out for production, and
its body states this.

DESIGN.md §24 requires every pull request that implements a part of it to name the subsection it
implements and to update the rows of DESIGN.md §2 and §3 that it supersedes. The pull request of
each of the three changes therefore names in its body the subsections of DESIGN.md §24 it
implements. DESIGN.md lives in the client repository, so a change that supersedes a row of
DESIGN.md §2 or §3 has a companion pull request there that updates exactly those rows; a change
that supersedes none states this in its body. The companion pull request is opened together with
its change and merged only after its change is merged.

Each environment then goes through these steps in this order:

1. Change 1 is deployed. Nothing visible changes.
2. Change 2 is rolled out. An operator reads the line `migrated 1` of the migration service on
   its first run and `unchanged 1` on every later run. A `failed <step>` line means change 2 is
   incomplete there, and change 3 must not reach that environment.
3. Change 3 is deployed. An operator reads `unchanged 1` of the migration service and the hub's
   start line `record verified 0 -`. A `record start failed` line of the hub means change 2 is
   incomplete.
4. The deployment enables the daily export and its three copies (section 12.4) and runs the first
   restore test.
5. Keys file pull requests give writers `record:read` and `record:write`, and a watcher its key
   (section 8). Until then nobody writes, and the release query answers `task_missing` for every
   pull request it is asked about.

From step 3 until step 5 is done in an environment, nobody can link a task there, so the release
query answers `task_missing` for every pull request without a linked task, which then is every
pull request. A guard must not rely on the release query before step 5 is done for its
environment and the team has linked its open pull requests to tasks; until then the answer for a
pull request without a linked task is `task_missing`, and it says nothing about that pull
request.

## 17. Decisions taken in this document

Each line is a decision where DESIGN.md §24 or the task left a detail open. The human owner
confirms or changes each one.

1. Tasks are built in this step together with security issues, and a security issue is a task of
   kind `security`, not its own object type (§1, §5.1).
2. Task kinds are exactly `work` and `security` (§5.2).
3. Status values are `open`, `in_progress`, `ready_for_review`, `merged`, `done`, `dropped`;
   `open` and `dropped` are added to the statuses DESIGN.md names, and spaces become `_` (§5.2).
4. Comments are their own object type, written once and never changed (§5.1, §5.3).
5. Every event value is text in a base64url value token; lists and numbers are canonical text
   (§4.1, §5.2).
6. The event hash covers the event text only, not the signature (§4.4).
7. The record hash covers position, `received_at`, event hash and the previous record hash, not
   the signature (§4.6).
8. `received_at` is the later of now and the previous `received_at`, so it never decreases (§4.6).
9. `signed_at` is at most 120 seconds in the future and never earlier than the key's previous
   event; there is no lower bound otherwise, so writers can sign offline (§4.5, §6.7).
10. The task's `team` is an explicit, immutable field of the create event and equals the actor's
    team, so the fold does not depend on later edits of the keys file (§5.2).
11. Actor rules are checked only at write time; the fold rechecks only state rules (§6.1).
12. New scope `record:security` restricts reading and writing security issues; a key sees only
    the objects of its own team; a `service` key sees only tasks touching its `subjects` (§3).
    The three exceptions are decisions 40, 41 and 59.
13. New scope `record:watch` for the watcher, because a `service` key cannot hold `record:write`;
    watchers write through their own endpoint `POST /api/key/record/watch` (§3, §7.3, §8).
14. `record:write`, `record:watch` and `record:security` require `record:read` in the keys file
    (§3).
15. A security issue's assignee is taken only by setting the actor's own handle, and handed over
    or given up only by the current assignee, except by decision 39 (§6.5).
16. The assignee of a task of kind `work` is the handle of a `staff` or `agent` entry of the
    task's team that is neither revoked nor expired at `received_at`. The assignee of a task of
    kind `security` is the handle of at least one entry that can act on the task: an `agent`
    entry of the task's team, neither revoked nor expired at `received_at`, that holds
    `record:read`, `record:write` and `record:security`; handing a security issue to any other
    handle is refused with `assignee_invalid` (§6.5, rule 5).
17. Links are frozen while a task is `merged` or `done`, so that the hub can apply DESIGN.md
    §24.11's rule change to `merged` without asking the code host (§6.4, §6.6).
18. "The condition is already true and no further pull request is linked" is decided by `links`
    being equal to `completed_links`, the value of `links` when row 14 last set the task back
    from `merged` or `done`; rows 11, 12 and 13 do not set it (§5.2, §6.4, row 10).
19. The confirmation that sets `done` under `verified` needs principal `staff`; an agent key is
    not a staff member's confirmation. Until session keys exist, such tasks stop at `merged`
    (§6.4, row 11; §10).
20. A security issue in `done` needs `fix`; the watcher may set `fix` to one of the links (§6.6,
    §8).
21. `short_name` is unique per team, not across teams, and has the form of a handle of at most 30
    characters (§5.2).
22. `dropped` tasks do not count as linked tasks in the release rule (§9).
23. The state is kept in memory and rebuilt at start; no materialised table exists, so nothing in
    the database is ever updated (§11.3, §11.6).
24. Appends, checkpoint attempts and the insert of `restore` are serialised by the advisory lock
    742180001; runs of `migrate` by the advisory lock 742180002 (§11.4, §11.5, §12.2, §12.3).
25. Checkpoints are written at the first moment at or after each full hour when the head moved
    since the last checkpoint of the hub key id; `published_at` is the hub's clock at that moment;
    under the lock, at most one checkpoint per hub key id and full hour and per hub key id and
    head. They are stored in their own insert-only table and listed by an endpoint (§12.2,
    §7.10).
26. A failed verification puts the hub process that ran it into `503 record_unverified` for the
    record endpoints and the release query until restart; the state belongs to one process, and
    the rest of the hub in that process (session store, pairing, sync, whoami) keeps working
    (§7.9, §9, §12.1).
27. New endpoints refuse any query parameter they do not define, instead of ignoring the query
    (§7.1).
28. Page sizes: events 1 to 100 (default 100); tasks, comments and checkpoints 1 to 200 (default
    50) (§7).
29. Text limits: title 200, description and comment body 16384, verification 2000, source 200,
    team 100 bytes; event text 65536 bytes; at most 20 links. The keys-file loader refuses an
    entry whose `team` is longer than 100 bytes, from change 3 on (KEY-PROTOCOL.md section 3).
    With these limits the largest valid event is at most 54461 bytes, so no valid event is
    refused by its size. Of the two alternatives of the reviews (exclude `team` from the size
    statement, or give it a limit), the stricter is taken (§3, §4.2, §5).
30. PostgreSQL 16 or later, `psycopg` 3 with libpq 16 or later; passwords are 64 lowercase hex
    characters; `sslmode` is required and one of `disable`, `require`, `verify-full`; `disable`
    only for a loopback address (`127.0.0.0/8` or `[::1]`) or exactly the name `localhost`,
    otherwise refused by the configuration check; a host name without a dot needs `require` or
    `verify-full`, so the compose file's database serves TLS. Of the two alternatives of the
    reviews (rely on the deployment for dotless names, or refuse them), the stricter is taken.
    `verify-full` checks against the system trust store (`sslrootcert=system`); a private
    certificate authority is not supported in this step (§11.1, §15).
31. Backup cadence: export every 24 hours, restore test every 30 days (§12.4).
32. The record tests fail, and are not skipped, when no test PostgreSQL is configured (§14).
33. Session-key ids are disjoint from keys file ids, and a session key has its own chain (§10).
34. The hub process holds only the writer role's connection string. The schema is applied by the
    separate command `python -m agent_core.record migrate`, the only program that reads
    `AGENT_CORE_RECORD_OWNER_DATABASE`; the deployment runs it to completion before the hub
    starts (in the compose file a one-shot service the hub depends on with
    `service_completed_successfully`). The hub refuses to start when the owner variable is set in
    its environment (§11.2, §11.4, §15).
35. At start the hub checks that the schema has exactly the version it expects and that its role
    has exactly the writer rights; a wrong version or any wider right stops the start with
    `record start failed schema` or `record start failed writer` (§11.4). The writer role is
    refused when it is a superuser, has `BYPASSRLS`, `CREATEROLE`, `CREATEDB` or `REPLICATION`, is
    a member of any role, directly or indirectly, including the predefined roles of PostgreSQL,
    owns any object, or is named in any privilege beyond the five objects and rights of §11.2;
    the deployment creates it with `LOGIN`, none of the other attributes, no membership and no
    right, and the migration grants its rights and nothing else. Membership is checked in `pg_auth_members`, not through `has_table_privilege`
    (§11.2, §11.4, §15, §16).
36. The database is owned by the owner role, so that it can create the schema; the migration
    revokes every right of `PUBLIC` on the database and on `record.refuse_change()`, and grants
    the writer `SELECT` on `record.schema_version` (§11.2, §11.3).
37. `restore` never migrates: it refuses a database without the schema with a message that names
    `migrate`, then checks the writer rights, refuses a non-empty database, and inserts with the
    writer role. It refuses a file with a checkpoint of a hub key id whose public key is not
    given (§12.3, §12.4).
38. Verification at start, periodic verification, verification on request and `export` read
    events, checkpoints and the export header in one read-only `REPEATABLE READ` transaction
    (§12.1).
39. When the assignee of a security issue is orphaned, that is, it has no entry left that can
    act on the task at `received_at` (the condition of decision 16), any writer of the same team
    that holds `record:security` may set `assignee` to its own handle; otherwise
    `change_not_allowed`. "Orphaned" and rule 5 use the same condition, so an assignee whose
    entries are revoked or expired, whose only valid entry is a passkey, or whose `agent` keys
    lack `record:write` or `record:security`, is orphaned; a later step adds the session keys of
    staff members (§6.5, rule 4 case (c); §10).
40. Withheld entries give every `record:read` key the position, `received_at` and hashes of
    events it may not read, and the record head of sections 7.4, 7.8 and 7.9 and the checkpoints
    of section 7.10 give it the position and record hash of the head of the whole record; they
    reveal that such events exist, how many and when. This goes beyond "team scope follows §6" of
    DESIGN.md §24.7 and is kept so that every reader can check the record hash chain (§3, §7.4,
    §7.10).
41. The release rule reads the tasks of every team and of both kinds; its yes/no can therefore
    reveal to the asking key that a linked task it does not see exists, such as a security issue
    or a task of another team (§3, §9).
42. In a process already in the state `record_unverified`, the signed `503` comes after checks 1
    to 10 of KEY-PROTOCOL.md section 5 and the query and body validation of the endpoint, and
    before any read of the record, on every record endpoint and on the release query. When the
    incremental fold of a request fails, the `503` comes where that fold runs: for an append
    after check 6 of §6.7 (§7.1, §6.7, §9, §11.6).
43. An event whose key is valid now but not at its later `received_at` is refused with its own
    value `409` `key_not_valid_at_receipt`; `key_revoked` and `key_expired` keep `401` of
    KEY-PROTOCOL.md and are used only by its request checks (§6.7, check 11).
44. A task create without `kind` and a comment create without `task` are refused with
    `field_required` at check 12 of §6.7, before the checks that need the kind or the task.
45. A commit reference has a commit id of 40 or 64 lowercase hex characters, so SHA-256
    repositories are covered (§2).
46. Verification on request runs at most once per 60 seconds per hub process, measured on a
    monotonic clock; a request inside that time or during a run gets a signed `429`
    `verify_busy` with no further detail (§7.9).
47. The checkpoint list is ordered and paged by the pair of position and hub key id, with the
    cursor parameters `before` and `before_key` (§7.10).
48. A guard must not rely on the release query in an environment before step 5 of the rollout is
    done there; until then a pull request without a linked task gets `task_missing` (§16).
49. `severity` has exactly the values `low`, `medium`, `high`, `critical`; DESIGN.md §24.4 names
    only "severity" (§5.2).
50. The completion values are `merged` and `verified`; `verified` is the wire name of the rule
    "verified in production" of DESIGN.md §24.11 (§5.2).
51. `prev_hash` of the first event of a key is written `-`, where DESIGN.md §24.3 says "empty",
    because a line of the event text is never empty (§4.1, §4.2).
52. Actor rule 1 (the `team` of a task create equals the actor's team) is checked at check 12 (c)
    of §6.7, before every check that reads the state, so that a create for another team never
    reveals that team's state, such as a used `short_name` (§6.5, §6.7).
53. Every hub process runs a full verification at start and then every 10 minutes on a monotonic
    clock, in one snapshot; two processes on one database disagree about whether the record is
    sound for at most 10 minutes plus the duration of one run, as long as a run takes less than
    10 minutes, and otherwise for at most twice the duration of a run (§12.1).
54. A checkpoint attempt first runs the incremental fold, which checks every row of the table of
    §12.1 from `position_gap` to `event_refused`, and then checks the newest checkpoint of its own
    hub key id as `checkpoint_invalid` does; it signs only a head that a fold checked, and writes
    nothing when the fold or that check fails or while the process is in `record_unverified`
    (§11.6, §12.2).
55. `verification` changes only by row 11, which sets a value; an event that sets or removes it
    otherwise is refused with `transition_invalid`, so a value is never removed (§5.2, §6.4).
56. Offline `verify` takes the teams file (`--teams`) and `restore` reads `AGENT_CORE_TEAMS`,
    because the keys file loader needs the roster; the export is verifiable offline with the
    public keys file and the public teams file (§12.3).
57. `export` prints `exported`, `refused config`, `refused file_exists`, `refused file_write` or
    `refused database`; a malformed export file is refused by `verify` and `restore` with the
    failure `file_invalid`, at position `0` for a fault of the whole file (not UTF-8, empty, no
    final `\n`) or of the header (§12.1, §12.3).
58. The event text is not bound to a hub or an environment: only the key holder can send its
    event to another hub, only where its key is registered, and the event then shows in that
    key's own chain there (§4.3).
59. A create with the `object_id` of an existing object the actor does not see gets
    `object_not_found`, and a used `event_id` gets `event_id_taken`; both confirm only that an
    id the caller already holds is in use. This is the third exception of §3 (§3, §6.7).
60. Row 10 starts only from `in_progress` or `ready_for_review`; a task in `open` needs row 2
    first, so its assignee needs two events to set `done` or `merged`. This is narrower than
    DESIGN.md §24.11 (§6.4).
61. The implementation is split into three changes: the migration and the record library, the
    deployment of each environment, and the record in the hub; the third is merged only after
    the second is rolled out in every environment that deploys from its target branch (§16).
62. `received_at` and `published_at` are kept in whole seconds by a `CHECK` in the database
    (§11.3).
63. An event holds 1 to 15 changes, one per field at most (§4.1, §4.2).
64. A task in `in_progress` or `ready_for_review` has an `assignee`; row 2 and row 14 of §6.4
    therefore need an `assignee` after the event (§6.4, §6.6).
65. `title` is required for both kinds, `security` included (§5.2).
66. The incremental fold before an answer, in an append and in a checkpoint attempt checks every
    row of the table of §12.1 from `position_gap` to `event_refused`, in table order, exactly as
    a full verification does for the events it reads. It runs in a `GET` endpoint and the release
    query after the `503` check, in an append only after checks 1 to 6 of §6.7, and never in
    `GET /api/key/record/verify` (§7.1, §11.5, §11.6).
67. A process checks against the keys file it loaded at start. During a rolling deployment of a
    keys file that adds a key, a process with the older file fails its fold with `key_unknown`
    at the first event of the new key and answers `503` until it is restarted with the newer
    file; this is intended (§11.6).
68. Every request under `/api/key/record/` that passed check 8 of KEY-PROTOCOL.md section 5
    writes exactly one log line, `record request <key id> <method> <raw path> <status>
    <result>`, for every principal; for a `service` key it is the line of KEY-PROTOCOL.md section
    5 extended by `<result>`, not a second line. A fold failure, a failed verification on request
    and a database failure of such a request are written in that line. `GET /api/key/release`
    keeps its line and may add one `record verify failed` or `record database error` line.
    Change 3 updates the sentence of KEY-PROTOCOL.md section 5 and its test (§1, §13).
69. Key ids may appear in log lines and may contain their holder's handle; a log line never
    holds a handle as a field of its own, and the log test forbids record content (titles,
    descriptions, comment bodies, short names, links and every other field value), object ids
    and event ids, not key ids. Of the two alternatives of the reviews (key ids without handles in
    the test, or a handle defined as a field of its own), the one that tests the key ids of the
    real convention is taken (§13, §14).
70. `restore` checks its file in the order file format (`file_invalid`), hub key ids of the
    checkpoints (`hub_key_missing`), then the rest of the verification (§12.3).
71. Each of the three changes names in its pull request body the subsections of DESIGN.md §24 it
    implements; a change that supersedes rows of DESIGN.md §2 or §3 has a companion pull request
    in the client repository that updates them and is merged after the change (§16).
