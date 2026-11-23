# SCN-04 – decisions (agent side of the M2b scenarios)

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
