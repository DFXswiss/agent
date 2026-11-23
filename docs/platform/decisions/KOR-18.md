# KOR-18 – Keep-alive race between runner and hub

Cause: the runner's HTTP client reused an idle connection for up to 5 s (httpx default) and the hub closed idle
connections after 5 s (uvicorn default). A request sent shortly before the hub's limit (as the hub sees it) travels
while the hub already closes the connection; it is lost with `hub_unreachable`. The window is as wide as the round trip
of the route, so it appeared only on the two-device topology. Callers that retry (outbox push) hid it; the credential
store at the end of the GitHub device flow does not retry, and it follows a GitHub poll with a 5 s interval, so it hit
the window well: the user had confirmed the code and the connect still failed with `upstream_unreachable`
(the sporadic failure KOR-16 could not determine, see the addendum there).

1. **Order of closing.** On every hop the client closes an idle connection before the server does. Runner client:
   `KEEPALIVE_EXPIRY_S = 2` s (`httpx.Limits`): well below the hub's limit and below the 5 s of common server defaults
   (an unchanged hub, a proxy with a short limit), with a margin far above any round trip; a new connection after 2 s
   idle costs one handshake, the device flow polls every 5 s anyway. Hub: `agent-hub serve` keeps idle connections for
   `KEEP_ALIVE_S = 120` s. The package suggested 60–75 s; 120 s was chosen because a reverse proxy in front is itself
   a client of the hub and must close first: cloudflared reuses origin connections for 90 s by default, nginx for
   60 s. Runbook `hub-operation.md` and the S6 check of `CUTOVER.md` name the proxy rule.
2. **One repeat on a stale connection.** A request that fails on a reused connection before any byte of the answer
   arrived (`RemoteProtocolError`, `ReadError`, `WriteError`) is sent once more, newly signed (time, nonce), with the
   same body bytes; the pool drops the broken connection and takes a new or another idle one (a failure of the repeat
   is final, never a third send). The pool keeps httpx's bounds (100 connections, 20 idle). Reuse and "answer began" come from httpx's `trace` extension
   (`connection.connect_*` absent, `http11.receive_response_headers.complete` absent). Timeouts, connect errors,
   failures on a new connection, failures after the answer began and transports that report no events (tests,
   custom transports) are never repeated; answers that do not verify are errors as before, without a repeat.
3. **Why a repeat is harmless** (byte-identical body, checked in the hub's handlers): outbox items by `origin_seq`
   (`replayed`), responses by `(request_id, seq)` and identical text, credential writes (the stored bytes are answered
   with the stored version), operations by `operation_id` (original decision, `replayed`), wraps (`ON CONFLICT DO
   NOTHING`), enrollment (same keys → the same answer), all reads. No endpoint needed an exception.
4. **Credential store with repetition.** `credentials.cache.put` takes `delays` and `sleep`: a read or write that
   failed with `hub_unreachable` is sent again after each delay (`RESPOND_DELAYS` 1, 2, 4 s, like a response). The
   write repeats the **same** record bytes, so a write that arrived but whose answer was lost yields the stored version
   instead of a second version (re-sealing would have stored version + 1). The retry sits in `put` (outside the
   package's file list) because only there the identical bytes exist. Used by the GitHub device flow and the
   subscription login (`login/requests.py` `_store`). The two other `upstream_unreachable` places of
   `login/requests.py` (`count_tokens` at the model provider, IMAP) are not hub calls; a failed check is answered and
   can be repeated by the user, so they stay as they are.
5. **Diagnosis.** `HubTransportError.describe()` = `reason=… [reused=yes|no] [cause=<class>]`; `push_failed` and the
   store failure lines use it; `github_connect failed` names `step=store kind=hub` with it (or the refusal's
   `status` and `error`); every repeat of a store logs `runner.credentials store_retry step=read|write attempt=n …`.
6. **Test hub kit.** The credential kit of the runner tests now answers a repeat of the stored bytes with the stored
   version (as the hub does) and can lose a PUT before or after storing (`put_lost`).
7. **Files outside the package's list:** `credentials/cache.py` (point 4), `sync/push.py` (the `push_failed` line of
   point 5) and the test kit `tests/platform/runner/credentials/kit.py` (point 6).
8. No contract changes, no ADR (the decisions stay within `03-api.md` §1 and `05-runner.md` §2.3/§11.4).
