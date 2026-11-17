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

Not built in this step, and unchanged by it:

- GitHub sign-in, device pairing, device tokens and every existing endpoint
- passkey sign-in and session keys (the keys file already has a place for passkeys)
- the allow list on a device, grants, the team record, the release query

Nothing that works today changes its behaviour.

## 2. Encodings

| Thing | Encoding |
|---|---|
| Binary values in files, headers and JSON | base64url without padding (RFC 4648 §5) |
| Hashes | SHA-256, written as 64 lowercase hex characters |
| Time | UTC, `YYYY-MM-DDTHH:MM:SSZ`, whole seconds |
| Text that is signed | UTF-8, lines joined by one line feed (`\n`), no trailing line feed |
| Ed25519 public key | the 32 raw bytes (RFC 8032) |
| Ed25519 signature | the 64 raw bytes |
| P-256 public key | the 65 bytes of the uncompressed point (`0x04`, X, Y) |
| P-256 signature | ECDSA with SHA-256, the 64 bytes `r` followed by `s`, low-`s` form |

A value that does not decode, has the wrong length or carries padding is refused.

## 3. The keys file

The keys file lies next to `teams.yaml`. Its path comes from the required environment variable
`AGENT_CORE_KEYS`. It changes only by pull request. It holds public keys and never a secret.

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
    public_key: "<base64url, P-256 point>"
    scopes: [record:read, record:write]

  - id: example-staff-laptop-1
    kind: ed25519
    principal: agent
    handle: example-staff
    team: example
    device: laptop-1
    level: controller
    public_key: "<base64url, 32 bytes>"
    scopes: [record:read, record:write]

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

Fields of an entry in `keys`:

| Field | Required | Meaning |
|---|---|---|
| `id` | always | Unique in the file. Lowercase letters, digits and `-`, 3 to 63 characters, starts with a letter or digit. Never reused, also not after revocation. |
| `kind` | always | `ed25519`, `p256` or `webauthn`. `p256` is for a device key kept in the machine's secure hardware. |
| `principal` | always | `staff`, `agent` or `service`. A `staff` key has kind `webauthn`; `agent` and `service` keys have kind `ed25519` or `p256`. |
| `handle` | always | The person, agent or program the key belongs to. For `staff` and `agent` keys it is a member of `team` in `teams.yaml`; for an `agent` key it is the staff member who owns the device. A handle, never a legal name or mail address. |
| `team` | always | A team in `teams.yaml`. |
| `public_key` | always | As in section 2. For `webauthn` the P-256 point of the credential. |
| `credential_id` | `webauthn` only | The credential id of the passkey. |
| `device` | `agent`, `service` | Label of the machine. Unique per handle. |
| `program` | optional | Label of the program when a device has more than one key. |
| `level` | `agent` device keys | `executor` or `controller`. |
| `scopes` | always | Non-empty list from the table below. |
| `subjects` | `service` only | The repositories (`owner/name`, lowercase) the key may ask about. Non-empty. |
| `expires` | `service` only | After this time the key is refused. |
| `revoked` | optional | Time from which the key is refused. An entry is revoked, never removed, so that what it signed stays attributable. |

Scopes:

| Scope | Allows |
|---|---|
| `record:read` | Read the team record |
| `record:write` | Write events to the team record |
| `release:query` | Ask the release query |
| `keys:invite` | Create invite links for passkeys. Exactly one handle may hold it. |

Entries in `hub` are the hub's own signing keys, kind `ed25519`. An entry may carry `retired`
with a time; a retired key signs nothing new, and answers signed before that time stay
verifiable.

The loader fails closed. The process does not start when the file is missing or unreadable, a
key is written twice, a field is unknown, missing, of the wrong type or not allowed for that
principal, an id or a public key appears twice, a handle or team is not in `teams.yaml`, a scope
is unknown, `keys:invite` is held by more than one handle, or `hub` is empty. An empty `keys`
list is valid and is the starting state.

## 4. The hub's signing key

The hub signs with one Ed25519 key. Its 32-byte seed comes from the required environment
variable `AGENT_CORE_SIGNING_KEY` (base64url). The process does not start unless the public key
of that seed equals the `public_key` of exactly one `hub` entry that is not retired. That entry's
`id` is the hub key id.

The seed is a secret. It never appears in a log, an error message or an answer.

## 5. A signed request

A caller adds four headers:

| Header | Value |
|---|---|
| `Agent-Key` | The `id` of its key |
| `Agent-Time` | The caller's clock |
| `Agent-Nonce` | 16 random bytes, new for every request |
| `Agent-Signature` | The signature over the text below |

The signed text has seven lines:

```text
agent-request-v1
<method, uppercase>
<host of the hub's public URL, lowercase, with the port if it is not the default>
<path and query exactly as sent, starting with "/">
<Agent-Time>
<Agent-Nonce>
<SHA-256 of the request body; of the empty string when there is no body>
```

The hub checks in this order and stops at the first failure:

| # | Check | Status | `error` |
|---|---|---|---|
| 1 | All four headers are present, once each, and well formed | 401 | `signature_missing` |
| 2 | `Agent-Key` names an entry in `keys` | 401 | `key_unknown` |
| 3 | The entry has no `revoked` time at or before now | 401 | `key_revoked` |
| 4 | The entry has no `expires` time at or before now | 401 | `key_expired` |
| 5 | The kind is `ed25519` or `p256` | 401 | `key_kind_not_allowed` |
| 6 | `Agent-Time` is at most 120 seconds from the hub's clock | 401 | `time_skew` |
| 7 | The signature verifies over the text built from this request | 401 | `signature_invalid` |
| 8 | The pair of key id and nonce was not seen before | 401 | `nonce_replayed` |
| 9 | The key has the scope the endpoint needs | 403 | `scope_missing` |
| 10 | For a `service` key: the subject of the request is in `subjects` | 403 | `subject_not_allowed` |

- A `webauthn` key never signs a request directly (check 5). A staff member's requests are
  signed by a session key in a later build step.
- A nonce is stored only after check 7 passed, and kept for 10 minutes. A request that fails an
  earlier check cannot fill the store.
- A refusal has the JSON body `{"error": "<value>"}` and nothing else. It carries no data of the
  hub. A refusal after check 7 passed is signed like any answer; an earlier one is not, because
  the caller is not known.
- The host line binds a request to one hub. A request signed for the development hub is refused
  by the production hub.

## 6. A signed answer

For every request that passed check 7 the hub adds three headers:

| Header | Value |
|---|---|
| `Agent-Hub-Key` | The hub key id |
| `Agent-Hub-Time` | The hub's clock |
| `Agent-Hub-Signature` | The signature over the text below |

```text
agent-response-v1
<Agent-Key of the request>
<Agent-Nonce of the request>
<status code, decimal>
<Agent-Hub-Time>
<SHA-256 of the answer body; of the empty string when there is no body>
```

The caller holds the hub's public key and key id in its own configuration. It did not fetch
them from the hub. It accepts an answer only when `Agent-Hub-Key` is that pinned id, the
signature verifies with the pinned key over the text built from its own request and the answer
it received, and `Agent-Hub-Time` is at most 120 seconds from its own clock. Otherwise it treats
the answer as missing and reports `hub answer not verified`; it never uses the body.

## 7. `GET /api/key/whoami`

Needs a signed request and no scope. The answer is signed.

```json
{
  "key": "example-staff-laptop-1",
  "kind": "ed25519",
  "principal": "agent",
  "handle": "example-staff",
  "team": "example",
  "device": "laptop-1",
  "level": "controller",
  "scopes": ["record:read", "record:write"],
  "hub_key": "hub-1"
}
```

Fields the entry does not have are left out. The endpoint shows nothing about any other key.
Without a valid signature it answers as in section 5, like every endpoint under this protocol.

## 8. The client

| Command | Does |
|---|---|
| `agent key create --id ID [--kind ed25519]` | Creates a key pair. Writes the private key to `$AGENT_HOME/keys/ID.key` with mode `0600`, refuses to overwrite, and prints the `public_key` value for the keys file. The private key is never printed. |
| `agent key list` | Lists the ids and public keys under `$AGENT_HOME/keys`. |
| `agent key pin-hub --id HUB_ID --public-key VALUE` | Stores the hub key id and public key in `$AGENT_HOME/hub-key.json`. |
| `agent key whoami --id ID` | Sends a signed `GET /api/key/whoami` to the configured hub, verifies the answer and prints it. |

- The client never sends a signed request to a host other than the configured hub.
- Without a pinned hub key, `agent key whoami` stops with `hub key is not pinned` and sends
  nothing.
- A key kept in secure hardware (`p256`) and the separate operating-system account for keys
  belong to a later build step.

## 9. Tests this step must carry

The hub, one test per line:

- each refusal of section 5 in its order, including two headers of the same name, a padded
  value, a wrong length, and a time exactly 120 and 121 seconds off
- a replayed request, and the same nonce under two different keys (accepted)
- a request signed for another host, another path, another query, another method and another body
- a revoked key, an expired key, a key whose `revoked` time is in the future (accepted)
- a `webauthn` key used directly
- the signed answer for 200 and for a signed refusal, and no hub signature on an unsigned refusal
- every loader failure of section 3, and a signing seed that matches no hub entry, a retired
  entry or two entries
- no secret in any log line or error body

The client:

- key creation with mode `0600`, no overwrite, no private key on standard output
- an answer with a wrong hub key id, a wrong signature, a changed body, a changed status and a
  time outside the window, each reported as `hub answer not verified`
- no request without a pinned hub key

## 10. Dependencies and configuration

- Both repositories use the `cryptography` package for Ed25519, P-256 and SHA-256. No own
  implementation of a primitive.
- New required environment variables of the hub: `AGENT_CORE_KEYS` and `AGENT_CORE_SIGNING_KEY`.
  Like every `AGENT_CORE_*` variable, a missing or empty value stops the process.
- No new table is needed except the nonce store: key id, nonce, time seen; rows older than 10
  minutes are deleted.
