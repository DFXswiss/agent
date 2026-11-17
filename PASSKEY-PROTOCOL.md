# Passkey protocol

This document fixes how a staff member gets a passkey, signs in with it, and signs every
request and command with a certified session key instead of a GitHub sign-in. It is the
technical specification for the build step "sign-in with passkeys and the certified session key
instead of GitHub".

**Status:** specified, not built. The rules behind it are DESIGN.md §24.2, §25.3 to §25.6,
§25.15, §25.16 and §25.21 in the
[client repository](https://github.com/DFXswiss/agent/blob/develop/DESIGN.md); if this document
and DESIGN.md disagree, DESIGN.md wins and this document is fixed.
[KEY-PROTOCOL.md](KEY-PROTOCOL.md) stays the specification of the keys file, of signed requests
and of signed answers; this document extends it and changes it only where a section below says
so. [PROTOCOL.md](PROTOCOL.md) stays the contract for everything that exists today.

The words "must", "never" and "only" state rules. Nothing in this document is optional unless it
says "may".

## 1. Scope of this build step

Built in this step:

- the invite: an administrator creates a single-use invite link, the invited person creates a
  passkey with it, the hub shows the received public key with its fingerprint and the text for
  the keys-file pull request (section 5)
- the bootstrap invite for the administrator's own passkey, created by an operator command on
  the hub host (section 5.3)
- sign-in with a passkey, which certifies a session key created in the browser (section 6)
- signed requests by a session key under `/api/key/`, with the changed checks of KEY-PROTOCOL.md
  section 5 (section 7)
- `GET /api/key/whoami` for a session key, and `POST /api/key/session/end` (sign-out)
- `POST /api/key/commands`: the hub verifies a command signed by a session key (section 8). In
  this step the only action is `noop`, and the hub delivers no command to a device.
- the exact verification a device runs on a certificate and a command (section 9), so that the
  client repository can build it without asking
- the pages `/passkey` and `/invite` of the website (section 10)
- the tests of section 13, including an end-to-end test in headless Chromium in CI (section 14)

Not built in this step:

- removing GitHub sign-in (the following build step). Until then both coexist (section 11).
- moving the existing website endpoints (`/api/state`, `/api/session/*`, `/api/pings`,
  `/api/assignments`, `/api/usage`, `/api/stream`, `/pair/*`) to signed requests. A passkey
  session cannot call them in this step.
- the team record. The invite creation is kept as the signed request that created it until the
  team record exists (section 5.1).
- delivery of commands to devices, the allow list, grants, maskings (DESIGN.md §25.20 steps 1, 2
  and 4). Section 9 is the binding format for them.
- the published hash of the website (DESIGN.md §25.16) and the access log (DESIGN.md §25.9)

No existing endpoint changes its behaviour. The hub needs no new environment variable. It refuses
to start when the host of `AGENT_CORE_PUBLIC_URL` is an IP address (section 3).

## 2. Encodings and constants

The encodings of KEY-PROTOCOL.md section 2 apply. Added here:

| Thing | Encoding |
|---|---|
| Session public key | the 65 bytes of the uncompressed P-256 point (`0x04`, X, Y), as for a passkey key |
| Session key signature | the 64 bytes `r ‖ s`, each a 32-byte big-endian integer (IEEE P1363, the output of WebCrypto ECDSA). `r` and `s` are each from 1 to n − 1, where n is the order of P-256; otherwise the signature is invalid. A high `s` is accepted. |
| Passkey signature | ASN.1 DER `ECDSA-Sig-Value` as WebAuthn returns it for ES256, at most 72 bytes. It is valid DER only when decoding it with `decode_dss_signature` and encoding the two integers again with `encode_dss_signature` gives the same bytes. |
| Credential id | the raw bytes, 16 to 1023, as in KEY-PROTOCOL.md section 3 |
| Fingerprint of a P-256 public key | the first 16 bytes of SHA-256 over the 65-byte point, as 32 lowercase hex characters in 8 groups of 4, separated by one space. Example: `3f2a 9c01 77de 0b4e 5a1c 2e9f 8d33 a0b7` |
| Session key id | `session.` followed by the first 16 bytes of SHA-256 over the statement bytes (section 6.2), as 32 lowercase hex characters; 40 characters in total |
| Invite id | `invite.` followed by 16 random bytes as 32 lowercase hex characters |
| Invite token | 32 random bytes, base64url (43 characters) |
| Sign-in id and registration id | 16 random bytes, base64url (22 characters) |

A session key id and an invite id contain a `.`, which the `id` rule of KEY-PROTOCOL.md section 3
does not allow. They can therefore never equal a keys-file id.

Every random value in this document comes from the operating system's secure random source
(`secrets.token_bytes`), in the browser from `crypto.getRandomValues`.

| Constant | Value | Meaning |
|---|---|---|
| `SESSION_KEY_MAX_SECONDS` | 43200 | One working day: the longest lifetime of a session key that the hub or a device accepts |
| `SESSION_KEY_ISSUE_SECONDS` | 43080 | The lifetime the hub writes into a statement: `SESSION_KEY_MAX_SECONDS` minus the 120-second clock window, so that a device whose clock is up to 120 seconds behind the hub still accepts it |
| `CEREMONY_SECONDS` | 300 | How long a sign-in or a registration may take from its first to its second request |
| `INVITE_MIN_SECONDS` | 300 | Shortest invite lifetime |
| `INVITE_MAX_SECONDS` | 86400 | Longest invite lifetime |
| `INVITE_OPEN_LIMIT` | 50 | Invites that are neither expired nor received, at most, at one time |
| `PENDING_SIGN_IN_LIMIT` | 1000 | Sign-ins that are begun, not finished and not older than `CEREMONY_SECONDS`, at most, at one time |
| `ANONYMOUS_BODY_LIMIT` | 65536 bytes | Largest body of a request to `/auth/passkey/*` and `/auth/invite/*` |
| `COMMAND_TIME_WINDOW` | 120 seconds | How far the `time` of a command may be from the hub's clock |
| `SEQUENCE_MAX` | 9007199254740991 | Largest command sequence number (the largest integer JavaScript represents exactly) |

## 3. Relying party and origin

- The **origin** is the scheme, host and port of `AGENT_CORE_PUBLIC_URL`, written as the browser
  writes `location.origin`: lowercase scheme, `://`, the host line of KEY-PROTOCOL.md section 5.
  Example: `https://hub.example`, or `http://localhost:8787` for a local run.
- The **relying-party id** (rp id) is the host of `AGENT_CORE_PUBLIC_URL` without port, lowercase,
  in ASCII form. Example: `hub.example`, or `localhost`.
- WebAuthn does not allow an IP address as rp id. The process therefore does not start when the
  host of `AGENT_CORE_PUBLIC_URL` is an IPv4 or IPv6 address. A local run uses `localhost`.
- A passkey is bound to its rp id. Changing the host of the public URL makes every passkey and
  every certificate useless; a new host means new passkeys through invites.

## 4. Verifying a passkey response

Registration (section 5), sign-in (section 6) and a device (section 9) verify the same fields with
the same code. This section fixes that code. It follows WebAuthn Level 3 §7.1 and §7.2 for exactly
the options this document uses. No server-side WebAuthn library is used (decision 3): the checks
are few, a device has to run the same checks offline, and a library would bring attestation code
this document never uses. Every cryptographic operation is done by the `cryptography` package; CBOR
is decoded by the `cbor2` package. No primitive is implemented by hand.

### 4.1 Client data

`client_data_json` is the exact byte string the browser returned. The verifier:

1. decodes it as UTF-8 and parses it as one JSON object. A key written twice, a value that is not
   JSON, or a top-level value that is not an object fails.
2. requires `type` to be the expected text: `webauthn.create` for a registration,
   `webauthn.get` for a sign-in.
3. requires `challenge` to be a text that decodes as canonical base64url (KEY-PROTOCOL.md section
   2) to exactly the expected bytes.
4. requires `origin` to be exactly the origin of section 3.
5. requires `crossOrigin` to be absent or `false`, and `topOrigin` to be absent.

Any other member (for example `other_keys_can_be_added_here`) is ignored.

### 4.2 Authenticator data

`authenticator_data` is the exact byte string the authenticator returned. Its first 37 bytes are
the rp id hash (32 bytes), the flags (1 byte) and the signature counter (4 bytes, big-endian). The
verifier requires:

1. at least 37 bytes
2. the rp id hash equal to SHA-256 over the rp id of section 3
3. flag UP (`0x01`) set
4. flag UV (`0x04`) set
5. if flag BS (`0x10`) is set, flag BE (`0x08`) is set as well
6. for a sign-in: flag AT (`0x40`) not set; if flag ED (`0x80`) is not set, exactly 37 bytes. The
   bytes after the first 37 are not parsed; the signature covers them.
7. for a registration: flag AT set, and the layout of check R11 (section 5.5)

Flags BE and BS are otherwise ignored. A passkey that the platform syncs between the person's own
devices is accepted (DESIGN.md §24.2).

### 4.3 Assertion signature

For a sign-in the passkey signature must verify, with ECDSA over P-256 and SHA-256, using the
passkey public key of the keys-file entry, over

```text
authenticator_data ‖ SHA-256(client_data_json)
```

where both are the exact received bytes and the signature has the passkey signature encoding of
section 2.

### 4.4 Signature counter

The hub keeps one counter per keys-file passkey entry (table `passkey_counter`, section 12). With
`stored` the kept value and `received` the counter in the authenticator data:

| Case | Result |
|---|---|
| No kept value | Accepted; `received` is kept |
| `stored` = 0 and `received` = 0 | Accepted. This is the normal case of a synced passkey, whose counter is always 0. |
| `received` > `stored` | Accepted; `received` is kept |
| every other case (`received` ≤ `stored` with one of them not 0) | Refused, `counter_regressed` |

The comparison and the update are one transaction. A counter is checked only on a sign-in at the
hub; a device does not check it (section 9). A passkey that is refused for `counter_regressed` is
replaced through a new invite.

## 5. Invites

### 5.1 Creating an invite

```text
POST /api/key/invites
```

Needs a signed request (section 7) by a key with the scope `keys:invite`. By KEY-PROTOCOL.md
section 3 only a `staff` entry can hold `keys:invite`, and a `webauthn` key never signs a request
itself, so only a session key certified by an administrator's passkey reaches the endpoint. Every
other key gets the signed `403` `{"error":"scope_missing"}` of check 9. The administrator may
invite their own handle for a further passkey.

Request body, exactly these three fields:

```json
{"handle":"example-staff","team":"example","lifetime_seconds":3600}
```

| Field | Rule |
|---|---|
| `handle` | The handle rule of KEY-PROTOCOL.md section 3, and not a login in the `agents` map of `teams.yaml` |
| `team` | A team in `teams.yaml`. The handle need not be a member yet; the loader checks membership when the keys-file pull request is deployed (decision 12). |
| `lifetime_seconds` | A JSON integer (not a boolean) from `INVITE_MIN_SECONDS` to `INVITE_MAX_SECONDS` |

Checks after check 9, in this order; every answer is signed (KEY-PROTOCOL.md section 6):

| # | Check | Status | `error` |
|---|---|---|---|
| I1 | The body is one JSON object with exactly the three fields, each valid | 400 | `invite_invalid` |
| I2 | Fewer than `INVITE_OPEN_LIMIT` invites are neither expired nor received | 409 | `invite_limit` |

On success the hub creates the invite id and the token, stores the invite (section 12) with the
SHA-256 of the token and never the token itself, and answers `201`:

```json
{"invite":"invite.0f1e2d3c4b5a69788796a5b4c3d2e1f0","handle":"example-staff","team":"example","created":"2026-11-17T10:00:00Z","expires":"2026-11-17T11:00:00Z","link":"https://hub.example/invite#<token>","hub_key":"hub-1"}
```

- `expires` is now plus `lifetime_seconds`.
- The token stands only in the fragment of the link. A browser never sends a fragment to a
  server, so the token is not in any request line, access log or `Referer`.
- This answer is the only place the token ever appears. It is never logged and never shown again.
- The hub stores with the invite the complete signed creation request: the four `Agent-*`
  headers, the method, the request target and the body. That is the administrator's signed
  statement that the invite was created (DESIGN.md §24.2) until the team record exists; the build
  step of the team record turns these rows into events.

### 5.2 Listing invites

```text
GET /api/key/invites
```

Needs a signed request by a key with `keys:invite`. Answer `200`, signed:

```json
{"invites":[{"invite":"invite.0f1e…","handle":"example-staff","team":"example","created":"2026-11-17T10:00:00Z","expires":"2026-11-17T11:00:00Z","status":"received","created_by":"session.5d1c…","received":"2026-11-17T10:04:12Z","credential_id":"<base64url>","public_key":"<base64url>","fingerprint":"3f2a 9c01 77de 0b4e 5a1c 2e9f 8d33 a0b7","keys_entry":"…","pull_request_title":"…","pull_request_body":"…"}],"hub_key":"hub-1"}
```

- The list holds every invite, newest first by the latest change (`received`, else `opened`,
  else `created`), then by invite id descending (CONTRIBUTING.md, "Dashboard lists").
- `status` is exactly one of: `pending` (not opened, not expired), `opened` (opened, no key
  received, the ceremony may still finish), `received` (a public key was received), `expired`
  (no key was received and the invite expired or its ceremony time passed).
- `created_by` is the session key id that created it, or `operator` for a bootstrap invite.
- `received`, `credential_id`, `public_key`, `fingerprint`, `keys_entry`, `pull_request_title`
  and `pull_request_body` are present only for status `received`.
- The query is ignored. No token and no token hash is ever in the answer.

### 5.3 The bootstrap invite

The administrator's own first passkey cannot be invited by anyone (DESIGN.md §24.2). It is created
through a bootstrap invite made by an operator on the hub host:

```text
python -m agent_core invite-bootstrap --handle HANDLE --team TEAM --seconds N
python -m agent_core invite-list
```

- Both commands read the same environment variables as the hub and use the same database.
- `invite-bootstrap` validates its arguments like I1 and checks I2. It refuses, writes nothing
  and exits with status 1 when the keys file, read at the current time, has an entry that is not
  revoked and holds `keys:invite`. Otherwise it creates an invite exactly as in section 5.1 with
  `created_by` `operator` and no stored request, and prints only the link.
- `invite-list` prints the list of section 5.2 as JSON, one invite per line.
- This is the path for the first administrator, for an administrator who lost every passkey (a
  pull request revokes them first and is deployed), and nothing else. It registers nothing: the
  passkey counts only after the pull request made by hand that adds it with `keys:invite`.
- Handing the role to another person needs no invite: one pull request removes `keys:invite` from
  the old administrator's entries and adds it to the new administrator's existing entries; the
  loader rule "at most one handle holds `keys:invite`" holds in every merged state.

### 5.4 Opening an invite

The invited person opens `https://hub.example/invite#<token>`. `GET /invite` serves a static page
without any data (section 10). The page reads the token from the fragment, removes the fragment
with `history.replaceState`, and sends:

```text
POST /auth/invite/open
{"token":"<token>"}
```

This is the one request that carries no registered key (DESIGN.md §24.2); the token
authenticates it. Checks in this order; every refusal is `{"error":"<value>"}`:

| # | Check | Status | `error` |
|---|---|---|---|
| O1 | The body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object with exactly `token`, and the token decodes to 32 bytes | 400 | `request_invalid` |
| O2 | An invite with the SHA-256 of the token exists | 401 | `invite_unknown` |
| O3 | The invite was not opened before | 401 | `invite_used` |
| O4 | The invite has not expired | 401 | `invite_expired` |

O3 and the marking as opened are one atomic update (`… SET opened = ? WHERE id = ? AND opened IS
NULL`); of two parallel requests exactly one passes. **Opening consumes the invite**: a ceremony
that the person cancels, or that fails, needs a new invite (decision 9).

On success the hub creates a registration (section 12): a registration id, a 32-byte random
challenge and a 16-byte random user id. It answers `200`:

```json
{"registration":"<id>","handle":"example-staff","rp_id":"hub.example","rp_name":"agent-core","user_id":"<base64url, 16 bytes>","challenge":"<base64url, 32 bytes>","exclude_credentials":["<base64url>"],"timeout_ms":300000}
```

`exclude_credentials` lists the credential ids of every keys-file entry with this handle, revoked
or not, so that an authenticator does not create a second passkey for the same person.

### 5.5 What the invited person's browser does, and what the hub checks

The page calls `navigator.credentials.create` with exactly these options:

```js
{publicKey: {
  rp: {id: rp_id, name: rp_name},
  user: {id: user_id, name: handle, displayName: handle},
  challenge: challenge,
  pubKeyCredParams: [{type: "public-key", alg: -7}],
  authenticatorSelection: {residentKey: "required", requireResidentKey: true, userVerification: "required"},
  attestation: "none",
  excludeCredentials: exclude_credentials.map(id => ({type: "public-key", id})),
  timeout: timeout_ms
}}
```

It then sends:

```text
POST /auth/invite/finish
{"registration":"<id>","credential_id":"<base64url>","client_data_json":"<base64url>","attestation_object":"<base64url>"}
```

Checks in this order; every refusal is `{"error":"<value>"}`:

| # | Check | Status | `error` |
|---|---|---|---|
| R1 | The body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object with exactly these four text fields, each canonical base64url | 400 | `request_invalid` |
| R2 | The registration exists and was not finished before. This atomic update also consumes it when a later check fails. | 401 | `registration_unknown` |
| R3 | At most `CEREMONY_SECONDS` passed since the invite was opened, and the invite has not expired | 401 | `registration_expired` |
| R4 | Client data, section 4.1, type `webauthn.create`, challenge the registration's challenge: a failure of step 1 or 2 | 401 | `client_data_invalid` |
| R5 | Section 4.1 step 3 | 401 | `challenge_mismatch` |
| R6 | Section 4.1 steps 4 and 5 | 401 | `origin_mismatch` |
| R7 | `attestation_object` decodes as one CBOR map with exactly the keys `fmt` (text), `attStmt` (map) and `authData` (bytes), and no data after it. `fmt` and `attStmt` are otherwise ignored: no attestation is verified (decision 10). | 401 | `attestation_invalid` |
| R8 | Section 4.2 step 2 on `authData` | 401 | `rp_id_mismatch` |
| R9 | Section 4.2 step 3 | 401 | `user_presence_missing` |
| R10 | Section 4.2 step 4 | 401 | `user_verification_missing` |
| R11 | Section 4.2 steps 1, 5 and 7: after the first 37 bytes come the AAGUID (16 bytes), the credential id length L (2 bytes, big-endian), the credential id (L bytes), and one CBOR item, the credential public key; when flag ED is set, one CBOR map follows; then nothing | 401 | `authenticator_data_invalid` |
| R12 | L is from 16 to 1023, the credential id equals `credential_id` of the body, and the credential public key is a CBOR map with exactly the entries `1: 2`, `3: -7`, `-1: 1`, `-2:` 32 bytes, `-3:` 32 bytes, whose point `0x04 ‖ x ‖ y` is a valid P-256 point | 401 | `credential_invalid` |
| R13 | Neither the credential id nor the public key appears in the keys file or in another received invite | 409 | `credential_known` |

On success the hub stores the credential id, the public key, the receive time and the proposed
keys-file id with the invite, sets its status to `received`, and answers `200`:

```json
{"handle":"example-staff","fingerprint":"3f2a 9c01 77de 0b4e 5a1c 2e9f 8d33 a0b7"}
```

The page also computes the fingerprint itself from the public key that
`AuthenticatorAttestationResponse.getPublicKey()` returns (the last 65 bytes of that P-256
SubjectPublicKeyInfo) and shows it. When the two differ, it shows an error and no fingerprint.
The page tells the person to compare this fingerprint with the administrator over a channel other
than the link.

The hub authors no event for the received key (DESIGN.md §24.2). The received key is not
registered: sign-in looks only into the keys file (section 6.3, S4).

### 5.6 The text for the keys-file pull request

The hub opens no pull request and holds no code-host credential for it. For a received invite it
produces three texts, shown in the list of section 5.2 and on the `/passkey` page of the
administrator.

The proposed id is `<handle>-passkey-<n>`, where `n` is the smallest integer from 1 for which the
id is neither in the loaded keys file nor the proposed id of another received invite. The
administrator checks it against the current keys file before merging; the loader refuses a
duplicate.

`keys_entry`, exactly, with two spaces of indentation so that it is pasted under `keys:`:

```yaml
  - id: example-staff-passkey-1
    kind: webauthn
    principal: staff
    handle: example-staff
    team: example
    credential_id: "<base64url>"
    public_key: "<base64url, 65 bytes>"
    scopes: [record:read, record:write]
```

The scopes are always `[record:read, record:write]`, the team scopes of DESIGN.md §24.2.
`keys:invite` is never proposed; the administrator adds it by hand to their own entry.

`pull_request_title`, exactly: `Add passkey <id> for <handle>`.

`pull_request_body`, exactly (lines joined by `\n`):

```text
Adds the passkey received through invite <invite id> to keys.yaml.

- Handle: <handle>
- Team: <team>
- Fingerprint: <fingerprint>
- Received: <received time>

Before merging, the administrator compares this fingerprint with <handle> over a channel other than the invite link (PASSKEY-PROTOCOL.md section 5.5). If <handle> is not yet a member of <team> in teams.yaml, this pull request adds the membership.
```

The comparison protects against an intercepted link. It does not protect against a hub that was
taken over, because the fingerprint the invited person sees comes from code the hub delivers
(DESIGN.md §25.16).

## 6. Sign-in and the certificate

### 6.1 Begin

The browser first creates the session key (section 10.2), then sends:

```text
POST /auth/passkey/begin
{"session_public_key":"<base64url, 65 bytes>"}
```

| # | Check | Status | `error` |
|---|---|---|---|
| B1 | The body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object with exactly `session_public_key`, which decodes to a valid 65-byte P-256 point | 400 | `request_invalid` |
| B2 | The key is not the session public key of a stored session or of a pending sign-in | 409 | `session_key_known` |
| B3 | Fewer than `PENDING_SIGN_IN_LIMIT` pending sign-ins exist; pending sign-ins older than `CEREMONY_SECONDS` are deleted first | 429 | `too_many_pending` |

The hub creates a sign-in id and a 32-byte random hub value, builds the statement (section 6.2)
with `expires` = now + `SESSION_KEY_ISSUE_SECONDS`, stores the pending sign-in (section 12) and
answers `200`:

```json
{"sign_in":"<id>","statement":"<the statement text>","challenge":"<base64url, 32 bytes>","rp_id":"hub.example","timeout_ms":300000}
```

`challenge` is SHA-256 over the statement bytes.

This request carries no registered key, because the passkey can sign only after it. It reveals
nothing about any key or person and can do nothing but hand out a challenge. DESIGN.md §24.2 names
opening an invite link as the only request without a registered key; this second exception is
unavoidable and is recorded as decision 28.

### 6.2 The statement

The statement is UTF-8 text of five lines, joined by `\n`, without a trailing line feed:

```text
agent-session-v1
<origin>
<session public key>
<hub value>
<expires>
```

| Line | Value |
|---|---|
| 1 | The label `agent-session-v1` |
| 2 | The origin of section 3, for example `https://hub.example` |
| 3 | The session public key, base64url of 65 bytes |
| 4 | The hub value, base64url of 32 bytes |
| 5 | The expiry, `YYYY-MM-DDTHH:MM:SSZ` |

The WebAuthn challenge is SHA-256 over these bytes. The passkey's assertion signature covers
`SHA-256(client_data_json)`, which contains that challenge, so the passkey signs the origin, the
session key, the hub's fresh value and the expiry together.

The **certificate** of a session key is the five values: the statement bytes, `credential_id`,
`authenticator_data`, `client_data_json` and `signature` of the assertion. Its JSON form, used
wherever it travels:

```json
{"statement":"<base64url of the statement bytes>","credential_id":"<base64url>","authenticator_data":"<base64url>","client_data_json":"<base64url>","signature":"<base64url>"}
```

The **session key id** is `session.` and the first 32 hex characters of SHA-256 over the
statement bytes.

### 6.3 Finish

The browser shows the fingerprint of its session key, checks that the statement it received has
its own session public key on line 3, the origin on line 2, and an expiry no later than its clock
plus `SESSION_KEY_MAX_SECONDS`, and that `challenge` is SHA-256 of the statement. Otherwise it
stops. It then calls `navigator.credentials.get` with exactly:

```js
{publicKey: {challenge: challenge, rpId: rp_id, allowCredentials: [], userVerification: "required", timeout: timeout_ms}}
```

and sends:

```text
POST /auth/passkey/finish
{"sign_in":"<id>","credential_id":"<base64url>","authenticator_data":"<base64url>","client_data_json":"<base64url>","signature":"<base64url>","user_handle":"<base64url>"}
```

`user_handle` may be `null`. It is checked for form only and otherwise ignored: the passkey is
identified by its credential id.

Checks in this order; every refusal is `{"error":"<value>"}`:

| # | Check | Status | `error` |
|---|---|---|---|
| S1 | The body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object with exactly these six fields, each canonical base64url text (`user_handle` also `null`) | 400 | `request_invalid` |
| S2 | The pending sign-in exists. It is deleted in the same atomic step, so it is consumed also when a later check fails, and a second finish with the same id gets this refusal. | 401 | `challenge_unknown` |
| S3 | At most `CEREMONY_SECONDS` passed since begin | 401 | `challenge_expired` |
| S4 | `credential_id` equals the `credential_id` of a `staff` entry in the keys file. A key received through an invite and not yet in the keys file is unknown. | 401 | `credential_unknown` |
| S5 | That entry is not revoked | 401 | `key_revoked` |
| S6 | Section 4.1 steps 1 and 2 with type `webauthn.get` | 401 | `client_data_invalid` |
| S7 | Section 4.1 step 3 with the challenge of S2 | 401 | `challenge_mismatch` |
| S8 | Section 4.1 steps 4 and 5 | 401 | `origin_mismatch` |
| S9 | Section 4.2 step 2 | 401 | `rp_id_mismatch` |
| S10 | Section 4.2 step 3 | 401 | `user_presence_missing` |
| S11 | Section 4.2 step 4 | 401 | `user_verification_missing` |
| S12 | Section 4.2 steps 1, 5 and 6 | 401 | `authenticator_data_invalid` |
| S13 | Section 4.3 | 401 | `signature_invalid` |
| S14 | Section 4.4 | 401 | `counter_regressed` |
| S15 | The statement's expiry is later than now and not later than now + `SESSION_KEY_MAX_SECONDS` | 401 | `certificate_expiry_invalid` |

Steps S6 to S15 are one function, `verify_certificate`, shared with the device checks of section 9
(without S14, whose counter only the hub keeps). On success the hub stores the session (section
12) with the certificate, the passkey's keys-file id, its credential id and public key, the handle
and the expiry, and answers `200`:

```json
{"session_key":"session.5d1c…","passkey":"example-staff-passkey-1","handle":"example-staff","expires":"2026-11-17T21:58:00Z"}
```

This answer is not signed (it is outside `/api/key/`). The browser stores the session key id with
its key (section 10.2) and then calls `GET /api/key/whoami` with a signed request; it treats itself
as signed in only after that answer is `200`.

That is the only time the passkey is asked during a session. Every later request and command is
signed by the session key without a further confirmation.

## 7. Signed requests by a session key

A session key signs requests under `/api/key/` exactly as KEY-PROTOCOL.md section 5 describes,
with the same four headers and the same eight-line text: `Agent-Key` is the session key id, and
`Agent-Signature` is the session key signature (section 2) over the text, ECDSA over P-256 with
SHA-256. Both signature encodings are 64 bytes, so check 1 keeps its length rule.

### 7.1 The checks

The checks of KEY-PROTOCOL.md section 5 run in the same order. They change as follows; a check
not named here is unchanged for every key:

| # | Check | Status | `error` |
|---|---|---|---|
| 1 | As before, except that `Agent-Key` follows the `id` rule **or** the session key id form | 401 | `signature_missing` |
| 2 | A keys-file id names an entry in `keys`. A session key id names a stored session that was not ended, whose passkey id names an entry in `keys` | 401 | `key_unknown` |
| 3 | The entry is not revoked; for a session key, the passkey entry is not revoked | 401 | `key_revoked` |
| 4 | The entry is not expired; for a session key, its expiry is later than now | 401 | `key_expired` |
| 5 | For a keys-file id: the entry's kind is `ed25519` (unchanged; a `webauthn` key still never signs a request itself) | 401 | `key_kind_not_allowed` |
| 5 | For a session key id: the passkey entry has kind `webauthn`, and its `credential_id` and `public_key` are exactly those the certificate was verified with at sign-in | 401 | `certificate_invalid` |
| 6 | Unchanged | 401 | `time_skew` |
| 7 | For a keys-file id unchanged. For a session key id: the session key signature verifies with the session public key | 401 | `signature_invalid` |
| 8 | Unchanged; the key id stored with the nonce is the session key id | 401 | `nonce_replayed` |
| 9 | The key has the scope the endpoint needs. A session key has the scopes of its passkey entry as loaded now. | 403 | `scope_missing` |
| 9a | The endpoint is not marked controller-only; a session key never passes this check | 403 | `session_key_not_allowed` |
| 10 | Unchanged; it applies only to `service` keys | 403 | `subject_not_allowed` |

- Check 9a runs after check 9 for every key and refuses only session keys. A route declares
  `controller_only=True` when it accepts something that only a controller device key may sign:
  a change of an allow list, a grant, the removal of a grant, a masking (DESIGN.md §25.6, §25.9).
  No such route exists in this build step; the check is tested through a route that exists only
  in the test suite. Section 8.3 lists the other places that keep a session key from signing such
  a statement.
- Checks 2 to 5 for a session key read the session row once; the read is part of check 2. Ending
  a session (section 7.3) makes check 2 refuse it.
- A revocation of the passkey takes effect, as every keys-file change, with the next deployment
  of the keys file; from then on check 3 refuses every session it certified.
- The answer is signed exactly as in KEY-PROTOCOL.md section 6, with the session key id as
  `<Agent-Key of the request>`.
- Every request of a session key that passed check 8 writes exactly one log line
  `key request <session key id> <passkey id> <method> <raw path without query> <status>`, like the
  line for a `service` key (decision 18). No body, no certificate and no signature is logged.
- Requests by session keys are not cross-site forgeable: they carry no cookie, and a page of
  another origin can neither read the session key nor set the `Agent-*` headers on a same-origin
  request.

### 7.2 `GET /api/key/whoami` for a session key

Needs no scope. Answer `200`, signed, with exactly these fields in this order:

```json
{"key":"session.5d1c…","kind":"session","principal":"staff","handle":"example-staff","team":"example","scopes":["record:read","record:write"],"hub_key":"hub-1","passkey":"example-staff-passkey-1","expires":"2026-11-17T21:58:00Z"}
```

The answer for a keys-file key is unchanged (KEY-PROTOCOL.md section 7).

### 7.3 `POST /api/key/session/end`

Signs out. Needs a signed request by a session key and no scope; the body is ignored. The hub
sets the session's `ended` time and answers `200` `{"ended":true,"hub_key":"hub-1"}`, signed. A
keys-file key gets the signed `403` `{"error":"session_key_required"}`. After this, every request
with that session key id is refused at check 2. The browser deletes the session key (section
10.2) whatever the answer.

## 8. Commands signed by a session key

A command is signed on its own, apart from the HTTP request that carries it, so that a device can
verify it later without the hub (DESIGN.md §25.6).

### 8.1 The command text

UTF-8, seven lines joined by `\n`, without a trailing line feed:

```text
agent-command-v1
<sender key id>
<target>
<action>
<sequence>
<time>
<content hash>
```

| Line | Value |
|---|---|
| 1 | The label `agent-command-v1` |
| 2 | The key id that signs: a session key id in this step |
| 3 | The keys-file id of the target device's `agent` key |
| 4 | The action, from the closed list below |
| 5 | The sequence number, decimal, 1 to `SEQUENCE_MAX`, no sign, no leading zero |
| 6 | The sender's clock, `YYYY-MM-DDTHH:MM:SSZ` |
| 7 | `-` when the action carries no content; otherwise SHA-256 hex over a 16-byte random value followed by the UTF-8 content. Content and random value travel beside the signed command (DESIGN.md §25.6). |

Actions in this build step, closed: `noop`. A `noop` command carries no content and does nothing on
a device; it proves the chain from passkey to device. Every later action is added to this list by
the pull request of the build step that needs it. A change of an allow list, a grant, the removal
of a grant and a masking are not commands (DESIGN.md §25.2) and never become actions.

### 8.2 `POST /api/key/commands`

Needs a signed request (section 7) with the scope `record:write`. Body, exactly:

```json
{"command":{"target":"example-staff-laptop-1","action":"noop","sequence":1,"time":"2026-11-17T10:00:00Z","content_hash":"-"},"signature":"<base64url, 64 bytes>"}
```

`sequence` is a JSON integer (not a boolean). The command's sender is the request's `Agent-Key`.
Checks after check 9a, in this order; every answer is signed:

| # | Check | Status | `error` |
|---|---|---|---|
| C1 | The request's key is a session key | 403 | `sender_not_allowed` |
| C2 | The body is one JSON object with exactly `command` and `signature`; `command` has exactly the five fields, each in the form of section 8.1; `action` is in the list; `content_hash` is `-` for `noop`; `signature` decodes to 64 bytes | 400 | `command_invalid` |
| C3 | `time` is at most `COMMAND_TIME_WINDOW` seconds from now | 400 | `command_time_skew` |
| C4 | The signature verifies, with the session public key, over the command text built from the fields and the sender | 403 | `command_signature_invalid` |
| C5 | `target` names an `agent` entry of the session's handle that is neither revoked nor expired | 403 | `target_not_allowed` |
| C6 | `sequence` is higher than the last sequence the hub accepted from this sender; the comparison and the update are one atomic step | 409 | `sequence_not_higher` |

A staff member signed in with a passkey has controller rights over their own devices (DESIGN.md
§25.4); C5 allows exactly those. Commands to another staff member's device need a grant and are
not possible in this build step.

On success the hub answers `200`:

```json
{"command":"<SHA-256 hex of the command text>","target":"example-staff-laptop-1","action":"noop","sequence":1,"delivered":false,"hub_key":"hub-1"}
```

`delivered` is `false` in this build step: the hub verifies and stores nothing but the sequence
number. Delivery comes with DESIGN.md §25.20 step 4. The website never sends a command again
because of a hub refusal; it sends again with a new number only after a refusal signed by the
device (DESIGN.md §25.6), which does not exist yet.

### 8.3 What a session key never signs

A session key signs exactly two kinds of text: a request (`agent-request-v1`, section 7) and a
command (`agent-command-v1`, section 8.1). It never signs a statement that changes an allow list,
a grant, the removal of a grant, a masking, a certificate for another key, or a team-record event
of a kind reserved to a controller device. This holds by five independent rules:

1. The signing function of the website (section 10.3) refuses every text whose first line is not
   one of these two labels.
2. Every signed text starts with its own label, so a signature over one kind is never valid as
   another kind.
3. The action list of section 8.1 contains no such statement.
4. A hub route that accepts such a statement is marked controller-only and refuses every session
   key at check 9a.
5. A device accepts such a statement only when the signer's allow-list entry is a controller
   device key (DESIGN.md §25.6). A session key is never one.

When the team record is built, an event signed by a session key is refused when the session is
ended, its expiry is at or before the hub's receive time, or its passkey is revoked, with the
errors of checks 2 to 4.

## 9. What a device checks

This section is binding for the client repository (DESIGN.md §25.20 step 2). A device receives,
with every command from a signed-in staff member, this bundle:

```json
{"sender":"session.5d1c…","certificate":{…section 6.2…},"command":{…section 8.2…},"signature":"<base64url>"}
```

The device's configuration holds the hub's public URL, from which it derives the origin and the
rp id of section 3, its own device key id, the time window, and the longest session-key lifetime,
which is at most `SESSION_KEY_MAX_SECONDS`. The device checks, in this order, and refuses at the
first failure:

1. The allow list has an entry for a passkey whose credential id equals `certificate.credential_id`
   and whose public key is the one the owner confirmed; the entry is not revoked and not
   suspended.
2. The certificate verifies with that public key exactly as section 6.3 steps S6 to S13, with the
   origin and rp id of its own configuration. The signature counter is not checked.
3. The statement has exactly five lines; line 1 is `agent-session-v1`; line 2 is its configured
   origin; line 3 decodes to a valid P-256 point; line 4 decodes to 32 bytes.
4. The expiry on line 5 is later than the device's clock and not later than the device's clock plus
   its configured longest lifetime.
5. `sender` is `session.` and the first 32 hex characters of SHA-256 over the statement bytes.
6. The command signature verifies with the session public key of line 3 over the command text of
   section 8.1.
7. `target` is the device's own key id; `time` is inside its configured window; `sequence` is
   higher than the last one it accepted from this sender, and the command was not seen before.
8. The action is permitted by the allow-list entry of step 1.

The device writes the bundle into its local log before it runs the command. A device cannot verify
when the certificate was signed; DESIGN.md §25.6 states the consequence.

## 10. The website

### 10.1 Pages

| Path | Method | Serves |
|---|---|---|
| `/passkey` | `GET`, `HEAD` | Static page: sign-in with a passkey; when signed in, the handle, the passkey id, the session key fingerprint and expiry, the `noop` command form (target id typed by hand), the invite form and the invite list for a key with `keys:invite`, and sign-out |
| `/invite` | `GET`, `HEAD` | Static page: opens an invite from the fragment and creates the passkey |
| `/static/passkey.js` | `GET`, `HEAD` | The script of both pages |

- The pages contain no data and no inline script. They are served with the headers
  `Content-Security-Policy: default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- The dashboard `/` gets one link "Sign in with a passkey" to `/passkey`, next to the GitHub
  button. Nothing else on `/` changes.
- The pages load nothing from another origin.

### 10.2 The session key in the browser

- At sign-in the page creates the session key with
  `crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"}, false, ["sign", "verify"])`.
  The private key is not extractable. Its public key is exported with `exportKey("raw", …)`.
- The page stores the key pair, the session key id and the expiry in IndexedDB (database
  `agent-core`, store `session`, one record with the key `current`). IndexedDB keeps a
  non-extractable `CryptoKey` without exposing its bytes. A new sign-in replaces the record.
- The page deletes the record on sign-out, when the expiry passed, and when a signed request is
  refused with `key_unknown`, `key_revoked`, `key_expired` or `certificate_invalid`.
- The next command sequence number is kept in the same record and incremented in one IndexedDB
  transaction before the command is signed.

### 10.3 Signing in the browser

`/static/passkey.js` has one function that signs, `signText(lines)`. It refuses, without signing,
every list whose first element is not `agent-request-v1` or `agent-command-v1`. It builds the
request text exactly as KEY-PROTOCOL.md section 5, with the host taken from the public URL that the
hub writes into the page as it does for the dashboard today (`__PUBLIC_URL__`), and the request
target from a fixed path and a query built only with `encodeURIComponent`. Signatures use
`crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, privateKey, text)`.

The website does not verify the hub's signature on answers: it would have to take the hub key from
the hub itself, which proves nothing (decision 17).

## 11. Coexistence with GitHub sign-in

- GitHub sign-in, the cookie session, the OAuth token, device pairing and every existing endpoint
  stay exactly as they are until the following build step removes them.
- The passkey path uses no cookie, does not read or write `request.session`, never reads the
  OAuth token, the `device` table or `teams.yaml` logins as identity, and never calls GitHub. Its
  identity is the keys-file entry alone.
- A passkey session is not a GitHub session and a GitHub session is not a passkey session: a
  cookie gives no access to any route of this document, and a session key gives no access to any
  cookie route. A browser may hold both at once; they are independent.
- The handle of a passkey and a GitHub login may be the same text. That is never used to join
  them in this build step.
- A test proves that every passkey route works when every GitHub call fails, and that no passkey
  route reads the cookie session (section 13).

## 12. Storage

New tables, created at start like the existing ones:

| Table | Columns | Rules |
|---|---|---|
| `passkey_invite` | `id` (primary key), `token_hash` (unique), `handle`, `team`, `created` (integer seconds), `expires`, `created_by`, `creation_request` (JSON of the signed request, or null for a bootstrap invite), `opened` (nullable), `received` (nullable), `credential_id` (nullable), `public_key` (nullable), `proposed_id` (nullable) | Kept, never deleted |
| `passkey_registration` | `id` (primary key), `invite_id`, `challenge`, `user_id`, `created`, `finished` (nullable) | Kept with its invite |
| `passkey_sign_in` | `id` (primary key), `statement`, `session_public_key` (unique), `created` | Deleted when consumed (S2) or older than `CEREMONY_SECONDS` |
| `passkey_session` | `id` (primary key, the session key id), `passkey_id`, `handle`, `credential_id`, `passkey_public_key`, `session_public_key` (unique), `statement`, `authenticator_data`, `client_data_json`, `signature`, `created`, `expires`, `ended` (nullable) | Kept, never deleted: a certificate is evidence of what its key signed |
| `passkey_counter` | `passkey_id` (primary key), `sign_count` | One row per passkey after its first sign-in |
| `command_sequence` | `sender` (primary key), `last_sequence` | Updated atomically by C6 |

All binary values are stored as base64url text, all times as integer seconds. No table holds a
token, a private key or a secret.

## 13. Tests this step must carry

Every hub test runs through the application with a fixed clock, asserts the exact status and the
exact body, and asserts the hub signature on every answer under `/api/key/` after check 8.

### 13.1 The software authenticator

The tests use a software authenticator written for this repository in
`tests/webauthn_authenticator.py`, built on `cryptography` and `cbor2` (decision 4). The package
`soft-webauthn` is not used: it never sets the UV flag, so it cannot pass a sign-in that requires
user verification, and it cannot be told to produce a wrong origin, a wrong rp id, a missing flag
or a counter that goes backwards without patching it.

The software authenticator:

- creates a real P-256 key pair and a random credential id of 16 to 64 bytes per credential
- `create(options, origin)` returns `credential_id`, `client_data_json` and an
  `attestation_object` in CBOR with `fmt` `none`, exactly as a browser sends them
- `get(options, origin)` returns `credential_id`, `authenticator_data`, `client_data_json`,
  `signature` (DER) and `user_handle`
- has settings for: the origin and rp id it puts in, the flags (UP, UV, BE, BS, AT, ED), the
  counter (fixed, increasing, or a given value), `crossOrigin`, `topOrigin`, the client data
  `type`, and a corrupted signature
- signs with `cryptography`'s ECDSA over exactly the bytes of section 4.3

The hub verifies its responses with the production code: the same functions, called through the
real endpoints.

### 13.2 The hub, invites

- an administrator's session key creates an invite: signed `201` with the exact body, the link
  holds the token in the fragment, the database holds only the token hash and the signed creation
  request
- a session key whose passkey lacks `keys:invite`, an `agent` key and a `service` key get the
  signed `403` `scope_missing`; a `webauthn` key used directly gets `key_kind_not_allowed`
- `invite_invalid` for: each field missing, an extra field, a handle outside the rule, a handle
  that is an agent login, an unknown team, a lifetime of 299, 86401, a boolean, a string and a
  float; 300 and 86400 are accepted
- `invite_limit` when 50 invites are open; an expired or received invite does not count
- open: `invite_unknown` for an unknown token, `invite_used` for a second open, `invite_expired`
  at and after the expiry time, `request_invalid` for a malformed body and a body over the limit;
  two parallel opens of one invite (exactly one succeeds)
- `exclude_credentials` lists the handle's keys-file credential ids, revoked ones included
- registration: each refusal R1 to R13 in its order, produced by the software authenticator:
  a reused registration, a registration older than 300 seconds, an invite that expired during the
  ceremony, type `webauthn.get`, a wrong challenge, a wrong origin, `crossOrigin` true, a
  `topOrigin`, an attestation object with an extra key or trailing bytes, a wrong rp id, UP not
  set, UV not set, AT not set, BS without BE, trailing bytes without ED, a credential id that
  differs from the body, a credential id of 15 and of 1024 bytes, an RS256 and an Ed25519
  credential key, a point not on the curve, a credential already in the keys file, and one already
  received by another invite
- a successful registration: the fingerprint in the answer, status `received`, the exact
  `keys_entry`, `pull_request_title` and `pull_request_body`, and the proposed id skipping an id in
  the keys file and an id proposed for another invite
- the list: newest first by latest change, every status, no token or token hash in the answer
- a received key that is not in the keys file cannot sign in (`credential_unknown`)
- `invite-bootstrap` creates an invite when no unrevoked entry holds `keys:invite`, refuses with
  status 1 and writes nothing when one does, and creates one again after that entry is revoked;
  `invite-list` prints the received key and the keys-file text

### 13.3 The hub, sign-in and the certificate

- a successful sign-in: the stored session, the exact answer, the session key id derived from the
  statement, and a following signed `whoami` with the exact body of section 7.2
- begin: `request_invalid` for a missing, extra or malformed field and an invalid point,
  `session_key_known` for a session public key already in use, `too_many_pending` at the limit,
  and deletion of pending sign-ins older than 300 seconds
- finish: each refusal S1 to S15 in its order: a reused challenge (second finish), an expired
  challenge (301 seconds), a wrong origin, a wrong rp id, a wrong signature, an unknown
  credential, a credential received through an invite but not in the keys file, a revoked keys-file
  entry, a revoked entry whose `revoked` time is in the future (accepted), UV not set, UP not set,
  AT set in an assertion, type `webauthn.create`, a challenge for another statement
- the counter: 0 then 0 accepted, 5 then 6 accepted, 6 then 6 refused, 6 then 3 refused, 6 then 0
  refused; two parallel sign-ins with the same counter value (exactly one accepted)
- `verify_certificate` directly: a statement whose expiry is beyond now + 43200 (refused), exactly
  now + 43200 (accepted), at now (refused); a statement with four or six lines, another label,
  another origin, a key that is not a point, a hub value of 31 bytes
- an entry with `expires` on a `staff` entry is refused by the loader (unchanged rule), so an
  expired passkey entry cannot exist

### 13.4 The hub, signed requests by a session key

- each changed check of section 7.1 in its order: a session key id in the wrong form
  (`signature_missing`), an unknown session id, an ended session (`key_unknown`), a session whose
  passkey is revoked (`key_revoked`), a session at and after its expiry (`key_expired`), a session
  whose passkey entry changed its public key or credential id (`certificate_invalid`), a session
  key signature over another text, an `r` or `s` of 0 or of n, a DER signature instead of `r ‖ s`
  (`signature_invalid`), a replayed nonce
- a high `s` is accepted
- check 9: a session key without the scope of a test-only route gets `scope_missing`; check 9a: a
  session key on a test-only controller-only route gets `session_key_not_allowed`, an `agent` key
  with `device:controller` passes it
- the log line of section 7.1 for a session key, and no certificate, signature or body in any log
  line
- sign-out: signed `200`, then `key_unknown`; a keys-file key gets `session_key_required`
- every existing test of KEY-PROTOCOL.md section 10 still passes unchanged

### 13.5 The hub, commands

- a `noop` command: signed `200` with the exact body
- each refusal C1 to C6 in its order: an `agent` key with `device:controller` (`sender_not_allowed`);
  each field missing or extra, an unknown action, the actions `allowlist.add`, `grant`,
  `grant.remove` and `masking`, a content hash other than `-`, a sequence of 0, `SEQUENCE_MAX` + 1,
  with a leading zero, as a string and as a boolean (`command_invalid`); a time 121 seconds off
  (`command_time_skew`, 120 accepted); a signature over another target, action, sequence, time or
  sender (`command_signature_invalid`); a target of another handle, a revoked target, a `service`
  key as target, an unknown target (`target_not_allowed`); a repeated and a lower sequence
  (`sequence_not_higher`); two parallel commands with the same sequence (exactly one accepted)
- a command signed by an expired session key is refused at check 4

### 13.6 The hub, coexistence and configuration

- every route of this document answers the same with a GitHub fake that fails on every call
- a GitHub cookie session gets `signature_missing` on every passkey route under `/api/key/`, and
  the passkey session gets `401` on `/auth/me`, `/api/state` and `/api/session/control`
- a public URL whose host is an IPv4 or IPv6 address stops the process at start; `localhost`
  starts
- the pages `/passkey` and `/invite` carry the headers of section 10.1 and no inline script
- the tables of section 12 are created at start, and after a full invite and sign-in no row holds
  the invite token or any private key

### 13.7 The website, without a browser

These run with Node.js like the existing dashboard tests:

- `signText` refuses every first line other than `agent-request-v1` and `agent-command-v1`
- the request text and the command text are built byte for byte as in the hub, checked against
  fixed vectors that the hub tests also use
- the statement check of section 6.3 stops for another session key, another origin and an expiry
  beyond 43200 seconds
- the fingerprint function gives the vector of section 2's format

### 13.8 End to end, in headless Chromium

`tests/e2e/test_passkey_e2e.py`, driven by Playwright for Python. Each browser context gets
Chromium's virtual authenticator through the DevTools protocol:

```text
WebAuthn.enable {"enableUI": false}
WebAuthn.addVirtualAuthenticator {"options": {"protocol": "ctap2", "transport": "internal",
  "hasResidentKey": true, "hasUserVerification": true, "isUserVerified": true,
  "automaticPresenceSimulation": true}}
```

The hub runs in the test process under uvicorn on `localhost` and one free port, with the public
URL `http://localhost:<port>`, a temporary database and keys file, and a GitHub stub that fails the
test on any call. Every request of the browser to another host is aborted by `context.route` and
fails the test. Changing the keys file is done by writing the file and restarting the hub on the
same port with the same database, which is what a merged pull request and its deployment do.

The positive path, in one test:

1. The bootstrap command creates an invite for `example-admin`. The admin context opens the link,
   creates the passkey, and the page shows a fingerprint equal to the one `invite-list` prints.
2. The test writes the proposed entry with `keys:invite` added and restarts the hub.
3. The admin context signs in on `/passkey`; the page shows `example-admin`. In the page,
   `crypto.subtle.exportKey("pkcs8", privateKey)` of the stored session key rejects.
4. The admin creates an invite for `example-staff`. A second context with its own virtual
   authenticator opens the link and creates the passkey. Both pages show the same fingerprint.
5. The test writes the proposed entry and an `agent` entry `example-staff-laptop-1` for
   `example-staff`, and restarts the hub.
6. The staff context signs in, sends a `noop` command to `example-staff-laptop-1` and sees the
   hub's answer with `delivered` false.
7. The staff context signs out: the session row has an `ended` time, and IndexedDB holds no
   record.

The negative paths, each in its own context:

- user verification refused: `WebAuthn.setUserVerified {"isUserVerified": false}` before the
  sign-in; the page shows that sign-in failed, the database has no session, IndexedDB has no
  record
- no credential: a context whose authenticator holds no credential; the sign-in fails in the
  page, no session exists, and the pending sign-in stays unconsumed
- an invite link opened a second time shows `invite_used`

What this test does not cover: browsers other than Chromium (Firefox, Safari), real platform
authenticators and their biometric or PIN prompts, security keys over USB or NFC, sign-in from a
phone over the hybrid transport, and the sync of passkeys by a platform. The hub-side handling of a
synced passkey (counter 0, flags BE and BS) is covered by the software authenticator in 13.3.

### 13.9 The client (in the client repository, DESIGN.md §25.20 step 2)

- each device check of section 9 in its order, each refused: an unknown passkey, a revoked or
  suspended entry, every certificate refusal of S6 to S13, an expiry beyond its configured
  lifetime, an expired session key, a wrong sender id, a command signature over another field,
  another target, a time outside the window, a repeated or lower sequence, an action outside the
  entry
- the bundle is written to the local log before the command runs
- an allow-list statement, a grant or a masking signed by a session key is refused

The refusal tests of DESIGN.md §25.21 that concern allow lists, grants, maskings, the signing
service and the team record belong to the build steps that build those parts. This step carries
the ones it can: an unknown key, an expired session key, a replayed command, a command outside its
permission (C5), a session-key expiry beyond the limit, and a session key signing what only a
controller device may sign.

## 14. CI

`.github/workflows/test.yml` gets a second job beside `pytest`:

```yaml
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[test,e2e]"
      - run: python -m playwright install --with-deps chromium
      - run: pytest -m e2e tests/e2e
        env:
          AGENT_CORE_E2E_REQUIRED: "1"
```

- The browser comes from Playwright's own download during the job; no browser service and no
  other external service is called by the tests.
- The tests in `tests/e2e` carry the marker `e2e`. The default `pytest` run deselects them
  (`addopts = "-m 'not e2e'"`), so `pytest` before a push does not need a browser. With
  `AGENT_CORE_E2E_REQUIRED=1` a missing Playwright or Chromium fails the run instead of skipping
  it.
- The job runs on every pull request and every push to `develop` and `main`, like `pytest`.

## 15. Dependencies and configuration

- New runtime dependency: `cbor2>=5.6,<6`, for decoding the attestation object and the credential
  public key. It is also used by the test authenticator for encoding.
- New optional dependency group `e2e`: `playwright>=1.47,<2`.
- `cryptography` (already required) does ECDSA over P-256, SHA-256 and the DER encoding.
- No new environment variable. The rp id and the origin come from `AGENT_CORE_PUBLIC_URL`.
- The README's local run uses `http://localhost:8787` instead of `http://127.0.0.1:8787` (also for
  the GitHub OAuth callback), because of section 3.
- The deployment forwards `POST /auth/passkey/*` and `POST /auth/invite/*` bodies unchanged. Rate
  limits at the network edge cover these anonymous routes; the hub's own limits are B3, I2 and the
  body limit.

## 16. Rollout order

1. Check that the public URL of every environment has a host name, not an IP address. A hub with
   an IP address in its public URL does not start with this change.
2. Merge the change. The tables are created at start; nothing else is needed.
3. The operator runs `invite-bootstrap` for the administrator. The administrator creates the
   passkey, compares the fingerprint with the output of `invite-list`, and opens the pull request
   that adds the entry with `keys:invite` by hand.
4. After that pull request is deployed, the administrator invites every other staff member
   (DESIGN.md §25.15). Each passkey counts after its own pull request is deployed.

GitHub sign-in keeps working during and after these steps. Its removal is the following build
step, and it is merged only after every staff member who uses the website has a passkey in the
keys file.

## 17. Decisions taken in this document

1. One working day is 43200 seconds (12 hours); this is `SESSION_KEY_MAX_SECONDS` for the hub and
   the upper bound of a device's configured lifetime. (Section 2, 6, 9)
2. The hub issues certificates for 43080 seconds, 120 seconds less than the maximum, so that a
   device with a clock up to 120 seconds behind accepts them. (Section 2, 6.1)
3. No server-side WebAuthn library; the verification steps are fixed in section 4 and shared by
   hub and device. (Section 4)
4. Tests use an own software authenticator built on `cryptography` and `cbor2`, not
   `soft-webauthn`, because that package never sets UV and cannot produce the refusal cases.
   (Section 13.1)
5. The session key is ECDSA P-256 with SHA-256 via WebCrypto, non-extractable, signatures as
   64-byte `r ‖ s`; a high `s` is accepted. (Section 2, 10.2)
6. The statement has five lines: label, origin, session public key, hub value, expiry. It holds no
   handle and no passkey id, because a discoverable-credential sign-in does not know the passkey
   before the assertion. (Section 6.2)
7. The session key id is `session.` plus 32 hex characters of SHA-256 over the statement,
   disjoint from keys-file ids by the `.`. (Section 2, 6.2)
8. Check 5 for a session key compares the passkey entry's credential id and public key with those
   the certificate was verified with, error `certificate_invalid`; check 9a refuses session keys
   on controller-only routes, error `session_key_not_allowed`. (Section 7.1)
9. Opening an invite consumes it; a cancelled or failed ceremony needs a new invite. (Section 5.4)
10. No attestation is verified; `fmt` and `attStmt` are ignored. Trust in the key comes from the
    fingerprint comparison and the pull request. (Section 5.5)
11. The invite token is in the URL fragment and only its SHA-256 is stored. (Section 5.1)
12. An invite needs an existing team but not membership of the handle; membership is checked by
    the loader when the keys-file pull request is deployed. (Section 5.1)
13. Invite lifetime is 300 to 86400 seconds; at most 50 invites are open at once. (Section 2, 5.1)
14. The user id of a new passkey is 16 random bytes per invite, never derived from the handle, so
    that no authenticator replaces an existing passkey. (Section 5.4)
15. The administrator's first passkey comes through a bootstrap invite created by an operator
    command, allowed only while no unrevoked entry holds `keys:invite`. (Section 5.3)
16. The administrator may invite their own handle for a further passkey; the proposed entry never
    carries `keys:invite`. (Section 5.1, 5.6)
17. The website does not verify hub answer signatures. (Section 10.3)
18. Every request of a session key writes one log line, like a `service` key, until the access
    log exists. (Section 7.1)
19. The signature counter rule of section 4.4; synced passkeys with counter 0 always pass; the
    device does not check the counter. (Section 4.4, 9)
20. The user handle returned at sign-in is ignored. (Section 6.3)
21. In this build step the only command action is `noop`, the only sender is a session key, the
    only targets are the signer's own `agent` keys, and the hub delivers nothing. (Section 8)
22. A passkey session cannot use the existing cookie endpoints; moving them is the following build
    step. (Section 1, 11)
23. Certificates, sessions and invites are kept, never deleted; pending sign-ins are deleted after
    300 seconds. (Section 12)
24. A public URL with an IP-address host stops the process at start. (Section 3)
25. The invite creation is kept as the stored signed request until the team record exists.
    (Section 5.1)
26. End-to-end tests are deselected by default and required in the CI job `e2e`. (Section 14)
27. The ceremony limit for sign-in and registration is 300 seconds; at most 1000 pending sign-ins;
    anonymous bodies at most 65536 bytes. (Section 2)
28. Besides opening an invite (`POST /auth/invite/open`, and `POST /auth/invite/finish`, which is
    bound to it by the registration id), these requests carry no registered key: `GET /passkey`,
    `GET /invite` and `GET /static/passkey.js` (static code without data), and
    `POST /auth/passkey/begin` and `POST /auth/passkey/finish` (the sign-in itself, authenticated
    by the passkey at finish). (Section 5.5, 6.1, 10.1)
