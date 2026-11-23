# HUB-02 – Hub core: configuration, app factory, signature gate, errors, CSP, static, read log, fixtures

Decisions taken where `03-api.md` sections 1–4.1, 11, 12, `06-web.md` section 10 and ADR-027 sections 4 and 6 were
silent or ambiguous.

## Configuration (`config.py`)

1. **Hub key ID.** Section 2 allows `[a-z0-9-]{1,64}`, but the reused `HubSigner` refuses anything outside
   `valid_key_id` (3–63 characters, alphanumeric at both ends). The configuration requires both rules, so a value the
   signer would refuse at start is refused with a clear message.
2. **Required without default:** `PUBLIC_URL`, `DATABASE`, `SIGNING_KEY`, `HUB_KEY_ID`, `PLATFORM_ROOT_KEY`, `HOST`,
   `MODEL_CATALOG` (section 2 gives defaults only for `PORT`, `STATIC_DIR`, `DEVICE_ONLINE_SECONDS` and the budget).
   Empty counts as unset (legacy rule). `AGENT_HUB_OWNER_DATABASE` stops the start whenever it is present, even empty.
   An unknown `AGENT_HUB_*` variable stops the start too (a misspelt optional setting such as the budget must not
   silently fall back to its default); `AGENT_HUB_TEST_*` is ignored.
3. **Public URL.** http/https, a lower-case DNS host name (it is the WebAuthn RP ID; IP addresses and numeric last
   labels refused as in the legacy `refuse_relying_party_host`), no user information, path, query or fragment. The
   host line of `agent-request-v1` is `host[:port]` without the default port; the origin is `scheme://host[:port]`
   (pattern of `contracts/api/config.response.json`). The checks are copied and adapted from `agent_core.config`
   (its messages name the legacy variables).
4. **Model catalog.** `contracts/` is not installed with the package and `jsonschema` is a test dependency, so the
   catalog is validated by hand-written code. A test compares the decision with the JSON Schema plus the two catalog
   rules for more than 500 single mutations of the example catalog and for every fixture. Like the schema (which has
   no `propertyNames`), the keys of `models` are not checked. The file is read as strict JSON (no repeated keys, no
   NaN/Infinity, ≤ 1 MiB).
5. **Limits** of section 12 that clients need are one table (`config.LIMITS`), published by `/v1/config` and used by
   the pipeline; `whiteboard_text_bytes` = 64 KiB from section 10.

## App factory and discovery (`app.py`, `registry.py`, `http/routing.py`)

6. **Routes are added as they are** to the app (`app.router.routes.extend`), not through `include_router`: FastAPI
   0.165 wraps included routers lazily (`_IncludedRouter`), which hides the routes from the pipeline's matching. The
   declaration (`RouteSpec`) is an attribute of the route object.
7. **Declaration.** `HubRouter.get/post/put/delete(path, auth=, right=, read_resource=, max_body=, streaming=)` and
   `hub_websocket(path, auth=, right=)`. `auth` uses the vocabulary of the `auth` column of `contracts/api/INDEX.json`
   (`anon`, `browser`, `anon_or_browser`, `device`, `device_enroll`, `root`); `right` the rights of section 3 plus the
   INDEX extras. The pipeline enforces the authentication class (`401 signature_missing` without signature, `403
   forbidden` for the wrong key kind); the right itself is checked by the area's handler (package text). A test
   compares every discovered `/v1/` route with its INDEX row (auth, rights, read resource, streaming), so every later
   package is checked as it adds routes. The app refuses a router that is no `HubRouter`, a route without declaration
   and a method/path registered twice.
8. **Discovery skips a module without the attribute.** `agent_hub/crypto/keys.py` (CRY-01) is a primitive module, not
   a key-resolver module; refusing it would forbid common module names. A missing registration therefore fails closed
   (no route, no resolver); each area's tests prove its registration. A wrong type and a key defined by two areas are
   refused. Signal subscribers of the same name are concatenated (several areas may listen).
9. **Key resolvers.** `auth/keys.py` holds only the interface (`Principal`, `ResolvedKey`, `KeyResolver`, verifiers);
   the platform-root resolver is `config_api/keys.py:RESOLVERS` (configuration area of this package). HUB-03's
   package text also lists `auth/keys.py` (browser resolver): it may add its own `RESOLVERS = {"session.": …}` there
   (a new name, the interface stays); HUB-08 registers `"device."` in `device/keys.py`. Registration keys are `session.`,
   `device.` or an exact key ID. A resolver returns None (unknown → `key_unknown`) or raises `HubError`
   (`key_revoked`, `session_expired`, …); the gate verifies the signature.
10. **Static routes last.** `static_routes.router` is appended after all area routers; its catch-all `GET /{rest:path}`
    answers unknown `/v1/` and `/assets/` paths with JSON `404 not_found`.

## Signature gate and pipeline (`auth/gate.py`, `http/pipeline.py`)

11. **Malformed signature headers** (incomplete, repeated, non-ASCII, wrong key-ID form, time, nonce or signature
    length) are `401 signature_missing`, as in the legacy `KeyGate` check 1. Order: headers → time window 120 s →
    key (resolver) → signature → nonce (600 s store, rows older than the window deleted on every insert and by the job
    `request_nonce.purge` every 60 s).
12. **Signed host** is the host line of `AGENT_HUB_PUBLIC_URL`, never the `Host` header (legacy `self.host`). The
    target is the raw path and raw query as sent (visible ASCII only).
13. **`Content-Encoding` is refused** (`400 request_invalid`): the specification defines the body hash over the body
    and no content coding; the legacy gzip support is not carried over.
14. **Body limits** apply to every request before the route (1 MiB, 4 MiB for routes declared with
    `max_body=EVENT_BATCH_BYTES`); `Content-Length` above the limit or a streamed body passing it is `413
    size_exceeded`. Rate limits of the source are checked before the body is read. Query parameters a route does not
    declare (its own or its dependencies') are `400 request_invalid` on `/v1/` (03 §1: unknown fields are refused).
15. **Which responses are signed:** every non-streaming response after the gate verified the request, including `403`,
    `404`, `429` and `503` (route failure). Refusals of the gate itself (`401`), `413` and `400` before verification
    are unsigned (legacy rule: no signature before check 8). `HEAD` responses are signed over the empty body. Streamed
    responses of non-streaming routes are buffered and signed.
16. **Device enrolment** (`auth="device_enroll"`): the pipeline requires and parses the headers, keeps them with the
    body, and the enrolment route (HUB-08) completes the check with `verify_enrollment(request, context, verify,
    principal)` using the key from the body; `principal.key_id` must equal the `Agent-Key` of the request. The response
    is then signed. Enrolment requests take a token of the source's anonymous bucket (no key bucket exists yet).
17. **WebSocket upgrades** pass the same gate (empty body hash) and are closed with 1008 on failure; never
    response-signed (section 1). HUB-07 declares `/v1/device/ws` with `hub_websocket`.
18. **Rate limits:** token buckets (capacity = per-minute limit, steady refill) per hub process with the injected
    clock; `Retry-After` in whole seconds until the next token. The anonymous limit applies to `/v1/` only (static
    assets and the SPA page are not limited). Failed signatures count in a separate bucket per source (30/min); a
    source over it gets `429` for signed requests before the body is read or a key is looked up, while unsigned
    requests and valid signatures of other users behind the same address keep their own limits. The platform root
    shares the browser limit (600/min). The source is the peer address; no forwarding header is trusted
    (a reverse proxy in front would share one bucket – deployment is out of scope, recorded as a risk).
19. **Errors.** The closed list has no 500 code: an unexpected failure is `503 unavailable` (logged with traceback,
    never in the body). Router `404`/`405` → `404 not_found`; other `HTTPException`s → `400 request_invalid` or `503
    unavailable`; validation errors → `400 request_invalid` with `detail.pointers` (RFC 6901, at most 64; for an
    unknown field the pointer names its parent object, so no request text is echoed). The codes and statuses are a copy of `contracts/enums/error-codes.json` (test compares).
20. **Security headers** of section 1 are set on every HTTP response by the outermost middleware and replace values a
    route set; API responses without caching get `Cache-Control: no-store`.

## Read log (`http/readlog.py`)

21. Rows for every authenticated `GET` with `read_resource` after the route ran, also refused ones: result `ok` (2xx),
    `denied` (401/403/404), `failed` (5xx), `rejected` (other). `org_id`/`wg_id`/`task_id`/`session_id` from path
    parameters of those names, else from single query parameters (`?org=`), when they are UUIDv7 text; target = last
    path parameter (kind = its name, or the resource for `{id}`); empty detail (`contracts/audit/read.*.json`).
    Requests refused by the gate or the rate limit write no row. Streaming routes have no read resource (INDEX).
22. The writer validates on record, writes all queued rows in one transaction through `append_audit` once the oldest
    is `max_delay` (1 s) old, keeps rows after a failed write (bounded to 10 000, oldest dropped with an error line)
    and writes the rest at shutdown.

## Other

23. **`/v1/health`** probes the database (`SELECT 1`, 2 s) and answers `503 unavailable` when it fails (the response
    schema allows only `ok: true`).
24. **Static delivery:** only `/assets/*` (immutable) and the SPA routes of 06 §2.4 (`index.html`, `no-cache`, 200);
    any other non-API path gets `index.html` with 404 (client not-found screen); without a build `503 unavailable`.
    Root files such as `favicon.ico` are not served (the build puts everything under `assets/`).
25. **CLI.** `agent-hub` without a command runs `serve` (00-overview.md section 7: `python -m agent_hub` is the
    process); exit codes 2 (configuration) and 3 (start failed, uvicorn's code; the pool waits 30 s for the
    database). uvicorn shuts down gracefully on SIGTERM and then re-raises the signal. The FND-00 skeleton test was
    updated for the two new areas (`health`, `config_api`) and `--help`.
26. **Signals** run synchronously in the emitter's thread; IDs are UUIDs, integers or identifier text
    (`[A-Za-z0-9._:-]{1,128}`) – anything else is refused as possible content.
27. **Live publishing** is `agent_hub.stream.publish.publish(Audience, frame, data)` with the in-process
    `MemoryPublisher`; HUB-07 installs the real fan-out with `set_publisher` and keeps the module path
    (`stream/__init__.py` stays HUB-07's).
28. **Jobs** are `Job(name, interval_seconds, run, initial_delay_seconds)`; `run(context)` gets the `HubContext` and
    runs in a worker thread; failures are logged and the loop continues; shutdown waits 10 s, then cancels.

## Test fixtures (`tests/platform/hub/conftest.py`)

29. `hub` runs the app with every discovered extension point on a migrated database and the fixed clock;
    `start_hub(config, pool, clock, routers=…, resolvers=…, jobs=…)` builds variants. Clients: `hub.anon`,
    `hub.browser(user_id, expired=, ended=)`, `hub.device(device_id, state=, kind=)`, `hub.root`. Session and device
    keys are written to `principal`/`passkey`/`browser_session` and `principal`/`device` (02 §2.1) so that the real
    resolvers of HUB-03/HUB-08 find them; until those exist, in-memory resolvers of the conftest stand in for the
    unregistered prefixes (the discovered ones win). Every request and response of an INDEX endpoint is validated
    against `contracts/api/` (`extensions={"contract": False}` for deliberately invalid requests); every response to
    a signed request is verified with `agent_core.signing.response_text`, and an unsigned success fails the test.

## Review findings not changed

30. *All anonymous traffic behind a reverse proxy shares one bucket* – kept: deployment is out of scope (PROJECT.md);
    a trusted-proxy setting belongs to the cutover plan. Recorded as an open risk.
31. *The FND-00 skeleton test was edited* – needed: it lists every area package and the no-argument behaviour that
    this package changes; the change only adds the two new areas and tests `--help`.
32. *Discovery skips a module without the attribute silently* – kept (decision 8); fail-closed and every area tests
    its registration.
33. *Read-log rows beyond 10 000 are dropped during a database outage* – kept: memory must stay bounded; the drop is
    logged as an error, and reads during such an outage fail anyway because every endpoint needs the database.
34. *Buffered responses to signed requests have no size cap* – kept: they are produced by the hub's own routes (lists
    are paginated); streaming routes are not buffered.
