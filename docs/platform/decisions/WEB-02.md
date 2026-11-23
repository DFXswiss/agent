# WEB-02 – decisions

Web core: signed fetch, response check, SSE client with cursors and gap repair, store with slices, IndexedDB
persistence, background-job runner with log and wrap verification (06-web.md §1, §2.2, §2.3, §2.6, §11, §12.5;
03-api.md §1, §5; 04-crypto.md §5.12, §6.1, §6.3; ADR-019; ADR-027 §4).

1. **Response signatures are a MUST.** `core/net/signedFetch.ts` accepts a 2xx answer only with a valid
   `agent-response-v1` signature under `hub_key` of `/v1/config` whose `Agent-Hub-Time` lies within 120 s of the
   browser clock (the request window of the hub; otherwise `time_skew`). A missing, partial or wrong signature is
   `signature_invalid`. Unsigned answers are accepted as errors (never as data) only for the statuses the hub sends
   before its signature gate (`400`, `401`, `413`, `429`) and for `5xx` of anything in front of the hub; an unsigned
   `403`/`404`/`409` is forged (it would drop a cursor or stop the channel) and is `signature_invalid`. Signed errors
   must verify like successes.
2. **Error mapping.** `HubError.code` is the body's `error` if it is a code of the error catalog
   (`i18n/errors.en.json`, generated from `contracts/enums/`), otherwise derived from the status (`401 unauthorized`,
   `403 forbidden`, `404 not_found`, `409 id_conflict`, `413 size_exceeded`, `429 rate_limited`, `5xx`/network
   `unavailable`, else `request_invalid`). An aborted request rethrows the abort.
3. **Host line.** The host line of `agent-request-v1` is `new URL(config.origin).host` (host plus non-default port),
   the same value the hub derives from `AGENT_HUB_PUBLIC_URL` (`PublicOrigin.host`).
4. **Signing fixture.** `web/tests/fixtures/net/signing.json` was produced with `agent_core.signing` (`request_text`,
   `response_text`, `HubSigner.from_seed_hex("hub-test", sha256("agent.dfx.swiss web-02 test hub key"))`):
   `python -c` over those three functions with the request/response values in the file. The TS tests compare the texts
   byte for byte, verify the Python Ed25519 signature and additionally use the request texts and P-256 session
   signatures of the legacy vectors `tests/vectors/passkey.json`. Check (Ed25519 is deterministic):

   ```python
   import hashlib, json
   from agent_core.signing import HubSigner, response_text, sha256_hex
   f = json.load(open("web/tests/fixtures/net/signing.json"))
   s = HubSigner.from_seed_hex("hub-test", hashlib.sha256(b"agent.dfx.swiss web-02 test hub key").hexdigest())
   r, q = f["response"], f["request"]
   t = response_text(q["key_id"], q["nonce"], q["agent_time"], r["status"], r["hub_time"], sha256_hex(r["body"].encode()))
   assert t.decode() == r["text"] and s.sign(t) == r["signature"]
   ```
5. **Session notices.** `expiring` before every request when less than 5 min remain, `expired` on `session_expired`;
   kept in the `connection` slice (`session`), never downgraded from `expired` to `expiring`; `sessionRenewed` resets.
6. **SSE channel.** Fatal codes stop the channel (`session_expired`, `unauthorized`, `key_unknown`, `key_revoked`,
   `forbidden`, `not_found`); everything else reconnects with backoff `min(30 s, 1 s·2^(n-1))·(0.8…1.2)` clamped to
   1–30 s. Heartbeat timeout 45 s (three missed 15 s comments). Frames are handled strictly in order; a pull failure
   reconnects. `stop()` also leaves the offline state, so `online()` does not restart a stopped channel.
7. **Cursors, gaps and backfill.** The cursor is the last *contiguous* `scope_seq`; frames beyond a gap are delivered
   at once (also watched `session.terminal` chunks, which a pull never returns) and remembered as "ahead" until the
   gap is repaired. A gap is repaired by one pull per scope at most 1 s after the frame that revealed it: unwatched
   terminal chunks leave gaps in the event seqs, and a pull per frame would exhaust the 600/min limit while a session
   runs. After a complete pull the cursor is the hub's `head` (seqs the pull does not return are other sessions'
   terminal chunks); if frames arrived beyond a stale head, another repair is scheduled. Pages are filtered to
   `(after, head]` and sorted. On `hello` a scope without a cursor starts at the announced head (no history backfill:
   screens pull history with their own filters), a scope behind its head is pulled, and a head *below* the cursor
   (hub restored from a backup) resets the cursor to it. A gap frame of an unseen scope pulls from 0. A scope more
   than 5 000 events behind skips to `head − 5000`. `404` on a pull drops the scope. Cursors go to IndexedDB at most
   every 2 s; `connectLive(...).stop()` ends the channel, the save timer and the listener.
8. **Store.** One store of named slices (`core/store/store.ts`), immutable per slice, synchronous listeners. Slice
   files export `slice = {name, initial}` and type themselves into `StoreState` by module augmentation, so further
   slices are new files only. The slice registry now requires `initial`; the registration test and its fixtures were
   updated (they asserted the empty registry of WEB-01). Base slices: `config`, `me`, `orgs`, `connection`, `events`,
   plus `jobs` (data of the "Sync" indicator). `store.reset()` returns every slice to its initial state (sign-out).
9. **Events LRU.** Per scope a `Map` in LRU order (write or `touchEvents` makes an entry most recent), bounded to
   5 000. Entry states: `pending` (log not verified yet), `locked` (signature and right verified, epoch key not yet
   available – re-read when the wrap job adds keys), `verified` (decrypted body, memory only) and `unverifiable` (code).
   A decrypted body that is not JSON is `header_invalid` (there is no separate body code). The header must name the
   org of the channel and the scope the event was delivered under (`header_invalid`), and one event ID may occupy only
   one `scope_seq` per scope (`event_conflict`): the hub can neither move an event into another workgroup nor repeat
   it. Terminal-chunk retention for watched sessions only (06-web §2.3) belongs to the terminal view package.
10. **Founder pinning without a stored pin.** First verification of an org on a browser takes the founder key from
    `genesis` and requires that the verified log contains the user's unlocked identity (from the passkey-decrypted
    bundle, never from the hub) as a principal with the same signing key; otherwise `log_genesis`. The hub cannot
    forge the user's own signed `genesis`/`accept`, and L2 chains everything to it. With a stored pin, L3 uses it.
11. **Log job details.** Platform log first (P1–P4, L9-style extension of the stored platform head; a hub without any
    platform entries is accepted while nothing was stored). Then the org log: full verification from `seq` 0 on the
    first run of a page load, incremental (`extendLog`) afterwards. The served `head` must equal the verified head
    (`log_chain`); an incremental read without new entries whose head differs is `log_rollback`. Every `CryptoError`
    blocks the org with `{code, stored, served, lh}`; hub/network errors only fail the run. L10: the reader triggers
    the log job for `lh` beyond the verified head and blocks with `log_fork` only if that refresh **succeeded** and the
    head is still below `lh`; a failed refresh leaves the event `pending` (retried later, never a block). Platform
    refreshes are serialized per store (org queues run in parallel). The stored pin never goes back: a stored head at
    or below the new head must lie on it (`log_rollback`), a stored head beyond it (another tab) is kept.
12. **Wrap job details.** `GET /v1/wraps` (all wraps for the user), filtered to the org's `org` scope and its
    workgroups; `cred` wraps are left to WEB-10. `verifyWraps` (signature, W1–W2, decertified platform devices
    excluded), then `unwrapKey` with the identity; refusals are kept per slot; the counter `wrap_unauthorized` is the
    number of distinct slots refused by signature or W1–W2 (`decrypt_failed` is kept but not counted); a slot accepted
    later leaves the refused set.
13. **Jobs registry.** `job = {name, order, triggers, run}`; the runner lives in `jobs/index.ts` (every other file of
    the directory is a job). Queues are per org; a job already waiting for the same org is coalesced; blocked orgs run
    nothing. `trigger` resolves with whether every run completed. Jobs never await the `changed` listener (the
    reader's retry may trigger jobs of the same org, which wait for the running one – awaiting it deadlocks).
    Sign-out: `JobRunner.stop()`, and `store.reset()` raises `store.generation`; a job, a platform refresh or a reader
    pass that started under an older generation writes neither the store nor IndexedDB. Triggers: `login`, `log`,
    `lh`, `wraps`, `interval` (the last one for WEB-06's 10-min catch-up).
14. **Live wiring** (`core/net/live.ts`): `log` frames trigger the log job only if the announced head differs from the
    verified one; `wraps` frames trigger the wrap job; other frames go to an `onFrame` callback (slices of later
    packages).
15. **Persistence.** Database `agent-platform` v1 with stores `session` (key ID, non-extractable private key, public
    point, user ID, credential ID, expiry; only a non-extractable ECDSA P-256 private key with usage `sign` and a
    65-byte point are stored or read back), `heads` (pinned founder and head per org), `platform` (platform head),
    `cursors` (per org). An extractable session key is refused on save; malformed stored values read as absent.
    Sign-out deletes `session` and `cursors`; pins and heads stay (they protect against rollback across sign-ins).
    `fake-indexeddb` was already a dev dependency (WEB-01), no addition was needed.
16. **E2E.** E2E-14 and PERF-03 do not exist yet; the browser part of the channel is exercised by the screens
    (WEB-03 ff.) and those specs.
17. **Review (independent subagent) – handled.** Fixed: job self-wait deadlock, transient refresh error turning into
    `log_fork`, moved/repeated events, terminal-chunk loss and per-frame pulls, sign-out races (runner stop, store
    generation, live disposer), parallel platform refreshes, unsigned 403/404/409, pin regression across tabs,
    `stop()` while offline, cursor advanced before handling, unordered pages, restored hub, `decrypt_failed` in the
    counter, stored-key checks. Not changed: copying the per-scope map on each write and re-sorting in the selector
    (bounded to 5 000; to be measured by PERF-03 when it exists); a Python-side test pinning the signing fixture
    (would add a file outside this package's paths; the snippet in item 4 reproduces it); the fixture location
    `web/tests/fixtures/net/` (beside the other fixtures).
