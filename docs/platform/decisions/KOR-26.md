# KOR-26 – decisions (Community)

Architecture: ADR-069, specification `spec/14-community.md`. Small decisions of the implementation:

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
10. **Hub-only formats**: the community formats live in `agent_hub.community.crypto`, not in `agent_hub.crypto`, whose
    modules are copied into the runner (`copy_tool.py`); runners never take part in the community. Only the new wrap
    scope `community` reaches the runner copy of `wrap.py`.
    **Vectors** in `contracts/community/vectors.json`, not `contracts/vectors/`: the runner's CON-02 test demands a
    runner section for every file there, and runners never take part in the community.
11. **Migration number 0018, ADR-069 and specification 14**: migrations and ADRs must both be numbered without a gap
    (`agent_hub.db.migrate`, `tests/platform/test_docs.py`); on `develop` migrations up to 0017, ADRs up to 067
    and specification 12 (`12-billing.md`) are taken by the packages merged before this one.
12. **One repository**: the package was built while hub and runner lived in two repositories and moved into the
    unified `agent` repository (INF-06) as one commit; the runner only learns the four new hub error codes
    (`agent_runner/sync/codes.py`, its list must equal the contracts).

13. **Audit completeness (ADR-063)**: joining and the read position only mark the caller and are generic writes
    (`agent_hub.audit.generic_routes`, row `request.write`); every idempotent repeat that changes nothing (a replayed
    post, a repeated deletion, mute, unmute or moderator change, a repeated key request or self-wrap, a wrap batch
    whose entries were all skipped) is declared with `recorder.unchanged()`; everything else writes its `community.*`
    row.

14. **Neighbouring test made independent of the umask**: `tests/platform/atk/test_guest_probe.py` expected the mode
    `0o644 | <mode of the file>` and failed on the test devices once they ran with a stricter umask (also in INF-07);
    the test now sets the file to `0o644` itself.

15. **Community commands of the agent tool** (`agent_platform/community.py`, parity rows `community-*` done, owner
    rule of `agent-parity.md`): `community status|key|serve|messages|post|delete|mutes|mute|unmute`.
    - `status` only joins and reads the state; the key flow of `14-community.md` §4.2 runs in `key` (with `--wait`,
      a read every 5 s), `messages`, `post` and `serve`, because those need the key. Serving is a command, not a
      background job: the tool runs only when called (the guide asks to run `serve` now and then).
    - Every run is a new process, so the one-off X25519 key of the tool's open request is kept in `state.json`
      (`0600`, next to the identity bundle and `r`) until the wrap is opened and replaced by the self-wrap; it can only
      ever open the community key, which is not confidential (ADR-069 decision 1). The pinned author keys, the name of
      the last post and a post kept for a retry live there too (one state per paired user, like the browser's
      per-user preferences). A wrapped request of another client of the user gets 60 s (from the tool's first sight)
      to store its self-wrap before the tool replaces it, as the web client's one minute.
    - Names and texts are cleaned like the web client (`cleanName`, `cleanText`) and then checked; the default name is
      the last one posted, else the name of the user's newest own message on the newest page, else the member profile
      name (as ADR-069 decision 9 says). A post refused with `429` names `retry_after` (the hub's `Retry-After`, which
      `agent_platform.hub` now passes on for every refusal; 6 s when missing) and is sent byte for byte again with the
      same name and text, like the web client, so a retry cannot post twice.
    - The read position moves only with `messages --mark-read` (the browser moves it while the screen is visible).
    - The formats are a twin of `agent_hub.community.crypto` (`agent_platform/communityfmt.py`, the tool must not
      import the hub); a test compares the two files and runs `contracts/community/vectors.json` against the twin. The
      runner's copy of the reference crypto (`copy_tool.py --agent-core . --rev HEAD`) was refreshed: decision 10's
      wrap scope `community` had not reached `agent_runner/crypto/wrap.py`, so `test_copy.py` failed on this branch.
    - New error code `community_key_missing` (with the key state) for a post without the key.
    - Tests: the hub fake's community routes check signatures, wraps and messages with the hub's reference code;
      fake hooks may return answer headers (`Retry-After`); a real-hub test runs key creation, request, serving,
      opening, posting, reading, moderation and the post limit through `agent_hub`.

## Independent review (before the merge)

Fixed: the duplicate `ceremony` line of the column map left by a rebase; rejections no longer bar a wrapper for others
(2 above); new key requests are rate limited and the re-announcement of a waiting request is bound to the global
5-second window (at most one `request` frame per 5 s per hub process, whoever triggers it); a conflicting post under a
used ID costs a token, and checking and taking a token is one step (concurrent posts cannot exceed the burst); real
concurrency tests for identical posts and for two epoch creations, a test for a muted user repeating an earlier post;
spec §2.1 states that the tables carry no foreign keys; `04-crypto.md` §9 names the community's residual risks.

Not changed, with reasons:
- **Number collisions with unmerged branches** (migration 0014, ADR-069): migrations and ADRs must be numbered without
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
