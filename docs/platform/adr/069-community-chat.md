# ADR-069: Community – one group chat of all users, under a shared community key

Status: Accepted (2026-11-23)

## Context
- The owner asked for a "Community" area on the platform: one group chat in which every user of the platform can
  talk with every other user, next to the organizations (KOR-26).
- [KONZEPT §5](../KONZEPT.md#k-5-b2) and ADR-003: the hub must not see confidential content in plaintext; `04-crypto.md` §7 lists what it
  sees. Every key of the platform so far belongs to a scope with a signed membership (`org`, `wg`) or to one user
  (`cred`). There is no scope that contains *all* users, and no principal that could distribute a key to all of them:
  the platform root key is offline (ADR-023), the hub must not hold content keys.
- Community messages are by definition readable by every user of the platform. Every user is admitted by an
  invitation that the operator (platform invitations) or an org admin hands out; the operator can always create an
  account for themselves. Whatever the cryptography, the operator of the hub can read the community the honest way.
- The runner verifies the platform log against a closed list of entry fields; `role_grant.role` is `inviter` only
  (`agent_runner.crypto.log.entries`). A new role value in the production platform log would make every runner refuse
  the log.
- KOR-24 makes a user's AI agent a full client of the user's account (signed requests, the user's identity). KI
  sessions in the VMs use the MCP platform API (`03-api.md` §8).

## Decision
1. **What the community protects, said honestly.** Messages are end-to-end encrypted under a **community key**
   `CMK_e` that every user receives. This keeps the text and the sender's display name out of the hub database, its
   backups, logs and of anyone who reads them (the passive hub operator and database theft of `04-crypto.md` §9). It
   does **not** make community messages confidential, and it does not protect against an *active* hub: every user can
   read them; an active hub can obtain the key like any new user (it can register an account or inject a key
   request); and because the hub serves the authors' identity keys with each page, an active hub can also **forge a
   message under any user ID**. Signatures protect only against other users. The UI says before the first post that
   name and messages are visible to all users of agent.dfx.swiss and must not contain confidential information, and
   the security page names the difference to tasks, whiteboards and credentials. Deleted messages stay readable in
   hub backups taken before the deletion to anyone who holds the key (the key does not rotate).
2. **Formats** (`14-community.md` §3): the community message is a signed AEAD record of the same shape as events and
   credential records (header `{v,alg,id,author,epoch,ts,salt,nonce}`, `k = HKDF(CMK_e, salt, P‖"community")`,
   AAD = canonical(header), signature label `community-sig\0` by the author's identity). Its body carries the
   author's display name and the text; readers enforce the body rules (no control, bidi or zero-width characters in
   names, NFC). Wraps of `CMK_e` use the existing wrap format with the new scope value `community` and the fixed
   scope ID `00000000-0000-0000-0000-000000000000`. Each epoch carries a **key check** `HMAC(CMK_e,
   P‖"community-check\0"‖canonical({"epoch":e}))` set by its creator, so a recipient detects a wrong key.
3. **Distribution: anonymous key requests.** A client without the key makes one open **request** with a fresh
   X25519 key (`PUT /v1/community/request`, signed by the user's identity; the hub checks the signature). Every
   client that holds the key serves open requests in the background: it sees only the request ID and the request's
   one-off public key – never the user ID – seals `CMK_e` to it (`recipient` = request ID) and signs. The requester
   opens the wrap with its one-off key, checks it against the key check, and stores a **self-wrap** to its identity
   (`POST /v1/community/wraps`); the request is then removed. A wrong key is rejected (`POST
   /v1/community/request/reject`): the hub drops that wrap, records the wrapper and never again accepts a wrap of that
   wrapper for that user (a rejection only affects the rejecting user, so rejecting cannot bar a holder for others;
   moderators stop a wrapper of wrong keys by muting it, and a muted user does not wrap). New requests are limited per
   user (3, then one every 20 s). The very first client finds no epoch
   and creates `CMK_1` with its key check and self-wrap (`POST /v1/community/epochs`, serialised: one epoch 1).
   Existing accounts need nothing: their client requests the key on the next start, and it arrives as soon as any
   holder is online. The hub hands each request to one holder at a time (lease of 30 s) and announces new requests
   at most every 5 s (repeated when a waiting requester reads its state after that). Invitation packages are **not**
   extended (no change of the strict package format that runners and old web clients parse); a user waiting for the key sees "Community is being unlocked …".
4. **Hub checks** (zero knowledge of content): the hub verifies the header, the size, that `author` is the signed-in
   user, that the user holds the current epoch, the author's signature (it knows the identity public keys), that the
   author is not muted and the per-user rate limit (which also counts refused attempts); for requests and wraps the
   identity signatures. The hub sees only the metadata of `14-community.md` §2.3 – never names or text.
5. **Moderation role at the hub, granted by the operator.** Moderators are kept in the hub table
   `community_moderator`, granted and revoked with the operator CLI (`agent-hub community moderator grant|revoke|list`,
   root-signed requests like `agent-hub platform`). They are **not** a platform-log role: moderation is enforced only by
   the hub (it removes ciphertexts and refuses posts; the hub can always withhold data, which is outside the
   zero-knowledge promise), and a new `role_grant` value would break the runners' platform-log verification, which
   accepts only `inviter`. Moderators remove any message and mute or unmute users; a moderator cannot be muted.
   Authors delete their own messages. A deletion erases header, ciphertext and signature at the hub and keeps a
   tombstone (ID, author, time, `author` or `moderator`); the ID can never be posted again. Every post, deletion,
   mute, unmute, key creation, key request, wrap, rejection and moderator change is audited (actor, time, target ID;
   never text or names). The hub marks moderators in the pages it serves, so clients can show a badge.
6. **Visibility of users.** A user appears to others only through messages they wrote, with the display name they
   chose for the community and a short mark derived from their user ID (so two people with the same name stay
   distinguishable). There is no list of accounts, no org names, no member count, and key requests carry no user ID.
7. **Limits.** Text ≤ 2000 characters, display name ≤ 64 characters, ciphertext ≤ 16 KiB; per user a token bucket of
   5 posts, refilled with one post every 6 s (`429 rate_limited` with `Retry-After`, a friendly message in the UI).
8. **Live updates.** A new audience `all` (every open browser stream, whatever org it follows) carries the frame
   `community {change, seq}`; `delete` names the `seq` of the removed message, so open clients drop it at once.
   Clients pull new messages. Old clients ignore the unknown frame; a new client against a hub without the community
   hides the area.
9. **AI.** Agents of KOR-24 act for their user with the user's identity and use the same HTTP endpoints (every step
   works without a browser; the display name comes from the user's newest own message, else the member profile).
   They were coupled explicitly by the user and act on the user's instructions. **KI sessions do not read or write
   the community** in this version: no MCP tool, no harness text. A session acts autonomously on untrusted content
   (repositories, web pages); letting it publish to all users of the platform would need its own review, rate and
   abuse rules. A session that wants to post asks its user.
10. **Rotation** is prepared in the formats (epochs) but not offered in this version: removing a user from the
    platform does not exist yet, and the community is not confidential (decision 1).

## Consequences
- The hub database holds no community text or names; the zero-knowledge scans (E2E-09 canary, column map of ADR-046)
  extend to the new tables.
- A brand-new user can read and write only after a key holder was online once after their request. On a platform in
  use this is seconds; on an empty platform the first user creates the key.
- A muted user can create new accounts only through invitations; moderators mute those as well. Residual risk,
  recorded in the report: no automatic link between an account and the accounts it invited.
- A first epoch created with a broken key check would make every request fail; the creator gains nothing by it, and
  the operator can remove the epoch rows while no message exists (runbook in the report).
- Moderation trusts the hub, as every availability decision does.

## Alternatives considered
- **Plaintext at the hub** (simplest; a group chat is not confidential): rejected – it would be the first user content
  in the hub database, would need an exception in the zero-knowledge checks and in the security page, and the effort
  of the key is moderate.
- **Key in the invitation package**: faster for users invited into an org, but changes a strict signed format that
  runners and the web parse, and does not cover platform invitations (no package). Request/catch-up covers both.
- **Key derived from the platform root or wrapped by the operator**: the root key is offline; the hub must not hold
  content keys.
- **A `moderator` role in the platform log**: verifiable by clients, but breaks the runners' closed role list and
  gains nothing, because only the hub enforces moderation.
- **Holders wrap directly to the requester's identity**: simpler, but every holder would learn the user IDs and
  identity keys of all accounts that wait (at the rollout: of all accounts), which contradicts decision 6.
- **Recipient verification through membership logs** (wrap only for users whose identity a holder can verify): a holder
  sees only the logs of its own orgs, so users of other orgs could never be served; and an active hub can read anyway
  (decision 1).

## Deviation from DESIGN.md
None beyond ADR-001 and ADR-003.
