# Passkey protocol

This document fixes how a staff member gets a passkey, signs in with it, and signs every
request and command with a certified session key instead of a GitHub sign-in. It is the
technical specification for the build step "sign-in with passkeys and the certified session key
instead of GitHub".

**Status:** specified, not built. The rules behind it are DESIGN.md §24.2, §25.3 to §25.6,
§25.15, §25.16 and §25.21 in the
[client repository](https://github.com/DFXswiss/agent/blob/develop/DESIGN.md); if this document
and DESIGN.md disagree, DESIGN.md wins and this document is fixed. Section 17.2 names the
sentences of DESIGN.md that this document asks the owner to adjust.
[KEY-PROTOCOL.md](KEY-PROTOCOL.md) stays the specification of the keys file, of signed requests
and of signed answers. This document extends its sections 2 (encodings), 3 (one loader rule that
fixes which `agent` entry is the device key of a device, section 7.4), 5 (signed requests) and 7
(`whoami`), adds endpoints under `/api/key/`, and adds start rules for the host of the public URL
(section 3); it changes KEY-PROTOCOL.md nowhere else. [PROTOCOL.md](PROTOCOL.md) stays the
contract for everything that exists today.

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
- moving the existing website endpoints to signed requests: every endpoint that today relies on
  the GitHub cookie session, for example `/auth/me`, `/api/state`, `/api/prs`, `/api/session`,
  `/api/session/*`, `/api/sessions/{id}`, `/api/sessions/{id}/*`, `/api/session-timeline`,
  `/api/pings`, `/api/assignments`, `/api/usage`, `/api/stream` and `/pair/*`. A passkey session
  cannot call any of them in this step.
- the team record. The invite creation is kept as the signed request that created it until the
  team record exists (section 5.1).
- the allow list (DESIGN.md §25.20 step 1), grants (step 2), delivery of commands to devices
  (step 4) and maskings (step 5). Section 9 is binding for how a device verifies a certificate
  and a command. The format of the allow list, of grants and of maskings is fixed by the build
  steps that build them.
- the published hash of the website (DESIGN.md §25.16) and the access log (DESIGN.md §25.9)

No existing endpoint changes its behaviour, except that the dashboard page `/` gets one link
(section 10.1). The hub needs no new environment variable. It refuses to start when the host of
`AGENT_CORE_PUBLIC_URL` ends with a dot or is an IP address (section 3); section 15 lists the
files of this repository that change because of that. `python -m agent_core` without an argument
still starts the hub; two operator commands are added (section 5.3).

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
| CBOR | every CBOR item this document decodes passes the three checks of "CBOR items" below |
| JSON | every JSON text this document parses (request bodies, client data) is UTF-8 and is parsed with Python's `json` so that a key written twice and the constants `NaN`, `Infinity` and `-Infinity` make it fail (`object_pairs_hook` refuses a repeated key, `parse_constant` refuses every constant). A JSON integer is a value whose parsed type is exactly `int`; `1.0`, `1e0`, `true` and `"1"` are not integers. |

**CBOR items.** An item is decoded with `cbor2.CBORDecoder` from an `io.BytesIO` over the bytes,
starting at the item's first byte; the item's bytes are those from that position to `tell()`
after `decode()`. The item is accepted only when all three checks pass:

1. **Canonical form.** Encoding the decoded item again with `cbor2.dumps(item, canonical=True)`
   gives exactly the item's bytes. This is the canonical form of RFC 7049 §3.9 as `cbor2` writes
   it with `canonical=True`: definite lengths, integers and lengths in their shortest form, map
   keys sorted by the length of their encoding first and then bytewise. A map with a key written
   twice fails, because `cbor2` keeps only its last value. For the maps this document reads (the
   integer keys 1, 3, −1, −2 and −3 of a credential public key, and text keys) this order is the
   same as the CTAP2 canonical order, which sorts by major type first.
2. **Types.** The decoded item and every value and map key inside it, at any depth, has the
   Python type exactly `int`, `bytes`, `str`, `list` or `dict`, tested with `type(value) is …`.
   Everything else is refused: `bool`, `None`, `float` (also a float in its shortest form, such
   as `-7.0` written as `f9 c7 00`, which check 1 does not catch), `cbor2.CBORTag` (an unknown
   tag, which check 1 does not catch), `cbor2.CBORSimpleValue`, `undefined`, every type that
   `cbor2` makes from a known tag (for example `datetime`, `Decimal` or `UUID`), and the `tuple` or
   frozen map it makes for an array or map used as a map key. The only
   exception: `true` and `false` are accepted as values, never as keys, inside the extensions map
   that follows the credential public key when flag ED is set (R12), because extension outputs
   may be booleans; that map is not otherwise read.
3. **Integer range.** Every `int` is from −2^64 to 2^64 − 1, the range of CBOR major types 0 and 1.
   A larger integer reaches Python through the bignum tags 2 and 3, which `cbor2` turns into an
   `int` and writes again unchanged, so neither check 1 nor check 2 sees the tag.

A session key id and an invite id contain a `.`, which the `id` rule of KEY-PROTOCOL.md section 3
does not allow. They can therefore never equal a keys-file id.

Every random value in this document comes from the operating system's secure random source
(`secrets.token_bytes`), in the browser from `crypto.getRandomValues`.

| Constant | Value | Meaning |
|---|---|---|
| `SESSION_KEY_MAX_SECONDS` | 43200 | One working day: the longest lifetime of a session key that the hub or a device accepts |
| `SESSION_KEY_ISSUE_SECONDS` | 43080 | The lifetime the hub writes into a statement: `SESSION_KEY_MAX_SECONDS` minus the 120-second clock window, so that a device configured with 43200 seconds whose clock is up to 120 seconds behind the hub still accepts it (section 9) |
| `CEREMONY_SECONDS` | 300 | How long a sign-in or a registration may take from its first to its second request |
| `INVITE_MIN_SECONDS` | 300 | Shortest invite lifetime |
| `INVITE_MAX_SECONDS` | 86400 | Longest invite lifetime |
| `INVITE_OPEN_LIMIT` | 50 | Open invites (status `pending` or `opened`, section 5.2), at most, at one time |
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
  host of `AGENT_CORE_PUBLIC_URL` ends with a dot or is an IP address. Both rules test the host
  name `h = urlsplit(public_url).hostname`, which has no port, no brackets and is lowercase; they
  never test the host line that `public_url_host` returns, so `http://localhost:8787` passes.
  In this order:
  1. When `h` ends with `.`, the message is
     `AGENT_CORE_PUBLIC_URL must not have a host name ending in a dot`. This also covers
     `127.0.0.1.`, which a browser reads as an IPv4 address, and a name such as `hub.example.`,
     whose rp id and origin would differ from what a browser writes for it.
  2. When `h` contains `:`, when `ipaddress.ip_address(h)` accepts it, or when its last
     dot-separated label consists only of digits or starts with `0x` (the forms a browser reads as
     an IPv4 address, such as `127.1` or `0x7f000001`), the message is
     `AGENT_CORE_PUBLIC_URL must have a host name, not an IP address`.

  A local run uses `localhost`.
- These refusals run in `Config.from_env`, directly after `public_url_host(public_url)` returned,
  and not inside `public_url_host`. Every existing refusal of the URL form (user information,
  path, query, fragment, scheme, port, host name) therefore runs first and keeps its message, and
  `public_url_host` still returns the host line for an IP address.
- A passkey is bound to its rp id. Changing the host of the public URL makes every passkey and
  every certificate useless; a new host means new passkeys through invites.

## 4. Verifying a passkey response

Registration (section 5), sign-in (section 6) and a device (section 9) verify the same fields by
the same steps. This section fixes those steps. The hub and a device implement them in their own
repositories; both run the shared test vectors of section 13.10, so that they agree on every
accepted and every refused input. The steps follow WebAuthn Level 3 §7.1 and §7.2 for exactly the
options this document uses. No server-side WebAuthn library is used (decision 3): the checks are
few, a device has to run the same checks offline, and a library would bring attestation code this
document never uses. Every cryptographic operation is done by the `cryptography` package; CBOR is
decoded and checked by the `cbor2` package. No primitive is implemented by hand.

### 4.1 Client data

`client_data_json` is the exact byte string the browser returned. The verifier:

1. decodes it as UTF-8 and parses it as one JSON object by the JSON rule of section 2. A key
   written twice, a value that is not JSON (`NaN`, `Infinity` and `-Infinity` count as not JSON),
   or a top-level value that is not an object fails.
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
verifier requires, and checks step 1 before it reads any byte:

1. at least 37 bytes
2. the rp id hash equal to SHA-256 over the rp id of section 3
3. flag UP (`0x01`) set
4. flag UV (`0x04`) set
5. if flag BS (`0x10`) is set, flag BE (`0x08`) is set as well
6. for a sign-in: flag AT (`0x40`) not set; if flag ED (`0x80`) is not set, exactly 37 bytes. The
   bytes after the first 37 are not parsed; the signature covers them.
7. for a registration: flag AT set, and the layout of check R12 (section 5.5)

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

The comparison and the update are one transaction, and it is the last check of a sign-in (S7), so
a sign-in refused for any other reason never changes the counter. A counter is checked only on a
sign-in at the hub; a device does not check it (section 9). A passkey that is refused for
`counter_regressed` is revoked and replaced through a new invite. Because `exclude_credentials`
also lists revoked credentials (section 5.4), the same authenticator refuses to create the
replacement until the person deletes the old passkey from it; another authenticator is not
affected.

## 5. Invites

### 5.1 Creating an invite

```text
POST /api/key/invites
```

Needs a signed request (section 7) by a key with the scope `keys:invite`. By KEY-PROTOCOL.md
section 3 only a `staff` entry can hold `keys:invite`, and a `webauthn` key never signs a request
itself, so only a session key certified by an administrator's passkey reaches the endpoint. An
`agent` key, a `service` key and a session key whose passkey lacks `keys:invite` get the signed
`403` `{"error":"scope_missing"}` of check 9. A `webauthn` key used directly, and any key refused
at checks 1 to 8 (for example a revoked or expired one), gets the unsigned refusal of that check
(section 7.1). The administrator may invite their own handle for a further passkey.

Request body, exactly these three fields:

```json
{"handle":"example-staff","team":"example","lifetime_seconds":3600}
```

| Field | Rule |
|---|---|
| `handle` | The handle rule of KEY-PROTOCOL.md section 3, and not a login in the `agents` map of `teams.yaml` |
| `team` | A team in `teams.yaml`. The handle need not be a member yet; the loader checks membership when the keys-file pull request is deployed (decision 12). |
| `lifetime_seconds` | A JSON integer (section 2: not a boolean, a string or a number with a fraction or exponent) from `INVITE_MIN_SECONDS` to `INVITE_MAX_SECONDS` |

Checks after checks 9 to 10, in this order; every answer is signed (KEY-PROTOCOL.md section 6):

| # | Check | Status | `error` |
|---|---|---|---|
| I1 | The body is one JSON object (section 2) with exactly the three fields, each valid | 400 | `invite_invalid` |
| I2 | Fewer than `INVITE_OPEN_LIMIT` invites are open (status `pending` or `opened`, section 5.2) | 409 | `invite_limit` |

I2 and the insert of the new invite are one write transaction (`BEGIN IMMEDIATE`), so two
parallel creations never bring the number of open invites above `INVITE_OPEN_LIMIT`.

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

- The list holds every invite, newest first by its latest change (CONTRIBUTING.md, "Dashboard
  lists"), then by invite id descending. The latest change is the invite's `received` time, else
  the `finished` time of its registration (section 12; this is the change of a `failed` invite),
  else its `opened` time, else its `created` time. Expiry is not a recorded change: an `expired`
  invite keeps the time of its last recorded change.
- `status` is exactly one of the following, evaluated at now:

  | Status | Meaning |
  |---|---|
  | `pending` | Not opened, and now is before `expires` |
  | `opened` | Opened, its registration not finished, now is before `expires`, and at most `CEREMONY_SECONDS` passed since it was opened |
  | `received` | A public key was received (R14 passed) |
  | `failed` | Its registration was finished (consumed at R2) and a later check refused it |
  | `expired` | No registration was finished, and either now is at or after `expires`, or more than `CEREMONY_SECONDS` passed since it was opened |

  An invite is **open** when its status is `pending` or `opened`. Only open invites count
  against `INVITE_OPEN_LIMIT` (I2). A ceremony the person cancels leaves the invite `opened`
  until its ceremony time passes, then `expired`.
- `created_by` is the session key id that created it, or `operator` for a bootstrap invite.
- `received`, `credential_id`, `public_key`, `fingerprint`, `keys_entry`, `pull_request_title`
  and `pull_request_body` are present only for status `received`.
- The query is ignored. No token and no token hash is ever in the answer.
- This answer passes through the network edge and the website does not verify it (section 10.3).
  The binding source of `credential_id`, `public_key` and `keys_entry` for the keys-file pull
  request is the output of `invite-list` on the hub host (section 5.6).

### 5.3 The bootstrap invite

The administrator's own first passkey cannot be invited by anyone (DESIGN.md §24.2). It is created
through a bootstrap invite made by an operator on the hub host:

```text
python -m agent_core invite-bootstrap --handle HANDLE --team TEAM --seconds N
python -m agent_core invite-list
```

- Both commands read the same environment variables as the hub and use the same database. They
  exist only as commands on the hub host; no HTTP route creates a bootstrap invite.
- `src/agent_core/__main__.py` today starts the hub and reads no argument. It gets a dispatch on
  the first argument: no argument starts the hub exactly as today (the image's
  `CMD ["python", "-m", "agent_core"]` and the `agent-core` script are unchanged);
  `invite-bootstrap` and `invite-list` run the commands above and never start the hub; any other
  first argument, and an unknown or repeated option, prints a usage line to standard error, starts
  nothing and exits with status 2.
- `invite-bootstrap` refuses, writes nothing, prints the reason to standard error and exits with
  status 1 when one of these holds, checked in this order:
  1. its arguments break the rules of I1 (with `--seconds` as `lifetime_seconds`)
  2. the keys file, read at the current time, has an entry that holds `keys:invite`, revoked or
     not
  3. another bootstrap invite is open (section 5.2)
  4. I2 fails

  Checks 3 and 4 and the insert are one write transaction (`BEGIN IMMEDIATE`), so two parallel
  runs never create two open bootstrap invites and never go above `INVITE_OPEN_LIMIT`.
- Otherwise it creates an invite exactly as in section 5.1 with `created_by` `operator`,
  `creation_request` null and `bootstrap` 1 (section 12). It writes one line
  `invite bootstrap created <invite id> <handle> <team> <expires>` to standard error and prints
  only the link to standard output.
- The hub writes one log line `invite bootstrap opened <invite id> <handle>` when a bootstrap
  invite passes O4, and one line `invite bootstrap received <invite id> <handle> <fingerprint>`
  when it passes R14.
- A bootstrap invite is only for an entry that carries `keys:invite`: its proposed keys-file entry
  carries the scopes `[record:read, record:write, keys:invite]`, and its pull-request text says
  that it adds an administrator passkey (section 5.6). A normal invite never proposes
  `keys:invite`.
- `invite-list` prints the list of section 5.2 as JSON, one invite per line, read directly from
  the database. It is the binding source of the keys-file text (section 5.6).
- This is the path for the first administrator, for an administrator who lost every passkey, and
  nothing else. In the second case a reviewed pull request first revokes every passkey of the old
  administrator and removes `keys:invite` from every entry that holds it, revoked ones included,
  and is deployed; only that opens the bootstrap path again. The scopes a revoked entry held
  before stay in the history of the keys file.
- The command registers nothing: the passkey counts only after the keys-file pull request that
  adds it is reviewed, merged and deployed. **That pull request, which the team reviews, is the
  real control over who becomes administrator**; the restrictions above only narrow what an
  operator on the hub host can start.
- A bootstrap invite carries no administrator's signature, because no administrator exists. Its
  row (`created_by` `operator`, `bootstrap` 1) is the record of its creation until the team
  record exists; the build step of the team record turns it into an event "invite created by the
  operator command" as soon as the record exists (decision 15).
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

This request carries no registered key; the token authenticates it. Decision 28 lists every
request that carries no registered key. Checks in this order; every refusal is
`{"error":"<value>"}`:

| # | Check | Status | `error` |
|---|---|---|---|
| O1 | The request has no `Content-Encoding` header (with any value, also `identity`); the body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object (section 2) with exactly `token`, and the token decodes to 32 bytes | 400 | `request_invalid` |
| O2 | An invite with the SHA-256 of the token exists | 401 | `invite_unknown` |
| O3 | Now is before the invite's `expires` | 401 | `invite_expired` |
| O4 | The invite was not opened before | 401 | `invite_used` |

O3 and O4 use the same value of now. O4 and the marking as opened are one atomic update
(`… SET opened = ? WHERE id = ? AND opened IS NULL AND expires > ?`); of two parallel requests
exactly one passes. An expired invite is therefore never marked opened. **Opening consumes the
invite**: a ceremony that the person cancels, or that fails, needs a new invite (decision 9).

On success the hub creates a registration (section 12): a registration id, a 32-byte random
challenge and a 16-byte random user id. It answers `200`:

```json
{"registration":"<id>","handle":"example-staff","rp_id":"hub.example","rp_name":"agent-core","user_id":"<base64url, 16 bytes>","challenge":"<base64url, 32 bytes>","exclude_credentials":["<base64url>"],"timeout_ms":300000}
```

`exclude_credentials` lists the credential ids of every keys-file entry with this handle, revoked
or not, so that an authenticator does not create a second passkey for the same person (section
4.4 states the consequence for a replacement).

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
| R1 | The request has no `Content-Encoding` header (with any value, also `identity`); the body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object (section 2) with exactly these four text fields, each canonical base64url | 400 | `request_invalid` |
| R2 | The registration exists and was not finished before. This atomic update marks it finished, so it is consumed also when a later check fails. | 401 | `registration_unknown` |
| R3 | At most `CEREMONY_SECONDS` passed since the invite was opened, and now is before the invite's `expires` | 401 | `registration_expired` |
| R4 | Client data, section 4.1, type `webauthn.create`, challenge the registration's challenge: a failure of step 1 or 2 | 401 | `client_data_invalid` |
| R5 | Section 4.1 step 3 | 401 | `challenge_mismatch` |
| R6 | Section 4.1 steps 4 and 5 | 401 | `origin_mismatch` |
| R7 | `attestation_object` decodes as one CBOR map that passes the three checks of "CBOR items" (section 2), with exactly the keys `fmt` (text), `attStmt` (map) and `authData` (bytes), and no data after it. The checks cover the whole map, the content of `attStmt` included; a boolean is refused there. `fmt` and `attStmt` are otherwise ignored: no attestation is verified (decision 10). | 401 | `attestation_invalid` |
| R8 | Section 4.2 step 1 on `authData` | 401 | `authenticator_data_invalid` |
| R9 | Section 4.2 step 2 | 401 | `rp_id_mismatch` |
| R10 | Section 4.2 step 3 | 401 | `user_presence_missing` |
| R11 | Section 4.2 step 4 | 401 | `user_verification_missing` |
| R12 | Section 4.2 steps 5 and 7: after the first 37 bytes come the AAGUID (16 bytes), the credential id length L (2 bytes, big-endian), the credential id (L bytes), and one CBOR item, the credential public key; when flag ED is set, one CBOR map follows, the extensions map; then nothing. Each CBOR item passes the three checks of "CBOR items" (section 2); `true` and `false` are accepted only as values inside the extensions map. | 401 | `authenticator_data_invalid` |
| R13 | L is from 16 to 1023, the credential id equals `credential_id` of the body, and the credential public key is a CBOR map with exactly the five keys `1`, `3`, `-1`, `-2` and `-3`, each of type exactly `int`, where the values of `1`, `3` and `-1` are of type exactly `int` and equal to 2, −7 and 1, the values of `-2` and `-3` are of type exactly `bytes` and 32 bytes long, and the point `0x04 ‖ x ‖ y` is a valid P-256 point | 401 | `credential_invalid` |
| R14 | Neither the credential id nor the public key appears in the keys file or in another received invite | 409 | `credential_known` |

R14, the choice of the proposed keys-file id (section 5.6) and the storing of the received key
are one database transaction, so two invites received at the same time never get the same
proposed id. On success the hub stores the credential id, the public key, the receive time and
the proposed keys-file id with the invite, which then has status `received`, and answers `200`:

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
produces three texts, shown in the list of section 5.2, on the `/passkey` page of the
administrator, and in the output of `invite-list`.

**Binding source.** The text that goes into the pull request is taken from, or compared with, the
output of `invite-list` on the hub host, which does not pass through the network edge. The copy on
the website is a convenience only (section 10.3). Before the pull request is merged, an operator
compares its `credential_id` and `public_key` with the `invite-list` output for that invite; the
pull-request body states this step.

The proposed id is `<handle>-passkey-<n>`, where `n` is the smallest integer from 1 for which the
id is neither in the loaded keys file nor the proposed id of another received invite. It is chosen
in the transaction of R14. The administrator checks it against the current keys file before
merging; the loader refuses a duplicate.

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

For a normal invite the scopes are always `[record:read, record:write]`, the team scopes of
DESIGN.md §24.2, and `keys:invite` is never proposed. For a bootstrap invite they are always
`[record:read, record:write, keys:invite]` (section 5.3).

`pull_request_title`, exactly: `Add passkey <id> for <handle>`; for a bootstrap invite
`Add administrator passkey <id> for <handle>`.

`pull_request_body` for a normal invite, exactly (lines joined by `\n`):

```text
Adds the passkey received through invite <invite id> to keys.yaml.

- Handle: <handle>
- Team: <team>
- Created by: <created_by>
- Fingerprint: <fingerprint>
- Received: <received time>

Before merging, the administrator compares this fingerprint with <handle> over a channel other than the invite link (PASSKEY-PROTOCOL.md section 5.5), and an operator compares credential_id and public_key in this pull request with the output of invite-list on the hub host (PASSKEY-PROTOCOL.md section 5.6). If <handle> is not yet a member of <team> in teams.yaml, this pull request adds the membership.
```

`pull_request_body` for a bootstrap invite, exactly (lines joined by `\n`):

```text
Adds the administrator passkey received through bootstrap invite <invite id> to keys.yaml, with keys:invite.

- Handle: <handle>
- Team: <team>
- Created by: operator (bootstrap invite, PASSKEY-PROTOCOL.md section 5.3)
- Fingerprint: <fingerprint>
- Received: <received time>

Before merging, <handle> compares this fingerprint with the one shown when the passkey was created, and an operator compares credential_id and public_key in this pull request with the output of invite-list on the hub host (PASSKEY-PROTOCOL.md section 5.6). The review of this pull request is the control over who becomes administrator. If <handle> is not yet a member of <team> in teams.yaml, this pull request adds the membership.
```

`<received time>` has the time form of KEY-PROTOCOL.md section 2.

The fingerprint comparison protects against an intercepted link. It does not protect against a
hub that was taken over, nor against a network edge that changes the invite page, because the
fingerprint the invited person sees comes from code delivered through both (DESIGN.md §25.16,
section 10.3).

## 6. Sign-in and the certificate

### 6.1 Begin

The browser first creates the session key (section 10.2), then sends:

```text
POST /auth/passkey/begin
{"session_public_key":"<base64url, 65 bytes>"}
```

| # | Check | Status | `error` |
|---|---|---|---|
| B1 | The request has no `Content-Encoding` header (with any value, also `identity`); the body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object (section 2) with exactly `session_public_key`, which decodes as canonical base64url to a valid 65-byte P-256 point | 400 | `request_invalid` |
| B2 | Pending sign-ins older than `CEREMONY_SECONDS` are deleted first. Then the key is not the session public key of a stored session or of a remaining pending sign-in | 409 | `session_key_known` |
| B3 | Fewer than `PENDING_SIGN_IN_LIMIT` pending sign-ins exist | 429 | `too_many_pending` |

B2, B3 and the insert of the new pending sign-in are one write transaction (`BEGIN IMMEDIATE`),
so two parallel begins never go above `PENDING_SIGN_IN_LIMIT` and never store the same session
public key twice. The deletion of B2 is kept also when B2 or B3 refuses.

The hub creates a sign-in id and a 32-byte random hub value, builds the statement (section 6.2)
with `expires` = now + `SESSION_KEY_ISSUE_SECONDS`, stores the pending sign-in (section 12) and
answers `200`:

```json
{"sign_in":"<id>","statement":"<the statement text>","challenge":"<base64url, 32 bytes>","rp_id":"hub.example","timeout_ms":300000}
```

`challenge` is SHA-256 over the statement bytes.

This request carries no registered key, because the passkey can sign only after it. It reveals
nothing about any key or person and can do nothing but hand out a challenge. Decision 28 lists
every request that carries no registered key.

`PENDING_SIGN_IN_LIMIT` is an availability limit, not a protection: anyone can send 1000 begins
within 300 seconds and so make every further begin get `too_many_pending` until those sign-ins
are older than `CEREMONY_SECONDS`. The rate limit per client address at the network edge
(section 15) is what keeps one sender from doing that.

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
its own session public key on line 3, its own `location.origin` on line 2, and an expiry no later
than its clock plus `SESSION_KEY_MAX_SECONDS`, and that `challenge` is SHA-256 of the statement.
Otherwise it stops. It then calls `navigator.credentials.get` with exactly:

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
| S1 | The request has no `Content-Encoding` header (with any value, also `identity`); the body is at most `ANONYMOUS_BODY_LIMIT` bytes, one JSON object (section 2) with exactly these six fields, each canonical base64url text (`user_handle` also `null`) | 400 | `request_invalid` |
| S2 | The pending sign-in exists. It is deleted in the same atomic step, so it is consumed also when a later check fails, and a second finish with the same id gets this refusal. | 401 | `challenge_unknown` |
| S3 | At most `CEREMONY_SECONDS` passed since begin | 401 | `challenge_expired` |
| S4 | `credential_id` equals the `credential_id` of a `staff` entry in the keys file. A key received through an invite and not yet in the keys file is unknown. | 401 | `credential_unknown` |
| S5 | That entry is not revoked | 401 | `key_revoked` |
| S6 | `verify_certificate` (section 6.4) with the stored statement bytes, the body's `credential_id`, `authenticator_data`, `client_data_json` and `signature`, the entry's public key, the origin and rp id of section 3, now, and the bound `SESSION_KEY_MAX_SECONDS` | 401 | the `error` of the first failed step V1 to V11 |
| S7 | Section 4.4 | 401 | `counter_regressed` |

A finish more than `CEREMONY_SECONDS` after its begin is refused either way, with one of two
values: `challenge_expired` (S3) while the pending sign-in still exists, or `challenge_unknown`
(S2) once a later begin deleted it (B2). Both are correct refusals.

On success the hub stores the session (section 12) with the certificate, the passkey's keys-file
id, its credential id, public key and handle, and the expiry, and answers `200`:

```json
{"session_key":"session.5d1c…","passkey":"example-staff-passkey-1","handle":"example-staff","expires":"2026-11-17T21:58:00Z"}
```

This answer is not signed (it is outside `/api/key/`). The browser stores the session key id with
its key (section 10.2) and then calls `GET /api/key/whoami` with a signed request; it treats itself
as signed in only after that answer is `200`.

That is the only time the passkey is asked during a session. Every later request and command is
signed by the session key without a further confirmation.

### 6.4 `verify_certificate`

`verify_certificate` is one sequence of steps that the hub (S6) and a device (section 9 step 2)
each implement in their own repository, with the same inputs, the same order and the same `error`
values; both implementations run the shared test vectors of section 13.10. Its inputs are the
certificate (statement bytes, `credential_id`, `authenticator_data`, `client_data_json`,
`signature`), the passkey public key to verify with, the origin and rp id, the
verifier's clock "now", and the bound `max_lifetime`: `SESSION_KEY_MAX_SECONDS` on the hub, the
configured longest lifetime on a device. It takes no expected challenge as input: it computes the
expected challenge itself as SHA-256 over the statement bytes. It runs these steps in this order
and returns the `error` of the first that fails:

| # | Step | `error` |
|---|---|---|
| V1 | The statement bytes are UTF-8 of exactly five lines joined by `\n`, without a trailing line feed; line 1 is `agent-session-v1`; line 2 is exactly the origin; line 3 decodes as canonical base64url to a valid 65-byte P-256 point; line 4 decodes as canonical base64url to 32 bytes; line 5 has the time form | `statement_invalid` |
| V2 | Section 4.1 steps 1 and 2 with type `webauthn.get` | `client_data_invalid` |
| V3 | Section 4.1 step 3, with the expected challenge SHA-256 over the statement bytes | `challenge_mismatch` |
| V4 | Section 4.1 steps 4 and 5 | `origin_mismatch` |
| V5 | Section 4.2 step 1 | `authenticator_data_invalid` |
| V6 | Section 4.2 step 2 | `rp_id_mismatch` |
| V7 | Section 4.2 step 3 | `user_presence_missing` |
| V8 | Section 4.2 step 4 | `user_verification_missing` |
| V9 | Section 4.2 steps 5 and 6 | `authenticator_data_invalid` |
| V10 | Section 4.3 with the given passkey public key | `signature_invalid` |
| V11 | The expiry on line 5 is later than now and not later than now + `max_lifetime` | `certificate_expiry_invalid` |

V3 is the step that binds the assertion to this statement, and so to the session key, the origin
and the expiry: an assertion of the same passkey made for any other statement fails V3. The
steps do not check the signature counter; only the hub does (S7).

## 7. Signed requests by a session key

A session key signs requests under `/api/key/` exactly as KEY-PROTOCOL.md section 5 describes,
with the same four headers and the same eight-line text: `Agent-Key` is the session key id, and
`Agent-Signature` is the session key signature (section 2) over the text, ECDSA over P-256 with
SHA-256. Both signature encodings are 64 bytes, so check 1 keeps its length rule.

### 7.1 The checks

The checks of KEY-PROTOCOL.md section 5 run in the same order, with check 9a added between checks
9 and 10. They change as follows; a check not named here is unchanged for every key:

| # | Check | Status | `error` |
|---|---|---|---|
| 1 | As before, except that `Agent-Key` follows the `id` rule **or** the session key id form | 401 | `signature_missing` |
| 2 | A keys-file id names an entry in `keys`. A session key id names a stored session that was not ended, whose passkey id names an entry in `keys` | 401 | `key_unknown` |
| 3 | The entry is not revoked; for a session key, the passkey entry is not revoked | 401 | `key_revoked` |
| 4 | The entry is not expired; for a session key, its expiry is later than now | 401 | `key_expired` |
| 5 | For a keys-file id: the entry's kind is `ed25519` (unchanged; a `webauthn` key still never signs a request itself) | 401 | `key_kind_not_allowed` |
| 5 | For a session key id: the passkey entry has kind `webauthn`, and its `credential_id`, `public_key` and `handle` are exactly those stored with the session at sign-in | 401 | `certificate_invalid` |
| 6 | Unchanged | 401 | `time_skew` |
| 7 | For a keys-file id unchanged. For a session key id: the session key signature verifies with the session public key | 401 | `signature_invalid` |
| 8 | Unchanged; the key id stored with the nonce is the session key id | 401 | `nonce_replayed` |
| 9 | The key has the scope the endpoint needs. A session key has the scopes of its passkey entry as loaded now. | 403 | `scope_missing` |
| 9a | Only on a route marked controller-only: the key is the device key of a device (section 7.4: the `agent` entry of that device without a `program` field) and holds `device:controller`. On every other route this check passes for every key. | 403 | `controller_device_required` |
| 10 | Unchanged; it applies only to `service` keys | 403 | `subject_not_allowed` |

- A route declares `controller_only=True` when it accepts something that only a controller device
  key may sign: a change of an allow list, a grant, the removal of a grant, a masking (DESIGN.md
  §25.6, §25.9). On such a route check 9a refuses every other key with the one value
  `controller_device_required`: a session key, an `agent` key with a `program` field (a program
  key, also one with `device:controller` on the same device as a controller device key), an
  `agent` key with `device:executor`, and a `service` key. No such route exists in this
  build step; the check is tested through a route that exists only in the test suite. Section 8.3
  lists the other places that keep a session key from signing such a statement.
- Checks 2 to 5 for a session key read the session row once; the read is part of check 2. Ending
  a session (section 7.3) makes check 2 refuse it.
- A revocation of the passkey takes effect, as every keys-file change, with the next deployment
  of the keys file; from then on check 3 refuses every session it certified. A change of the
  passkey entry's credential id, public key or handle makes check 5 refuse every session it
  certified.
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

Signs out. Needs a signed request by a session key and no scope; the body is ignored. After checks
9 to 10, a keys-file key gets the signed `403` `{"error":"session_key_required"}`. For a session
key the hub sets the session's `ended` time and answers `200` `{"ended":true,"hub_key":"hub-1"}`,
signed. After this, every request with that session key id is refused at check 2. The browser
deletes the session key (section 10.2) whatever the answer.

### 7.4 The device key of a device (extends KEY-PROTOCOL.md section 3)

KEY-PROTOCOL.md section 3 allows `program` on an `agent` entry but does not say which entry of a
device is the device itself. This document adds one rule to its loader:

- The `agent` entries of one handle with the same `device` belong to one device. Among those that
  are not revoked at start, at most one has no `program` field. That entry is the **device key**
  of the device. Every other `agent` entry of that device carries `program` and is a **program
  key**.
- The loader refuses, and the process does not start, when two `agent` entries of one handle with
  the same `device`, neither revoked at start, both have no `program`. For `agent` entries this
  rule takes the place of the sentence "`program` is required when two such entries share a
  `device`": of the unrevoked `agent` entries that share a device, one may lack `program` (the
  device key), and every other one carries it. The rule that the pair of `device` and `program`
  is unique among the unrevoked `agent` and `service` entries of one handle stays as it is.
- A device whose unrevoked `agent` entries all carry `program` has no device key. Then no key of
  that device passes check 9a and no command can name it as target (C5).
- A program key is never the device key, also when it holds `device:controller` and sits on the
  same device as the device key. It never passes check 9a and is never a command target.

The rule for `service` entries is unchanged.

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
| 3 | The target: the keys-file id of the target device's device key (section 7.4), never the id of a program key |
| 4 | The action, from the closed list below |
| 5 | The sequence number, decimal, 1 to `SEQUENCE_MAX`, no sign, no leading zero |
| 6 | The sender's clock, `YYYY-MM-DDTHH:MM:SSZ` |
| 7 | `-` when the action carries no content; otherwise SHA-256 hex over a 16-byte random value followed by the UTF-8 content. Content and random value travel beside the signed command (DESIGN.md §25.6), in the fields `content` and `content_random` of the device bundle (section 9). |

Actions in this build step, closed: `noop`. A `noop` command carries no content and does nothing on
a device; it proves the chain from passkey to device. Every later action is added to this list by
the pull request of the build step that needs it. A change of an allow list, a grant, the removal
of a grant and a masking are not commands (DESIGN.md §25.2) and never become actions.

### 8.2 `POST /api/key/commands`

Needs a signed request (section 7) with the scope `record:write`. Body, exactly:

```json
{"command":{"target":"example-staff-laptop-1","action":"noop","sequence":1,"time":"2026-11-17T10:00:00Z","content_hash":"-"},"signature":"<base64url, 64 bytes>"}
```

`sequence` is a JSON integer (section 2: not a boolean, not a string and not a number with a
fraction or exponent such as `1.0`). The command's sender is the request's `Agent-Key`.
Checks after checks 9 to 10, in this order; every answer is signed:

| # | Check | Status | `error` |
|---|---|---|---|
| C1 | The request's key is a session key | 403 | `sender_not_allowed` |
| C2 | The body is one JSON object (section 2) with exactly `command` and `signature`; `command` has exactly the five fields, each in the form of section 8.1; `action` is in the list; `content_hash` is `-` for `noop`; `signature` decodes to 64 bytes | 400 | `command_invalid` |
| C3 | `time` is at most `COMMAND_TIME_WINDOW` seconds from now | 400 | `command_time_skew` |
| C4 | The signature verifies, with the session public key, over the command text built from the fields and the sender | 403 | `command_signature_invalid` |
| C5 | `target` is exactly the id of a device key (section 7.4): an `agent` entry without a `program` field that is not revoked and whose handle is the handle stored with the session at sign-in (check 5 holds it equal to the passkey entry's current handle). A program key, a `staff` or `service` entry, a revoked entry and an unknown id are refused. | 403 | `target_not_allowed` |
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
4. A hub route that accepts such a statement is marked controller-only, and check 9a lets only a
   controller device key (the device key of section 7.4, holding `device:controller`) reach it; a
   session key and a program key are refused there with `controller_device_required`.
5. A device accepts such a statement only when the signer's allow-list entry is a controller
   device key (DESIGN.md §25.6). A session key is never one.

When the team record is built, an event signed by a session key is refused when the session is
ended, its expiry is at or before the hub's receive time, or its passkey is revoked, with the
errors of checks 2 to 4.

## 9. What a device checks

This section is binding for the client repository (DESIGN.md §25.20 step 2). A device receives,
with every command from a signed-in staff member, this bundle:

```json
{"sender":"session.5d1c…","certificate":{…section 6.2…},"command":{…section 8.2…},"signature":"<base64url>","content":null,"content_random":null}
```

`content` is the UTF-8 content as a JSON string and `content_random` the 16-byte random value as
base64url; both are `null` exactly when line 7 of the command text is `-`. For `noop` both are
`null`.

The device's configuration holds the hub's public URL, from which it derives the origin and the
rp id of section 3, the id of its own device key (section 7.4), the time window, and the longest
session-key lifetime L, from 1 to `SESSION_KEY_MAX_SECONDS`. The device checks, in this order, and
refuses at the first failure:

1. The allow list has an entry for a passkey whose credential id equals `certificate.credential_id`
   and whose public key is the one the owner confirmed. The entry is not revoked, not suspended,
   and its "until" time is later than the device's clock. When the entry comes from a grant, the
   grant has not expired and was not removed.
2. `verify_certificate` (section 6.4), the same steps as on the hub, with the bytes that
   `certificate.statement` decodes to (canonical base64url) as the statement bytes, the public key
   of step 1, the origin and rp id of its own configuration, the device's clock as now, and L as
   `max_lifetime`. The expected challenge is SHA-256 over `certificate.statement` (those decoded
   bytes), computed inside the steps. The signature counter is not checked.
3. `sender` is `session.` and the first 32 hex characters of SHA-256 over the statement bytes.
4. The command signature verifies with the session public key of line 3 of the statement over the
   command text of section 8.1.
5. When line 7 of the command text is `-`, `content` and `content_random` are `null`. Otherwise
   `content_random` decodes to 16 bytes, and SHA-256 hex over those bytes followed by the UTF-8
   bytes of `content` equals line 7.
6. `target` is exactly the id of the device's own device key (section 7.4), as configured; a
   command that names a program key on the same device, or any other id, is refused. `time` is
   inside its configured window; `sequence` is higher than the last one it accepted from this
   sender, and the command was not seen before.
7. The action is permitted by the allow-list entry of step 1.

The hub issues certificates for `SESSION_KEY_ISSUE_SECONDS` (43080 seconds). Step 2 (V11) accepts
one only when its expiry is at most the device's clock plus L. A device with L = 43200 whose clock
is up to 120 seconds behind the hub accepts a certificate from the moment it is issued. A device
with a smaller L refuses a certificate during its first 43080 − L seconds (plus the amount by which
the device's clock is behind) and accepts it only after that, for the rest of its 43080 seconds;
the smaller L, the longer the refusal. Such a device obeys less, never more. The client
repository's default for L is 43200 (decision 2).

The device writes the bundle into its local log before it runs the command. A device cannot verify
when the certificate was signed; DESIGN.md §25.6 states the consequence.

## 10. The website

### 10.1 Pages

| Path | Method | Serves |
|---|---|---|
| `/passkey` | `GET`, `HEAD` | Static page: sign-in with a passkey; when signed in, the handle, the passkey id, the session key fingerprint and expiry, the `noop` command form (target id typed by hand), the invite form and the invite list for a key with `keys:invite`, and sign-out |
| `/invite` | `GET`, `HEAD` | Static page: opens an invite from the fragment and creates the passkey |
| `/static/passkey.js` | `GET`, `HEAD` | The script of both pages |

- The pages and the script are the same bytes for every request: they contain no data, no value
  substituted by the hub (also not the public URL) and no inline script. They are served with the
  headers
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
  non-extractable `CryptoKey` without exposing its bytes. The record survives a browser restart,
  so a session lasts until sign-out or its expiry (decision 30). A new sign-in replaces the record.
- The page deletes the record on sign-out, when the expiry passed, and when a signed request is
  refused with `key_unknown`, `key_revoked`, `key_expired` or `certificate_invalid`.
- The next command sequence number is kept in the same record and incremented in one IndexedDB
  transaction before the command is signed.

### 10.3 Signing in the browser, and what the browser can trust

`/static/passkey.js` has one function that signs, `signText(lines)`. It refuses, without signing,
every list whose first element is not `agent-request-v1` or `agent-command-v1`. It builds the
request text exactly as KEY-PROTOCOL.md section 5, with the host line taken from `location.host`
(which a browser writes in lowercase and without a default port, as the host line requires; the
host is never an IP address, section 3), and the request target from a fixed path and a query
built only with `encodeURIComponent`. A page reached under another host than the public URL's
therefore signs a host the hub refuses at check 7. Signatures use
`crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, privateKey, text)`.

The website does not verify the hub's signature on answers: it would have to take the hub key from
the hub itself, which proves nothing (decision 17).

**Consequence.** A browser has no key pinned outside the hub. The page code (`/passkey`, `/invite`,
`/static/passkey.js`) and every answer the website shows are therefore only as trustworthy as the
connection that delivered them. The network edge terminates TLS, so it can change them: it can
show wrong data, including a wrong invite list, fingerprint or `keys_entry`; with changed page code
it can make a signed-in person send requests and commands they did not intend, and it can put its
own session public key into the statement at sign-in so that the person's passkey certifies a key
the edge holds; at an invite it can put a key of its own choice into the received invite and show
the invited person a matching fingerprint. A hub that was taken over can do the same, because it
delivers the page code. Until the published hash of the website exists (DESIGN.md §25.16), no
check in this document detects a changed page.

What an edge that changes the page code can do, in full:

- It can use the signed-in person's session key, through the page, for as long as the page is open
  and the key is valid (not ended, not past its expiry, its passkey not revoked), within that
  session's rights: every signed request the scopes of the person's passkey entry allow (for an
  administrator, creating invites), and `noop` commands to the person's own devices.
- At a sign-in it can certify a session key of its own instead of the browser's. It then holds a
  key that works without the page, within the same rights, until the certificate expires, the
  session is ended, or the passkey is revoked. The person cannot end that session from the
  website, because the website is the edge's code.
- At an invite it can substitute the received key, as stated above.

What it cannot do:

- It cannot extend the lifetime. The hub writes the expiry into the statement and verifies the
  assertion against the statement it stored (S6), so a changed statement is refused; every
  verifier refuses an expiry later than now plus `SESSION_KEY_MAX_SECONDS` (V11), a device one
  later than now plus its configured L.
- It cannot create a session without the person's passkey confirmation: each certificate needs one
  assertion with user verification (V8). The changed page code can ask for that confirmation at a
  moment the person did not expect.
- It cannot sign anything that changes rights: a session key never signs an allow-list change, a
  grant, the removal of a grant, a masking or a certificate (section 8.3), and the keys file
  changes only by a reviewed pull request.
- It cannot forge an answer to a device or a program, which verify every answer against the pinned
  hub key (KEY-PROTOCOL.md section 6), nor a certificate: a device verifies every command and the
  session key's certificate itself (section 9), against the passkey public key its owner
  confirmed.

What bounds the damage is the lifetime of a session key (at most `SESSION_KEY_MAX_SECONDS`), the
rights of a session key (the scopes of its passkey entry, in this build step `noop` commands only
to the person's own device keys, nothing that changes rights), and the record of every command:
the hub's log line for every request of a session key (section 7.1), which names the session key
and the passkey, and, once commands are delivered, the bundle every device writes into its local
log before it runs a command (section 9). These records show afterwards what was signed; they
stop nothing while it happens. A key
substituted at an invite gets only the scopes its keys-file entry is given by the reviewed pull
request: the team scopes, or for a bootstrap invite `keys:invite`; the visible sign is that the
invited person's own passkey then fails to sign in with `credential_unknown`. Nothing in this
build step detects a changed page or a substituted key before that. The owner decides whether
this is acceptable until the published hash of the website exists, or whether a later build step
adds a countermeasure (decision 17).

**After a compromise of the network edge**, every passkey that was used for a sign-in or
registered through an invite during it is revoked and removed from the allow lists, as after a hub
takeover (DESIGN.md §25.6). No keys-file pull request is merged for an invite that was created,
opened or received during it; a person who still needs a passkey gets a new invite after the
compromise ended.

This contradicts DESIGN.md §24.2, §25.6 and §25.16 as written; section 17.2 names the sentences
the owner adjusts.

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
| `passkey_invite` | `id` (primary key), `token_hash` (unique), `handle`, `team`, `created` (integer seconds), `expires`, `created_by`, `bootstrap` (0 or 1), `creation_request` (JSON of the signed request, or null for a bootstrap invite), `opened` (nullable), `received` (nullable), `credential_id` (nullable), `public_key` (nullable), `proposed_id` (nullable) | Kept, never deleted |
| `passkey_registration` | `id` (primary key), `invite_id`, `challenge`, `user_id`, `created`, `finished` (nullable) | Kept with its invite. The status of section 5.2 is derived from this row and its invite. |
| `passkey_sign_in` | `id` (primary key), `statement`, `session_public_key` (unique), `created` | Deleted when consumed (S2), or by a begin when older than `CEREMONY_SECONDS` (B2) |
| `passkey_session` | `id` (primary key, the session key id), `passkey_id`, `handle`, `credential_id`, `passkey_public_key`, `session_public_key` (unique), `statement`, `authenticator_data`, `client_data_json`, `signature`, `created`, `expires`, `ended` (nullable, set by sign-out, section 7.3) | Kept, never deleted: a certificate is evidence of what its key signed |
| `passkey_counter` | `passkey_id` (primary key), `sign_count` | One row per passkey after its first sign-in |
| `command_sequence` | `sender` (primary key), `last_sequence` | Updated atomically by C6 |

All binary values are stored as base64url text, all times as integer seconds. No table holds a
token, a private key or a secret.

## 13. Tests this step must carry

Every hub test of an endpoint runs through the application with a fixed clock, asserts the exact
status and the exact body, and asserts the hub signature on every answer under `/api/key/` after
check 8. The tests named "directly" below, the loader and configuration tests and the shared
vectors call the production functions without an HTTP request.

### 13.1 The software authenticator

The tests use a software authenticator written for this repository in
`tests/webauthn_authenticator.py`, built on `cryptography` and `cbor2` (decision 4). The package
`soft-webauthn` is not used: it never sets the UV flag, so it cannot pass a sign-in that requires
user verification, and it cannot be told to produce a wrong origin, a wrong rp id, a missing flag
or a counter that goes backwards without patching it.

The software authenticator:

- creates a real P-256 key pair and a random credential id of 16 to 64 bytes per credential
- `create(options, origin)` returns `credential_id`, `client_data_json` and an
  `attestation_object` in canonical CBOR with `fmt` `none`, exactly as a browser sends them
- `get(options, origin)` returns `credential_id`, `authenticator_data`, `client_data_json`,
  `signature` (DER) and `user_handle`
- has settings for: the origin and rp id it puts in, the flags (UP, UV, BE, BS, AT, ED), the
  counter (fixed, increasing, or a given value), `crossOrigin`, `topOrigin`, the client data
  `type`, the challenge it puts in, bytes appended to the authenticator data, a shortened
  authenticator data, non-canonical or duplicate-key CBOR, a tag (an unknown tag and a bignum
  tag), a floating-point value in its shortest form, a simple value, `true`, `false` and `null`
  placed at a chosen position of the attestation object, the credential public key or the
  extensions map (for example `3: -7.0`, `-1: true`, a key `1.0`), and a corrupted signature
- signs with `cryptography`'s ECDSA over exactly the bytes of section 4.3

The hub verifies its responses with the production code: the same functions, called through the
real endpoints.

### 13.2 The hub, invites

- an administrator's session key creates an invite: signed `201` with the exact body, the link
  holds the token in the fragment, the database holds only the token hash and the signed creation
  request
- a session key whose passkey lacks `keys:invite`, an `agent` key and a `service` key get the
  signed `403` `scope_missing`; a `webauthn` key used directly gets the unsigned
  `key_kind_not_allowed`, a revoked key the unsigned `key_revoked`
- `invite_invalid` for: each field missing, an extra field, a handle outside the rule, a handle
  that is an agent login, an unknown team, a lifetime of 299, 86401, a boolean, a string and a
  float; 300 and 86400 are accepted
- `invite_limit` when 50 invites are open (`pending` or `opened`); an `expired`, `failed` or
  `received` invite does not count; an `opened` invite counts until its ceremony time passed; two
  parallel creations with 49 open invites (exactly one succeeds, the other gets `invite_limit`)
- on each of `/auth/invite/open`, `/auth/invite/finish`, `/auth/passkey/begin` and
  `/auth/passkey/finish`: `request_invalid` for a `Content-Encoding` header of `gzip`, of
  `identity` and of an empty value, for a body with `NaN`, `Infinity` or `-Infinity` as a value,
  and for a key written twice
- open: `request_invalid` for a malformed body and a body over the limit, `invite_unknown` for an
  unknown token, `invite_expired` at and after the expiry time (and the invite is not marked
  opened), `invite_used` for a second open; two parallel opens of one invite (exactly one
  succeeds)
- `exclude_credentials` lists the handle's keys-file credential ids, revoked ones included
- registration: each refusal R1 to R14 in its order, produced by the software authenticator:
  a malformed body and a body over the limit, a reused registration, a registration older than
  300 seconds, an invite that expired during the ceremony, type `webauthn.get`, a wrong challenge,
  a wrong origin, `crossOrigin` true, a `topOrigin`, an attestation object with an extra key,
  trailing bytes, a key written twice and a non-canonical length, and an attestation object with
  an unknown tag, a float, a simple value, `true` or `null` inside `attStmt` (each
  `attestation_invalid` at R7), authenticator data of 36 bytes (`authenticator_data_invalid` at
  R8), a wrong rp id, UP not set, UV not set, AT not set, BS without BE, trailing bytes without
  ED, a non-canonical credential public key, a credential public key with `3: -7.0` written as
  `f9 c7 00`, with `-1: true`, with the key `1.0`, with a tag and with a bignum (each
  `authenticator_data_invalid` at R12), an extensions map after a set ED flag with a boolean value
  (accepted) and with a float value (`authenticator_data_invalid`), a credential id that differs
  from the body, a credential id of 15 and of 1024 bytes, an RS256 and an Ed25519 credential key,
  a point not on the curve (each `credential_invalid` at R13), a credential already in the keys
  file, and one already received by another invite
- the R13 check called directly on a decoded map refuses `-1: True`, `3: -7.0` and a key `1.0`,
  so that it holds also without the type check of R12
- a failed registration gives the invite status `failed` at once
- a successful registration: the fingerprint in the answer, status `received`, the exact
  `keys_entry`, `pull_request_title` and `pull_request_body`, and the proposed id skipping an id in
  the keys file and an id proposed for another invite; two parallel registrations of two invites
  for one handle get different proposed ids
- the list: newest first by the latest change of section 5.2, every status of section 5.2, no
  token or token hash in the answer; a `failed` invite sorts above an invite that was opened
  after it but whose registration was not finished, and below an invite created after its
  registration finished; two invites with the same latest change are ordered by invite id
  descending; `invite-list` prints the same order
- a received key that is not in the keys file cannot sign in (`credential_unknown`)
- `invite-bootstrap`: creates an invite when no entry holds `keys:invite`, with the
  `invite bootstrap created` line on standard error and only the link on standard output; refuses
  with status 1 and writes nothing for invalid arguments, when an unrevoked entry holds
  `keys:invite`, when only a revoked entry holds it, when another bootstrap invite is open, and
  at the open limit; creates one again after a keys file in which no entry holds `keys:invite`;
  two parallel runs create exactly one bootstrap invite
- `python -m agent_core` without an argument starts the hub as today; an unknown first argument
  and an unknown option exit with status 2, print a usage line and start nothing
- a bootstrap invite: the hub's `invite bootstrap opened` and `invite bootstrap received` log
  lines, the proposed entry with `keys:invite`, the bootstrap title and body, `created_by`
  `operator`
- `invite-list` prints the received key and the keys-file text, equal to the website's list

### 13.3 The hub, sign-in and the certificate

- a successful sign-in: the stored session, the exact answer, the session key id derived from the
  statement, and a following signed `whoami` with the exact body of section 7.2
- begin: `request_invalid` for a missing, extra or malformed field, an invalid point and a body
  over the limit, `session_key_known` for the session public key of a stored session and of a
  pending sign-in not older than 300 seconds, `too_many_pending` at the limit, deletion of pending
  sign-ins older than 300 seconds also when the begin is refused, a begin with the session public
  key of a pending sign-in older than 300 seconds (accepted: the old row is deleted first), and
  two parallel begins with 999 pending sign-ins (exactly one accepted)
- finish, each refusal S1 to S7 in its order: a malformed body, a missing or extra field and a
  body over the limit (S1); a reused challenge, that is a second finish (S2); a finish 301
  seconds after begin with no begin in between (`challenge_expired`, S3) and with a begin in
  between (`challenge_unknown`, S2); an unknown credential, and a credential received through an
  invite but not in the keys file (S4); a revoked keys-file entry (S5), and a revoked entry whose
  `revoked` time is in the future (accepted); the steps V2 to V10 through S6 as listed below;
  each counter case below (S7). V1 and V11 cannot fail for a statement the hub built and stored
  within `CEREMONY_SECONDS`; they are tested directly (below).
- through S6: type `webauthn.create` (V2), an assertion whose challenge is the hash of another
  statement (V3), a wrong origin, `crossOrigin` true and a `topOrigin` (V4), authenticator data of
  36 bytes (V5), a wrong rp id (V6), UP not set (V7), UV not set (V8), BS without BE, AT set and
  trailing bytes without ED (V9), a wrong signature and a signature that is not canonical DER
  (V10)
- a sign-in refused at S6 leaves the stored counter unchanged
- the counter: 0 then 0 accepted, 5 then 6 accepted, 6 then 6 refused, 6 then 3 refused, 6 then 0
  refused; two parallel sign-ins with the same counter value (exactly one accepted)
- `verify_certificate` directly: an expiry beyond now + `max_lifetime` (refused), exactly now +
  `max_lifetime` (accepted), at now (refused), each with `max_lifetime` 43200 and 3600; a statement
  with four or six lines, a trailing line feed, another label, another origin, a key that is not a
  point, a hub value of 31 bytes, an expiry not in the time form (each `statement_invalid`); a
  valid assertion for another statement (`challenge_mismatch`)
- an entry with `expires` on a `staff` entry is refused by the loader (unchanged rule), so an
  expired passkey entry cannot exist

### 13.4 The hub, signed requests by a session key

- each changed check of section 7.1 in its order: a session key id in the wrong form
  (`signature_missing`), an unknown session id, an ended session (`key_unknown`), a session whose
  passkey is revoked (`key_revoked`), a session at and after its expiry (`key_expired`), a session
  whose passkey entry changed its public key, credential id or handle (`certificate_invalid`), a
  session key signature over another text, an `r` or `s` of 0 or of n, a DER signature instead of
  `r ‖ s` (`signature_invalid`), a replayed nonce
- a high `s` is accepted
- check 9: a session key without the scope of a test-only route gets `scope_missing`
- check 9a on a test-only controller-only route: a session key, an `agent` key with
  `device:controller` and a `program` field, the same program key on the same device as a
  controller device key, an `agent` key with `device:executor` and no `program` field, and a
  `service` key each get the signed `403` `controller_device_required`; a device key with
  `device:controller` passes; on a route not marked controller-only a session key passes check 9a
- the loader rule of section 7.4: a keys file with two unrevoked `agent` entries of one handle on
  one `device` that both lack `program` is refused at start; a device key and a program key on
  one device load, and so does a revoked device key without `program` next to its unrevoked
  replacement without `program`; a device whose only `agent` entry carries `program` loads and has
  no device key (that entry fails check 9a and C5)
- the log line of section 7.1 for a session key, and no certificate, signature or body in any log
  line
- sign-out: signed `200`, then `key_unknown`; a keys-file key gets `session_key_required`
- every existing test of KEY-PROTOCOL.md section 10 still passes. The only existing test fixture
  that changes is the base `AGENT_CORE_PUBLIC_URL` in `_full_env` of `tests/test_config.py`, from
  `http://127.0.0.1:8787` to `http://localhost:8787` (section 15); every other existing test is
  unchanged.

### 13.5 The hub, commands

- a `noop` command: signed `200` with the exact body
- each refusal C1 to C6 in its order: an `agent` key with `device:controller` (`sender_not_allowed`);
  each field missing or extra, an unknown action, the actions `allowlist.add`, `grant`,
  `grant.remove` and `masking`, a content hash other than `-`, a sequence of 0, `SEQUENCE_MAX` + 1,
  with a leading zero, as a string, as a boolean and as the float `1.0` (`command_invalid`); a
  time 121 seconds off (`command_time_skew`, 120 accepted); a signature over another target,
  action, sequence, time or sender (`command_signature_invalid`); a target of another handle, a
  revoked target, a program key of the session's handle as target (also one on the same device as
  a device key of that handle), a `staff` entry and a `service` key as target, an unknown target
  (`target_not_allowed`); a repeated and a lower sequence
  (`sequence_not_higher`); two parallel commands with the same sequence (exactly one accepted)
- a command signed by an expired session key is refused at check 4

### 13.6 The hub, coexistence and configuration

- every route of this document answers the same with a GitHub fake that fails on every call
- a GitHub cookie session gets `signature_missing` on every passkey route under `/api/key/`, and
  the passkey session (its `Agent-*` headers, no cookie) gets `401` on `/auth/me`, `/api/state`
  and `/api/session/control`; the request to `/api/session/control` carries the query `id` and a
  JSON object body, because without them the application answers `422` before the sign-in check
- `Config.from_env` with a public URL whose host is `127.0.0.1`, `[::1]`, `127.1` or `0x7f000001`
  stops the start with the IP-address message of section 3, and one whose host is `127.0.0.1.` or
  `hub.example.` with the trailing-dot message; `http://localhost:8787` and
  `https://hub.example` start; the URL-form cases of `tests/test_config.py` keep their IP hosts
  and their messages; and `public_url_host` still returns the host line for `[::1]` and
  `[2001:db8::1]`
- the pages `/passkey`, `/invite` and `/static/passkey.js` carry the headers of section 10.1, no
  inline script and no substituted value, and answer `HEAD`
- the dashboard `/` carries the link to `/passkey` and is otherwise unchanged
- the tables of section 12 are created at start, and after a full invite and sign-in no row holds
  the invite token or any private key

### 13.7 The website, without a browser

These run with Node.js like the existing dashboard tests:

- `signText` refuses every first line other than `agent-request-v1` and `agent-command-v1`
- the request text and the command text are built byte for byte as in the hub, checked against
  the shared vectors of section 13.10
- the statement check of section 6.3 stops for another session key, another origin, an expiry
  beyond 43200 seconds, and a `challenge` that is not SHA-256 of the statement
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
2. The test writes the `keys_entry` that `invite-list` prints, which carries `keys:invite`, and
   restarts the hub.
3. The admin context signs in on `/passkey`; the page shows `example-admin`. In the page,
   `crypto.subtle.exportKey("pkcs8", privateKey)` of the stored session key rejects.
4. The admin creates an invite for `example-staff`. A second context with its own virtual
   authenticator opens the link and creates the passkey. Both pages show the same fingerprint.
5. The test writes the `keys_entry` that `invite-list` prints and an `agent` entry
   `example-staff-laptop-1` for `example-staff` without `program` (the device key of section
   7.4), and restarts the hub.
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

- each device check of section 9 in its order, each refused: an unknown passkey; a revoked, a
  suspended and an expired allow-list entry; an entry from a grant that expired and from one that
  was removed; every step V1 to V11 of `verify_certificate`, including a valid assertion whose
  challenge is the hash of another statement (V3), an expiry beyond the configured lifetime and
  an expired session key (V11); a wrong sender id; a command signature over another field; a
  `content` or `content_random` that is not `null` for `-`, and a content that does not match
  line 7; another target, including the id of a program key on the same device; a time outside
  the window, a repeated or lower sequence; an action outside the entry
- a device configured with L = 3600 refuses a fresh certificate and accepts it 39480 seconds
  later
- the bundle is written to the local log before the command runs
- the shared test vectors of section 13.10 give the same result as in the hub
- a grant signed by a session key, by a program key and by an executor key is refused; the same
  for an allow-list statement, once DESIGN.md §25.20 step 1 has fixed its format

The refusal tests of DESIGN.md §25.21 that concern maskings (DESIGN.md §25.20 step 5, including a
masking signed by a session key, a program key or an executor key), the signing service and the
team record belong to the build steps that build those parts; the ones for allow lists and grants
are in the client list above. This repository carries the ones it can: an unknown key, an expired
session key, a replayed command, a command outside its permission (C5), a session-key expiry
beyond the limit, and a session key, a program key or an executor key on a route reserved to a
controller device (check 9a).

### 13.10 Shared test vectors

`tests/vectors/passkey.json` in this repository holds fixed inputs and the expected result for
each of them: for every step of section 4 and V1 to V11, one accepted input and at least one
refused input per `error` value, including the CBOR cases of 13.2; the request text, the command
text, the statement and the fingerprint of section 2, byte for byte; and for each of the device
checks 2 to 6 of section 9, one accepted and one refused bundle (checks 1 and 7 depend on the
allow-list format, which this document does not fix). The hub tests run every vector through the
production code, the website tests of 13.7 run the text and fingerprint vectors, and the client
repository copies the file unchanged and runs every vector through its own implementation (13.9).
A change of the steps changes the file in the same pull request.

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
- `pyproject.toml` registers the marker and deselects it by default:
  `markers = ["e2e: end-to-end tests in headless Chromium"]` and `addopts = "-m 'not e2e'"` under
  `[tool.pytest.ini_options]`. The `-m e2e` of the CI job comes later on the command line and
  replaces it. A plain `pytest` therefore runs the unit tests only and needs no browser.
- `e2e` is a required check exactly like `pytest`. The same change edits, in this repository:
  - `.github/workflows/test.yml`: the job above.
  - `.github/a38.json`: a second entry in `jobs`, beside `pytest` and in its form, so that every
    workflow job has its policy entry (each job of every workflow is in `jobs` or in
    `exclusions`) and the A38 local CI report runs this job:

    ```json
    {"id": "e2e", "name": "e2e", "timeout_s": 1800,
     "workflow": ".github/workflows/test.yml", "job": "e2e",
     "executor": {"adapter": "commands", "config": {
       "env": {"CI": "true", "AGENT_CORE_E2E_REQUIRED": "1"},
       "steps": [
         ["python3", "-m", "pip", "install", "-q", "-e", ".[test,e2e]"],
         ["python3", "-m", "playwright", "install", "chromium"],
         ["python3", "-m", "pytest", "-m", "e2e", "tests/e2e"]]}}}
    ```

    The local step installs Chromium without `--with-deps`, which needs administrator rights;
    the machine that measures the report has Chromium's system libraries installed. With
    `AGENT_CORE_E2E_REQUIRED` set to `1` a missing library fails the job, it never skips it.
  - `.github/pr-guard.json`: `"e2e"` added to `lifecycle.required_checks[".github/workflows/test.yml"]`,
    after `"pytest"`, so that the guard sets Ready only while both jobs are green.
    `.github/workflows/test.yml` is already in `workflow_approval.workflows` and in
    `lifecycle.required_workflows`; nothing else in that file changes.
  - `CONTRIBUTING.md`: the sentence "Run `pytest` before you push." becomes "Run `pytest` and,
    with the `e2e` extra and Chromium installed (`pip install -e ".[test,e2e]"` and
    `python -m playwright install chromium`), `AGENT_CORE_E2E_REQUIRED=1 pytest -m e2e tests/e2e`
    before you push."
- The guard reads its policy as described in CONTRIBUTING.md. Where it reads
  `.github/pr-guard.json` from the default branch, the new required check applies to the pull
  requests after this change reached that branch; for the pull request of this change itself, its
  reviewers confirm that the `e2e` job is green before they approve.
- No module in `tests/e2e` imports Playwright at module level directly. The first statement after
  the standard imports is: when `AGENT_CORE_E2E_REQUIRED` is `1`, `import playwright.sync_api`,
  so that a missing Playwright fails the run; otherwise
  `pytest.importorskip("playwright.sync_api")`, so that collection in a run without the `e2e`
  extra skips the module instead of failing. With `AGENT_CORE_E2E_REQUIRED=1` a missing Chromium
  also fails the run instead of skipping it.
- The job runs on every pull request and every push to `develop` and `main`, like `pytest`.

## 15. Dependencies and configuration

- New runtime dependency: `cbor2>=5.6,<6`, for decoding and checking the attestation object and
  the credential public key. It is also used by the test authenticator for encoding.
- New optional dependency group `e2e`: `playwright>=1.47,<2`.
- `cryptography` (already required) does ECDSA over P-256, SHA-256 and the DER encoding.
- The hub needs no new environment variable. The rp id and the origin come from
  `AGENT_CORE_PUBLIC_URL`. `AGENT_CORE_E2E_REQUIRED` (section 14) is read only by the
  end-to-end tests.
- Because of the host refusals of section 3 (trailing dot, IP address), the same change edits
  these files:
  - `src/agent_core/config.py`: the refusals in `Config.from_env`, after `public_url_host`,
    outside it (section 3)
  - `docker-compose.yaml`: `AGENT_CORE_PUBLIC_URL: http://localhost:8787` instead of
    `http://127.0.0.1:8787`
  - `tests/test_config.py`: the base `AGENT_CORE_PUBLIC_URL` in `_full_env` becomes
    `http://localhost:8787`; the new cases of section 13.6 are added. The URL-form cases with IP
    hosts and the host-line cases for `[::1]` and `[2001:db8::1]` stay as they are and keep their
    expected results, because the host refusals run after the URL-form checks and outside
    `public_url_host`.
  - `README.md`: the local run uses `http://localhost:8787` for `AGENT_CORE_PUBLIC_URL`, for the
    GitHub OAuth callback and for the address to open; the list of rules for
    `AGENT_CORE_PUBLIC_URL` says that its host is a name, not an IP address, and does not end with
    a dot. `AGENT_CORE_HOST=127.0.0.1` stays: it is the address the process listens on, not the
    public URL.
- `src/agent_core/__main__.py`: the dispatch of section 5.3; without an argument it still starts
  the hub.
- `pyproject.toml`: the dependencies above and the pytest settings of section 14.
- The CI files and CONTRIBUTING.md of section 14: `.github/workflows/test.yml`,
  `.github/a38.json`, `.github/pr-guard.json` and the sentence in `CONTRIBUTING.md`.
- `tests/vectors/passkey.json`: the shared test vectors of section 13.10.
- The deployment forwards `POST /auth/passkey/*` and `POST /auth/invite/*` bodies unchanged. The
  network edge rate-limits these anonymous routes per client address; that limit is what keeps
  one sender from filling `PENDING_SIGN_IN_LIMIT` (section 6.1). The hub's own limits are B3, I2
  and the body limit.

## 16. Rollout order

1. Check that the public URL of every environment has a host name, not an IP address, and that
   the host name does not end with a dot. A hub whose public URL breaks either rule does not start
   with this change.
2. Merge the change, together with the edits of section 15 (`src/agent_core/config.py`,
   `src/agent_core/__main__.py`, `docker-compose.yaml`, `tests/test_config.py`, `README.md`,
   `pyproject.toml`, `tests/vectors/passkey.json`, and the CI files and the sentence of
   `CONTRIBUTING.md` of section 14: `.github/workflows/test.yml`, `.github/a38.json`,
   `.github/pr-guard.json`). Both CI jobs, `pytest` and `e2e`, are green. The tables are created
   at start.
3. The operator runs `invite-bootstrap` for the administrator. The administrator creates the
   passkey and compares the fingerprint with the output of `invite-list`. The pull request adds
   the `keys_entry` that `invite-list` prints, which carries `keys:invite`; the operator compares
   its `credential_id` and `public_key` with that output; the team reviews it.
4. After that pull request is deployed, the administrator invites every other staff member
   (DESIGN.md §25.15). Each passkey counts after its own pull request is deployed; for each, the
   operator compares the pull request with `invite-list` (section 5.6).

GitHub sign-in keeps working during and after these steps. Its removal is the following build
step, and it is merged only after every staff member who uses the website has a passkey in the
keys file.

## 17. Decisions taken in this document

### 17.1 Decisions

1. One working day is 43200 seconds (12 hours); this is `SESSION_KEY_MAX_SECONDS` for the hub and
   the upper bound of a device's configured lifetime. (Section 2, 6, 9)
2. The hub issues certificates for 43080 seconds, 120 seconds less than the maximum, so that a
   device configured with 43200 seconds and a clock up to 120 seconds behind accepts them at once.
   A device may be configured with a smaller lifetime L (1 to 43200); it then refuses a fresh
   certificate for its first 43080 − L seconds plus its clock offset. The hub does not let a
   sign-in ask for a shorter lifetime. The client repository's default for L is 43200. Choice for
   the owner: of the alternatives "L must be 43200" and "a smaller L is allowed and refuses more",
   the stricter second is taken. (Section 2, 6.1, 9)
3. No server-side WebAuthn library; the verification steps are fixed in section 4. The hub and a
   device implement them in their own repositories and both run the shared test vectors
   `tests/vectors/passkey.json`, which the client repository copies unchanged. (Section 4, 6.4,
   13.10)
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
8. Check 5 for a session key compares the passkey entry's credential id, public key and handle
   with those stored at sign-in, error `certificate_invalid`. Check 9a lets only the device key of
   a device (section 7.4) that holds `device:controller` reach a controller-only route and refuses
   every other key there with the one error `controller_device_required`: a session key, an
   executor device key, a `service` key, and every program key, also one with
   `device:controller` on the same device as a controller device key. Choice for the owner: of
   "refuse every key but an `agent` key with `device:controller`" and "refuse program keys as
   well", the stricter second is taken. (Section 7.1, 7.4, 8.3)
9. Opening an invite consumes it; a cancelled or failed ceremony needs a new invite. (Section 5.4)
10. No attestation is verified; `fmt` and `attStmt` are ignored. Trust in the key comes from the
    fingerprint comparison and the reviewed pull request. (Section 5.5)
11. The invite token is in the URL fragment and only its SHA-256 is stored. (Section 5.1)
12. An invite needs an existing team but not membership of the handle; membership is checked by
    the loader when the keys-file pull request is deployed. (Section 5.1)
13. Invite lifetime is 300 to 86400 seconds; at most 50 invites are open at once. (Section 2, 5.1)
14. The user id of a new passkey is 16 random bytes per invite, never derived from the handle, so
    that no authenticator replaces an existing passkey. (Section 5.4)
15. The administrator's first passkey comes through a bootstrap invite, restricted as far as
    possible: only from the operator command on the hub host; only while no keys-file entry,
    revoked or not, holds `keys:invite`; only one open bootstrap invite at a time; only for an
    entry that carries `keys:invite`; written to standard error and the hub's log with the
    handle; kept as a row with `created_by` `operator` and recorded as an event in the team record
    as soon as the record exists. It carries no administrator's signature, unlike DESIGN.md §24.2
    "Creating an invite is an event in the team record, signed by the administrator". After every
    administrator passkey is lost, a reviewed pull request that removes `keys:invite` from every
    entry opens the path again. The keys-file pull request, which the team reviews, is the real
    control. Owner decision. (Section 5.3, 5.6)
16. The administrator may invite their own handle for a further passkey; the proposed entry of a
    normal invite never carries `keys:invite`, the proposed entry of a bootstrap invite always
    does. (Section 5.1, 5.3, 5.6)
17. The website does not verify hub answer signatures, and a browser has no key pinned outside
    the hub. Consequence, stated in full in section 10.3: the page code and the answers the
    website shows are only as trustworthy as the connection that delivered them, and the network
    edge, like a hub that was taken over, can change them. An edge that changes the page code can
    use the signed-in person's session key for as long as the page is open and the key is valid,
    within that session's rights; at a sign-in it can get a session key of its own certified,
    which it then holds within the same rights until that key expires, its session is ended or its
    passkey is revoked; at an invite it can substitute the received key. It cannot extend the
    lifetime, cannot create a session without the person's passkey confirmation, cannot sign
    anything that changes rights, and cannot forge an answer to a device or a program; a device
    verifies every command and certificate itself. The damage is bounded by the lifetime of a
    session key, the rights of a session key, and the record of every command (the hub's log line
    of section 7.1 and the device's local log). Nothing in this build step detects a changed page.
    After a compromise of the network edge, every passkey that was used for a sign-in or
    registered through an invite during it is revoked and removed from the allow lists, as after
    a hub takeover (DESIGN.md §25.6), and no keys-file pull request is merged for an invite that
    was created, opened or received during it. Owner decision: whether this is acceptable until
    the published hash of the website exists, or whether a later build step adds a
    countermeasure; section 17.2 names the sentences to adjust. (Section 10.3)
18. Every request of a session key writes one log line, like a `service` key, until the access
    log exists. (Section 7.1)
19. The signature counter rule of section 4.4; synced passkeys with counter 0 always pass; the
    device does not check the counter; the counter is the last check of a sign-in (S7), so a
    refused sign-in never changes it. (Section 4.4, 6.3, 9)
20. The user handle returned at sign-in is ignored. (Section 6.3)
21. In this build step the only command action is `noop`, the only sender is a session key, the
    only targets are the unrevoked device keys (section 7.4) of the handle stored with the session,
    never a program key, and the hub delivers nothing. A device accepts a command only when its
    target is exactly the id of its own device key. (Section 8, 9)
22. A passkey session cannot use the existing cookie endpoints; moving them is the following build
    step. (Section 1, 11)
23. Certificates, sessions and invites are kept, never deleted; pending sign-ins are deleted when
    consumed or by a begin after 300 seconds, before that begin's check B2, so an old pending
    sign-in never blocks its session public key. A finish after 300 seconds is refused with
    `challenge_expired` or `challenge_unknown`, depending on whether a begin deleted the row in
    between. (Section 6.1, 6.3, 12)
24. A public URL whose host name ends with a dot, or whose host is an IP address, stops the
    process at start. Both rules test the host name without port (`urlsplit(...).hostname`), never
    the host line. Choice for the owner: of "drop one trailing dot before the IP test" and "refuse
    a trailing dot", the stricter second is taken. The checks run in `Config.from_env` after the
    URL-form checks and outside `public_url_host`; `docker-compose.yaml`, the base URL of
    `tests/test_config.py` and the README's local run move to `localhost`. (Section 3, 15)
25. The invite creation is kept as the stored signed request until the team record exists.
    (Section 5.1)
26. End-to-end tests are deselected by default through a registered marker, are skipped at
    collection without the `e2e` extra, and are required in the CI job `e2e`. That job is a
    required check like `pytest`: it is in `jobs` of `.github/a38.json`, so the A38 local CI report
    runs it, and in `required_checks` of `.github/pr-guard.json`; CONTRIBUTING.md asks to run it
    before a push. (Section 14, 15, 16)
27. The ceremony limit for sign-in and registration is 300 seconds; at most 1000 pending sign-ins,
    an availability limit that the edge's per-address rate limit protects; anonymous bodies at
    most 65536 bytes. (Section 2, 6.1, 15)
28. DESIGN.md §24.2 says every request is authenticated and names opening an invite link as the
    only exception. These requests carry no registered key, and no others:
    `POST /auth/invite/open` (authenticated by the token); `POST /auth/invite/finish` (bound to the
    open by the registration id); `POST /auth/passkey/begin` (hands out a challenge, unavoidable
    because the passkey signs only after it); `POST /auth/passkey/finish` (authenticated by the
    passkey); `GET` and `HEAD` on `/passkey`, `/invite` and `/static/passkey.js` (static code
    without data). Owner decision. (Section 5.4, 5.5, 6.1, 6.3, 10.1)
29. `COMMAND_TIME_WINDOW` is 120 seconds, like the request time window; `SEQUENCE_MAX` is
    9007199254740991, the largest integer JavaScript represents exactly. (Section 2, 8)
30. The session key is kept in IndexedDB across browser restarts, with the next sequence number in
    the same record, until sign-out, its expiry or a refusal that ends it. (Section 10.2)
31. The fingerprint of a P-256 key is the first 16 bytes of SHA-256 over the 65-byte point, in 8
    groups of 4 hex characters. (Section 2)
32. The proposed keys-file id is `<handle>-passkey-<n>` with the smallest free `n`, chosen in the
    same transaction as storing the received key. (Section 5.6)
33. Sign-out is `POST /api/key/session/end`; it sets the session's `ended` time, after which
    check 2 refuses the key. (Section 7.3, 12)
34. `exclude_credentials` includes revoked credentials of the handle. A person replacing a revoked
    passkey on the same authenticator deletes the old passkey from it first. (Section 4.4, 5.4)
35. `verify_certificate` is one sequence of steps for hub and device, implemented in each
    repository and checked against the shared test vectors. It computes the expected challenge as
    SHA-256 over the statement bytes itself, checks the statement's structure (error
    `statement_invalid`), and takes the expiry bound as a parameter: `SESSION_KEY_MAX_SECONDS` on
    the hub, the configured lifetime on a device. (Section 6.3, 6.4, 9)
36. A device obeys a passkey only while its allow-list entry is not revoked, not suspended and
    not past its "until" time, and, for an entry from a grant, while the grant has not expired and
    was not removed. (Section 9)
37. The binding source of the keys-file text is `invite-list` on the hub host; an operator
    compares every keys-file pull request with it; the website copy is a convenience. Owner
    decision. (Section 5.2, 5.6, 16)
38. Invite statuses are `pending`, `opened`, `received`, `failed` and `expired`; an invite is
    open when `pending` or `opened`, and only open invites count against the limit. A failed
    registration makes its invite `failed` at once. (Section 5.2)
39. An invite's expiry is checked before it is marked opened, in the same atomic update, so an
    expired invite is never marked opened. (Section 5.4)
40. Every CBOR item the hub decodes passes three checks: the RFC 7049 §3.9 canonical form as
    `cbor2` writes it with `canonical=True`, checked by encoding again; only the types integer,
    byte string, text string, array and map, checked by a walk with `type(value) is …`, with
    `true` and `false` allowed only as values in the extensions map after flag ED; and integers
    only in the range of CBOR major types 0 and 1. A duplicate map key, a non-canonical encoding,
    a tag, a floating-point value, a simple value, `null` and a boolean elsewhere are refused. R13
    also requires `type(value) is int` and `type(value) is bytes` for the credential public key.
    A browser or authenticator that sends anything else cannot register; that real authenticators
    (platform authenticators, password managers, security keys) send such CBOR is assumed and not
    yet confirmed by a capture, and a wrong assumption shows as a refused registration, never as
    an accepted one. Choices for the owner: of "refuse duplicate keys only" and "require canonical
    CBOR", the stricter second is taken; of "allow booleans and `null` everywhere" and "allow a
    boolean only where a boolean is expected", the stricter second is taken; of "drop the rule
    against tags and floats" and "enforce it by a walk", the stricter second is taken. (Section 2,
    5.5, 13.1, 13.2)
41. The authenticator data length (at least 37 bytes) is checked before any byte of it is read
    (R8, V5). (Section 4.2, 5.5, 6.4)
42. The device bundle carries `content` and `content_random` beside the command, both `null`
    when line 7 is `-`, and the device checks them against line 7. (Section 8.1, 9)
43. The pages and the script contain no value substituted by the hub; the script takes the
    origin and host line from `location`. (Section 6.3, 10.1, 10.3)
44. The device key of a device is its one unrevoked `agent` entry without `program`; every other
    `agent` entry of that device carries `program` and is a program key; the loader refuses a
    file with two unrevoked `agent` entries of one handle and one device without `program`. This
    extends KEY-PROTOCOL.md section 3. A device whose entries all carry `program` has no device
    key. Choice for the owner: the rule requires at most one device key per device, not exactly
    one, so that an existing file with only program keys on a device still loads; such a device
    can neither pass check 9a nor be a command target. (Section 7.4, 13.4)
45. The invite list is ordered newest first by the latest recorded change: `received`, else the
    registration's `finished`, else `opened`, else `created`; the passing of an expiry is not a
    recorded change. (Section 5.2, 13.2)
46. Every JSON text this document parses refuses a key written twice and the constants `NaN`,
    `Infinity` and `-Infinity`, and a JSON integer is a value of type exactly `int`. (Section 2,
    4.1, 5.1, 8.2)
47. The anonymous routes `/auth/invite/open`, `/auth/invite/finish`, `/auth/passkey/begin` and
    `/auth/passkey/finish` refuse any `Content-Encoding` header, also `identity`, with
    `request_invalid`. Unlike check 1 of KEY-PROTOCOL.md section 5, which accepts `gzip`, no
    content coding is accepted there: their bodies are at most `ANONYMOUS_BODY_LIMIT` bytes.
    (Section 5.4, 5.5, 6.1, 6.3)
48. The open-invite limit (I2 and the bootstrap checks 3 and 4) and the pending sign-in limit
    (B2 and B3) are each checked and updated in one write transaction, so parallel requests never
    exceed them. (Section 5.1, 5.3, 6.1)
49. `python -m agent_core` without an argument starts the hub as today; `invite-bootstrap` and
    `invite-list` run the operator commands; any other argument starts nothing and exits with
    status 2. Choice for the owner: of "ignore unknown arguments and start the hub" and "refuse
    them", the stricter second is taken. (Section 5.3, 15)
50. The client tests (DESIGN.md §25.20 step 2) refuse a grant and, once step 1 has fixed its
    format, an allow-list statement signed by a session key, a program key or an executor key;
    the masking case belongs to step 5. (Section 13.9)

### 17.2 Decisions for the human owner

The owner confirms decisions 2, 8, 15, 17, 24, 26, 28, 37, 40, 44, 45, 49 and 50, and adjusts these
sentences of DESIGN.md:

- §24.2: the sentence that the network edge "cannot forge a request or an answer, because both are
  signed". It holds for devices and programs, which pin the hub key; it does not hold for the
  website, whose page code and answers the edge can change until the published website hash
  exists (decision 17, section 10.3).
- §25.16, the last row of its table, which says the same about the edge.
- §25.16, the row "The website shows one command and signs another, or certifies a key of the
  attacker at sign-in | Possible while the hub is taken over …": it is also possible while the
  network edge is compromised (decision 17, section 10.3).
- §25.6 "One limit remains: the hub delivers the website, so … a hub that was taken over can get
  commands signed in that person's name": the network edge is a second such limit, because it can
  change the page code as well. Together with that sentence, the rule for a hub takeover gets its
  twin for the edge: after a compromise of the network edge, every passkey that was used for a
  sign-in or registered through an invite during it is revoked and removed from the allow lists,
  as after a hub takeover, and no keys-file pull request is merged for an invite that was
  created, opened or received during it (decision 17, section 10.3).
- §24.2 "Creating an invite is an event in the team record, signed by the administrator": the
  bootstrap invite is created without an administrator's signature (decision 15).
- §24.2 "Every request is authenticated", with opening an invite link as the only exception: the
  list of decision 28 replaces that exception.
