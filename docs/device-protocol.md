# Device protocol: device keys, levels, the allow list and grants

**Status:** specification, not built. It fixes the first two build steps of
[DESIGN.md §25.20](../DESIGN.md#2520-build-order):

1. device keys with levels and inventory; the allow list and its checks on the device;
2. verification of the session key's certificate on the device; grants.

The binding concept is [DESIGN.md §25](../DESIGN.md#25-central-operation-decided-not-implemented)
together with §24, on which §25 builds. Where this document disagrees with DESIGN.md §25 or §24,
DESIGN.md wins and this document is fixed; DESIGN.md §25 names this document and states the same
rule. An explanation for the whole team is in [central-operation.md](central-operation.md).

Words: **must** and **never** state a rule. "Refused with `x`" means the operation stops, `x` is
the refusal value that is recorded and shown, and nothing changes except the decision line
(§17.1) and, for a command or a statement, the record of its id in `seen` and its signed refusal
(§17.2).

## Contents

1. [Scope](#1-scope)
2. [Dependencies](#2-dependencies)
3. [Encodings](#3-encodings)
4. [Accounts, files and modes](#4-accounts-files-and-modes)
5. [Configuration](#5-configuration)
6. [Device key and program keys](#6-device-key-and-program-keys)
7. [Levels](#7-levels)
8. [The keys file entry and the keys-file copy](#8-the-keys-file-entry-and-the-keys-file-copy)
9. [Revocation notices](#9-revocation-notices)
10. [Inventory](#10-inventory)
11. [The allow list](#11-the-allow-list)
12. [Statements: signed changes of the allow list](#12-statements-signed-changes-of-the-allow-list)
13. [Grants](#13-grants)
14. [Commands and the checks before acting](#14-commands-and-the-checks-before-acting)
15. [The session key's certificate](#15-the-session-keys-certificate)
16. [Obey less, never more](#16-obey-less-never-more)
17. [Local log, refusals and team-record events](#17-local-log-refusals-and-team-record-events)
18. [Client commands](#18-client-commands)
19. [The guard service, the socket and the daemon](#19-the-guard-service-the-socket-and-the-daemon)
20. [Tests](#20-tests)
21. [Rollout order](#21-rollout-order)
22. [Changes this forces in existing code](#22-changes-this-forces-in-existing-code)
23. [Decisions taken in this document](#23-decisions-taken-in-this-document)

## 1. Scope

Built in these two steps:

- the device key, program keys, the keys-file entry the device prints, and the device's level;
- the device's copy of the keys file and of the hub's revocation notices;
- the inventory report;
- the allow list, the statements that change it, grants, and every check of §14 and §15 as one
  verifier;
- the guard service in its own operating-system account, its socket, its local log, and the
  client commands of §18.

Not built in these two steps, and named here only where the verifier needs a field:

- fetching commands from the hub and acting on them (§25.20 step 4). Until then no command from
  the hub runs on a device; the verifier is exercised by tests and by `agent device verify`
  (§18.12). Today's control frames (DESIGN.md §17) and local orders (§19.10) stay unchanged.
- the local signing service for the owner's own AI session (§25.6, §25.14). The guard socket of
  this step offers no signing operation at all (§19.3).
- the sequence counter of a program key that signs commands: step 4 fixes where its sender keeps
  it. In these steps only the device key's counter exists (`own_sequence`, §12.1).
- session logs, recording of direct logins and masking (§25.20 step 5).

## 2. Dependencies

### 2.1 The hub's key protocol

`KEY-PROTOCOL.md` in the hub repository defines, and this document does not restate:

| Rule | Used here for |
|---|---|
| K1. The id rule of the keys file: 3 to 63 lowercase letters, digits or `-`, starting and ending with a letter or digit (`keys.ID_PATTERN`) | every key id |
| K2. Signed request: the eight lines `agent-request-v1`, key id, method, host, target, time, nonce, body hash; headers `Agent-Key`, `Agent-Time`, `Agent-Nonce`, `Agent-Signature` (`keys.request_text`) | every request the guard sends |
| K3. Signed answer: the seven lines `agent-response-v1`, key id, nonce, request time, status, hub time, body hash; headers `Agent-Hub-Key`, `Agent-Hub-Time`, `Agent-Hub-Signature`; 120-second window; at most 1 MiB; no `Content-Encoding`; no redirect (`keys.verify_answer`, `keys._read_answer`) | every answer the guard accepts |
| K4. The keys file: one entry per key with id, kind, public key, principal kind, handle, device label, program label, team, scopes; changed only by pull request | §8 |
| K5. The pinned hub key in `hub-key.json` (`keys.pin_hub_key`, `keys.load_hub_pin`) | §4, §8, §9 |
| K6. The repository rule `OWNER/NAME` (`keys.valid_repository`) | allow-list entries, commands |

This document adds requirements on the hub's key protocol. Each is marked **[hub key protocol]**
and is listed again in §21.1. The hub repository adds them to `KEY-PROTOCOL.md`; the names and
values here are the ones the device implements.

### 2.2 The hub's passkey specification

The bytes of the session key's certificate are fixed by the hub's passkey specification, written
in parallel. Every point the device needs from it is a requirement marked **[passkey
specification]** with a number P1 to P9 (§15.1). The two documents are checked against each other
by these numbers.

## 3. Encodings

All encodings of the key protocol apply (`keys.py` module docstring): binary values are base64url
without padding and canonical (`keys.b64url_decode`); hashes are SHA-256 as 64 lowercase hex
characters (`keys.sha256_hex`); times are UTC in whole seconds `YYYY-MM-DDTHH:MM:SSZ`
(`keys.parse_time`).

Added here:

- **Text values.** Every string value in a document of this specification (allow list,
  statement, command, inventory, notice, refusal, change request) consists of printable ASCII
  from `0x20` to `0x7E` without `"` and `\`, unless its field says otherwise. A value outside that
  alphabet makes the document malformed.
- **Canonical JSON.** A JSON object written with its member names sorted by code point, no
  whitespace outside strings, integers in decimal without sign `+`, leading zero or exponent,
  `true`, `false`, `null`, and strings as `"` value `"`. Because of the text-value rule no string
  needs an escape. Python: `json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=True)` gives these bytes for every valid document.
- **Strict parsing.** Every document is parsed with repeated member names refused (as
  `keys._release_answer` refuses a repeated name among the pairs that `keys._Members` collects),
  every listed member present, no other member, each of the stated type. A boolean is never
  accepted where an integer is required.
- **Signed document text.** The signed text of a document with format tag `T` is the UTF-8 bytes
  of `T`, one line feed, and the canonical JSON of the document without its `signature` member.
- **Fingerprint.** The fingerprint of a public key is `sha256_hex` of the public-key bytes exactly
  as they are decoded from the `public_key` value: the 32 raw bytes of an `ed25519` key, the DER
  bytes of a `webauthn` key (§8.2), the 65 bytes of a `p256` session key. It is shown as the 64
  hex characters.
- **UUID.** Lowercase, 36 characters, version 4 (`[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}`).
- **Handle.** A staff member's handle follows `keys.OWNER_PATTERN` (1 to 39 lowercase letters,
  digits or `-`, not starting or ending with `-`, no `--`).
- **Session id.** 1 to 128 text-value characters.

## 4. Accounts, files and modes

### 4.1 Two accounts

DESIGN.md §25.6 requires that the allow list and the device key belong to an operating-system
account that differs from the account running sessions, orders and direct logins.

| Account | Name in this document | Runs | Holds |
|---|---|---|---|
| Key account | the **key account** (operator-chosen name, for example `agent-keys`) | `agent device serve` (the **guard**) and every `agent device` command of §18 marked *key account* | `$AGENT_KEY_HOME` |
| Session account | the **session account** (the account that runs `agent daemon`, sessions, orders and direct logins today) | everything else, including `agent device report` and `agent device status` | `$AGENT_HOME` |

Rules:

- `AGENT_KEY_HOME` is an environment variable of the key account. It has no default; a key-account
  command without it stops with `AGENT_KEY_HOME is not set`.
- `agent device init` without `--reset-state` is the only key-account command that runs while
  `$AGENT_KEY_HOME/guard.json` does not exist. It stops with `this command runs only in the key
  account` when the effective user id is 0 or is the user id of the account named by
  `--session-account`, and with `$AGENT_KEY_HOME must be absent or an empty directory of the key
  account` unless `$AGENT_KEY_HOME` does not exist (it is then created with mode `0700`) or is an
  empty directory, not a symbolic link, with mode `0700` and owned by the effective user id. It
  stops when `guard.json` exists (§18.1).
- Every other key-account command, `agent device init --reset-state` included, and the guard stop
  with `guard.json: <member> <rule>` when `guard.json` is missing or invalid (§5.1), and with
  `this command runs only in the key account` when the effective user id differs from the owner
  of `$AGENT_KEY_HOME`, when the effective user id is 0, or when the effective user id is the
  user id of the session account named in `guard.json`.
- They stop with `<path> must have mode <mode> and belong to the key account` when a path under
  `$AGENT_KEY_HOME` listed in §4.2 has another mode or owner, or is a symbolic link. The guard
  checks the socket directory in the same way: owner the key account, mode `2750`, not a symbolic
  link. The paths under `$AGENT_HOME` belong to the session account and are not checked by the
  key account.
- Creating the key account, its group membership and the socket directory is an administrator
  action outside the client (§21.2). The client never changes an account or a group.
- No key-account file is ever under `$AGENT_HOME`, and no state of the guard is in the session
  store (PostgreSQL): the session account can reach both.

### 4.2 Files

| Path | Mode | Written by | Content |
|---|---|---|---|
| `$AGENT_KEY_HOME/` | `0700` | `agent device init` | directory |
| `$AGENT_KEY_HOME/guard.json` | `0600` | `agent device init` | configuration, §5.1 |
| `$AGENT_KEY_HOME/hub-key.json` | `0600` | `agent device pin-hub` | pinned hub key, the existing form of `keys.pin_hub_key` |
| `$AGENT_KEY_HOME/keys/` | `0700` | `agent device init` | key directory |
| `$AGENT_KEY_HOME/keys/<id>.key` | `0600` | `agent device init`, `agent device program-key` | Ed25519 seed, the existing form of `keys.create_key` |
| `$AGENT_KEY_HOME/allow-list.json` | `0600` | guard and key-account commands | §11 |
| `$AGENT_KEY_HOME/keys-file.json` | `0600` | guard, `agent device refresh` | keys-file copy, §8.3 |
| `$AGENT_KEY_HOME/revocations.json` | `0600` | guard, `agent device refresh` | revocation notices, §9 |
| `$AGENT_KEY_HOME/state.json` | `0600` | guard and key-account commands | replay state, §4.3 |
| `$AGENT_KEY_HOME/lock` | `0600` | every writer | exclusive `flock` held for every read-modify-write of the files above |
| `$AGENT_KEY_HOME/log/` | `0700` | `agent device init` | local log directory |
| `$AGENT_KEY_HOME/log/decisions.jsonl` | `0600` | guard and key-account commands | one line per decision, §17.1 |
| `$AGENT_KEY_HOME/log/refusals.jsonl` | `0600` | guard and key-account commands | signed statement refusals in the order written; `state.json` `refusals_sent` counts the lines the hub has accepted, §17.2 |
| `$AGENT_KEY_HOME/log/events.jsonl` | `0600` | guard and key-account commands | signed device events and their upload state, §17.3 |
| socket directory from `guard.json` `socket` | `2750` (set-group-id), owner key account, group of §21.2 | administrator | directory; the set-group-id bit gives the socket the directory's group |
| the socket | `0660`, group of the socket directory | guard | Unix socket, §19.3 |
| `$AGENT_HOME/guard.json` | `0600` | operator, session account | session-account side: `{"socket": "<absolute path>"}` and nothing else |
| `$AGENT_HOME/device-inventory.json` | `0600` | operator, session account | optional runtime list, §10.2 |

Every write of a file in `$AGENT_KEY_HOME` is atomic: write a new file with mode `0600` beside it
(`keys._write_new_file`), `fsync` it, then `os.replace`. A log line is appended with one `write`
call followed by `fsync`.

### 4.3 `state.json`

```json
{
  "format": "agent-guard-state-v1",
  "sequences": {"<key id>:<fingerprint>": 41},
  "session_sequences": {"<fingerprint>": {"sequence": 3, "expires_at": "2026-12-01T21:00:00Z"}},
  "seen": {"<uuid>": "2026-12-15T09:14:03Z"},
  "fresh_values": {"<fresh value>": {"statement_hash": "<64 hex>", "expires_at": "2026-12-01T21:00:00Z"}},
  "tainted_sessions": {"<session id>": "<grant entry id>"},
  "tainted_before": null,
  "reset_at": null,
  "keys_file_serial": 17,
  "revocation_serial": 5,
  "revocations_incomplete": false,
  "statement_cursor": 0,
  "refusals_sent": 0,
  "own_sequence": 0,
  "allow_list_hash": "<64 hex>"
}
```

| Member | Rule |
|---|---|
| `sequences` | Highest accepted sequence number per registered signing key (device or program key), for commands and statements together. The member name is the key id, `:`, and the key's fingerprint. A member is never dropped. At most 65536 members. |
| `session_sequences` | Highest accepted sequence number per session key: member name the session key's fingerprint, value the sequence and the latest certificate expiry accepted for that key. A member is dropped once its `expires_at` has passed, because V7 then refuses that key. At most 65536 members. |
| `seen` | Ids of commands and statements that were accepted, or refused with their id recorded (§17.2), each with its drop time: the later of the device time of the decision and the document's `signed_at`, plus 7200 s for a command or 1209600 s for a statement (twice the largest window §5.1 allows). A member is dropped after its drop time. At most 65536 members. |
| `fresh_values` | Fresh value of every accepted certificate (§15.3), dropped after its `expires_at`. At most 4096 members. |
| `tainted_sessions` | Every session id named by a command accepted under a grant (§13.6). Never dropped while the session id can exist. At most 65536 members. |
| `tainted_before` | `null`, or the time of the last reset (§4.4): the signing service of step 4 treats every session that started before it as tainted. |
| `reset_at` | `null`, or the device time of the last reset (§4.4); C13 and S9 refuse what the device could have accepted before it. |
| `keys_file_serial` | Serial of the keys-file copy in use (§8.3); 0 when no copy was taken into use. |
| `revocation_serial` | Highest revocation-notice serial applied under the pinned hub key (§9); 0 when none. |
| `revocations_incomplete` | `true` from `agent device init`, a reset (§4.4) or a new hub pin (§18.4) until the next complete fetch, and while a revocation notice is known to be missing or did not verify (§9). |
| `statement_cursor` | Highest statement cursor the guard has processed (§12.2), from 0. |
| `refusals_sent` | Number of lines of `log/refusals.jsonl` the hub has accepted, from 0 (§17.2). |
| `own_sequence` | On a controller: the last sequence number this device key used (§12.1). |
| `allow_list_hash` | `sha256_hex` of `allow-list.json` as the client last wrote it, or `null` after a reset (§4.4, §11.5). |

When accepting a command or statement would add a member beyond its limit, the command or
statement is refused with `replay_state_full` (C13, S9). When `seen` is full, a refusal cannot
record its id and is not answered with a signed refusal (§17.2).

A missing or invalid `state.json` after `agent device init` stops the guard and every key-account
command except `agent device init --reset-state` with `state.json is missing` or `state.json is
invalid`; the device obeys nothing until an administrator runs `agent device init --reset-state`
(§4.4). An administrator never puts back an older copy of `state.json`, for example from a
backup: an older copy would accept replays.

### 4.4 Resetting `state.json`

`agent device init --reset-state` (key account, given alone) is the only repair of a missing or
invalid `state.json`. It stops with `state.json is valid; nothing was reset` when the file loads,
and with `log/decisions.jsonl is not readable; nothing was reset` when the decision log cannot be
read. A reset never clears replay protection or revocations. It writes a new `state.json` in
which:

- `reset_at` and `tainted_before` are the device clock;
- `sequences`, `session_sequences`, `seen`, `fresh_values` and `tainted_sessions` are empty.
  Replays are refused by `reset_at` instead: C13 refuses every command whose `signed_at` is not
  later than `reset_at` plus 3600 s, and every certificate whose expiry is not later than
  `reset_at` plus 43200 s; S9 refuses every statement other than `remove` whose `signed_at` is
  not later than `reset_at` plus 604800 s; each with `signed_before_reset`. These are the largest
  windows §5.1 allows and the longest session-key lifetime of P6, so nothing the device could have
  accepted before the reset passes again. A `remove` statement only takes away and is not held
  back;
- `keys_file_serial` is the serial of `keys-file.json` when that copy verifies, otherwise 0;
- `revocation_serial` is the highest serial among the notices in `revocations.json` signed by the
  pinned hub key, otherwise 0;
- `revocations_incomplete` is `true` until the next complete fetch (§9);
- `statement_cursor` and `refusals_sent` are 0; the hub accepts a refusal it already holds
  (§17.2);
- `own_sequence` is the highest `sequence` of a `signed` line in `log/decisions.jsonl` (§17.1),
  otherwise 0;
- `allow_list_hash` is `null`, so that the guard checks the allow list at its next load as an
  edit outside the client (§11.5).

The reset appends a `reset` line to the decision log and writes the device event
`guard.state_reset` (§17.3). It changes neither the allow list nor `keys-file.json`,
`revocations.json`, the keys or the logs.

## 5. Configuration

### 5.1 `$AGENT_KEY_HOME/guard.json`

```json
{
  "format": "agent-guard-v1",
  "hub_url": "https://hub.example",
  "website_origin": "https://hub.example",
  "device_key_id": "device-example-device",
  "owner": "example-staff",
  "session_account": "agent",
  "socket": "/var/run/agent-guard/guard.sock",
  "session_key_max_lifetime_seconds": 43200,
  "command_window_seconds": 300,
  "statement_window_seconds": 86400,
  "keys_file_max_age_seconds": 86400
}
```

| Member | Type and limit | Default written by `init` |
|---|---|---|
| `format` | exactly `agent-guard-v1` | — |
| `hub_url` | accepted by `keys.hub_origin` | from `--hub`, required |
| `website_origin` | `https://` plus a host, optional `:port`, no path; the exact origin of the hub's website | from `--website-origin`, required |
| `device_key_id` | id rule K1, starts with `device-` | from `--key-id`, required |
| `owner` | handle | from `--owner`, required |
| `session_account` | 1 to 32 characters `[a-z_][a-z0-9_-]*`, an existing account other than the key account | from `--session-account`, required |
| `socket` | absolute path, at most 100 bytes | from `--socket`, required |
| `session_key_max_lifetime_seconds` | integer 300 to 43200 | 43200 |
| `command_window_seconds` | integer 30 to 3600 | 300 |
| `statement_window_seconds` | integer 300 to 604800 | 86400 |
| `keys_file_max_age_seconds` | integer 600 to 604800 | 86400 |

The relying-party id is the host of `website_origin` without port. Any other member, a missing
member or a value outside its limit stops every key-account command and the guard with
`guard.json: <member> <rule>`. The file changes only by hand in the key account; the guard reads
it at start.

### 5.2 Fixed values

| Name | Value |
|---|---|
| keys-file refresh interval | 600 s |
| revocation and statement poll interval | 60 s |
| inventory interval of `agent device report --follow` | 900 s, and at once after a change |
| inventory resend without change | 86400 s |
| longest grant lifetime | 2592000 s (30 days) |
| shortest grant lifetime | 60 s |
| allow-list entries, entries and grants together | 256 |
| removed ids kept (`removed`) | 65536 |
| permissions per entry | 7 |
| repositories per entry | 64 |
| statement size | 16384 bytes |
| command size, certificate included | 65536 bytes |
| socket request size | 65536 bytes |
| socket read deadline per connection | 5 s |
| socket connections handled at once | 8 |
| hub request timeout of the `inventory` socket operation | 30 s |
| inventory size | 65536 bytes |
| revocation notices per answer | 1000 |
| statements per answer | 100 |
| change requests per answer | 256 |
| restarts of `device-report` | 10 deaths in 60 s (§19.2) |
| hub answer size | 1 MiB (K3) |

## 6. Device key and program keys

### 6.1 Built now

- Kind `ed25519`. The private key is a 32-byte seed in `$AGENT_KEY_HOME/keys/<id>.key`, written
  by the existing `keys.create_key` with `$AGENT_KEY_HOME` as its home (§22). It never leaves
  that file: it is never printed, logged, sent or copied, and no error message contains it.
- One device key per device, created by `agent device init` on the device. A device key is never
  copied to another device and never created elsewhere.
- **Device key id:** `device-` followed by the device label, as a whole within rule K1, for
  example `device-example-device`. The device label is chosen by the operator and is the device
  label of the keys-file entry.
- **Program keys** (DESIGN.md §24.2, one key per program where it matters) are created in the key
  account with `agent device program-key --id ID --label LABEL`. Program key id: `program-`
  followed by the device label, `-`, and the program label, within rule K1, for example
  `program-example-device-ai`. A program key that may send commands is always in the key account
  (§25.6). Program keys under `$AGENT_HOME/keys` (the existing `agent key create`) stay for
  programs that send no command, such as a release query; the owner never lists one as a sender,
  and the guard cannot detect one (§11.4, E4).

### 6.2 Left for later

- Kind `p256`: an ECDSA P-256 device key held in the machine's secure hardware (DESIGN.md §25.6,
  Keys in hardware). This step reserves the value: the allow list, statements and the keys-file
  copy parse it and refuse it with `key kind p256 is not supported by this client`. The later
  pull request adds key creation in hardware, the signature over the same signed texts, and the
  keys-file entry. Until then every device key is `ed25519`.
- `p256` as the algorithm of the session key (§15) is a different use and is built in step 2.

## 7. Levels

The level is a scope on the device key in the keys file (DESIGN.md §25.4):

| Scope | Level | On this device | Toward other devices |
|---|---|---|---|
| `device:executor` | executor | runs work it was given (from step 4) | sends no command; signs no statement; is never listed as a sender (§11.4, E3) |
| `device:controller` | controller | as an executor | its device key may sign statements for devices of the same owner (§12) and grants (§13); its device key and its program keys may be listed as senders on devices of the same owner, and on other owners' devices only through a grant |

- A device key has exactly one of the two scopes. A keys-file entry of a device key with both or
  neither makes that entry unusable for the device: it counts as unknown (`unknown_key`).
- A program key's level is the level of the device key with the same device label and handle in
  the keys-file copy, and only while that device key is not revoked (E3, C10).
- A device learns its own level from the keys-file copy. `agent device sign` (§18.9) stops with
  `this device is not a controller` unless the copy is fresh and gives this device key
  `device:controller`.
- A staff member signed in with a passkey has controller rights over their own devices
  (DESIGN.md §25.4) through the entry of that passkey in each device's allow list; there is no
  scope for it.

## 8. The keys file entry and the keys-file copy

### 8.1 How the device key gets into the keys file

1. `agent device init` creates the key and prints the entry (§18.1).
2. The owner opens a pull request to the hub repository that adds the entry to the keys file,
   with the scope of the chosen level. The pull request is reviewed and merged like every keys
   file change (DESIGN.md §24.2). The device sends nothing to the hub for this.
3. After deployment, `agent device refresh` finds the key in the keys-file copy.

The printed entry, one JSON object on standard output:

```json
{"id": "device-example-device", "kind": "ed25519", "public_key": "<base64url>", "fingerprint": "<64 hex>", "principal": "device", "handle": "example-staff", "device": "example-device", "program": null, "scopes": ["device:executor"]}
```

`scopes` is `["device:executor"]` unless `--controller` was given. The member names are those of
H1 to H8 (§8.2) as written here; where `KEY-PROTOCOL.md` names them differently, the
implementation prints its names, and the values stay these **[hub key protocol]**. `fingerprint`
is printed for the owner's comparison (§3). `team` is not printed: the pull request author adds
it.

### 8.2 What the device needs from each keys-file entry **[hub key protocol]**

| Need | Value |
|---|---|
| H1. id | rule K1 |
| H2. kind | `webauthn`, `ed25519`, or the reserved `p256` |
| H3. public key | `ed25519`: 32 bytes base64url; `webauthn`: base64url of the DER `SubjectPublicKeyInfo` of the credential public key, at most 1024 bytes, with H4 |
| H4. `alg` of a `webauthn` key | the COSE algorithm: `-7` (ES256), `-8` (EdDSA, Ed25519) or `-257` (RS256, modulus 2048 to 4096 bits) **[passkey specification]** |
| H5. principal | `staff`, `device`, `program` or `service` |
| H6. handle | the staff member; for a device or program key the owner of the device |
| H7. device label and program label | for `device` and `program` keys |
| H8. scopes | list of strings; the device reads only `device:executor` and `device:controller` |
| H9. revoked | a revoked entry stays in the file, marked revoked, with its id and public key; an id is never given to another key |
| H10. serial | the keys file as a whole has a serial: an integer from 1 that grows by at least 1 with every merged change |

### 8.3 The copy: fetch, verification, age

- **Fetch.** The guard sends a signed `GET /api/key/keys-file` with the device key (K2) every
  600 s and on `agent device refresh` **[hub key protocol]**. A 200 answer that verifies (K3)
  carries the body `{"serial": <H10>, "keys": [<entry>, …]}`.
- **Kept.** `keys-file.json` stores the whole verified answer so that it can be verified again
  offline at every load:

  ```json
  {"format": "agent-keys-file-copy-v1", "key_id": "<device key id>", "nonce": "…", "request_time": "…",
   "status": 200, "hub_key": "<pinned hub key id>", "hub_time": "…", "signature": "…", "body": "<base64url of the body>"}
  ```

- **Verified at every load:** `hub_key` equals the pinned hub key id, and the signature verifies
  over `keys.response_text(key_id, nonce, request_time, 200, hub_time, sha256_hex(body))` with the
  pinned hub key. The 120-second window applies only when the answer is received. A copy that
  does not verify is treated as absent and recorded as `keys_file_invalid`.
- **Replacing the stored copy.** A newly fetched copy that verifies is compared with the stored
  copy. Each refusal is recorded, and the stored copy stays in use and keeps ageing:
  - a `serial` lower than `keys_file_serial` is refused with `keys_file_rollback`;
  - an equal serial with a different body is refused with `keys_file_conflict`;
  - an equal serial with an identical body replaces the stored answer, which renews `hub_time`;
  - a higher serial replaces the stored copy and `keys_file_serial`, unless an entry that the
    stored copy marks revoked is missing from it, is not marked revoked in it, or has another
    public key in it: that copy is refused with `keys_file_conflict` (H9).

  When the stored copy is absent or invalid, only the comparison with `keys_file_serial`
  applies, and an equal serial is refused with `keys_file_conflict`.
- **Age.** The age of the copy is the device clock minus the `hub_time` of the stored answer, which
  is the hub's signed time of the last fetch whose copy replaced it, also when the serial did not
  change. The copy is **fresh** while the age is at most `keys_file_max_age_seconds`. A `hub_time`
  later than the device clock plus 120 s makes the copy invalid.
- **Taken into use.** When a copy is stored, every allow-list entry whose key id
  the copy marks revoked gets `status` `revoked`, with the same further effects as a notice for
  that key id (§9).
- **Too old.** While the copy is absent, invalid or not fresh, the device:
  - refuses every command with `keys_file_stale` (§14, check C5);
  - refuses every statement and every by-hand change that adds, changes or confirms an entry
    with `keys_file_stale`;
  - still accepts a statement or a by-hand change that removes an entry (S7 states which checks
    a `remove` statement keeps), and still applies revocation notices (§16);
  - keeps running work it already accepted (from step 4).
- **What the copy can do.** The copy only takes rights away: a key that is absent, revoked or of
  the wrong level in the copy is refused even when the allow list lists it. A key in the copy
  that the allow list does not list is refused. The copy never adds an entry (§16).

## 9. Revocation notices

A revocation notice is a document signed by the hub's signing key, the pinned key
**[hub key protocol]**. The hub writes one for each key that a merged keys-file change marks
revoked.

```json
{"format": "agent-revocation-v1", "serial": 6, "key_id": "device-example-device", "fingerprint": "<64 hex>",
 "revoked_at": "2026-12-01T09:00:00Z", "hub_key": "<hub key id>", "signature": "<base64url, 64 bytes>"}
```

- The signature is Ed25519 over the signed document text with tag `agent-revocation-v1` (§3).
- `serial` starts at 1 and grows by exactly 1 per notice, over all keys.
- The guard sends a signed `GET /api/key/revocations?after=<revocation_serial>` every 60 s and
  on `agent device refresh` **[hub key protocol]**. The verified 200 answer is
  `{"notices": [<notice>, …]}` with at most 1000 notices in serial order.
- Each notice is verified on its own: format, `hub_key` equal to the pinned id, signature with
  the pinned key. A notice that does not verify is not applied; the guard records
  `revocation_invalid`, sets `revocations_incomplete`, and does not advance past it.
- A notice whose serial is not `revocation_serial + 1` is not applied; the guard records
  `revocation_gap` and sets `revocations_incomplete`.
- `revocations_incomplete` is cleared only by an answer that verifies, holds fewer than 1000
  notices, and whose every notice verifies and is applied in serial order without a gap. While it
  is set, the device refuses every command (C5) and every `add`, `change` and `confirm`, by
  statement (S7) or by hand (E1), with `revocations_incomplete`; removals still pass.
- `revocations.json` keeps every applied notice (`{"format": "agent-revocations-v1", "notices":
  [...]}`), so that a decision can be checked later without the hub. It is never shortened, not
  by a reset (§4.4) and not by a new hub pin (§18.4). Notices of different hub keys with the same
  serial are both kept.
- **Applying a notice** (§16) acts on its key id, whatever the fingerprint: an id is never given
  to another key (H9), and matching more entries only makes the device obey less. The fingerprint
  is recorded and shown.
  - Every entry with that key id gets `status` `revoked`.
  - Every entry and grant whose `signer.key_id` is that key id gets `status` `suspended`, unless
    it is `revoked` or `removed`. Only controller device keys sign entries (S6), so this covers every entry a
    revoked controller signed.
  - When the key id is a device key id `device-<label>`, every entry of principal `program` whose
    key id starts with `program-<label>-` gets `status` `revoked`: the program keys of a revoked
    device are not obeyed any longer either.
- A notice is applied whatever the age of the keys-file copy and even when the copy does not mark
  the key revoked.
- **A revoked key id never works again on any device.** `revoked` is final: no statement, no
  by-hand command and no later copy makes such an entry active again, and C7, S7 and E2 refuse the
  key id for good. A revocation that turns out to be wrong is undone only by a new key (§16).

## 10. Inventory

### 10.1 What is reported

DESIGN.md §25.3: AI accounts and subscriptions, code-host accounts and runtimes, names and states,
never a credential.

```json
{
  "format": "agent-inventory-v1",
  "device_key_id": "device-example-device",
  "reported_at": "2026-12-01T09:14:03Z",
  "client_version": "0.0.0",
  "ai_accounts": [{"name": "driver-a-profile", "provider": "driver-a"}],
  "subscriptions": [{"provider": "driver-a", "status": "active"}],
  "code_host_accounts": [{"name": "worker-a", "login": "example-worker-a", "can_push": true}],
  "runtimes": [{"name": "host", "kind": "host"}],
  "omitted": 0,
  "signature": "<base64url>"
}
```

| Member | Source | Rule |
|---|---|---|
| `ai_accounts` | `accounts` of `$AGENT_HOME/ai-accounts.json` | account name and provider; nothing else |
| `subscriptions` | `$AGENT_HOME/ai-subscriptions.json` | provider and `status` (`null` when absent); `account`, `label` and `note` are never reported |
| `code_host_accounts` | `accounts` of `$AGENT_HOME/github-accounts.json` | account name, `login`, and `can_push` = the account has a `git` object |
| `runtimes` | `$AGENT_HOME/device-inventory.json` | §10.2 |
| `omitted` | — | number of items left out because a value broke the text-value rule or a limit |

Each list holds at most 64 items, each string at most 100 characters; items beyond are left out
and counted in `omitted`. A missing source file gives an empty list. A source file that does not
load gives an empty list and `omitted` stays as counted; the report is still sent.

### 10.2 `$AGENT_HOME/device-inventory.json`

`{"runtimes": [{"name": "<1 to 64 text characters>", "kind": "host" | "container" | "virtual-machine"}]}`,
at most 64 items, no other member. A missing file means `runtimes: []`.

### 10.3 Signing and sending

1. `agent device report` (session account) builds the document without `reported_at`,
   `device_key_id` and `signature` and sends it over the socket (op `inventory`, §19.3).
2. The guard checks the form again, sets `device_key_id` and `reported_at`, signs the signed
   document text with tag `agent-inventory-v1` with the device key, and sends it as the body of
   a signed `POST /api/device/inventory` (K2 with the body hash) **[hub key protocol]**.
3. A verified 200 answer is the acceptance; the guard answers `{"ok": true}` on the socket. Any
   other outcome answers `{"error": "<value>"}` with `hub_unreachable`, `hub_not_verified` or
   `hub_refused`.

### 10.4 Where the hub keeps it **[hub key protocol]**

The hub verifies the request and the document signature against the device key in its keys file,
refuses a `reported_at` that is not later than the stored one, and keeps exactly the newest
accepted document per device key id, as received with its signature, together with its receive
time. It is not a team-record event; earlier documents are replaced.

## 11. The allow list

### 11.1 Place and form

`$AGENT_KEY_HOME/allow-list.json`, the device's counterpart of an SSH `authorized_keys` file.

```json
{
  "format": "agent-allow-list-v1",
  "device_key_id": "device-example-device",
  "owner": "example-staff",
  "entries": [
    {
      "entry_id": "3f0c…",
      "type": "entry",
      "key_id": "example-staff-laptop-passkey",
      "kind": "webauthn",
      "alg": -7,
      "public_key": "<base64url>",
      "principal": "staff",
      "handle": "example-staff",
      "permissions": ["order.submit", "question.answer", "question.withdraw", "session.input", "session.start", "session.stop"],
      "repositories": ["*"],
      "expires_at": null,
      "status": "active",
      "signer": {"by": "hand", "key_id": null, "fingerprint": null, "statement_id": null, "sequence": null},
      "added_at": "2026-12-01T09:14:03Z"
    }
  ],
  "removed": ["<uuid>", "…"]
}
```

### 11.2 Fields

| Field | Type and limit |
|---|---|
| `format` | exactly `agent-allow-list-v1` |
| `device_key_id`, `owner` | equal to `guard.json`; otherwise the whole file is invalid |
| `entries` | 0 to 256 entries |
| `removed` | 0 to 65536 UUIDs: every entry id ever removed; never shrinks |
| `entry_id` | UUID; unique in `entries` and not in `removed` |
| `type` | `entry` (a key of the owner) or `grant` (a key of another staff member, §13) |
| `key_id` | rule K1; at most one entry per key id |
| `kind` | `webauthn` for principal `staff`; `ed25519` for principal `device` or `program`; `p256` is refused (§6.2) |
| `alg` | `webauthn`: `-7`, `-8` or `-257`; otherwise `null` |
| `public_key` | as H3 |
| `principal` | `staff`, `device` or `program` |
| `handle` | handle |
| `permissions` | 1 to 7 distinct values of §11.3, sorted |
| `repositories` | exactly `["*"]` (every repository), or 1 to 64 distinct sorted `OWNER/NAME` values by K6 |
| `expires_at` | time or `null`; a `grant` always has a time |
| `status` | `active`; `suspended` (its signer was revoked, §9); `revoked` (a notice or the copy names its key id, or the key id of its device key; final); `removed` (removed while `removed` held 65536 ids; final) |
| `signer.by` | `hand` or `statement` |
| `signer.key_id`, `signer.fingerprint`, `signer.statement_id`, `signer.sequence` | for `statement`: the signing controller key id, the fingerprint of its key, the statement id and its sequence; for `hand`: all `null` |
| `added_at` | time the device applied it |

A file that breaks any rule is **invalid**: the device then obeys no key (`allow_list_invalid`)
and accepts only by-hand changes, which rewrite it. The file is never repaired silently.

**Removal.** A removal (by statement, by hand, or the expiry of a grant, §13.5) deletes the entry
and adds its `entry_id` to `removed`. While `removed` holds 65536 ids, a removal instead keeps the
entry with `status` `removed`, which obeys nothing and is never active again; every further add is
then refused with `allow_list_full` (E9).

### 11.3 Permissions

| Permission | Allows the command kind | In an `entry` | In a `grant` |
|---|---|---|---|
| `order.submit` | `order.submit` | yes | yes |
| `question.answer` | `question.answer` | yes | yes, never in the owner's own AI session |
| `question.withdraw` | `question.withdraw` | yes | no |
| `session.input` | `session.input` | yes | yes, never in the owner's own AI session |
| `session.start` | `session.start` | yes | yes |
| `session.stop` | `session.stop` | yes | yes |
| `allow_list.change` | signing statements (§12); no command | only for a controller device key of the owner | no |

The list is closed. A command kind not in it is malformed.

### 11.4 Entry rules

Every new or changed entry, whether by hand or by statement, must meet all of these; otherwise it
is refused with the value given. A confirmation (S10, §18.8) meets E1, E2 and E3 for the
confirmed entry.

| Rule | Refusal |
|---|---|
| E1. The keys-file copy is fresh, and `revocations_incomplete` is not set (§9). | `keys_file_stale`, `revocations_incomplete` |
| E2. The copy has an entry with this key id, not marked revoked, with the same kind, `alg`, public key and handle; no applied notice names the key id; its principal and kind are `staff` with `webauthn`, or `device` or `program` with `ed25519`. Any other principal or combination counts as unknown. | `unknown_key`, `key_revoked`, `key_mismatch` |
| E3. A `device` key has `device:controller`. A `program` key's device key (same device label and handle in the copy) has `device:controller`, is not marked revoked in the copy, and no applied notice names it. | `executor_key`, `key_revoked` |
| E4. The key is not this device's own device key and not one of the program keys in `$AGENT_KEY_HOME/keys` (compared by fingerprint). A key held under `$AGENT_HOME/keys` cannot be told apart by the guard; the owner never lists such a key, and the keys-file pull request of a program key that may send commands names a key created with `agent device program-key`. | `own_key` |
| E5. `type` is `entry` when the handle is the owner, `grant` otherwise. | `entry_invalid` |
| E6. `allow_list.change` only for a `device` key with the owner's handle. | `entry_invalid` |
| E7. A `grant` meets §13.2. | `grant_invalid` |
| E8. No other entry with this key id; for an add, no entry with this `entry_id`. | `entry_exists` |
| E9. Before an add: fewer than 256 entries, and fewer than 65536 ids in `removed`. | `allow_list_full` |
| E10. `expires_at`, when set, is later than the device clock. | `entry_invalid` |

### 11.5 Who changes it

- **By hand** in the key account with `agent device allow add|remove|confirm` (§18.6–§18.8). This
  is the only way to set the first entries. The owner confirms each key by giving its full
  fingerprint with `--fingerprint`; the command shows the key id, kind, handle, principal and
  fingerprint from the keys-file copy before it writes. Keys the hub delivers in the copy are
  only proposals until this confirmation.
- **By statement** signed by a controller device key of the owner that is already in the file
  with `allow_list.change` (§12).
- **Never** by a session key, a passkey, a program key, a grant, the keys-file copy, the hub, the
  website, the API, or anything that reaches the socket. The website and the API only create a
  change **request** at the hub (§12.4); a controller signs it with `agent device sign --request`
  (§18.9).
- An administrator who edits the file directly makes a by-hand change. The guard detects it by
  the SHA-256 it keeps of the file it last wrote (`allow_list_hash`; a `null` value counts as a
  difference), checks the file, writes a device event `allow_list.changed_outside_client`
  (§17.3) and uses the file if it is valid. An edit that marks an entry of a revoked key id
  `active` changes nothing: C7, S7 and E2 refuse that key id.

## 12. Statements: signed changes of the allow list

### 12.1 Form

```json
{
  "format": "agent-statement-v1",
  "statement_id": "<uuid>",
  "action": "add",
  "target_device": "device-example-device",
  "signer": "device-example-laptop",
  "sequence": 42,
  "signed_at": "2026-12-01T09:14:03Z",
  "entry_id": "<uuid>",
  "entry": { "type": "…", "key_id": "…", "kind": "…", "alg": null, "public_key": "…", "principal": "…", "handle": "…", "permissions": ["…"], "repositories": ["…"], "expires_at": "…" },
  "signature": "<base64url, 64 bytes>"
}
```

| Member | Rule |
|---|---|
| `action` | `add`, `change`, `remove` or `confirm` |
| `entry` | for `add` and `change`: the entry fields of §11.2 from `type` to `expires_at`; for `remove` and `confirm`: `null` |
| `sequence` | integer 1 to 2^53−1; one counter per signing key over all its statements and commands |
| `signature` | Ed25519 by `signer` over the signed document text with tag `agent-statement-v1` |
| size | at most 16384 bytes |

There is no `certificate` member: a statement is never signed by a session key.

A controller keeps its counter in `state.json` member `own_sequence` (integer, starts at 0; the
next statement uses the value plus 1). The new value and a `signed` line in the decision log
(§17.1) are written before the statement leaves the device; a reset takes `own_sequence` from
those lines (§4.4).

### 12.2 Transport

- Normal path: the controller sends the statement with a signed `POST /api/device/statements`;
  the target's guard fetches `GET /api/device/statements?device=<own key id>&after=<statement_cursor>`
  every 60 s **[hub key protocol]**. The verified 200 answer is
  `{"statements": [{"cursor": <integer>, "statement": <statement>}, …]}` with at most 100 items
  in rising cursor order; the cursor is an integer from 1 that the hub assigns per target device
  and that grows with every statement it stores for that device. The guard runs §12.3 on each
  statement and then sets `statement_cursor` to its cursor, in the same locked write. The hub
  verifies, stores and delivers; it changes nothing.
- Offline path: `agent device sign --out FILE` on the controller, `agent device apply --file
  FILE` on the target, both in the key account.

### 12.3 Checks, in this order

The first failing check decides. Each check runs with the device clock read once at the start.

| # | Check | Refusal |
|---|---|---|
| S1 | Parses as §12.1 with every limit; no `certificate` member (a member `certificate` gives `session_key_not_allowed`) | `malformed`, `session_key_not_allowed` |
| S2 | `target_device` is this device key id | `wrong_target` |
| S3 | The allow list is valid | `allow_list_invalid` |
| S4 | `statement_id` not in `seen`; for `add`, `change` and `confirm`, `entry_id` not in `removed` (a `remove` of an id already in `removed` is accepted and changes only the replay state) | `replayed`, `entry_removed` |
| S5 | `signer` has an entry, `status` `active`, not expired | `not_in_allow_list`, `entry_inactive`, `entry_expired` |
| S6 | That entry is `type` `entry`, principal `device`, handle the owner, with `allow_list.change` (a program key, a passkey or a grant fails here) | `not_a_controller_entry` |
| S7 | Keys-file copy and revocations. For `add`, `change` and `confirm`: the copy is fresh and `revocations_incomplete` is not set; the signer is in the copy, not marked revoked, with the same public key and `device:controller`; no applied notice names its key id. For `remove`: when the copy is valid, fresh or not, the same signer checks against it; when the copy is absent or invalid, only the check that no applied notice names its key id | `keys_file_stale`, `revocations_incomplete`, `unknown_key`, `key_revoked`, `key_mismatch`, `not_a_controller_key` |
| S8 | The signature verifies with the entry's public key | `signature_invalid` |
| S9 | Replay: `sequence` higher than the member of `sequences` for the signer's key id and fingerprint; `signed_at` within `statement_window_seconds` of the device clock, either direction; for an action other than `remove`, when `reset_at` is set, `signed_at` later than `reset_at` plus 604800 s (§4.4); the members that acceptance adds fit the limits of §4.3 | `sequence_not_higher`, `outside_time_window`, `signed_before_reset`, `replay_state_full` |
| S10 | `add`: the entry meets §11.4 (E1 to E10). `change`: an entry with `entry_id` exists; it is `type` `entry` and the new `entry.type` is `entry` (a grant is never changed and never created by a change); its `status` is `active`; the new fields meet §11.4. `remove`: none. `confirm`: an entry with `entry_id` exists with `status` `suspended`, and its key meets E1, E2 and E3 | the value of the failed rule, `entry_unknown`, `grant_unchangeable`, `entry_inactive`, `entry_not_suspended` |
| S11 | The decision line is written (§17.1) | `log_write_failed` |

On acceptance, in one locked write: the action is applied, `sequences` and `seen` are updated,
and a device event is written (§17.3):

- `add` writes the entry with `status` `active` and `signer` this statement;
- `change` replaces the fields from `type` to `expires_at`, keeps `status` `active` and sets
  `signer` to this statement, so that a later revocation of the changing controller suspends the
  changed entry;
- `remove` removes the entry (§11.2, Removal);
- `confirm` sets `status` `active` and `signer` to this statement. It never applies to a `revoked`
  or `removed` entry (§9, §16).

A refusal changes no state except that its `statement_id` is recorded in `seen` (§17.2); it is
recorded and answered with a signed refusal (§17.2).

### 12.4 Change requests **[hub key protocol]**

The website and the API create change requests; the hub stores them and changes nothing. A request
is a proposal: it takes effect only as a statement that the owner signs on a controller device
(DESIGN.md §25.6).

```json
{"request_id": "<uuid>", "target_device": "device-example-device", "action": "add", "entry_id": null,
 "entry": { "type": "…", "key_id": "…", "kind": "…", "alg": null, "public_key": "…", "principal": "…", "handle": "…", "permissions": ["…"], "repositories": ["…"], "expires_at": "…" },
 "requested_by": "<key id>", "requested_at": "2026-12-01T09:14:03Z"}
```

| Member | Rule |
|---|---|
| `action` | as §12.1 |
| `entry_id` | `null` for `add` (the signing controller creates a new UUID); for `change`, `remove` and `confirm` the UUID of the target entry |
| `entry` | as §12.1 |
| `requested_by` | the key id that created the request (rule K1) |

- `GET /api/device/requests`, signed with the controller's device key: the verified 200 answer is
  `{"requests": [<request>, …]}` with at most 256 open requests, oldest first, only for target
  devices whose device key has the same handle as the requesting key in the keys file.
- A request parses strictly (§3). A request that does not parse is not listed and is shown as
  `agent: change request <request_id> is not valid` when it is named with `--request`.
- After `agent device sign --request` has sent the statement, the hub marks the request closed
  when it stores the statement.

## 13. Grants

### 13.1 What a grant is

A grant is an allow-list entry of `type` `grant`: a permission that the owner of this device
signed for a key of another staff member (DESIGN.md §25.6). It is written by an `add` statement
of a controller device key of the owner, or by hand on the device.

### 13.2 Limits

| Rule | Value |
|---|---|
| G1. Grantee key | a passkey (`staff`), a controller device key, or a program key of a controller device, of a handle other than the owner |
| G2. Permissions | only `order.submit`, `question.answer`, `session.input`, `session.start`, `session.stop` |
| G3. Repositories | as §11.2 |
| G4. `expires_at` | required; at least 60 s and at most 2592000 s after `signed_at` of the statement (by hand: after the device clock) |
| G5. Number | at most one grant per key id (E8) |
| G6. Change | never; a wider or longer grant is a `remove` and a new `add` |

### 13.3 Signed form

A grant is the `add` statement of §12.1 with `entry.type` `grant`. Its removal is the `remove`
statement naming its `entry_id`. No other form exists: a `change` never sets `type` `grant`
(S10).

### 13.4 Who may sign

Only a controller device key of the owner with an active `allow_list.change` entry on the target
device (S5 to S7). A passkey, a session key, a program key, a grant holder and the hub cannot.

### 13.5 Expiry and revocation

- At `expires_at` the grant stops counting (`grant_expired`). The guard removes expired grants
  once a minute as §11.2 (Removal) states, with a device event.
- Earlier: a `remove` statement or `agent device allow remove` by hand.
- When the signing controller key is revoked, the grant is `suspended` (§9).
- When the grantee key is revoked, the grant is `revoked` (§9), for good.

### 13.6 Checking a command under a grant, and no signature for it

A command whose signer's entry is a grant passes the checks of §14 with these additions:
`grant_expired` instead of `entry_expired`, and C14 (own AI session). When it is accepted, every
session id it names (the target session of `session.*` and `question.*` commands, and the
session that an accepted `order.submit` starts, from step 4) is added to `tainted_sessions` with
the grant's entry id. A tainted session stays tainted for its life.

**An order or input received under a grant never produces a signature request.** In these two
steps the socket has no signing operation, so no session can obtain a signature at all (§19.3).
The signing service of step 4 must refuse every request from a session in `tainted_sessions`,
from a session that started before `tainted_before` (§4.4), from a session started by any order,
and from a session that is not the owner's own AI session, and it never signs a statement
(DESIGN.md §25.6).

## 14. Commands and the checks before acting

### 14.1 Envelope

The verifier checks this envelope. The content of each kind (order body, answer, typed text) is
defined in step 4; the envelope carries only its hash (DESIGN.md §25.6).

```json
{
  "format": "agent-command-v1",
  "command_id": "<uuid>",
  "kind": "order.submit",
  "target_device": "device-example-device",
  "signer": "<key id>",
  "sequence": 7,
  "signed_at": "2026-12-01T09:14:03Z",
  "repository": "example/repo",
  "session_id": null,
  "content_hash": "<64 hex>",
  "certificate": null,
  "signature": "<base64url>"
}
```

| Member | Rule |
|---|---|
| `kind` | `order.submit`, `question.answer`, `question.withdraw`, `session.input`, `session.start`, `session.stop` |
| `signer` | key id of the signing key; for a staff member, the key id of the passkey that certified the session key |
| `repository` | for `order.submit`: `OWNER/NAME` (K6), required; for every other kind: `null`. The repository of a command that names a session is the target session's repository from the device's own store (C14), never a value the sender fills in |
| `session_id` | for `order.submit`: `null`; for every other kind: a session id, required |
| `content_hash` | `sha256_hex(random value || content)` or `null`; required for `order.submit`, `question.answer`, `session.input` |
| `certificate` | `null` for a device or program key; the object of §15.2 for a session key |
| `signature` | Ed25519 (64 bytes) by the signer, or P-256 by the session key (§15.4), over the signed document text with tag `agent-command-v1` |
| size | at most 65536 bytes |

Step 4 refuses an `order.submit` whose order body names another repository than the envelope's
`repository`.

### 14.2 Delivery record

A command reaches the verifier inside the hub answer it came in. The **delivery record** keeps
that answer: the members of `keys-file.json` (§8.3) with format `agent-delivery-v1` and an added
`index`, the position of the command in the body's `commands` list. The body of an answer that
carries commands holds envelopes only; content travels in a separate answer that is not part of
the delivery record (step 4) **[hub key protocol]**, so the decision log holds no content
(§17.1). In these two steps a delivery record is produced by tests and read by
`agent device verify --file`; step 4 adds the fetch.

### 14.3 The checks, in this order

The first failing check decides. Each check runs with the device clock read once at the start.

| # | Check | Refusal |
|---|---|---|
| C1 | The delivery record verifies with the pinned hub key (§8.3 rule, without the time window) and `index` names a command | `delivery_not_verified` |
| C2 | The command parses as §14.1 with every limit, including the rules for `repository` and `session_id` per kind | `malformed` |
| C3 | `target_device` is this device key id | `wrong_target` |
| C4 | The allow list is valid | `allow_list_invalid` |
| C5 | The keys-file copy is fresh, and `revocations_incomplete` is not set | `keys_file_stale`, `revocations_incomplete` |
| C6 | `signer` is in the copy with a usable entry (§7) whose principal and kind are `staff` with `webauthn`, or `device` or `program` with `ed25519`; any other principal or combination counts as unknown | `unknown_key` |
| C7 | The copy does not mark it revoked and no applied notice names its key id | `key_revoked` |
| C8 | The allow list has an entry for `signer` with the same public key as the copy | `not_in_allow_list`, `key_mismatch` |
| C9 | The entry has `status` `active` and has not expired | `entry_inactive`, `entry_expired` or for a grant `grant_expired` |
| C10 | Level: a `device` signer has `device:controller`; a `program` signer's device key (same device label and handle in the copy) has `device:controller`, is not marked revoked in the copy, and no applied notice names it | `executor_key`, `key_revoked` |
| C11 | Certificate: a `staff` signer has a `certificate`, and the certificate passes §15.3; a `device` or `program` signer has `certificate` `null` | `certificate_missing`, `certificate_unexpected`, the value of §15.3 |
| C12 | The signature verifies: with the entry's Ed25519 key, or with the certified session key | `signature_invalid` |
| C13 | Replay: `command_id` not in `seen`; `sequence` higher than the member of `sequences` for the signer, or of `session_sequences` for a session key; `signed_at` within `command_window_seconds` of the device clock, either direction; when `reset_at` is set, `signed_at` later than `reset_at` plus 3600 s and, for a session key, the certificate expiry later than `reset_at` plus 43200 s (§4.4); the members that acceptance adds fit the limits of §4.3 | `replayed`, `sequence_not_higher`, `outside_time_window`, `signed_before_reset`, `replay_state_full` |
| C14 | Target and permission, in this order: for a kind that names a session, the device knows the target session; `kind` in the entry's `permissions`; the command's repository is in the entry's `repositories` or the entry has `["*"]`, where the command's repository is the envelope's `repository` for `order.submit` and the target session's repository from the device's own store for every other kind, and a target session without a repository passes only `["*"]`; under a grant, `question.answer` and `session.input` do not target the owner's own AI session | `session_unknown`, `permission_denied`, `repository_denied`, `own_ai_session_under_grant` |
| C15 | The decision line with the command and its proofs is written (§17.1) | `log_write_failed` |

On acceptance, in one locked write: `sequences` or `session_sequences`, `seen`, `fresh_values`
and `tainted_sessions` are updated, then the decision is returned. Only then may step 4 act on
it. A refusal changes no state except that its `command_id` is recorded in `seen` (§17.2).

For C14 the caller passes the origin of the target session, `owner_ai` (an interactive AI session
of the owner on this device) or `other`, and its repository, `OWNER/NAME` or `none`, as the
device's own store holds them; the sender never supplies them. In these two steps
`agent device verify --session-origin --session-repository` supplies them, and a command that
names a session without both flags is refused with `session_unknown`. Step 4 derives both from
the device's store and refuses a session the store does not hold with `session_unknown`.

## 15. The session key's certificate

### 15.1 Requirements on the hub's passkey specification

| No. | Requirement **[passkey specification]** |
|---|---|
| P1 | The certificate consists of four byte strings: the **statement**, the WebAuthn `authenticatorData`, the `clientDataJSON` and the assertion `signature`. |
| P2 | The statement is UTF-8 text of exactly seven lines joined by one line feed, no trailing line feed: `agent-session-key-v1`, the website origin, the passkey key id (K1), the session key algorithm `p256`, the session public key (base64url of the 65-byte uncompressed SEC1 point), the expiry (time), the fresh value (base64url of 32 bytes the hub created). At most 1024 bytes. |
| P3 | The WebAuthn challenge is the 32 bytes `SHA-256(statement)`; `clientDataJSON.challenge` is their base64url. |
| P4 | The assertion is a `webauthn.get` for the relying-party id equal to the host of the website origin, with user verification required. |
| P5 | The session key is ECDSA P-256 with SHA-256, created by the browser as not extractable; its signatures are the 64-byte `r || s` form that WebCrypto returns. |
| P6 | The expiry is at most 43200 s after the hub issued the fresh value (the hub's longest session-key lifetime, DESIGN.md §24.3). |
| P7 | Passkey public keys in the keys file have the form H3 and H4. |
| P8 | Limits: `authenticatorData` at most 1024 bytes, `clientDataJSON` at most 4096 bytes, assertion signature at most 512 bytes. |
| P9 | A session key signs commands only in the envelope of §14.1; it never signs a statement. |

### 15.2 Form in a command

`"certificate": {"statement": "<b64url>", "authenticator_data": "<b64url>", "client_data_json": "<b64url>", "signature": "<b64url>"}`.

### 15.3 Verification, offline, in this order

All values come from the command, the allow-list entry and the keys-file copy; nothing is asked
of the hub.

| # | Check | Refusal |
|---|---|---|
| V1 | The four values decode and keep P2 and P8; the statement has seven lines of the stated forms | `certificate_invalid` |
| V2 | Statement line 2 equals `website_origin`; line 3 equals the command's `signer`; line 4 is `p256`; line 5 is a valid P-256 point | `certificate_invalid` |
| V3 | The passkey public key from the keys-file copy (H3, H4) equals the allow-list entry's | `key_mismatch` |
| V4 | `clientDataJSON` is a UTF-8 JSON object with `type` = `webauthn.get`, `challenge` = base64url of `SHA-256(statement)`, `origin` = `website_origin`, `crossOrigin` absent or `false`, `topOrigin` absent; other members are ignored | `certificate_invalid` |
| V5 | `authenticatorData` has at least 37 bytes; bytes 0–31 equal `SHA-256` of the relying-party id; flag bit 0 (user present) and bit 2 (user verified) are set. The signature counter is not checked | `certificate_invalid`, `user_not_verified` |
| V6 | The assertion signature verifies with the passkey public key over `authenticatorData || SHA-256(clientDataJSON)`: ES256 as DER, EdDSA as 64 bytes, RS256 as PKCS #1 v1.5 with SHA-256 | `certificate_signature_invalid` |
| V7 | Expiry later than the device clock | `session_key_expired` |
| V8 | Expiry not later than the device clock plus `session_key_max_lifetime_seconds` | `session_key_lifetime_exceeded` |
| V9 | The fresh value is not in `fresh_values` with a different `statement_hash` | `certificate_fresh_value_reused` |

The device cannot know when a certificate was signed (DESIGN.md §25.6); V8 is the only bound on
its lifetime.

### 15.4 The session key's signature

C12 for a session key: decode statement line 5 as a P-256 point, convert the 64-byte `r || s`
signature to DER (`cryptography.hazmat.primitives.asymmetric.utils.encode_dss_signature`) and
verify ECDSA with SHA-256 over the signed document text. Replay state (C13) uses the member of
`session_sequences` named by the fingerprint of the session public key; on acceptance its
`expires_at` becomes the later of the stored value and the certificate's expiry.

## 16. Obey less, never more

| Source | May take away | May never |
|---|---|---|
| A revocation notice of the pinned hub key, or a revoked mark in the keys-file copy | mark the entries of the key id `revoked` for good; for a device key also the entries of its program keys; suspend every entry and grant the key signed | add, widen or re-activate an entry |
| The keys-file copy | refuse a key that is absent, revoked or of the wrong level | add an entry; drop a revoked mark (§8.3) |
| A hub that is unreachable or answers wrongly | — (the copy ages; after `keys_file_max_age_seconds` every command is refused) | lead to accepting anything |
| A `remove` statement | remove an entry, also with a stale, absent or invalid copy (S7) and while `revocations_incomplete` is set | — |
| `add`, `change`, `confirm` statements, by-hand adds and by-hand confirms | only with a fresh copy and no known missing revocation (E1) | bypass E1 to E10; re-activate a `revoked` or `removed` entry |

- **A revoked key id never works again on any device** (§9). An entry marked `revoked` stays in
  the file until the owner removes it (DESIGN.md §25.6), and `confirm` never applies to it.
- **A false notice is undone only by a new key.** The owner creates a new key with a new key id
  (a new passkey through an invite, a new program key with `agent device program-key`, or for a
  device key a new connection of that device with a new device label, DESIGN.md §25.15), adds it
  to the keys file by pull request, and adds a new allow-list entry for it on each device, by hand
  or by a statement of another controller key. The entries that a falsely
  revoked controller key signed are only `suspended`; the owner makes them active again with a
  `confirm` statement of another controller key, or by hand with `agent device allow confirm`,
  which requires that the entry's own key is not revoked (S10).
- A statement that the hub held back and delivers late is refused when its `entry_id` was
  removed meanwhile (S4), when its sequence is not higher (S9), or when it is outside the window
  (S9).

## 17. Local log, refusals and team-record events

### 17.1 Decision log

Every decision on a command or statement, accepted or refused, every by-hand change, every
statement a controller signs, and every reset appends one line to `log/decisions.jsonl` before it
takes effect:

`{"at": "<time>", "document": "command" | "statement" | "hand" | "signed" | "reset", "id": "<uuid or null>", "signer": "<key id or null>", "sequence": <int or null>, "decision": "accepted" | "refused" | "done", "refusal": "<value or null>", "record": <the delivery record, the statement, the by-hand change, the signed statement, or the new state.json>}`

`signed` and `reset` lines have `decision` `done`. The line holds hashes of content only, never
content (§14.2). If the line cannot be written the operation is refused with `log_write_failed`
and nothing changes. The file is append-only for the client; it is never truncated or rotated by
the client. Socket requests are never written to it (§19.3).

### 17.2 Signed refusal

Every refusal of a command or statement that parsed far enough to have an id (from C3 or S2 on)
records that id in `seen` (§4.3) in the same locked write as its decision line, so that the
refused document can never be accepted later, and is answered with a refusal document signed by
the device key:

`{"format": "agent-refusal-v1", "device_key_id": "…", "document": "command" | "statement", "id": "<command or statement id>", "signer": "<key id>", "sequence": <int>, "document_hash": "<64 hex>", "refusal": "<value>", "refused_at": "<time>", "signature": "<base64url>"}`

- `document_hash` is `sha256_hex` of the canonical JSON (§3) of the document as received, with
  every member, `signature` and `certificate` included. `id`, `signer` and `sequence` are copied
  from the received document, whose signature may not have been checked; they prove nothing about
  what the sender signed. Only `document_hash` ties the refusal to the sender's bytes.
- The signature is Ed25519 by the device key over the signed document text with tag
  `agent-refusal-v1`.
- No signed refusal is sent, and the decision stays in the log only, when the id cannot be
  recorded in `seen` because it is full, when the decision line cannot be written
  (`log_write_failed`), or when the refusal is `signed_before_reset` (the device cannot know
  whether it accepted that document before the reset).
- **When a sender signs again.** A sender signs a document again, with a new id and a new
  sequence, only after a refusal that verifies with the target's device key from the sender's
  fresh keys-file copy, whose `document_hash` equals the hash of the document it sent, and whose
  value is not `replayed` (a `replayed` refusal can follow an acceptance). A statement of the hub
  alone is no reason to send again (DESIGN.md §25.6). In these two steps no client sends again by
  itself; the rule binds the owner and the senders of step 4.

In these two steps statement refusals are appended to `log/refusals.jsonl` in the write of their
decision and sent in that order with `POST /api/device/refusals` **[hub key protocol]**;
`refusals_sent` counts the lines the hub has accepted, and the rest are retried every 60 s. The
hub answers 200 for a refusal it already holds with the same `device_key_id`, `id` and
`document_hash` **[hub key protocol]**. `agent device apply` prints the refusal. Command refusals
are printed by `agent device verify` only, which writes nothing and records no id.

### 17.3 Device events for the team record

The guard writes a signed event for every change of the allow list that it makes or detects
(DESIGN.md §25.9): `object_type` `device_allow_list`, `object_id` the device key id, signed with
the device key in the event form and chain of DESIGN.md §24.3. Its `changes` hold the member
`action` with old value `null` and the event value as new value, and one member per changed entry
id holding the old and the new entry (or `null`). Values: `allow_list.added`,
`allow_list.changed`, `allow_list.removed`, `allow_list.confirmed`, `allow_list.revoked`,
`allow_list.suspended`, `allow_list.expired`, `allow_list.changed_outside_client`.

Changes of the guard's own state are events with `object_type` `device_guard`, `object_id` the
device key id, and `changes` holding `action` as above: `guard.state_reset` (§4.4) and
`guard.hub_key_replaced` (§18.4).

Each event is appended to `log/events.jsonl` first and uploaded to the team record by the guard
through the hub's event append operation; an event the hub has not accepted stays in the file
with its state and is retried every 60 s. Allow-list entries hold key ids, handles, permissions,
repositories and times only, so the events hold no personal data and no secret.

A controller that signs a grant or its removal also writes the team-record event of DESIGN.md
§25.6 with its own key, `object_type` `device_grant`, when it sends the statement.

## 18. Client commands

All commands follow `agent key`: flags are given once each, unknown flags stop with the usage
line, errors print `agent: <message>` and exit 1 (`die`), a private key never appears in output.
Usage line:

`agent device init … | init --reset-state | entry [--controller] | program-key … | pin-hub … | refresh | show [--json] | allow list [--json] | allow add … | allow remove … | allow confirm … | sign … | requests | apply --file PATH | verify --file PATH … | serve [--install|--uninstall] | report [--once|--follow] | status`

Key account: §18.1 to §18.13. Session account: §18.14 and §18.15.

| § | Command | Does | Output and exit |
|---|---|---|---|
| 18.1 | `agent device init --hub URL --website-origin URL --owner HANDLE --key-id ID --session-account NAME --socket PATH [--controller]` | runs only as §4.1 states; checks every value, creates the files of §4.2 under `$AGENT_KEY_HOME` (empty allow list, empty `revocations.json`, `state.json` with zero serials, `revocations_incomplete` `true`, `null` times and empty maps), creates the device key; refuses when `guard.json` or the key exists | the entry of §8.1; 0 |
| 18.1 | `agent device init --reset-state` | the reset of §4.4 | `state reset at TIME`; 0 |
| 18.2 | `agent device entry [--controller]` | prints the entry of the existing device key again; `scopes` follow `--controller` as for `init`, since the level is held by the keys file, not on the device | entry; 0 |
| 18.3 | `agent device program-key --id ID --label LABEL` | creates a program key (§6.1) | its entry with `principal` `program`; 0 |
| 18.4 | `agent device pin-hub --id HUB_ID --public-key VALUE [--replace]` | `keys.pin_hub_key` with `$AGENT_KEY_HOME`. With `--replace` (after a hub takeover, DESIGN.md §25.6) it also, in one locked write: deletes `keys-file.json`, which was verified with the old key; sets `keys_file_serial` and `revocation_serial` to 0 and `revocations_incomplete` to `true`, so that the fetches under the new key start over (copies and notices signed by the old key no longer verify, so no rollback becomes possible); keeps `revocations.json`, every entry with its status and every other member of `state.json`; writes the device event `guard.hub_key_replaced` | `pinned hub key HUB_ID`; 0 |
| 18.5 | `agent device refresh` | one fetch of keys file, revocations and statements | `keys file serial N, age S s; revocations up to N` followed by `, incomplete` while `revocations_incomplete` is set; `statements applied N, refused N`; 0, or 1 when the hub is unreachable or not verified |
| 18.6 | `agent device allow add --key-id ID --fingerprint HEX --permissions P[,P…] --repos R[,R…]\|* [--expires TIME]` | by-hand add of §11.5; E1 to E10; the type follows E5 | `added <entry id> <type> <key id>`; 0 |
| 18.7 | `agent device allow remove --entry UUID` | by-hand remove (§11.2, Removal); needs no keys-file copy | `removed <entry id>`; 0 |
| 18.8 | `agent device allow confirm --entry UUID --fingerprint HEX` | re-activates a `suspended` entry whose key meets E1, E2 and E3, as S10 `confirm`; refuses a `revoked` or `removed` entry with `entry_not_suspended` | `confirmed <entry id>`; 0 |
| — | `agent device allow list [--json]` | the entries: id, type, key id, handle, permissions, repositories, expiry, status, signer | one line each, or the JSON array; 0 |
| 18.9 | `agent device sign --target DEVICE_KEY_ID --action add\|change\|remove\|confirm [--entry UUID] [--key-id ID --fingerprint HEX --permissions … --repos … [--expires TIME]] [--request ID] [--out FILE]` | on a controller: builds, shows and signs a statement, appends the `signed` line (§12.1), and sends it (§12.2) or writes it to FILE. With `--request ID` the target, action, entry id and entry come from the change request of §12.4, and only `--fingerprint` and `--out` may be given; `--fingerprint` is required for `add` and must equal the copy's fingerprint of the entry's key | `signed <statement id> sequence N` and `sent` or `written FILE`; 0 |
| 18.10 | `agent device requests` | lists the open change requests of §12.4 | one line each: request id, target, action, key id, requested by, requested at; 0 |
| 18.11 | `agent device apply --file PATH` | runs §12.3 on one statement | `applied <statement id>`, exit 0; or `refused: <value>` on standard error, exit 3 |
| 18.12 | `agent device verify --file PATH [--session-origin owner_ai\|other --session-repository OWNER/NAME\|none]` | runs §14.3 on one delivery record without writing anything (C15 is skipped and reported as `not written`); the two flags are given together, only for a command that names a session (§14.3) | `accepted` exit 0; `refused: <value>` on standard error, exit 3 |
| 18.13 | `agent device serve [--install\|--uninstall]` | runs the guard in the foreground, or installs or removes it as a user service of the key account (§19.1) | 0 on a signal; 1 on a start error |
| 18.14 | `agent device report [--once\|--follow]` | §10.3. `--once` sends one report. `--follow` keeps running: a socket or hub error is printed on standard error and retried at the next interval | `--once`: `inventory sent` or `inventory unchanged`, 0, or 1 on error; `--follow`: 0 on a signal, 1 only on a start error (`$AGENT_HOME/guard.json` missing or invalid) |
| 18.15 | `agent device status` | op `status` over the socket | device key id, level, copy age, entry and grant counts, `revocations_incomplete` when set, the number of socket requests refused since the guard started; 0, 1 when the socket is unreachable |

Messages that the implementation must use exactly: `AGENT_KEY_HOME is not set`, `this command runs
only in the key account`, `$AGENT_KEY_HOME must be absent or an empty directory of the key
account`, `state.json is missing`, `state.json is invalid`, `state.json is valid; nothing was
reset`, `log/decisions.jsonl is not readable; nothing was reset`, `this device is not a
controller`, `key kind p256 is not supported by this client`, `fingerprint does not match the keys
file copy; nothing was written`, `the keys file copy is not fresh; run agent device refresh`,
`change request <request_id> is not valid`, `the guard socket is not reachable`.

`agent cli-bridge` does not forward `device` (it is not in its allow list, DESIGN.md §17), and a
test asserts it.

## 19. The guard service, the socket and the daemon

### 19.1 The guard (`agent device serve`, key account)

At start it checks §4 and §5, loads and verifies every file, and stops with the message of the
first failure. It then runs, each step under the lock:

| Every | Step |
|---|---|
| 60 s | fetch revocation notices (§9) and statements (§12.2); remove expired grants (§13.5); retry unsent events (§17.3) and unsent refusals of `log/refusals.jsonl` (§17.2) |
| 600 s | fetch the keys file (§8.3) |

Socket requests are handled apart from these steps (§19.3).

A failed fetch is logged to standard error with its reason and retried at the next interval; it
never stops the guard. The service unit reuses `daemon.service_unit_text` with the label
`swiss.dfx.agent.guard`, the argv `agent device serve`, no `AGENT_HOME`, and `AGENT_KEY_HOME`
passed through `extra_env` (§22); `--install` stops with `AGENT_KEY_HOME is not set` when the
variable is missing.

### 19.2 The session daemon (`agent daemon`, session account)

`daemon.child_specs` gains the child `device-report` = `agent device report --follow`. It starts
only while `$AGENT_HOME/guard.json` exists. It has its own restart counter, separate from `sync`:
after 10 deaths within 60 s the supervisor stops restarting `device-report`, prints `daemon
device-report restart limit` on standard error, and keeps running every other child; the next
start of the daemon starts it again. A death of `device-report` never ends the supervisor. Without
the file the daemon behaves as today.

### 19.3 The socket

The guard handles the socket in its own thread, apart from the steps of §19.1, so that no
connection delays a fetch. At most 8 connections are handled at once; a further connection is
closed without an answer. One request per connection: one JSON object of at most 65536 bytes
followed by a line feed, received within 5 s of the connection; one JSON object answer followed by
a line feed; then the guard closes. A connection that has not sent its line within 5 s is closed
without an answer. The `inventory` operation sends its hub request with a timeout of 30 s. Access
is by file mode and group (§4.2): the socket has mode `0660` and the group of its set-group-id
directory; the guard stops at start when the socket's group differs from the directory's.
Operations, closed list:

| `op` | Request | Answer |
|---|---|---|
| `status` | `{"op": "status"}` | the values of §18.15 |
| `inventory` | `{"op": "inventory", "inventory": {…}}` | §10.3 |

Every other request, including every request to sign anything, change the allow list, add a
grant or read a key, is answered `{"error": "unknown_operation"}`. A request larger than the limit
or not JSON is answered `{"error": "malformed"}`. Socket refusals are counted in memory and shown
by `status`; they are never written to `log/decisions.jsonl`, so that the session account cannot
fill the disk and block decisions.

## 20. Tests

The rule [Complete test coverage is mandatory](../CONTRIBUTING.md#complete-test-coverage-is-mandatory)
applies: every line and branch of every new or changed function, through the real code path. The
guard keeps no state in PostgreSQL, so its tests run with the `no_pg` marker on a temporary
`$AGENT_KEY_HOME`; the tests of `agent device report`, the daemon and the CLI use the real store as
the existing tests do. The hub is an `httpx.MockTransport` that signs its answers with a test hub
key; every answer form is exercised: 200 verified, each refusal status with an `error` value,
unsigned, wrong hub key, bad signature, outside the window, oversized, `Content-Encoding`,
redirect, transport error. Passkey assertions are produced in the tests with keys the tests
generate for ES256, EdDSA and RS256. The clock is injected. Fixtures use `example-device`,
`example-staff`, `example-colleague` and `hub.example` and hold no secret.

### 20.1 Refusal paths of DESIGN.md §25.21 in these steps

| §25.21 path | Test |
|---|---|
| an unknown key | `test_command_from_key_missing_in_keys_file_copy_is_refused_unknown_key`, `test_statement_signer_missing_in_copy_is_refused_unknown_key` |
| a key outside the allow list | `test_command_from_registered_key_not_in_allow_list_is_refused` |
| an expired session key | `test_certificate_with_past_expiry_is_refused_session_key_expired` |
| an expired or removed grant | `test_command_under_expired_grant_is_refused_grant_expired`, `test_command_under_removed_grant_is_refused_not_in_allow_list`, `test_expired_grants_are_deleted_and_tombstoned` |
| an entry signed by a revoked controller key | `test_revoked_controller_notice_suspends_entries_it_signed`, `test_revoked_controller_notice_revokes_its_program_key_entries`, `test_command_under_suspended_entry_is_refused_entry_inactive`, `test_confirm_by_other_controller_reactivates_suspended_entry` |
| a signature request from an order to the signing service | `test_socket_refuses_sign_operation` (no signing service exists in these steps; step 4 adds the full test) |
| a signature request from a session that received input under a grant | `test_command_accepted_under_grant_taints_its_session` (the record the signing service of step 4 refuses on) |
| a replayed allow-list statement or grant | `test_replayed_statement_id_is_refused`, `test_statement_with_lower_sequence_is_refused`, `test_late_statement_for_removed_entry_is_refused_entry_removed`, `test_replayed_grant_add_is_refused`, `test_statement_signed_before_reset_is_refused` |
| an allow-list statement or grant signed by a program key or requested through the signing service | `test_statement_signed_by_program_key_is_refused_not_a_controller_entry`, `test_grant_signed_by_program_key_is_refused`, `test_socket_refuses_allow_list_change_operation` |
| a replayed command | `test_replayed_command_id_is_refused`, `test_command_sequence_not_higher_is_refused`, `test_command_outside_time_window_is_refused`, `test_command_signed_before_reset_is_refused`, `test_refused_command_id_is_never_accepted_later` |
| a command outside its permission | `test_command_kind_outside_permissions_is_refused`, `test_command_repository_outside_entry_is_refused`, `test_command_into_session_of_other_repository_is_refused_repository_denied`, `test_command_on_session_without_repository_needs_wildcard`, `test_grant_input_into_owner_ai_session_is_refused` |
| a change of an allow list signed by a session key | `test_statement_with_certificate_is_refused_session_key_not_allowed`, `test_statement_signed_by_passkey_entry_is_refused_not_a_controller_entry` |
| a session-key expiry beyond the device's limit | `test_certificate_expiry_beyond_device_limit_is_refused` |

Not in these steps: maskings (step 5), the signing service itself (step 4), and a team-record
event signed by an expired session key (checked by the hub, agent-core).

### 20.2 Every other refusal and rule

Each refusal value that this document names, in a check table or in a rule, has its own test for
each kind of document it refuses, named `test_<document>_refused_<value>` with `<document>` one
of `command`, `statement`, `hand`, `keys_file` and `revocation`. Examples:
`test_command_refused_delivery_not_verified`, `test_command_refused_wrong_target`,
`test_command_refused_keys_file_stale`, `test_command_refused_revocations_incomplete`,
`test_command_refused_executor_key`, `test_command_refused_certificate_missing`,
`test_command_refused_certificate_unexpected`, `test_command_refused_user_not_verified`,
`test_command_refused_certificate_signature_invalid`,
`test_command_refused_certificate_fresh_value_reused`, `test_command_refused_signed_before_reset`,
`test_command_refused_replay_state_full`, `test_command_refused_session_unknown`,
`test_command_refused_log_write_failed`, `test_statement_refused_entry_exists`,
`test_statement_refused_allow_list_full`, `test_statement_refused_own_key`,
`test_statement_refused_grant_unchangeable`, `test_statement_refused_grant_invalid`,
`test_statement_refused_entry_inactive`, `test_statement_refused_entry_not_suspended`,
`test_statement_refused_revocations_incomplete`, `test_statement_refused_signed_before_reset`,
`test_statement_refused_replay_state_full`, `test_hand_refused_revocations_incomplete`,
`test_hand_refused_entry_not_suspended`, `test_keys_file_refused_keys_file_rollback`,
`test_keys_file_refused_keys_file_conflict`, `test_keys_file_refused_keys_file_invalid`,
`test_revocation_refused_revocation_invalid`, `test_revocation_refused_revocation_gap`. In
addition:

- `test_check_order_reports_first_failure` (a command failing C3 and C12 reports `wrong_target`);
- `test_refusal_changes_only_seen` and `test_acceptance_updates_state_in_one_write`;
- `test_refusal_not_signed_when_seen_is_full_or_signed_before_reset`;
- `test_stale_copy_still_accepts_remove_statement`, `test_absent_copy_remove_checks_only_notices`
  and `test_stale_copy_refuses_add_statement`;
- `test_keys_file_same_serial_identical_body_renews_age` and
  `test_keys_file_higher_serial_dropping_revoked_mark_is_refused_keys_file_conflict`;
- `test_unverified_notice_sets_revocations_incomplete`,
  `test_revocations_incomplete_refuses_add_change_confirm_but_not_remove` and
  `test_complete_answer_clears_revocations_incomplete`;
- `test_false_revocation_notice_only_reduces_and_confirm_restores_suspended_entries`,
  `test_revoked_key_id_never_works_again`, `test_confirm_of_revoked_entry_is_refused` and
  `test_notice_applies_to_key_id_whatever_the_fingerprint`;
- `test_program_key_of_revoked_device_key_is_refused_key_revoked` (command and entry);
- one test per principal and kind combination that E2 and C6 refuse:
  `test_entry_staff_with_ed25519_key_is_refused_unknown_key`,
  `test_entry_device_with_webauthn_key_is_refused_unknown_key`,
  `test_entry_program_with_webauthn_key_is_refused_unknown_key`,
  `test_entry_service_principal_is_refused_unknown_key`,
  `test_command_from_staff_ed25519_key_is_refused_unknown_key`,
  `test_command_from_device_webauthn_key_is_refused_unknown_key`,
  `test_command_from_program_webauthn_key_is_refused_unknown_key`,
  `test_command_from_service_key_is_refused_unknown_key`;
- `test_change_keeps_active_and_sets_signer`, `test_change_to_grant_is_refused_grant_unchangeable`
  and `test_change_of_suspended_entry_is_refused_entry_inactive`;
- `test_add_with_existing_entry_id_is_refused_entry_exists`;
- `test_removal_with_full_removed_list_keeps_entry_removed_and_refuses_add`;
- `test_session_command_with_repository_field_is_malformed` and
  `test_order_submit_with_session_id_is_malformed`;
- `test_certificate_accepts_es256_eddsa_rs256_passkeys` and one refusal per V1 to V6 rule;
- `test_grant_lifetime_longer_than_30_days_is_refused_grant_invalid`;
- `test_allow_list_invalid_file_obeys_nobody` and `test_outside_edit_is_detected_and_recorded`;
- `test_init_runs_without_guard_json_and_refuses_non_empty_home`,
  `test_key_account_commands_refuse_session_account`, `test_refuse_root`,
  `test_wrong_mode_is_refused` and `test_wrong_socket_directory_mode_is_refused`;
- `test_reset_state_keeps_serials_revocations_and_own_sequence`,
  `test_reset_state_refuses_valid_state` and `test_reset_state_marks_earlier_sessions_tainted`;
- `test_pin_hub_replace_keeps_revocations_and_restarts_serials`;
- `test_session_sequences_member_dropped_after_certificate_expiry`;
- `test_statement_cursor_advances_with_each_processed_statement`;
- `test_p256_kind_is_refused_not_supported`;
- `test_inventory_never_reports_subscription_account_label_or_note` and
  `test_inventory_omits_values_outside_text_rule`;
- `test_signed_refusal_verifies_with_device_key`,
  `test_signed_refusal_names_hash_of_received_document` and
  `test_tampered_document_refusal_hash_differs_from_sent_document`;
- `test_refusals_are_queued_and_retried_until_hub_accepts`;
- `test_device_events_chain_and_retry`;
- `test_change_request_parsed_and_signed_with_fingerprint`;
- `test_socket_unknown_operation_is_not_written_to_decision_log`,
  `test_socket_read_deadline_closes_idle_connection`,
  `test_socket_connection_limit` and `test_socket_does_not_block_fetch_steps`;
- `test_cli_bridge_refuses_device`;
- `test_daemon_starts_device_report_only_with_guard_json` and
  `test_device_report_restart_limit_does_not_stop_daemon`;
- `test_report_follow_keeps_running_on_socket_error`;
- `test_guard_service_unit_passes_agent_key_home`;
- one test per command of §18 for its output, each error message and each exit code.

## 21. Rollout order

### 21.1 On the hub first

These must be deployed before a device uses this:

1. Keys-file entries for device and program keys with `device:executor` or `device:controller`,
   the fields H1 to H10, and the keys-file serial.
2. `GET /api/key/keys-file` (§8.3).
3. Revocation notices and `GET /api/key/revocations` (§9).
4. `POST /api/device/inventory` and its storage (§10.4).
5. `POST /api/device/statements`, `GET /api/device/statements` with cursors,
   `POST /api/device/refusals` accepting a repeat, change requests and `GET /api/device/requests`
   (§12.2, §12.4, §17.2).
6. For step 4: answers that carry commands hold envelopes only (§14.2).
7. For step 2: passkey sign-in with the certificate of P1 to P9, and the team-record append
   operation used by §17.3.

### 21.2 On each device

1. An administrator creates the key account, a group that holds only the key account and the
   session account, and the socket directory (`2750`, owner the key account, that group).
2. In the key account: `export AGENT_KEY_HOME=…`, `agent device init …`, then
   `agent device pin-hub …`.
3. A pull request adds the printed entry to the keys file; it is merged and deployed.
4. `agent device refresh`; the copy now lists the device key.
5. `agent device allow add …` by hand for each passkey and controller device key of the owner
   (the first entries, DESIGN.md §25.6).
6. `agent device serve --install`.
7. In the session account: write `$AGENT_HOME/guard.json` with the socket path; the daemon starts
   `device-report`.

## 22. Changes this forces in existing code

- `keys.py`: split `verify_answer` into the signature check and the time-window check, so that a
  stored answer is verified again without the window (§8.3, C1); add a signed request with a body
  (`POST`, body hash) beside `_signed_get`; make the atomic replace in `pin_hub_key` a helper for
  every file of §4.2; `create_key`, `pin_hub_key` and `load_hub_pin` are used with
  `$AGENT_KEY_HOME` as their home and need no change of behaviour.
- `main.py`: `cmd_device` and the `device` entry in `COMMANDS`.
- `daemon.py`: the child `device-report` with its own restart counter (§19.2);
  `service_unit_text` and the install functions take the label and argv as parameters, the
  `AGENT_HOME` member becomes optional, and the guard unit passes `AGENT_KEY_HOME` through
  `extra_env` (§19.1). Today `service_unit_text` writes only `AGENT_HOME`, and a guard started
  from such a unit would stop with `AGENT_KEY_HOME is not set`.
- New modules: `device_keys.py` (documents, encodings, signatures, certificate), `device_guard.py`
  (allow list, statements, checks, state, log), `device_service.py` (guard loop, socket, fetches),
  `device_inventory.py` (inventory from the existing loaders of `ai_accounts`, `ai-subscriptions`
  and `github_accounts`).
- DESIGN.md §16 lists the commands of §18, and the rows of §25.17 for §24.2 key storage and §7
  pairing are updated in the implementing pull request. That pull request also adds an entry for
  this document to DESIGN.md §23 (document history) and a pointer to it in the build-step table of
  [central-operation.md §16](central-operation.md#16-build-order-and-status).

## 23. Decisions taken in this document

| Decision | Section |
|---|---|
| The key account and the session account; `AGENT_KEY_HOME` without default; refusal for root and for the session account | §4.1 |
| `init` is the only command that runs without `guard.json`, on an absent or empty `$AGENT_KEY_HOME`; mode and owner checks cover the paths under `$AGENT_KEY_HOME` and the socket directory | §4.1 |
| Socket directory with the set-group-id bit (`2750`), so that the socket gets the shared group; the guard checks the socket's group | §4.2, §19.3 |
| No guard state in PostgreSQL; all in files under `$AGENT_KEY_HOME` | §4.1, §20 |
| Replay state limits and `replay_state_full` as a check in C13 and S9; registered keys' sequence numbers are never dropped; session-key sequence numbers are dropped after the certificate's expiry | §4.3, §12.3, §14.3 |
| `seen` holds refused ids too, with a drop time from the largest allowed window, so that a refused document never runs later | §4.3, §17.2 |
| `statement_cursor` and `refusals_sent` in `state.json`; refusals queued in `log/refusals.jsonl` | §4.3, §12.2, §17.2 |
| No restore of `state.json` from a backup; the only repair is `init --reset-state` | §4.3 |
| A reset never clears replay protection or revocations: it refuses what was signed before `reset_at` plus the largest window (3600 s commands, 604800 s statements except `remove`, 43200 s certificates) with `signed_before_reset`, keeps the serials from the stored files and `own_sequence` from the log, marks every earlier session tainted, and treats the allow list as edited outside the client. Chosen over rebuilding `seen` and `sequences` from the log (report B's first option) because it holds also when the log is incomplete | §4.4 |
| `signed_before_reset` refusals are not answered with a signed refusal | §4.4, §17.2 |
| Default and allowed ranges of the four configured times; "one working day" is 43200 s | §5.1 |
| Fixed intervals, size limits, socket deadline 5 s, 8 socket connections, 30 s hub timeout | §5.2 |
| Device key id `device-<label>`, program key id `program-<label>-<program>` | §6.1 |
| Program keys that may send commands live in the key account; keys under `$AGENT_HOME/keys` are never senders, which the guard cannot detect and the owner and the keys-file review ensure | §6.1, §11.4 |
| The sequence counter of a program key that signs commands is fixed in step 4 | §1 |
| `p256` device keys reserved and refused | §6.2 |
| A device key with both or neither level scope counts as unknown | §7 |
| A program key counts only while its device key is not revoked; refused with `key_revoked` (chosen over `executor_key`, both offered by report A) | §7, §11.4, §14.3 |
| Principal and kind are tied: `staff` with `webauthn`, `device` and `program` with `ed25519`; every other combination is `unknown_key` | §11.2, §11.4, §14.3 |
| The printed keys-file entry and its member names (H1 to H8); `agent device entry` takes `--controller` again because the device does not store its level | §8.1, §18.2 |
| Passkey algorithms ES256, EdDSA, RS256 2048–4096; public key as DER SubjectPublicKeyInfo | §8.2, §15.1 |
| Keys-file serial, rollback and conflict refusal; copy stored as the full signed answer | §8.3 |
| An equal serial with an identical body renews the copy; freshness is measured from the hub's signed time of the last successful fetch | §8.3 |
| A new copy that drops a revoked mark of the stored copy is refused as a conflict | §8.3 |
| Copy older than its maximum age: commands and widening changes refused, removals and notices still applied; a `remove` statement with an absent or invalid copy checks only the applied notices | §8.3, §12.3, §16 |
| Revocation notices with one global serial; a gap or an unverifiable notice sets `revocations_incomplete`, which blocks every command and every `add`, `change` and `confirm`, by statement and by hand | §9, §11.4, §12.3, §14.3 |
| A notice acts on its key id whatever the fingerprint; a notice for a device key also revokes the entries of its program keys (by key id prefix `program-<label>-`) | §9 |
| A revoked key id never works again on any device; `revoked` is final; a wrong revocation is undone only by a new key and a new entry; `confirm` restores only `suspended` entries. This is the binding decision of the orchestrator; it is stricter than report A's and B's alternative fixes (recording an override per notice), and it reads DESIGN.md §25.6 "until the owner confirms them again" as: suspended entries by `confirm`, a revoked key by a new entry for a new key | §9, §12.3, §16, §18.8 |
| `revocations.json` is never shortened, not by a reset and not by a new hub pin | §4.4, §9, §18.4 |
| `pin-hub --replace` deletes the stored copy, restarts both serials, sets `revocations_incomplete`, and keeps every notice and entry status | §18.4 |
| Inventory leaves out subscription account, label and note; runtimes from `device-inventory.json`; hub keeps only the newest report, outside the team record | §10 |
| Allow list as strict JSON; entries and grants in one file; one entry per key id and per entry id; `removed` never shrinks | §11 |
| When `removed` holds 65536 ids, a removal keeps the entry with final `status` `removed` and every add is refused with `allow_list_full` (chosen over deleting without a record) | §11.2, §11.4 |
| The signer of an entry is recorded with key id and fingerprint | §11.2 |
| Permission names and the grant subset | §11.3, §13.2 |
| By-hand confirmation is the full fingerprint given with `--fingerprint` | §11.5 |
| Outside edits of the allow list are accepted as by-hand changes and recorded | §11.5 |
| One sequence counter per signing key over statements and commands; a controller logs every statement it signs | §12.1, §14.3, §17.1 |
| Statement transport through hub endpoints with a per-device cursor, with a file path for offline use | §12.2 |
| A `change` needs an `active` entry of type `entry`, never yields a grant, keeps `status` `active` and sets `signer` to the changing statement | §12.3 |
| Change requests: their form and the answer of `GET /api/device/requests` | §12.4 |
| Grants are never changed; at most 30 days, at least 60 s | §13.2 |
| Tainted sessions recorded now for the signing service of step 4 | §13.6 |
| Command envelope carries only a content hash; delivery record keeps the hub answer, whose body holds envelopes only | §14.1, §14.2 |
| The repository of a command that names a session is the target session's repository from the device's own store; the envelope's `repository` is `null` for those kinds and `session_id` is `null` for `order.submit` (chosen over report A's comparison of a sender field with the session's repository, because the device then never reads a repository the sender fills in); an unknown target session is `session_unknown`; step 4 refuses an order whose body names another repository | §14.1, §14.3 |
| A target session without a repository passes only an entry with `["*"]` | §14.3 |
| Session-key algorithm P-256; statement of seven lines; signature counter not checked; fresh-value reuse refused | §15 |
| Signed refusals carry `document_hash` over the whole received document, signature included; a sender signs again only when that hash equals its own document's and the value is not `replayed`; decision log never truncated by the client | §17 |
| Device events carry their value in `changes.action`; guard-state events under `object_type` `device_guard` | §17.3 |
| Command names under `agent device`, exit 3 for a refusal | §18 |
| `init` before `pin-hub` in the rollout | §21.2 |
| Socket access by file mode and group, two operations only; socket handled apart from the fetch steps; socket refusals counted, never logged to the decision log | §19.3 |
| `device-report` keeps running on errors and has its own restart counter whose limit never stops the daemon (both of report B's options, as the stricter combination) | §18.14, §19.2 |
| The guard's service unit sets `AGENT_KEY_HOME` and no `AGENT_HOME` | §19.1, §22 |
