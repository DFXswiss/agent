# KOR-26 – decisions (Community)

Architecture: ADR-061, specification `spec/12-community.md`. Small decisions of the implementation:

1. **Key distribution with anonymous requests** instead of wraps to identities (design review finding): holders see
   only `{request_id, one-off kem_pub}`, never a user ID; the requester self-wraps after opening. No list of accounts
   reaches any user.
2. **Key check** (`HMAC(CMK_e, P‖"community-check\0"‖canonical({"epoch":e}))`) stored with the epoch; a wrong key is
   rejected, the wrapper recorded (`community_reject`) and never accepted again for that user. A rejection never bars
   a wrapper for other users (the hub cannot check the claim, so a few accounts could otherwise stop the distribution
   – review finding); a muted user does not wrap, so moderation stops a wrapper of wrong keys. New key requests are
   limited per user (3, then one every 20 s), because each one is audited and announced to all browsers.
3. **Moderator role at the hub** (`community_moderator`), operator CLI `agent-hub community moderator …` with
   root-signed requests; not a platform-log role because the runner's platform-log verifier accepts only `inviter`
   (a new role value in the production log would make every runner refuse the log).
4. **Moderators cannot be muted** (also not by other moderators); a moderator change is done by the operator.
5. **No foreign keys from the community tables to `principal`** (like `audit`): the community is hub-wide and every
   org export carries it completely (`agent_hub.backup.tables`), also with users the export of one org lacks.
6. **`seq` gapless under an advisory lock** (no sequence object): posts are rare, the lock keeps the order equal to
   the commit order and the backup needs no sequence handling.
7. **Rate limit**: 5 posts burst, one every 6 s, per user and hub process (like the other limits, one hub process
   per database); refused attempts after the mute check take a token too; identical repeats (retries) neither need
   nor take one.
8. **Deleted IDs can never be posted again** (`id_conflict`), so a replay cannot restore a removed message.
9. **Frame coalescing**: `community {change:"request"}` at most once per 5 s per hub process; requests are leased to
   one holder for 30 s, so a rollout with many waiting accounts does not make every holder wrap for everybody.
10. **Vectors** in `contracts/community/vectors.json`, not `contracts/vectors/`: the runner's CON-02 test demands a
    runner section for every file there, and runners never take part in the community.
11. **Migration number 0014 and ADR-061**: migrations and ADRs must both be numbered without a gap
    (`agent_hub.db.migrate`, `tests/platform/test_docs.py`); on `ad5f2026-live` migrations 0010–0013 and ADRs up to
    060 are taken (KOR-24, HOT-06, KOR-34, KOR-29, direct admission). Parallel packages that add migrations or ADRs later renumber their unreleased files when rebasing
    (ADR-027 §5).
12. **No change in the agent repository**: the runner code is unaffected; the contract files reach the agent copy through
    the usual contract sync of the integration merge.

## Independent review (before the merge)

Fixed: the duplicate `ceremony` line of the column map left by a rebase; rejections no longer bar a wrapper for others
(2 above); new key requests are rate limited and the re-announcement of a waiting request is bound to the global
5-second window (at most one `request` frame per 5 s per hub process, whoever triggers it); a conflicting post under a
used ID costs a token, and checking and taking a token is one step (concurrent posts cannot exceed the burst); real
concurrency tests for identical posts and for two epoch creations, a test for a muted user repeating an earlier post;
spec §2.1 states that the tables carry no foreign keys; `04-crypto.md` §9 names the community's residual risks.

Not changed, with reasons:
- **Number collisions with unmerged branches** (migration 0014, ADR-061): migrations and ADRs must be numbered without
  a gap on the integration branch, so "staying above" every open branch is impossible; the package merged later
  renumbers its unreleased files (11 above).
- **Pinned author keys in `localStorage`**: they are public keys of authors the user has already seen in a chat
  everyone can read; a legitimately changed identity key (a new account) shows "could not be verified", which is the
  intended trust-on-first-use behaviour. A re-pin action can follow when identity rotation exists.
- **Community key after sign-out**: the instance is bound to the store generation and writes nothing after a sign-out;
  the key is a non-confidential group key that every user of the platform receives anyway.
- **Central lists** (`backup/tables.py`, `http/routing.py` rights, `stream/publish.py` frames, `nav.ts`): these
  registries have no discovery mechanism; every area (also KOR-24) adds its entry there. The navigation change is a
  small generic extension (`hidden`, `badge`).
- **Read-log rows per live pull**: every authenticated read is logged by design (03-api.md); the volume is one row per
  online client and message, like the task chat.
