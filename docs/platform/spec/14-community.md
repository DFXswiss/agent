# 12 Community: one group chat of all users

Status: binding (KOR-26, ADR-069). Extends `02-data-model.md` (§2 here), `03-api.md` (§4, §5 here), `04-crypto.md`
(§3 here) and `06-web.md` (§6 here). Where this file and the others disagree on the community, this file wins.

## 1. Scope

- One room, **Community**, for every signed-in user of the platform, independent of the organization in view.
- Read and write for every user with an account; live delivery; sender name and exact time; history with loading of
  older messages; unread count in the navigation; Enter sends, Shift+Enter breaks the line; links clickable, no HTML,
  nothing loaded from elsewhere (the CSP of `03-api.md` §1 is unchanged); usable on a phone.
- Order: limits per user; authors delete their own messages; moderators remove any message and mute users.
- Not in this version: files and images, direct messages, several rooms, reactions, mentions with notifications,
  KI-session access (ADR-069 §9), key rotation (ADR-069 §10).

## 2. Data model (hub, migration `0018_community`)

### 2.1 Tables

`→ principal` names the meaning of a user column; like `audit`, the community tables carry **no** foreign keys (the
community is hub-wide and every org export carries it completely, `decisions/KOR-26.md` 5).

| Table | Columns | Notes |
|---|---|---|
| `community_epoch` | `epoch` int PK (≥ 1), `key_check` bytea(32), `created_by` uuid → principal, `created_at` | one row per community key epoch; v1 creates only epoch 1 |
| `community_wrap` | (`epoch` → community_epoch, `user_id` → principal) PK, `wrap` bytea, `sig` bytea(64), `created_at` | the user's self-wrap of `CMK_e` (signed by the user) |
| `community_request` | `user_id` uuid PK → principal, `id` uuid UNIQUE, `kem_pub` bytea(32), `sig` bytea(64), `created_at`, `offered_at` NULL, `epoch` NULL, `wrap` NULL, `wrapped_by` uuid NULL, `wrap_sig` bytea(64) NULL, `wrapped_at` NULL | one open key request per user with its one-off X25519 key; the wrap fields are set together by the first holder |
| `community_reject` | (`wrapper_id`, `user_id`) PK → principal, `rejected_at` | a user rejected a wrong key of that wrapper |
| `community_member` | `user_id` uuid PK → principal, `joined_at`, `last_read_seq` bigint ≥ 0 (default 0), `muted_at` NULL, `muted_by` uuid NULL | created by `join` |
| `community_moderator` | `user_id` uuid PK → principal, `granted_at` | maintained by the operator CLI only |
| `community_message` | `seq` bigint PK (gapless, ascending), `id` uuid UNIQUE, `author_id` uuid → principal, `epoch` int, `created_at`, `ct_bytes` int, `header` jsonb NULL, `ct` bytea NULL, `sig` bytea(64) NULL, `deleted_at` NULL, `deleted_as` text NULL (`author`\|`moderator`), `deleted_by` uuid NULL | `header`/`ct`/`sig` are NULL exactly when `deleted_at` is set (tombstone) |

Every table is in the backup (`agent_hub.backup.tables`) and in the column map of ADR-046.

### 2.2 Lifecycle

- `join` inserts the member row (idempotent). A request is replaced by a new one of the same user and removed when
  the user stores the self-wrap. A deleted message keeps its row as a tombstone; nothing is purged in v1.
- Rollback of the hub to a release before the community: the migration is forward-only like every hub migration;
  the runbook step is `DROP TABLE hub.community_message, hub.community_request, hub.community_reject,
  hub.community_wrap, hub.community_member, hub.community_moderator, hub.community_epoch; DELETE FROM
  hub.schema_version WHERE version = 18;` (as the owner role) after a backup.

### 2.3 What the hub sees (addition to `04-crypto.md` §7)

| Data | Purpose |
|---|---|
| community message IDs, author IDs, sequence numbers, receive times, sender times (`header.ts`), ciphertext sizes, epoch, tombstone facts | ordering, paging, deletion, limits, audit |
| key epochs and key checks, who holds a self-wrap, open key requests (user, request ID, one-off public key, lease and wrap times, wrapper), rejections | key distribution |
| members (user ID, join time, read position, whether they posted), mutes, moderators | unread count, order |

Not visible: message text, display names, the community key.

## 3. Cryptography

| Item | Definition |
|---|---|
| Community key `CMK_e` | 32 random bytes, epoch `e`; created by the first client (§4.2) |
| Key check | `key_check = HMAC-SHA256(CMK_e, P‖"community-check\0"‖canonical({"epoch":e}))` (32 B) |
| Wrap | `04-crypto.md` §4.2 with `scope = "community"`, `scope_id = "00000000-0000-0000-0000-000000000000"`; `recipient` = the user ID (self-wrap) or the request ID (request wrap); signed by the wrapper's identity over `P‖"wrap\0"‖canonical({epoch,recipient,scope,scope_id,wrap})` |
| Key request | `{id, kem_pub}` with a fresh X25519 key; `sig = Ed25519_user(P‖"community-request\0"‖canonical({"id":id,"kem_pub":kem_pub,"user":user}))` |
| Message key | `k_msg = HKDF(ikm=CMK_e, salt=header.salt (32 B), info=P‖"community")` |
| Message | `{"header":{"v":1,"alg":"A256GCM-HKDF","id":uuid,"author":user,"epoch":e,"ts":time,"salt":b64u32,"nonce":b64u12},"ct":b64u(AES-GCM(k_msg, body, AAD=canonical(header))),"sig":b64u(Ed25519_author(P‖"community-sig\0"‖canonical(header)‖ct_raw))}`; exact key sets; `ct` ≤ 16 KiB decoded |
| Body (body JSON, `04-crypto.md` §1) | `{"name":…,"text":…,"v":1}`, exact key set. `name`: 1–64 code points, NFC, no surrounding white space, no characters of the Unicode categories Cc, Cf, Zl, Zp. `text`: 1–2000 code points, no surrounding white space, no Cc except line feed and tab, none of U+202A–U+202E, U+2066–U+2069, U+200B–U+200F, U+2028, U+2029, U+FEFF |

Rules: C1 a user may store a self-wrap only for itself, when creating the epoch or after opening a request wrap; C2
a holder of the current epoch may wrap it for any open, not yet wrapped request whose user has not rejected this
wrapper, unless the wrapper is muted; the hub checks C1, C2 and every identity signature. The requester
checks the wrapper's signature (key served with the wrap), opens the wrap and compares the key check; on any failure
it rejects. Readers verify every message signature under the author's identity key (served with the page and pinned
by the reader on first sight: a changed key is shown as "could not be verified") and check the body rules; a message
that fails any check shows "Message could not be verified", never its content. Vectors:
`contracts/community/vectors.json`.

## 4. API (signed requests of a browser session or, with KOR-24, of the user's agent; right `user` unless stated)

### 4.1 Endpoints

| Method and path | Right | Request → response |
|---|---|---|
| `GET /v1/community` | `user` | → `{epoch, wrap, request, member, moderator, unread, head_seq, limits}`: `epoch` `{epoch, key_check}` of the current epoch or `null`; `wrap` my self-wrap `{epoch, wrap, sig}` or `null`; `request` my open request `{id, wrapped}` with `wrapped` = `{epoch, wrap, sig, wrapped_by, wrapper_sig}` or `null`, or `null` without request; `member` `{joined, muted, posted, last_read_seq}`; `unread` messages of others after `last_read_seq` that are not deleted; `head_seq` the newest `seq` or 0; `limits {text_chars, name_chars, burst, refill_seconds}` |
| `POST /v1/community/join` | `user` | `{}` → `{joined:true}`; idempotent |
| `POST /v1/community/epochs` | member | `{epoch:1, key_check, wrap, sig}` (self-wrap) → `{epoch}`; only while no epoch exists (`409 community_epoch_exists`) |
| `PUT /v1/community/request` | member without self-wrap of the current epoch | `{id, kem_pub, sig}` → `{id}`; replaces the user's open request; same body again → same answer; per user 3 requests, then one every 20 s (`429 rate_limited`, `Retry-After`) |
| `GET /v1/community/requests` | holder of the current epoch | `?limit=` (≤ 100, default 20) → `{requests:[{id, kem_pub}]}`: open, unwrapped requests not leased in the last 30 s, never the caller's own or of users who rejected the caller; each returned request is leased to the caller for 30 s |
| `POST /v1/community/requests/wraps` | holder | `{wraps:[{request, epoch, wrap, sig}]}` (≤ 100) → `{stored, skipped}`; a request that is gone, already wrapped or rejected the caller is skipped; publishes `community {change:"key"}` to each requester |
| `POST /v1/community/request/reject` | `user` with a wrapped request | `{}` → `{rejected:true}`; clears the wrap, records the wrapper |
| `POST /v1/community/wraps` | member | `{epoch, wrap, sig}` (self-wrap of the current epoch, `recipient` = caller) → `{epoch}`; only after a holder wrapped the epoch for the caller's request (rule C1, else `403 forbidden`); removes the caller's request; idempotent once stored |
| `GET /v1/community/messages` | `user` | `?before=<seq>` (newest first, page) or `?after=<seq>` (ascending, catch-up), `limit` ≤ 100 (default 50) → `{messages:[row], authors:{user:{sig_pub, moderator}}, next_before}`; `row = {seq, id, author, created_at, deleted: null \| "author" \| "moderator", message: {header,ct,sig} \| null}` |
| `POST /v1/community/messages` | holder, member, not muted | `{message}` → `{seq, replayed?}`; idempotent by `header.id` (same bytes → `replayed:true`; other bytes or a deleted ID → `409 id_conflict`) |
| `DELETE /v1/community/messages/{id}` | author, or `community:moderator` | → `{deleted:"author"\|"moderator"}`; an already deleted message answers the same (idempotent) |
| `PUT /v1/community/read` | member | `{seq}` → `{last_read_seq}`; the position never moves back nor beyond the newest message |
| `GET /v1/community/mutes` | `community:moderator` | → `{mutes:[{user, muted_at}]}` |
| `PUT /v1/community/mutes/{user}` | `community:moderator` | `{}` → `{muted:true}`; the user must be a member and no moderator |
| `DELETE /v1/community/mutes/{user}` | `community:moderator` | → `{muted:false}` |
| `GET /v1/community/moderators` | `root` | → `{moderators:[{user, granted_at}]}` |
| `POST /v1/community/moderators` | `root` | `{user, granted: bool}` → `{user, moderator}`; the user must exist |

Error codes (new, `03-api.md` §11): `community_muted` (403), `community_epoch_exists` (409), `community_no_key` (409:
no epoch, or the caller holds no self-wrap of it), `community_not_member` (409). Existing: `rate_limited` (429,
`Retry-After`), `size_exceeded` (413), `header_invalid` (400), `signature_invalid` (401), `epoch_stale` (400),
`id_conflict` (409), `request_invalid`, `forbidden`, `not_found`.

### 4.2 Flows

1. **Start of a client** (browser after unlock, or an agent): `POST /v1/community/join`, `GET /v1/community`.
   No epoch → draw `CMK_1`, compute the key check, self-wrap, `POST /v1/community/epochs` (on
   `community_epoch_exists` read again). A self-wrap → unwrap, compare with the key check, keep `CMK_e` in memory.
   Neither → draw a one-off X25519 key (memory only), `PUT /v1/community/request`, wait for the frame `key` (or read
   again every minute); a wrapped request → check the wrapper signature, open, compare with the key check, store the
   self-wrap (`POST /v1/community/wraps`), else `POST /v1/community/request/reject`. A client that restarted without
   its one-off key makes a new request.
2. **Serve requests** (holders): `GET /v1/community/requests`, seal `CMK_e` to each `kem_pub` with `recipient` = the
   request ID, sign, `POST /v1/community/requests/wraps`; an entry that cannot be sealed is skipped. Triggers: start,
   the frame `community {change:"request"}` after a random delay of 0–3 s, every 10 minutes.
3. **Read**: `GET /v1/community/messages?before=` page by page; verify and decrypt; on `community {change:"message"}`
   pull `?after=<newest seq>`; on `community {change:"delete", seq}` drop the plaintext of that `seq` at once.
4. **Post**: build the body, seal under `CMK_e`, `POST /v1/community/messages`; on `429` show the wait time.
5. **Unread**: `unread` from `GET /v1/community`; while the community screen is visible the client sets the read
   position to the newest `seq` it shows.

### 4.3 Hub checks for `POST /v1/community/messages` (in this order)

Body schema (`request_invalid`) → member (`community_not_member`) → not muted (`community_muted`) → rate limit
(`rate_limited`) → holder of the current epoch (`community_no_key`) → message key sets and formats
(`header_invalid`), `header.author` = caller (`forbidden`), `ct` size (`size_exceeded`), epoch = current
(`epoch_stale`) → signature under the caller's identity (`signature_invalid`) → store (idempotency by `id`). Every
attempt after the mute check that is not an identical repeat takes a token of the rate limit, refused ones included.

### 4.4 Rights

New right `community:moderator` (`03-api.md` §3): the caller is in `community_moderator`.

## 5. Live channel

New audience `all`: every open `GET /v1/stream` of every user, whichever `org` it follows. Frame (`03-api.md` §5):

| Frame | Data |
|---|---|
| `community` | `{change, seq}`: `message` (`seq` of the new message, to all), `delete` (`seq` of the removed message, to all), `request` (newest `seq`, to all; at most one per 5 s per hub process; repeated when a requester with an open, unwrapped and unleased request reads `GET /v1/community` after that window, so a request made inside the window waits at most for the requester's next one-minute read), `key` (to the requester), `mute` (to the muted or unmuted user) |

## 6. Web (`06-web.md` additions)

- Route `/community` (no org in the path), navigation entry "Community" for every signed-in user, with a badge of
  unread messages (shown as "99+" above 99); on the phone in "More" and as a dot on the "More" button. A hub without
  the community (`404` on `GET /v1/community`) hides the entry and stops the job.
- Screen: messages oldest at the top, newest at the bottom, grouped by day; each message shows the display name, the
  sender mark (`#` and four characters of base32 of SHA-256 of the user ID), a "Moderator" badge for moderators, the
  hub's receive time (`HH:MM`; date and time with seconds as tooltip and in `<time dateTime>`), the text with line
  breaks, long words broken, `http(s)` links as links (`target=_blank`, `rel="noopener noreferrer nofollow"`), no
  HTML; own messages aligned right. "Load older messages" at the top. A message that fails a check shows "Message
  could not be verified". Deleted messages show "Message removed" (by the author or by moderation).
- Composer: textarea, Enter sends, Shift+Enter breaks the line, a send button (phones), character counter near the
  limit. Before the first own post the screen shows the display name (from the user's newest own message, else the
  `member.profile` in the active org; editable; remembered per user in the browser) and: "Your name is visible to
  all users of agent.dfx.swiss. Community messages are readable by every user – do not share confidential
  information here. agent.dfx.swiss never asks for codes or passkeys in the community."
- States: loading; waiting for the key ("Community is being unlocked …", after two minutes "… as soon as another user
  is online"); empty ("No messages yet – write the first one"); muted ("You have been muted in the community");
  errors through the error catalog; offline like the rest.
- Own message menu: "Delete" (confirm). Moderator menu: "Remove" and "Mute author"; a moderator sees the list of muted
  users (with the last known name and mark, else the mark) and can unmute.
- Background job `community`: join, bootstrap, request and open the key, serve requests (§4.2), unread count.
- Settings section "Account" shows the user ID with a copy button (needed by the operator to grant the moderator role).

## 7. Operator CLI

`agent-hub community moderator grant --user ID`, `… revoke --user ID`, `… list`, all with `--key-file F --hub URL`
like `agent-hub platform` (root-signed requests, pinned hub key). Output: one line per moderator / the result.

## 8. Tests

Hub: every endpoint, check and error of §4 with real PostgreSQL, including a wrong-key rejection, a wrap of a
non-holder, a wrap for a rejecting user, a request that names another user, `author` ≠ caller, re-posting a deleted
ID, a muted user repeating an earlier post, two concurrent epoch creations, the lease and the frame coalescing, `key`
and `mute` frames reaching only the affected user, the audit rows (no names), the backup tables, the column map
(ADR-046), the org wrap endpoint refusing the `community` scope, and a zero-knowledge test that writes messages with
canary text and names and scans every table of the hub database and the published frames for them. Contracts:
schemas, fixtures, vectors in Python and TypeScript. Web: Vitest for the core (formats, store, job) at 100 %;
Playwright on desktop and phone: two users of different orgs chat live; history after a new login; a freshly invited
user reads and writes; moderation removes and mutes; the rate limit message; deletion reaching an open client.
