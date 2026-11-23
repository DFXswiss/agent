# DBL-01 decisions (model double and adversary scenarios)

Small decisions of the model-double package (07-testing.md §9, §2.3; 05-runner.md §10.2, §10.4, §15; ADR-010,
ADR-025, ADR-027).

## Layout
1. The adversary scenarios are flat files `src/agent_doubles/model/scenarios/atk-NN.json`, as the ownership lists of
   DBL-01, ATS-01 and ATS-02 name them; the mapping table the package text places at `scenarios/atk/README.md` stays
   there. Consumers load scenarios by name (`packaged_scenario("atk-07")`, CLI `--scenario atk-07`,
   `POST /_double/scenario {"packaged": "atk-07"}`) and do not depend on the layout.
2. Besides the listed modules the package adds `model/access.py` (credential check per access kind) and
   `common/client.py` (HTTP client for the control routes, for tests and the E2E orchestrator). `model/journal.py`
   holds the per-route summary of the journal (`summarize_endpoints`, `unknown_names`) – the Claude Code upgrade guard
   of 05 §15; the journal format itself lives in `common/journal.py` so DBL-02/DBL-03 share it.
3. `agent_doubles.common` has no TLS helper: the model double serves plain HTTP like Spike C. A test CA belongs to the
   mail double (DBL-03), which needs IMAPS.

## Routes and failures
4. `GET /v1/models` of the spike is dropped: Claude Code 2.1.330 never called it, and an unknown route must fail so a
   new Claude Code version that starts calling it is noticed (05 §15). Every route other than `HEAD|GET /api/hello`,
   `POST /v1/messages`, `POST /v1/messages/count_tokens` and the control routes answers `404 not_found_error`, is
   journaled and recorded as a failure.
5. A wrong credential is an unexpected request: `401 authentication_error` in the Anthropic error shape **and** a failure
   in `/_double/state`. The check is strict: the expected header with the token (constant-time comparison) and no
   header of the other credential form (`api_key` refuses `authorization`; `openrouter` and `subscription` refuse
   `x-api-key`), so a gateway that forwards the session token or both headers is caught. `subscription` additionally
   needs `oauth-2025-04-20` among the `anthropic-beta` values (refusal text as the real API words it). `/api/hello`
   needs no credential. The token is given through an environment variable (`--token-env`), never argv.
6. Body limits: a request body above 64 MiB gets `413 request_too_large`, an invalid `content-length` gets
   `400 invalid_request_error`, a body without `content-length` (`transfer-encoding`) gets `411`; all close the
   connection, are journaled and are failures. The doubles bind only to the given address; empty and wildcard
   addresses (`0.0.0.0`, `::`) are refused.

## Scenario format (extensions of the Spike C format)
7. New matcher keys `has_tools` (main loop versus background requests – the Spike B adversary double distinguished them
   the same way), `has_tool_result`, `headers` (name → expected substring) and `headers_absent`; mismatch texts never
   repeat the value of a credential header. New step key `repeat` (a step answers n requests; `consumed` lists the id
   per request, `remaining` per pending request); side steps cannot repeat because they are never consumed.
8. Top-level `variables` are default template values; callers override them (`--var`, `"variables"` of
   `POST /_double/scenario`, `ModelDouble(variables=…)`), and captures from requests override both.
9. The default `cwd` capture stops at white space, quotes and angle brackets (`[^\s<>"']+`) instead of `\S+`, so an
   environment block wrapped in tags does not leak `</env>` into paths. The recorded wire forms are unchanged.
10. OpenRouter extras are a scenario flag (`"openrouter": {"provider": …}`): the message carries `provider`, the final
    usage carries `cost`, `is_byok` and `cost_details` (the start usage carries no cost, as recorded live in Spike C),
    successful message responses carry `x-generation-id: gen-double-NNNN`. A reply may fix `cost` and `provider`;
    otherwise the cost follows fixed double prices (input 1e-7, cache read 1e-8, output 4e-7 USD per token), rounded
    to 12 digits, so tests can assert exact values. `cost`/`provider` without the flag are refused at load time.

## Control routes and state
11. `POST /_double/scenario` switches the scenario and restarts its progress (consumed, failures, variables, issued
    tool ids, request count); the journal continues. `"access"` changes the credential check only when present (null
    switches it off). `POST /_double/reset` restarts the progress and clears the journal in memory. Journal sequence
    numbers never restart and the journal file is append-only, so it stays unambiguous across switches. Control
    requests are neither journaled nor counted; their errors are `{"error": text}` with 400/404/405.
12. `state.ok` = every step consumed (with repetitions) and no failure; failures are expectation mismatches, requests
    after the end of the script, tool results for ids the double never issued, template errors, unknown routes, wrong
    credentials and refused bodies. A step is consumed only when its answer could be prepared (templates filled in,
    tool ids issued, under one lock): a template error answers `500 api_error`, is a failure and leaves the step in
    place, so Claude Code's retry does not drift into the next step. Generated tool ids are `toolu_double_<step>_<i>`
    (as in the spike); when a repeated step or a side step answers again, the request number is added
    (`toolu_double_<step>_<NNNN>_<i>`), so ids stay unique within a conversation.

## Adversary scenarios
13. One file per ID; one tool call per step, each step after the first expects `has_tool_result: true` without fixing
    whether the tool was denied, absent or allowed, so one file serves both suite modes. Paths use captures of the
    environment block (`${cwd}`, `${session_dir}`, `${session_id}`; the latter two without defaults, so a wrong
    environment fails visibly) and `${foreign_work}` (default: a fixed session id, overridden by the suite).
14. The spike's `/etc/platform` stays in ATK-21/ATK-26 and is complemented by files the guest really has
    (`/etc/agent-guest/control-token`, `/var/lib/agent-guest/journal.db`, `/run/agent-guest/{control,api}.sock`), because
    a read of a missing file proves nothing; ATK-32 writes the file it then tries to make setuid, ATK-18 also reads a
    foreign harness (`${foreign_session}`), and the trailing-`/` variant of ATK-20 uses directory tools. The spike harness `/opt/harness/CLAUDE.md` becomes `/run/agent-guest/harness/<id>.md`.
    Spike B entries 21–23 (legitimate work, a platform read) form the control scenario `atk-baseline`; entries 24–27
    (operations) belong to ATK-33 (ATS-02) and are mapped, not scripted here.
15. Shell-only attacks (exec, loader, copied binary, sockets, `unshare`, `setuid`) are attempted through `Bash` and,
    where a tool exists, through it (`WebFetch` for network). Their OS-level part is the sandbox probe of the suite.

## Tests
16. The real Claude Code run is the `live`-marked test `tests/platform/doubles/model/test_claude_live.py` (needs a
    local `claude` of the pinned version; never part of `make platform-ci`). It isolates `CLAUDE_CONFIG_DIR` under
    `build/claude-live/<id>` of the checkout, `HOME` in a temporary directory, a dummy API key, an allow-list
    environment and a dead proxy.
17. "Byte-exact against Spike C": the fixtures `wire-*.sse|json` were recorded from the Spike C double (whose output
    Claude Code 2.1.330 accepted in the recorded runs) for one scenario covering every answer form; the port must
    reproduce them byte for byte. `double-errors.json` is replayed case by case (statuses and consumed steps), and a
    request shaped after `double-endpoints.json` must pass and summarise without unknown names.

## Review (independent subagent) – outcome
18. Fixed: real guest targets in ATK-21/26 (medium), `live` tests deselected unless `-m live` selects them (hook in the
    doubles conftest; medium), unique tool ids, consume-after-prepare, scenario extras read under the lock, 411 for
    bodies without length, wildcard bind refused, ATK-18/20/32 strengthened, seeding preconditions (ATK-08 `.mcp.json`,
    ATK-24 `secret.txt`) stated in the README, journal assertion of the unknown route, and a test that the tool calls of
    every ATK file equal the README "Tool calls" column.
19. Not changed: control routes stay unauthenticated. A session cannot reach them: the sandbox allows only
    `127.0.0.1:7700` (05 §4.3 nftables, Landlock `NET_CONNECT_TCP` 7700) and the gateway forwards only the three model
    routes, everything else is `404` (05 §10.2). Only the trusted guest agent (`platform` UID) and the orchestrator
    reach the double. Control paths are not journaled by design (test plumbing, not traffic under test).
20. Noted for later packages (not defects here): the OS layer alone does not stop writes into the session's own home
    (`ATK-14`, `ATK-19` step 2, `ATK-20` step 1) nor reads of the session's own `/proc/self/environ` (`ATK-22`) – only
    the managed settings do (05 §4.3 binds the whole session directory and mounts `/proc` read-only); ATS-01 has to
    judge these against "each layer alone". In hermetic E2E the quota polls of 05 §10.6 (`/api/v1/key`,
    `/api/oauth/usage`) must point at a double that serves them; the model double treats them as unknown routes.
