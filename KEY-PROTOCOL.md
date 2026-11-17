# Key protocol

This document fixes the wire format for key identity: the keys file, how a request is signed,
how the hub signs its answer, and every refusal. It is the technical specification for the
first build step of the team record.

**Status:** specified, not implemented. The rules behind it are in
[DESIGN.md §24 and §25](https://github.com/DFXswiss/agent/blob/develop/DESIGN.md) of the client
repository; if this document and DESIGN.md disagree, DESIGN.md wins and this document is fixed.
[PROTOCOL.md](PROTOCOL.md) stays the contract for everything that exists today.

## 1. Scope of the first build step

Built in this step:

- the keys file and its loader
- verification of a signed request
- the hub's signature on its answer
- one endpoint that uses both: `GET /api/key/whoami`
- in the client: creating a key, pinning the hub key, calling that endpoint

Not built in this step:

- passkey sign-in and session keys (the keys file already has a place for passkeys)
- device keys in secure hardware (kind `p256`)
- the allow list on a device, grants, the team record, the release query

No existing endpoint changes its behaviour: GitHub sign-in, device pairing and device tokens
work as before. The hub additionally requires two new environment variables at start
(section 10).

## 2. Encodings

| Thing | Encoding |
|---|---|
| Binary values in files, headers and JSON | base64url without padding (RFC 4648 §5), canonical: encoding the decoded bytes again gives the same text |
| Hashes | SHA-256, written as 64 lowercase hex characters |
| Time | UTC, `YYYY-MM-DDTHH:MM:SSZ`, whole seconds. In the keys file a time is a quoted string. |
| Text that is signed | UTF-8, lines joined by one line feed (`\n`), no trailing line feed |
| Ed25519 public key | the 32 raw bytes (RFC 8032) |
| Ed25519 signature | the 64 raw bytes, plain Ed25519 (not the prehashed or the context variant) |
| Passkey public key | the 65 bytes of the uncompressed P-256 point (`0x04`, X, Y) |

A value that does not decode, is not canonical or has the wrong length is refused.

Wherever a time is compared with a clock, the clock is cut to whole seconds first. "Now" in
this document means the hub's clock cut to whole seconds.

## 3. The keys file

The hub reads the keys file from the path in the required environment variable
`AGENT_CORE_KEYS`; by convention it lies next to `teams.yaml`. It changes only by pull request.
It holds public keys and never a secret.

```yaml
hub:
  - id: hub-1
    kind: ed25519
    public_key: "<base64url, 32 bytes>"

keys:
  - id: example-staff-passkey-1
    kind: webauthn
    principal: staff
    handle: example-staff
    team: example
    credential_id: "<base64url>"
    public_key: "<base64url, 65 bytes>"
    scopes: [record:read, record:write]

  - id: example-staff-laptop-1
    kind: ed25519
    principal: agent
    handle: example-staff
    team: example
    device: laptop-1
    public_key: "<base64url, 32 bytes>"
    scopes: [record:read, record:write, device:controller]

  - id: example-guard-1
    kind: ed25519
    principal: service
    handle: example-guard
    team: example
    device: runner-1
    public_key: "<base64url, 32 bytes>"
    scopes: [release:query]
    subjects: [example-org/example-repo]
    expires: "2027-06-30T00:00:00Z"
```

### Entries in `keys`

| Field | Meaning |
|---|---|
| `id` | Unique across `keys` and `hub`. Lowercase letters, digits and `-`, 3 to 63 characters, starts and ends with a letter or digit. Never reused, also not after revocation. |
| `kind` | `ed25519` or `webauthn`. |
| `principal` | `staff`, `agent` or `service`. |
| `handle` | The person or program the key belongs to; for an `agent` key the staff member who owns the device. Lowercase. A handle, never a legal name or mail address. |
| `team` | A team in `teams.yaml`. |
| `public_key` | As in section 2. Unique across `keys` and `hub`. |
| `credential_id` | The credential id of the passkey. |
| `device` | Label of the machine, same form as an `id`. |
| `program` | Label of the program, same form as an `id`. |
| `scopes` | Non-empty list from the table below, no scope twice. |
| `subjects` | Non-empty list of repositories (`owner/name`, lowercase) the key may ask about. |
| `expires` | From this time on the key is refused. |
| `revoked` | From this time on the key is refused. An entry is revoked, never removed, so that what it signed stays attributable. |

Which field an entry has depends on its principal:

| Field | `staff` | `agent` | `service` |
|---|---|---|---|
| `id`, `principal`, `handle`, `team`, `public_key`, `scopes` | required | required | required |
| `kind` | `webauthn` | `ed25519` | `ed25519` |
| `credential_id` | required | refused | refused |
| `device` | refused | required | required |
| `program` | refused | allowed | allowed |
| `subjects`, `expires` | refused | refused | required |
| `revoked` | allowed | allowed | allowed |

Further rules:

- The `handle` of a `staff` or `agent` entry is a member of that entry's `team` in `teams.yaml`
  and is not a login in the `agents` map of that file. The `handle` of a `service` entry need not
  be in `teams.yaml`.
- Several entries may name the same `device`. Among the entries of one handle that are not
  revoked, the pair of `device` and `program` is unique; `program` is required when two such
  entries share a `device`.
- An entry is revoked when its `revoked` time is at or before now, and expired when its `expires`
  time is at or before now. A time in the future has no effect yet.

### Scopes

| Scope | Allows | May be held by |
|---|---|---|
| `record:read` | Read the team record | every principal |
| `record:write` | Write events to the team record | `staff`, `agent` |
| `release:query` | Ask the release query | `service` |
| `keys:invite` | Create invite links for passkeys | `staff` |
| `device:executor` | The device runs work it was given | `agent` |
| `device:controller` | The device may also give work to executors | `agent` |

- Every `agent` entry holds exactly one of `device:executor` and `device:controller`. This is
  the level of the device (DESIGN.md §25.4).
- Among the entries that are not revoked, at most one handle holds `keys:invite`. That handle is
  the administrator (DESIGN.md §24.2).

### Entries in `hub`

A `hub` entry is one of the hub's own signing keys. It has exactly `id` (rule as above),
`kind: ed25519`, `public_key` and optionally `retired` (a time). An entry is retired when its
`retired` time is at or before now; a retired key signs nothing.

### Loader

The loader fails closed. The process does not start when:

- the file is missing or unreadable, or a mapping key is written twice
- a field is unknown, missing, of the wrong type, refused for that principal or not in the form
  given above
- an `id` or a `public_key` appears twice
- a `team` is not in `teams.yaml`, or a `staff` or `agent` handle breaks the handle rule
- a scope is unknown or not allowed for the principal, an `agent` entry does not hold exactly
  one device scope, or `keys:invite` is held by more than one handle
- the pair of `device` and `program` is not unique as required
- `hub` is empty

An empty `keys` list is valid and is the starting state.

## 4. The hub's signing key

The hub signs with one Ed25519 key. Its 32-byte seed comes from the required environment
variable `AGENT_CORE_SIGNING_KEY` (base64url). The process does not start unless the public key
of that seed equals the `public_key` of a `hub` entry that is not retired. That entry's `id` is
the hub key id.

The seed is a secret. It never appears in a log, an error message or an answer.

## 5. A signed request

A caller adds four headers:

| Header | Value |
|---|---|
| `Agent-Key` | The `id` of its key |
| `Agent-Time` | The caller's clock |
| `Agent-Nonce` | 16 random bytes, new for every request |
| `Agent-Signature` | The signature over the text below |

The signed text has eight lines:

```text
agent-request-v1
<Agent-Key>
<method, uppercase>
<host>
<request target>
<Agent-Time>
<Agent-Nonce>
<body hash>
```

- **Host** is the host of the hub's public URL (`AGENT_CORE_PUBLIC_URL`), lowercase, followed by
  `:` and the port only when the port is not the default of the URL's scheme. The public URL has
  no path.
- **Request target** is the path and query as the hub receives them: the raw path with its
  percent-encoding unchanged, followed by `?` and the raw query only when the query is not
  empty. A client never sends `?` with an empty query. Whatever stands in front of the hub
  forwards path and query byte for byte.
- **Body hash** is the SHA-256 of the body bytes after removing any `Content-Encoding`; of the
  empty string when there is no body.

The hub builds the text from what it received and checks in this order, stopping at the first
failure:

| # | Check | Status | `error` |
|---|---|---|---|
| 1 | All four headers are present, once each, and well formed | 401 | `signature_missing` |
| 2 | `Agent-Key` names an entry in `keys` | 401 | `key_unknown` |
| 3 | The entry is not revoked | 401 | `key_revoked` |
| 4 | The entry is not expired | 401 | `key_expired` |
| 5 | The entry's kind is `ed25519` | 401 | `key_kind_not_allowed` |
| 6 | `Agent-Time` is at most 120 seconds from now | 401 | `time_skew` |
| 7 | The signature verifies | 401 | `signature_invalid` |
| 8 | The pair of key id and nonce was not seen before | 401 | `nonce_replayed` |
| 9 | The key has the scope the endpoint needs | 403 | `scope_missing` |
| 10 | If the endpoint names a subject: it is in the key's `subjects` | 403 | `subject_not_allowed` |

- A `webauthn` key never signs a request directly (check 5). A staff member's requests are
  signed by a session key in a later build step.
- Check 8 is one atomic insert of key id and nonce under a unique constraint; the request whose
  insert fails is refused. A nonce is stored only after check 7 passed and is kept for 10
  minutes, so a request that fails an earlier check cannot fill the store.
- Check 10 applies only to an endpoint that names a subject, and then only to `service` keys.
  `GET /api/key/whoami` names none.
- A refusal has the JSON body `{"error": "<value>"}` and nothing else. It carries no data of the
  hub. The keys file is public, so the values of checks 2 to 5 tell a caller nothing it could
  not read there.
- Every accepted request of a `service` key writes one log line with the key id, method, request
  target and status.

## 6. A signed answer

For every request that passed check 8 the hub adds three headers, whatever the status of the
answer:

| Header | Value |
|---|---|
| `Agent-Hub-Key` | The hub key id |
| `Agent-Hub-Time` | The hub's clock |
| `Agent-Hub-Signature` | The signature over the text below |

```text
agent-response-v1
<Agent-Key of the request>
<Agent-Nonce of the request>
<Agent-Time of the request>
<status code, decimal>
<Agent-Hub-Time>
<body hash of the answer, as in section 5>
```

A refusal at checks 1 to 8 is not signed: before check 7 the sender is not authenticated, and a
signed `nonce_replayed` could be used to replace the real answer to the first request. A refusal
at checks 9 and 10, a `404` and a `500` are signed like any answer.

The caller holds the hub's public key and key id in its own configuration. It did not fetch
them from the hub. It accepts an answer only when:

- `Agent-Hub-Key` is the pinned id
- the signature verifies with the pinned key over the text built from its own request and the
  answer it received
- `Agent-Hub-Time` is at most 120 seconds from its own clock

Otherwise it reports `hub answer not verified` and never uses the body. It may show the `error`
value of an unsigned refusal, marked as unverified, to help find a mistake. It never follows a
redirect.

## 7. `GET /api/key/whoami`

Needs a signed request, no scope, and names no subject. The answer is signed.

```json
{
  "key": "example-staff-laptop-1",
  "kind": "ed25519",
  "principal": "agent",
  "handle": "example-staff",
  "team": "example",
  "device": "laptop-1",
  "scopes": ["record:read", "record:write", "device:controller"],
  "hub_key": "hub-1"
}
```

The answer has exactly these fields, plus `program`, `subjects` and `expires` when the entry has
them. It shows nothing about any other key.

## 8. The client

| Command | Does |
|---|---|
| `agent key create --id ID` | Creates an Ed25519 key pair. Writes the private key to `$AGENT_HOME/keys/ID.key` and prints the `public_key` value for the keys file. |
| `agent key list` | Lists the ids and public keys under `$AGENT_HOME/keys`. |
| `agent key pin-hub --id HUB_ID --public-key VALUE` | Stores the hub key id and public key in `$AGENT_HOME/hub-key.json`. |
| `agent key whoami --id ID` | Sends a signed `GET /api/key/whoami` to the configured hub, verifies the answer and prints it. |

- `ID` must follow the `id` rule of section 3; any other value is refused and nothing is written.
- The directory `$AGENT_HOME/keys` has mode `0700`. A key file has mode `0600` and holds the
  32-byte seed as base64url text. An existing file is never overwritten. The private key is
  never printed.
- The client never sends a signed request to a host other than the configured hub.
- Without a pinned hub key, `agent key whoami` stops with `hub key is not pinned` and sends
  nothing.
- The separate operating-system account for keys (DESIGN.md §25.6) belongs to a later build
  step.

## 9. Tests this step must carry

The hub:

- each refusal of section 5 in its order. Checks 9 and 10 are tested through a route that
  exists only in the test suite.
- two headers of the same name, a padded value, a value that is not canonical, a wrong length
- a time exactly 120 seconds off (accepted) and 121 seconds off (refused)
- a replayed request, two parallel copies of one request (exactly one accepted), and the same
  nonce under two different keys (both accepted)
- a request signed for another key id, host, method, path, query and body
- a percent-encoded path, and a request sent with `?` and an empty query
- a revoked key, an expired key, and a key whose `revoked` time is in the future (accepted)
- a `webauthn` key used directly
- the signed answer for 200, for a refusal at check 9, and for an unknown route; no hub
  signature on a refusal at checks 1 to 8
- every loader failure of section 3, and a signing seed that matches no `hub` entry or a retired
  one
- one log line per accepted request of a `service` key, and no secret in any log line or body

The client:

- key creation: directory and file modes, no overwrite, no private key on standard output, an
  `ID` outside the rule refused with nothing written
- an answer with a wrong hub key id, a wrong signature, a changed body, a changed status, a time
  outside the window, and an answer sent with a `Content-Encoding`
- a redirect is not followed
- no request without a pinned hub key

## 10. Dependencies and configuration

- Both repositories use the `cryptography` package for Ed25519 and SHA-256. No own
  implementation of a primitive.
- New required environment variables of the hub: `AGENT_CORE_KEYS` and `AGENT_CORE_SIGNING_KEY`.
  Like every `AGENT_CORE_*` variable, a missing or empty value stops the process. The list of
  required variables in the README and the compose file are updated in the same change.
- One new table, the nonce store: key id, nonce, time seen, with a unique constraint on key id
  and nonce. Rows older than 10 minutes are deleted.
