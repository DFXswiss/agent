# contracts/vectors

Shared crypto test vectors of `docs/platform/spec/04-crypto.md` section 10. Every implementation – the Python
reference `agent_hub.crypto` (agent-core), its copy `agent_runner.crypto` (agent) and the TypeScript core
`web/src/core/crypto` – reproduces the positive vectors byte for byte and refuses every negative vector with exactly
the stated error code (CON-02).

## Generation

```
python contracts/tools/make_vectors.py           # write the files that changed
python contracts/tools/make_vectors.py --check   # compare only; exit 1 if a file is missing or stale
```

The generator logic is `agent_hub.crypto.vectors` (ADR-027 section 2); `make_vectors.py` only calls it. Every file is
one **section** `Section(name, build)` and is written as `<name>.json`: `json.dumps(build(), indent=2,
sort_keys=True, ensure_ascii=False) + "\n"` (UTF-8). All inputs are fixed – derived from labels by
`fixed(label) = SHA-256("agent.dfx.swiss vectors v1 " || label)[:n]`, including AES-GCM nonces, HKDF salts, HPKE
ephemeral keys and UUIDv7 values – so the output never changes unless the code does. `test_vectors_file_is_current`
(`tests/platform/hub/crypto/test_vectors.py`) fails while a file is stale; then `make contracts-version` updates
`contracts/VERSION`.

Further sections are **added, not registered by editing**: a module `agent_hub/<area>/vectors.py` or a file
`contracts/tools/vectors_<name>.py` with a list `SECTIONS` of `agent_hub.crypto.vectors.Section` is discovered
(areas, then tool files, each sorted by name). Duplicate section names are refused. The log vectors of CRY-02
(`log*.json`, `platform-log*.json`) come in this way. Files that no section produces are left untouched.

## Conventions

- Byte strings are b64u (base64url without padding) unless a key says `hex`. Texts that are themselves canonical JSON
  or body JSON (`canonical`, `aad`, `body_json`, `plaintext`, `text`) are stored as JSON strings.
- Actors: `keys.json` → `identities` (`alice`, `bob` users, `device`) with `id` (UUIDv7), `kem_sk` (X25519 private),
  `sig_sk` (Ed25519 seed) and public `kem`/`sig`. Every other file repeats the keys it needs.
- Every file has `about` and `invalid`. An `invalid` entry is `{"name", "op", "args", "error"}`: calling operation
  `op` with `args` MUST fail with `error` (a code of 04-crypto.md section 8) and nothing else. The generator runs
  each one and refuses to write a vector whose code differs.

### Check order (error precedence)

When an input has several faults the first check wins; the negative vectors contain one fault each, but
implementations follow this order so that they agree:

1. Signed structures (`event`, credential `record`, request/response `envelope`): object key set exactly
   `{header, ct, sig}` → `header_invalid`.
2. Header key set exactly as specified → `header_invalid`; then canonical JSON of the header (any string or key
   outside 0x20-0x7E → `non_ascii`; float, unsafe integer → `header_invalid`); then the field formats in the order
   of the implementation (all `header_invalid`).
3. `ct` strict b64u → `header_invalid`; size → `size_exceeded`.
4. Signature (missing, not b64u, wrong length or wrong) → `signature_invalid`.
5. HPKE: `enc || ct` shorter than 48 bytes → `hpke_short`; every other HPKE/AEAD failure (including a small-order
   `enc`) → `decrypt_failed`.

Code texts: remove `-` and spaces, uppercase; a character outside `A-Z2-7` → `invite_code_invalid`; length ≠ 26
→ `invite_code_length` (recovery codes: `recovery_code_length`); non-zero trailing bits → `invite_code_invalid`.
An invitation package that does not open with the code's key → `invite_code_invalid` (04 section 5.1 step 2);
one that opens but has the wrong structure → `header_invalid`. Malformed b64u anywhere → `header_invalid`.

### JSON details every implementation must match

- **Canonical JSON**: first every string and key is checked for 0x20-0x7E (`non_ascii`, whatever else is wrong),
  then the types (`header_invalid` for floats, integers beyond ±(2^53-1), non-string keys). Integers must be sent as
  integer literals: the reference refuses `3.0` or `1e0` in a header (`header_invalid`); a JavaScript parser cannot
  see the difference, so the TypeScript core MUST NOT emit such literals (it never does with `JSON.stringify` of
  integers).
- **Body JSON** sorts object keys by Unicode **code point** (not by UTF-16 code unit, which JavaScript's default
  sort uses): `encoding.json` → `body_json` contains a key above U+FFFF next to U+FFFF to pin this. Control
  characters are escaped as `\u00XX` except `\b \t \n \f \r`; U+2028/U+2029 and DEL stay raw (identical in
  Python `json.dumps(ensure_ascii=False)` and `JSON.stringify`).
- **Wrap signature** (04 section 4.2) signs canonical JSON that contains the wrap as **b64u text**; this is the
  literal rule of 4.2 and is unambiguous because b64u decoding is strict. Event, credential and request signatures
  cover the raw ciphertext bytes (section 1).
- **Salt and nonce** of events and credential records are drawn by the sealing function for every seal; callers
  pass the header without them. The vectors fix them through a test-only parameter.
- **Intermediate values** for locating mismatches: `wrap.json` carries `shared_secret`, `key` and `base_nonce` of
  every HPKE setup; `event.json` carries `k_event`; `credential.json` carries `k_cred`.

### Dedupe field sets (`dedupe.json`, 04 section 4.10, 03-api.md section 9)

| Type | Fields |
|---|---|
| `github.checkout` | `repo`, `ref` (resolved commit SHA), `session` |
| `github.create_branch` | `repo`, `branch` |
| `github.commit` | `repo`, `branch`, `parent_sha`, `tree_sha` |
| `github.push` | `repo`, `branch`, `commit_sha` |
| `github.create_pull_request` | `repo`, `head`, `base` |
| `github.comment` | `repo`, `number`, `body` = `sha256_hex(UTF-8 body)` |
| `subagent.request` | `session` (parent session), `prompt` = `sha256_hex(UTF-8 purpose ‖ 0x00 ‖ UTF-8 prompt)` |
| with idempotency key | `idempotency_key`, `task` (only for a type without dedupe fields; a type with fields keeps them, KOR-28) |

`repo` is lowercased, SHAs are lowercased, `branch`/`head`/`base` lose a leading `refs/heads/`; a branch, ref, SHA or
idempotency key that is not printable ASCII is replaced by `sha256:<hex of its UTF-8>`. The `0x00` separator makes
the purpose/prompt split unambiguous (the plain concatenation of 03 section 9 is not). Other operation types have no
dedupe fields (`header_invalid`).

## Operations named by negative vectors

Arguments are JSON; byte strings b64u. Private keys are passed as raw bytes (`*_sk`), public keys as raw bytes
(`*_pk`, `public_key`).

| `op` | `args` | Python reference |
|---|---|---|
| `b64u_decode` | `text`, optional `length` | `encoding.b64u_decode` |
| `canonical` | `value` | `encoding.canonical` |
| `parse_time` | `text` | `encoding.parse_time` (`YYYY-MM-DDTHH:MM:SS.mmmZ`) |
| `kek_from_prf` | `prf_output`, `credential_id` | `kdf.kek_from_prf` |
| `kek_from_recovery_code` | `code` | `kdf.kek_from_recovery_code` |
| `recovery_id` | `code` | `recovery.recovery_id` |
| `invite_secrets` | `code` | `invite.invite_secrets` |
| `ed25519_verify` | `public_key`, `signature`, `message` | `keys.ed25519_verify` |
| `p256_verify_raw` | `public_key` (65-byte point), `signature` (`r‖s`), `message` | `keys.p256_verify_raw` |
| `open_bundle` | `kek`, `user_id`, `slot`, `sealed` | `bundle.open_bundle` |
| `hpke_open` | `recipient_kem_sk`, `info`, `sealed` | `hpke.open_` |
| `unwrap_key` | `recipient_kem_sk`, `scope`, `scope_id`, `epoch`, `recipient`, `wrap` | `wrap.unwrap_key` |
| `verify_wrap` | `wrapper_sig_pk`, `scope`, `scope_id`, `epoch`, `recipient`, `wrap`, `sig` | `wrap.verify_wrap` |
| `verify_event` | `event`, `author_sig_pk` | `event.verify_event` (E1, E2, E4, E8) |
| `open_event` | `scope_key`, `event`, `author_sig_pk` | `event.open_event` |
| `verify_credential` | `record`, `author_sig_pk` | `credential.verify_credential` |
| `open_credential` | `cred_key`, `record`, `author_sig_pk` | `credential.open_credential` |
| `verify_request` / `verify_response` | `envelope`, `user_sig_pk` / `device_sig_pk` | `request.verify_request` / `verify_response` |
| `open_request` / `open_response` | `device_kem_sk` / `user_kem_sk`, `envelope`, `user_sig_pk` / `device_sig_pk` | `request.open_request` / `open_response` |
| `parse_code` / `parse_recovery_code` | `text` | `invite.parse_code` / `recovery.parse_recovery_code` |
| `open_package` | `code`, `sealed` | `invite.open_package(invite_secrets(code), sealed)` |
| `verify_proof` | `identity_sig_pk`, `user_id`, `challenge`, `proof` | `recovery.verify_proof` |
| `verify_enrollment` | `body`, `pk_inv` | `enroll.verify_enrollment` |
| `dedupe_fields` | `type`, `params` | `dedupe.dedupe_fields` |

## Files

| File | Content (positive part) |
|---|---|
| `encoding.json` | b64u (`hex` ↔ `b64u`), canonical JSON (`value` → `canonical`), body JSON with non-ASCII and control characters (`value` → `body_json`), header times, UUIDv7 (`unix_ms`, `random_hex` → `uuid`; version and variant bits overwrite the random bytes). |
| `kdf.json` | `prefix`, `prf_input`, `KEK_i`, `KEK_rec`, `recovery_id`, `invite_id`, `package_key`, `sk_inv` seed and `pk_inv`, `k_event`, `dk`, `k_cred`, runner storage key. |
| `keys.json` | identities; one Ed25519 signature; P-256 scalar, 65-byte point and a deterministic (RFC 6979) raw signature – WebCrypto signatures are random, so implementations verify it and check that their own signatures verify. |
| `bundle.json` | bundle `plaintext` (canonical), passkey slot (`prf_output`, `credential_id`, `slot`, `kek`, `aad`, `nonce`, `sealed`) and recovery slot. |
| `wrap.json` | three HPKE wraps of one workgroup key (`alice`, `bob`, `device`) with `ephemeral_sk`, `info`, `wrap`, and the wrapper signature `sig` by `alice` over `P‖"wrap\0"‖canonical({epoch,recipient,scope,scope_id,wrap})`. |
| `event.json` | one `wg` event (`prompt`) and one `org` event (`org.profile`) with non-ASCII bodies: `scope_key`, `author_sig_pk`, `body_json`, `aad` (= canonical header), sealed `event` (salt and nonce in the header); E8 `limits`. |
| `credential.json` | a `github` record: `cred_key` (CK), `body_json`, sealed `record`. |
| `request.json` | user and device keys; two requests (`claude.login` and its follow-up with `follows`) and two responses (`seq` 1 and 2, the last `final:true`), each with `ephemeral_sk`, `body_json`, `envelope`. |
| `invite.json` | `code`, `text`, `parse` cases (case and separators), `invite_id`, `package_key`, `pk_inv`, `package` with non-ASCII names, `package_body_json`, `package_aad`, `nonce`, `sealed`. |
| `recovery.json` | `code`, `text`, `recovery_id`, `kek`, recovery `proof` (`identity_sig_pk`, `user_id`, `challenge`, `proof`, signed `text`). |
| `enroll.json` | invitation code and `pk_inv`, enrollment `text`, signed `body` with `enroll_sig` and `inv_sig`. |
| `dedupe.json` | workgroup key, `dedupe_key`, and per operation type `params` → normalised `fields` → `tag`; one idempotency-key tag. |
| `agent.json` | delegated agent access (11-agent-access.md section 3); `own_identity` (ADR-082): the delegation certificate (`cert.text` = `P‖"agent-cert\0"‖canonical({agent, gen, pub{kem,sig}, user, v: 1})`, `cert.cert` = the user's signature) and the user's anchor `user_anchor` (`info` = `P‖"agent-user\0"‖canonical({agent, user})`, inner AES-GCM under the bundle key of `r` with AAD = `info`, `plaintext` = `canonical({cert, kem, sig})`, `sealed`); negative ops `verify_cert` (`signer`, `cert`, `agent_id`, `pub`, `user_id`, `gen`) and `open_user` (`agent_kem_sk`, `secret`, `agent_id`, `user_id`, `sealed`). |

## Log vectors (CRY-02)

Generated by `agent_hub.log.vectors` (section discovery, see above). They cover `04-crypto.md` sections 4.3, 4.7,
4.8, 5.10, 5.11 and 6.1-6.4. Implementations: `agent_hub.log` (reference), `agent_runner.crypto` (copy, CRY-03),
`web/src/core/log` (WCR-02).

### Structure

- Every log file carries the signed logs it uses under `logs` (`name -> [entry, ...]`). Arguments refer to a log
  prefix as `"log": {"name", "count"}` (the first `count` entries); `entries` (optional) are appended to that prefix
  for `verify_log`, `append` is the batch of a hub append. The platform state for `device_assign` with
  `platform_seq` is `"platform": {"name", "count", "root"}` (the verified platform log of that prefix).
- `identities` give `id`, `kem_sk`, `sig_sk`, `kem`, `sig` of every actor (`alice` founder, `bob`, `carol`,
  `dave`, `erin`, the org devices `device`/`dev2`, the platform devices `pdev`/`qdev`, the platform `root`, a
  malicious `hub` and the agents `bot`…`bot6`, ADR-082); invitation codes are `fixed("log/code/<label>", 16)`,
  scope keys `fixed("log/key/<scope>/<scope_id>/<epoch>")`.
- Times are arguments (`now`, `rotated_at` in header time format); nothing reads a clock.

| File | Positive part |
|---|---|
| `log.json` | `logs.main`: a valid chain with every entry type (27 entries, the comments of `main_builder` give each `seq`); `valid.heads` and `valid.projections` = the state after every entry (`OrgState.projection()`); `extends` (L9 cases that pass), `lh.needs_refresh` (L10) and `excluded`. `logs.agents` (ADR-082, the comments of `agents_builder` give each `seq`): `genesis` and `accept` through an agent, `agent_add` by the user and by the agent itself, an agent with its user's admin rights, the replacement by a higher generation, the removal with the user and the return with the rejoining user, an agent that leaves; `agents_valid.heads`/`projections` (members of kind `agent` carry `parent`, `gen`, `removed_with_user`; users that joined through an agent carry `join_agent`). |
| `platform-log.json` | `logs.platform`: every platform entry type incl. a decertification; `valid.heads`, `valid.projections`, `valid.platform_access` (ADR-026). |
| `log-rotation.json` | `cases`: `R(before, after, subject)` for every entry of the main log and the edge cases (device without workgroup, last workgroup of a device, archived workgroup, admin without workgroup access, leave, rejoin); `before` = state of the prefix, `after` = state after `entry`. `batches`: hub batches that pass with their `required`, `pending` and `head`. Every case also gives `losers` (the subject plus the active agent of a user subject, or the agent an `agent_add` replaces), `lost` (the union of R over `losers`) and `self_owed`; the agent cases and batches (`agent_*`, `user_*`, `replacement_*`) run on `logs.agents`. |
| `log-wraps.json` | `valid`/`cred_valid`: wraps W1/W2 accept (`cred_org_*`: the `cred` scope of the org itself, read by its workgroup devices only and wrapped by them or an active admin, KM-35); `catch_up`/`cred_catch_up`/`org_cred_catch_up`: `existing` slots and the expected `missing` plan per wrapper (`org_cred_catch_up`: owner = the org). `agents` (ADR-082): `valid`/`cred_valid` with agents, `cred_readers` (`logs`, `owner` → sorted `readers`) and `revoked_agents` (`logs` → sorted `revoked`) over one or two logs (`agents_other_org`: `bot6` of generation 2 supersedes `bot` everywhere), `catch_up`/`cred_catch_up` (every slot of a user is also a slot of its active agent, for the epochs since the
user's current membership: `rejoin_catch_up` on `logs.agent_rejoined`), `revocation` (`logs.agent_removed_by_admin`,
`agent_removed_by_owner`, `bob_org`: only a removal by its user or itself revokes an agent in every log) and `admin_catch_up` (the agent of an admin without explicit access gets every epoch, `logs.edge_agent`). |
| `log-events.json` | `matrix` (= `contracts/enums/event-kinds.json` without `description`, CON-03); `valid` (reader checks, with the `scope_key` to open the event), `hub_valid`, `replay`, `grace_seconds`. `agents.valid`/`agents.hub_valid`: events of agents against `logs.agents`. |

### Operations named by the log vectors

| `op` | `args` | Python reference |
|---|---|---|
| `verify_log` | `log`, `entries`?, `founder`, `platform`? | `verify.verify_log` (L1-L8) |
| `append_log` | `log`, `founder`, `platform`?, `append`, `now`?, `platform_access`? (default false) | `verify.append_entries` on the verified prefix (hub mode with `now`) |
| `check_extends` | `head {seq, hash}`, `log` | `entries.check_extends` (L9) |
| `require_lh` | `log`, `founder`, `platform`?, `lh` | `verify.require_lh` (L10) |
| `verify_platform_log` | `log`, `entries`?, `root` | `platform.verify_platform_log` (P1-P4 + type rules) |
| `append_platform_log` | `log`, `root`, `append` | `platform.append_platform` |
| `check_batch` | `log`, `founder`, `platform`?, `append`, `wraps`, `wrapped_by`, `now`, `platform_access`?, `excluded`? | `batch.check_batch` |
| `check_wrap` | `log`, `founder`, `platform`?, `wrap`, `wrapped_by`, `excluded`?, `require_active`? (default false) | `wraps.check_wrap` (W3) |
| `check_cred_wrap` | as `check_wrap` plus `current_epoch`; `logs`? (`[{log, founder}]`, else the one log) | `wraps.check_cred_wrap` |
| `cred_readers` | `logs`, `owner` | `wraps.cred_readers` over the states of the logs (result sorted) |
| `revoked_agents` | `logs` | `wraps.revoked_agents` over the states of the logs (result sorted) |
| `check_event` | `log`, `founder`, `platform`?, `event`, `session_device`?, `excluded`? | `events.check_event` (reader) |
| `check_event_hub` | as `check_event` plus `now`, `rotated_at`? | `events.check_event_hub` |
| `check_replay` | `stored`, `incoming` | `events.check_replay` (E9) |

`excluded` lists the platform devices of the org whose certification has ended (`PlatformState.excluded(state)`:
principals whose `platform_seq` names a certification that a later `device_decertify` ended – also after the device
was certified again with new keys). `log.json` → `excluded` gives the expected value for two platform logs
(`platform_recertified` = `platform` + decertification of `pdev` + a new certification with the keys `pdev-new`).

### Check order of the log rules

- **Entry format** (`entries.check_log_entry` / `check_platform_entry`): entry object with an object `body` whose
  `type` is a string (`header_invalid`); known type (`log_type`); entry key set `{body, sig}`, plus `inv_sig`
  exactly on `accept` (`header_invalid`); exact body key set of the type (`contracts/common.json`
  `log_body_*`/`platform_body_*`, `device_assign.platform_seq` optional) (`header_invalid`); canonical JSON
  (`non_ascii`); field formats (`header_invalid`). A malformed or wrong entry signature is a rule failure.
- **Membership log** per entry, against the state *before* it: L1 `seq` (`log_chain`; first entry of a hub append
  `seq_conflict`) and unchanged `org` (`log_chain`); L2 `prev` (`log_chain`); L3 `genesis` exactly at `seq` 0, `subject
  == by`, `pub.sig` = pinned founder, valid `sig` (`log_genesis`); `accept`: invitation present
  (`invite_not_in_log`), not revoked (`invite_revoked`), L7 not accepted (`invite_consumed`), not expired at hub
  receipt (`invite_expired`, append mode only), then `by == subject`, kind as invited, subject not an active member
  (a former member of the same kind may rejoin; a user only with its earlier identity keys), `sig` under `pub.sig`,
  `inv_sig` under `pk_inv` (`log_accept`); with the member `agent` `{id, pub, gen, cert}` (ADR-082) `sig` is checked
  under `agent.pub.sig` instead of the subject's key (`log_genesis`/`log_accept`), then the agent rule below;
  `agent_add`: L4 under `pub.sig` of the body when `by == subject`, else under the key of `by` with L8, then the
  agent rule;
  other types: L4 `by` known and `sig` valid (`log_signature`), L8 `by` active (`log_rights`), admin-only types
  (`log_rights`), L5 type rule (`log_rights`, `last_admin`, `invite_*` for `invite_revoke`), L6 `rotate.epoch =
  current + 1`, `wg_create.epoch = 1` (`log_epoch`).
- **Type rules (L5)**: `invite` – new `invite_id`, workgroups existing and not archived, a user invitation never
  names the default workgroup; `role` – active user, role changes, one admin remains; `wg_create` – new ID (not the
  org ID); `wg_archive` – existing, not archived, not default; `grant` – active user, workgroup not archived and not
  default, access changes; `ungrant` – an explicit grant of an active user; `device_assign` – workgroup not
  archived, not yet assigned; without `platform_seq` an active org device, with `platform_seq` the platform entry at
  that `seq` is a `device_certify` of this device (an active platform device keeps its `platform_seq`), and in hub
  append mode the org has platform access (`platform_access`, fail-closed) and that certification is still valid; `device_unassign` – assigned; `remove` – active subject, by an admin
  or by the subject itself (leave), one admin remains; an agent subject also by its user or the user's agent, a user
  subject also by its own agent (leave); a removed user's active agent is removed at the same `seq`
  (`removed_with_user`); `rotate` – org: admin; wg: admin or write access (also on an
  archived workgroup). Rights of an agent author are its user's (`OrgState.acting`); agents are never subjects of
  `grant`, `ungrant`, `role`, `admit`. **Agent rule** (`log_agent`): `by` is the user or the agent (`agent_add`);
  the user is an active user member; the agent ID is no principal, or an agent of the same user removed with its user
  with the same keys and `gen`; `gen` is greater than every other agent generation of the user in the log; `cert`
  valid under the user's key. An active agent of the user is replaced (removed at the same `seq`). The R of an agent
  removed by its user (or the user's agent) or replaced by `agent_add` is owed by the user (`agent_owed` in the
  projection): the user or its active agent may `rotate` those scopes in addition to admins and writers; a `rotate`
  clears the scope.
- **Platform log**: P1/P2 as L1/L2; P3 `root` exactly at `seq` 0 (`log_genesis`) with the pinned key
  (`platform_root_mismatch`); P4 `sig` under the pinned root (`log_signature`); invitation IDs unique
  (`log_rights`); `device_certify`: an open `device_invite` (`invite_not_in_log`; used: `invite_consumed`), valid
  `enroll_sig`/`inv_sig` over the enrollment text rebuilt as `{device_id: device, kind: "platform", invite_id, pub,
  agent_version, os, arch, capacity}` (`log_accept`), device not currently certified (`log_rights`);
  `device_decertify` of a certified device; `role_grant`/`role_revoke` change the role (`log_rights`).
- **Batch** (`check_batch`): entries (as above, hub mode), then R = union over the entries of `R(before, after,
  p)` for every `p` of `rotation.losers` except a self-`remove` or self-demotion, also of a user through its agent
  (`rotation.self_owed`; its R becomes `pending`); with R non-empty the `rotate` entries are exactly R and
  all after the last entry contributing to R (`rotation_required`); every wrap W3 (`header_invalid`,
  `signature_invalid`, `wrap_unauthorized`; the wrapper must be active after the batch); every epoch created by `genesis`/`wg_create`/`rotate` and current at the
  end has a wrap for every entitled recipient (`rotation_required`).
- **Wraps** (`check_wrap`): key set `{scope, scope_id, epoch, recipient_id, wrap, sig}` and integer epoch
  (`header_invalid`); wrapper known – its key from an org in which it is active, else from any org that knows it;
  with `require_active` (hub) only active wrappers (`wrap_unauthorized`); wrapper signature (`signature_invalid`); scope `org`/`wg`
  of the log, `1 <= epoch <= current`, recipient entitled now, and recipient = wrapper (W1) or wrapper a holder of
  the epoch (creator, reader while it was current, or entitled now) (W2) – else `wrap_unauthorized`. `cred`: the
  readers are the owner, its active agents not in `revoked_agents` (superseded by a higher generation, or removed by
  the user, an agent of the user or itself – `removed_by_owner` – in any of the logs; a removal by an admin only ends
  the agent's membership in that org), and devices assigned to a workgroup in which the owner has access in
  an org the owner joined by its own entry, not signed by a revoked agent.
- **Events**: see the module docstring of `agent_hub.log.events`: E1/E2 format and `org`, L10 `lh` (`log_fork`),
  author active at `lh` and not excluded (`author_not_writer`), E3 (`kind_not_allowed`), kind header rules
  (`header_invalid`), E8 (`size_exceeded`), E4 (`signature_invalid`), E5 at `lh` (`author_not_writer`; never into an
  archived workgroup), E7 if the
  reader knows the session's device; hub additionally E5 at receipt, E6 (`epoch_stale`, previous epoch within 600 s
  after `rotated_at` if the author can read the scope now and is not excluded), E7 (`author_not_writer`).
  An agent author (ADR-082) counts as a user for E3, has its user's rights for E5, and its `member.profile` `ref` is
  its user.

### Public functions (Python reference, `agent_hub.log`)

| Module | Functions | Used by |
|---|---|---|
| `entries` | `check_log_entry`, `check_platform_entry`, `log_text`, `platform_text`, `entry_hash`, `sign_log_entry`, `sign_platform_entry`, `Head`, `check_extends` | all |
| `state` | `OrgState` (`member`, `acting`, `agent_of`, `is_admin`, `admins`, `wg_access`, `assigned`, `device_wgs`, `writable`, `can_write_wg`, `can_read`, `readers`, `scopes`, `epoch`, `readable_scopes`, `pending`, `projection`) | HUB-05 projections, WCR-02 |
| `verify` | `apply`, `verify_log`, `append_entries`, `VerifiedLog` (`state`, `head`, `state_at`, `epoch_holders`), `needs_refresh`, `require_lh` | HUB-05, CRY-03, WCR-02 |
| `rotation` | `required_set`, `entry_subject`, `losers`, `lost`, `self_owed`, `as_json` | HUB-05, WEB-06 |
| `batch` | `check_batch`, `BatchResult` | HUB-05 |
| `platform` | `apply_platform`, `verify_platform_log`, `append_platform`, `PlatformState` (`certification`, `valid`, `status`, `excluded`, `has_role`, `platform_access`, `head_value`, `projection`) | HUB-04, CRY-03, WCR-02 |
| `wraps` | `may_wrap`, `may_wrap_cred`, `cred_readers`, `revoked_agents`, `check_wrap`, `check_cred_wrap`, `WrapSlot` | HUB-05, clients |
| `catchup` | `missing_wraps`, `missing_cred_wraps`, `agent_slots` | HUB-05 (`GET /v1/orgs/{org}/catch-up`), clients |
| `events` | `KIND_RULES`, `matrix`, `kind_rule`, `check_header_rules`, `has_right`, `check_event`, `check_epoch`, `check_event_hub`, `check_replay`, `GRACE` | HUB-06, CRY-03, WCR-02 |
