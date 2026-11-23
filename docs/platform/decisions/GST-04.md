# GST-04 decisions (model gateway and MCP skeleton)

Small decisions of the gateway/MCP package (05-runner.md §10.2–§10.5, §4.2, §5.1; 03-api.md §7.1 credential, §7.2,
§8, §8.1 `get_context`; ADR-010, ADR-024, ADR-025).

## Contracts (changed in agent-core, `contracts/changes/GST-04.md`)
1. **Credential provisioning carries the policy.** 05 §10.2 makes the gateway inject per access kind (the subscription
   appends `oauth-2025-04-20` to `anthropic-beta`, a reserved header name), refuse models outside the catalog
   allow-list and refuse requests over the daily budget; `PUT /v1/sessions/{id}/credential` carried none of this, and
   the API process never sees `POST /v1/sessions`. Defect fix: required `access` and `models_allowed`
   (`{model_id, context_window}` – the window feeds `usage.context_window_tokens`, which the contract fixtures define
   as the catalog window), optional `budget {usd_per_day, spent_usd}`. The runner owns the catalog (05 §10.5); the
   guest holds only this slice (`catalog.py`, `ModelPolicy`) and never reads `model-catalog.json`.
2. **Provider audit.** The guest cannot write audit items; additive `usage.provider_outside_allowlist: true` lets the
   runner write `model.provider_outside_allowlist` with session and provider from one record. Never blocks.
3. **Missing credential.** Additive `guest.error` code `credential_missing` (with the session ID); the runner
   provisions again or ends the session with `model_access_unavailable` (RUN-12). The request is answered
   `503 api_error` (Claude Code retries, so a quick re-provisioning stays invisible) and gets a `usage` record with
   status 503, like every other refusal after authentication.
4. `test_ctr02.py` (agent-core) lists the new fields next to the specified ones.

## Credential path (memory only)
5. The runner talks to the control process; the credential must live in the API process only. The control process
   keeps the checked provisioning in a hand-off slot (memory, one per session, newer replaces older;
   `control_routes/credential.py`) until the API process takes it over `api.sock` (op `credential.take`) on the
   session's next model request; afterwards only `CredentialStore` holds it. No thread or push channel is needed and
   `control.py`/`server.py` stay unchanged (GST-01). A provisioning not taken yet is lost with a control restart; a
   restart of the API process loses the store – both cases end in `credential_missing`, the runner provisions again.
   After the slot is filled the route calls `sessions.credential_provisioned` (GST-05), which starts the agent of a
   session waiting in `sandbox_ready` (05 §6.1 step 5) and does nothing for a running session (refresh).
6. Revocation = session end: every take also returns the active sessions; the API process drops credentials of all
   others (`retain`), and the control process discards slots of inactive sessions. A changed credential is a new
   provisioning (refresh) and restarts the observed cost.
7. The hand-off slot is attached to the `ControlContext` (`vars(context)`, as GST-05 attaches its session manager);
   the gateway and the MCP server of an API process are kept per `ApiClient` (weak mapping), because `ApiContext`
   (GST-01) has no extension field and an API process has exactly one client.
8. Checks beyond the schema at provisioning (`400 request_invalid` with the pointer): extra header names that are
   reserved, hop-by-hop, `cookie`/`proxy-*`/`expect`, or repeated; upstream URL with user information, query, fragment,
   empty host or invalid port. Unknown or inactive session `404 not_found`. Body limit 64 KiB.

## Gateway (05 §10.2)
9. Token: `Authorization: Bearer` (scheme case-insensitive) or `x-api-key`; both may be sent if equal (Claude Code
   may send an empty `x-api-key`, which is ignored); different values or a repeated header authorize nobody.
10. Order of checks: token → body (`{"model": string}`, else `400 invalid_request_error`, nothing forwarded, no usage)
    → credential → model allow-list (`400`, error type `model_not_allowed`) → budget (`429`, type `budget_exceeded`)
    → forward. Errors use the Anthropic shape `{"type":"error","error":{"type","message"}}`; the types follow the
    spec's code names literally. Usage records are written for every refusal from the credential step on, unless
    the model text is not a valid `model_id` (the record would be invalid).
11. Budget: refused when `spent_usd + observed ≥ usd_per_day`; `observed` sums OpenRouter `usage.cost` of the
    session since its last provisioning (per session, not per VM; the runner adds the spending of all sessions and
    devices to `spent_usd` when it re-provisions, RUN-12).
12. Upstream: `<base path><route>?<query>` (Claude Code's `?beta=true` is kept), `http.client` with TLS verification
    (default context), connect timeout 30 s, read timeout 600 s (Claude Code's own request timeout); handler limit of
    the two model routes 610 s, because a non-streaming request waits for the whole answer before the headers
    (GST-01 decision 11 allows route limits). Removed from the session's request: the reserved names plus
    hop-by-hop headers, `cookie`, `proxy-*`, `expect`; provisioned extra headers replace same-named session headers;
    `Accept-Encoding: identity` so the usage can be read. Unreachable provider, TLS failure, connection closed without
    answer → `502 api_error` (Claude Code retries 5xx). Control process unreachable → `503 api_error`.
13. Response: status and body unchanged, chunks of at most 64 KiB via `read1`; headers except hop-by-hop,
    `content-length`, `cache-control`, `date`, `server`, `set-cookie`; repeated headers are joined with `, `.
    An upstream that breaks mid-stream breaks the client stream too (no clean end-of-stream that would hide the
    failure); a client that hangs up closes the upstream. Both still write the usage record.
14. Usage: SSE `message_start` (usage, `provider`) and `message_delta` (cumulative usage, `cost`,
    `output_tokens_details.thinking_tokens`), JSON `usage`/`provider`; events over 1 MiB and JSON bodies over 16 MiB
    are not parsed (forwarded anyway). `request_id` from `request-id`, `x-request-id` or `x-generation-id`, else
    `gw_<uuid4 hex>`. `cost_usd` only from OpenRouter; list prices are computed by the runner (05 §10.4).
15. `count_tokens` is forwarded without a usage record (it bills nothing and would replace the context figure of the
    latest Messages request). `HEAD|GET /model/api/hello` answers `200 {}` without a token. Every other method or
    path under `/model` (incl. `/model` itself and `..`/`//` tricks, which do not match the two routes) answers
    `401` without a valid token, else `404 not_found_error` plus `guest.error gateway_route` with the session ID
    (unauthenticated callers cannot fill the journal).
16. `routing.py` (GST-01) gains `do_HEAD`: routes declare `HEAD` themselves and the body is never written. Needed for
    `HEAD /model/api/hello`; no other route changes behaviour.

## MCP (03 §8)
17. Streamable HTTP without SSE and without MCP sessions: `POST /mcp` only; `GET`/`DELETE /mcp` answer `405` with
    `Allow: POST` (the transport's rule for servers without a stream). GST-01's test used `GET /mcp` as an example of
    an unknown path; it now uses `/mcp/x`, and its exact list of API routes became a membership check.
18. Authentication before the body is read: Bearer only (`401 {"error":"unauthorized"}`). Parse error `-32700` and
    invalid request `-32600` with HTTP 400 (id echoed only when it is a valid JSON-RPC id; batches are invalid since
    2025-06-18); every method answer with HTTP 200. Any notification (no `id`) → `202` without body.
19. `initialize` validates its params (`-32602`) and echoes `protocolVersion`; the contract requires the field, so
    the default `2025-06-18` applies only to a request without it (kept for older clients). `tools/list` returns the
    registered tools in module order with the dereferenced input schema (all `$ref` inlined, annotations removed);
    the contract's "exactly twelve" holds once GST-07/RUN-14 register the rest. A missing `arguments` counts as `{}`;
    any non-object or schema mismatch is the tool error `params_invalid` and nothing is forwarded or journaled.
20. Tools: `mcp_tools/<tool>.py:TOOL` must be a `mcp.Tool` whose name equals the module name (else
    `registry_invalid`). `query_tool(name, description)` builds a read tool; `forward_query` is the generic
    forwarding: `pending.create` (`kind:"query"`, journal `platform.query`) → `pending.wait` ≤ `QUERY_WAIT_SECONDS`
    (20 s, the MCP wait of 03 §12) → no answer `timeout`, control unreachable or session inactive `unavailable`,
    runner error passed on, result not matching `mcp/tools/<tool>.output.json` `unavailable`. Route limit 35 s.
21. "Every call is journaled": forwarded calls by their pending record; the read audit `read.session.<tool>` is written
    by the runner when it answers (it knows the outcome). Calls refused before forwarding are not journaled by the
    API process (its allowed kinds are `session.hook`, `usage`, `ping.delivered`, `guest.error`, GST-01 decision 10);
    they reach the runner through the transcript (`session.tool` with `is_error`, GST-05). Expiry of unanswered
    pending calls stays with GST-07.

## After the independent review
23. Credential take is serialized in the API process (take, `retain`, store under one lock): parallel first requests
    of a session never miss the slot, and an older list of active sessions is never applied after a newer take.
24. Bodies with OpenRouter routing fields (`models`, `provider`, `route`, `transforms`, `preset`) are refused with
    `400 invalid_request_error`: they could make the provider serve a model or provider other than the checked one.
    Claude Code does not send them.
25. At most 32 model requests wait for their upstream at a time (`529 overloaded_error` beyond, Claude Code retries),
    so hooks and MCP always keep 32 of the 64 handler slots (GST-01 decision 11).
26. `auth_header` must fit the access kind (`x-api-key` only for `api_key`, `authorization: Bearer …` otherwise).
    Session headers named in `Connection` and header values containing CR/LF (obsolete folding) are not forwarded.
    The SSE parser scans the buffer once per chunk. `tools/call` params with keys other than `name`, `arguments`,
    `_meta` are `-32602`.
27. Rejected after review, with reasons:
    - "Every call is journaled" (03 §8) for refused or protocol-level calls: the API process may only journal
      `session.hook`, `usage`, `ping.delivered`, `guest.error` (GST-01 decision 10), and no record kind or
      `guest.error` code describes an MCP call. Forwarded calls are journaled (`platform.query`), refusals reach the
      runner through the transcript (`session.tool` with `is_error`). A dedicated record needs a contract change and
      belongs to GST-07, which adds the remaining tools and the call accounting.
    - MCP waits of 20 s (route 35 s) against "handlers time out after 10 s" of 05 §5.1: 03 §12 sets 20/30 s for MCP
      waits explicitly and GST-01 decision 11 resolved the conflict in favour of route-specific limits.
    - Budget overrun by parallel requests and cost of streams that end before `message_delta` are not charged in the
      guest: the budget is a daily organization limit maintained by the runner from the usage records (RUN-12); the
      guest check only stops further requests once the limit is known to be reached.
    - Other HTTP methods (`PATCH`, `OPTIONS`) under `/model` get `404 {"error":"not_found"}` from `http.server` without
      `guest.error`: adding methods to the shared handler would change GST-01's behaviour for every route.
    - Usage of a response that is never iterated (handler timeout after the headers, client gone before the first
      byte): the generator's `finally` does not run, the record is missing and the connection is closed by garbage
      collection. Rare; the runner still sees the transcript.
    - Ownership: `routing.py` (`do_HEAD`) and two lines of GST-01's `test_server.py` are the smallest possible changes
      outside the owned paths; the call of `sessions.credential_provisioned` is the interface GST-05 published for this
      package.

## Tests
22. Gateway tests run against the real model double (DBL-01) per access kind: the double accepts a request only with
    exactly the injected credential (and the OAuth beta for the subscription), so its `state.ok` proves injection and
    removal of the session token. Failure shapes the double cannot produce (client abort with a held stream, stream
    cut mid-chunk, close without answer, repeated/cookie headers) use a small raw socket upstream in the test.
    Thinking tokens are covered by unit tests of the usage meter (the double's usage takes flat integers only).
