# SCN-04 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## SCN-04 – decisions (agent side of the M2b scenarios)

The scenarios themselves live in agent-core (`e2e/platform/`, `docs/platform/decisions/SCN-04.md` there). Running
them against the integration state showed three defects on this side, fixed here:

1. **Model double: `GET /v1/key`** (`agent_doubles/model/server.py`): the runner's spend poller of the platform key
   (RUN-13) reads the OpenRouter key document from the model upstream at start and every 900 s. The double answered
   `404` and recorded a failure, so every E2E iteration failed its final double check. It now answers the document
   with zero spend and no limit; the read is journaled but not counted (an idle double stays idle) and independent
   of the scenario's access kind; only a request without a bearer token is refused (`401`, a failure).
2. **GitHub double: `html_url`** (`agent_doubles/github/server.py`, `--html-url`): the runner accepts only `https`
   result URLs. With `html_url` the double writes an `https` site (never called) into its web links, while the
   codeload redirect and the device verification page keep the served base, so a client that follows them still
   reaches the double.
3. **Golden bundle: `contracts/mcp/tools/`** (`agent_runner/vm/golden.py`): `CONTRACT_SKIP` was applied at every
   depth, which dropped the MCP tool schemas from the image; the guest's `tools/list` then failed with
   `schema_unreadable` and Claude Code could not use any platform tool. The skip list applies to the top level of
   `contracts/` only for `tools` and `changes`; `fixtures`, `vectors` and `node_modules` are skipped at every depth.
   Tests cover nested directories and the real checkout bundle.
4. **`platform.read` header `ref`** (`agent_runner/subagents/controller.py`, `sessions/events.py`): the events were
   written with an empty `ref`, which the hub's kind rule refuses (`header_invalid`; 04-crypto.md section 4.3 "`ref`
   = read target"), so no read event reached the hub. The event now names the audited target ID (a UUID, as the
   header contract allows). The tests pin `ref` (the runner's test hub does not apply the kind rules).

## SCN-04 – decisions (M2b scenarios: E2E-19/20/21/22/24)

1. **GitHub double in every run** (`orchestrator/run.py` `start_doubles`, `services.py`, `config.py`): like the model
   double, the GitHub double of the agent checkout runs on the runner device behind its own relay (port offsets 13 and
   14, chosen away from the next free ones so another scenario package can add a double without a clash). The runners
   get `AGENT_RUNNER_GITHUB_API_URL` = `AGENT_RUNNER_GITHUB_WEB_URL` = the relay and the double's default App client
   ID. The double starts empty; each spec seeds it (reset + seed) through the generic route `POST
   /doubles/github/request`, so no new control route was needed (seed, faults, expectations, device-flow decision,
   journal, repository inspection are all control routes of the double). The final check of every iteration requires
   the GitHub double's `state.ok` (every armed fault fired, every expectation met, nothing unexpected) like the model
   double's.
2. **https web links of the double** (agent: `--html-url`): the runner accepts only `https://` result URLs (push, pull
   request, comment URLs, as GitHub sends them), while the double is reached over loopback `http`. The double now
   names `https://github.example` in its web links only; the codeload redirect and the device verification page stay
   on the served base, so the runner still follows them to the double. Not a product change.
3. **Spend poll of the platform key** (agent: model double `GET /v1/key`): since RUN-13 the runner reads the OpenRouter
   key document from its model upstream at start and every 900 s. The model double answered 404 and recorded a
   failure, which turned every E2E iteration red (also the existing M1 specs). The double now answers the document
   with zero spend; the read is journaled but not counted, so a double with the idle scenario stays idle.
4. **MCP tool schemas in the golden image** (agent: `vm/golden.py`): the golden bundle skipped every directory named
   `tools` below `contracts/`, including `contracts/mcp/tools/`. In the VM `tools/list` of the platform MCP server
   failed (`schema_unreadable`), Claude Code reported "listing their tools failed" and no platform tool could be
   called (the earlier specs never called one). `tools` and `changes` are now skipped at the top level of
   `contracts/` only, `fixtures`, `vectors` and `node_modules` at every depth (tests for nested directories and for
   the MCP schemas in the real bundle). This changes the shared golden cache name.
13. **`platform.read` events refused** (agent: `subagents/controller.py`, `sessions/events.py`): the runner wrote them
    with an empty header `ref`; the hub's kind rule requires the read target (04-crypto.md section 4.3) and refused
    every one (`header_invalid`), so no read reached the task log while the audit rows did. The event now carries the
    audited target ID (a UUID, as the header contract `uuid_or_empty` allows). The runner's test hub did not apply the
    kind rules; the tests now pin `ref`.
14. **Edits outside the package's file ownership** (review M1/M2): starting a further double needs the orchestrator
    core (`run.py`, `services.py`, the port table in `config.py` and their fakes and tests); no extension point exists
    for doubles and 07-testing.md section 5.1 has the orchestrator start all doubles. The four agent-side changes
    (decisions 2-4 and 13) fix defects that made every M2b scenario impossible; their owner packages are merged and no
    package owns these files now. All are minimal, tested and recorded in both repositories; the SCN-03 mail double
    can take its ports next to the GitHub double's.
15. **`non_fast_forward` as `failed`** (E2E-22, review L1): 03 §9 lists fast-forward among the formal checks, but it
    needs the branch head at GitHub at execution time, so the runner decides it in the executor (a later push may
    win the race); the outcome is `failed` with `retryable:false`, not a local `rejected`. The spec pins that.
16. **Verification link in T2** (E2E-20, review L5): the device code page of the double is on the runner device's
    loopback; the spec asserts the link the device reported, it does not open it (real GitHub names its https page).
5. **MCP tools in the scenarios**: Claude Code connects the HTTP MCP server while the first request is already on its
   way, so the platform tools are not in the first request's `tools` list; the scenarios call them by name without
   a `tools_include` expectation, and each step checks the previous result (`tool_result_contains`) instead. Values
   from results (commit SHA, operation ID, child session ID) are captured per step from the compact JSON answers
   (keys sorted). A task of another workgroup (E2E-19) is passed in as a scenario variable.
6. **Exact REST sequence** (E2E-21): asserted as the double's route templates in order, from the first call after
   the connect flow; it is the pre-check + executor sequence of 05-runner.md section 12.1 per operation.
7. **Secret in a commit** (E2E-22): a `ghp_` token shape with 16 characters – inside the runner's pattern
   (`gh[pou]_` + 16 or more) but below the repository hygiene scan (20 or more) and the artefact secret scan (30 or
   more), so neither the repository nor the archived journals look like they hold a token.
8. **Non-fast-forward** (E2E-22): two commits on the same parent (the branch head at GitHub), the newer pushed first;
   pushing the older one then diverges. 5xx-then-success is a 502 on the first ref creation of `create_branch`; the
   operation succeeds with two POSTs and one hub record.
9. **`get_notifications` read audit** (E2E-19): answered in the guest from the session's own pings, it writes neither
   `read.session.get_notifications` nor a `platform.read` event (RUN-14 decision 15; 03 §8 asks for an audit row of
   every read). E2E-19 asserts the audit and the event for every forwarded read and the tool event for
   `get_notifications`, and leaves that tool out of the comparison, so the spec stays green once the audit is added.
   Open point for the acceptance package (ACC-M2): either forward the audit or amend 03 §8.
   Whiteboards (review M4): the spec writes an org and a workgroup entry first and expects them in the answers and on
   the web whiteboard; the task whiteboard of the foreign task is `not_found` as well.
10. **Own sandbox** (E2E-24): every child's workspace `/srv/sessions/<id>/work` is owned by its own session user (file
    facts in the VM), all sessions run in the workgroup's VM (runner store).
11. **Limits** (E2E-24): depth by the grandchild's own request; running children by four requests in one assistant
    message (Claude Code runs MCP tools one after the other: three accepted, the fourth `limit_exceeded`). The
    parallel children answer through a side step because their order is not fixed, after 8 s, so all three are still
    running when the fourth request is checked. `get_subagent_result` is called
    three times in a row (30 s each) so a slow child start never desynchronises the scenario.
12. **Monitoring of the child** (E2E-24 last clause): pending until SCN-07 delivers the monitoring scenario; SCN-08 /
    ACC-M2 check it.
