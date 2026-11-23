# CTR-01 decisions (contracts I: conventions, harness, browser API, enums)

Small decisions taken while writing `contracts/` from `03-api.md`, `04-crypto.md` and `02-data-model.md`. The
conventions themselves are in `contracts/README.md`.

## Conventions
1. **Schema identity.** Every schema has `$id = https://agent.dfx.swiss/contracts/<path>` (an identifier, never
   fetched) and refers to others with relative references to named `$defs`. Python (`referencing` registry) and Ajv
   (`addSchema`) resolve them the same way; the guest's stdlib validator only needs path arithmetic.
2. **A JSON file is a schema iff its top level declares `$schema`.** Everything else (enums, `INDEX.json`,
   fixtures, later vectors and rule tables) is data. Definitions-only schemas (`common.json`, `api/objects.json`)
   need no fixtures; their formats are covered by the table test `test_common.py`.
3. **Keyword whitelist** exactly as the package text lists it plus the annotations `$schema`, `$id`, `title`,
   `description`, `$comment`. `type` is always a single name (unions via `anyOf`), because Ajv strict mode refuses
   union types and the stdlib validator stays simpler. `$ref` with sibling keywords is allowed (Draft 2020-12); a
   sibling length limit repeats `"type": "string"` (Ajv `strictTypes`).
4. **Integers** are refused when they are booleans or floats (also `4.0`). Python's `jsonschema` accepts `4.0` as an
   integer and Python's `$` matches before a trailing line feed; the Python harness redefines `integer`/`number` and
   anchors `$` at the very end (ECMA-262). The whole-float case is tested in Python only: JavaScript's `JSON.parse`
   cannot tell `4.0` from `4`, so there is no fixture file for it.
5. **Fixtures** live in `fixtures/<schema path without .json>/{valid,invalid}/`. Every message schema has one valid
   `basic.json`, the derived invalid cases `missing-<first required>`, `extra-field`, `wrong-type-<first field>` and
   targeted cases (boolean/float as integer, b64u padding and non-canonical last character, UUIDv4, time without
   milliseconds, oversized ciphertext, 101 events, alternatives of `oneOf`, unsafe WebAuthn options). The Python test
   additionally mutates every valid fixture (each required top-level field removed, one extra field added).
6. **Query parameters** are described by the `request` schema of a GET/DELETE endpoint as an object of text values
   (`request_kind: "query"`), with patterns that encode the limits (`limit` ≤ 1000 for logs/events, ≤ 200 for lists;
   `after` without leading zeros). Endpoints without parameters have the empty object schema. Path parameters are
   listed in `INDEX.json` (`path_params`, naming a definition of `common.json`).
7. **List responses are objects** with a named array (`{"tasks":[…],"next_before":…}`), never bare arrays, so that
   fields can be added later without breaking clients (ADR-027 §3 allows only additive changes). Where the
   specification writes "→ record" or "→ task" for a single object, the response wraps it (`{"record":…}`,
   `{"task":…}`) for the same reason.
8. **Signed structures live once, in `common.json`.** The event envelope and its exact header key set, wraps, the
   membership- and platform-log entries (one exact body per type: `log_body_<type>`, `platform_body_<type>`; the
   `invite` type has a user and a device variant; `inv_sig` exactly on `accept`), the credential record and the device
   request/response envelopes (04-crypto §4.2–§4.8) are needed by the browser API already, so they are written here
   and nowhere else. CTR-03 owns the plaintext *bodies* (`events/<kind>.json`, `credentials/<kind>.json`,
   `requests/<kind>.json`), the device API and the live channels, and its files `$ref` these envelopes; per-type log
   files under `log/`, if CTR-03 wants them, are thin `$ref`s to `common.json#/$defs/log_body_<type>`.
   Events carry two variants so that the size rule E8 is a schema rule: `session.terminal` ≤ 64 KiB, every other kind
   ≤ 256 KiB; `event_prompt` fixes the kind for `POST /v1/tasks/{task}/prompts`.

## INDEX.json
9. `INDEX.json` lists **every** HTTP endpoint of 03 §4, §5, §6.1 and §6.2 now. Device endpoints, `/v1/stream` and
   `/v1/device/ws` carry `request`/`response` = `null` and `owner: "CTR-03"`; CTR-03 fills in the schema names in
   these existing rows instead of appending rows (fewer merge conflicts) and removes itself from
   `INDEX.json#/pending_owners` (the test fails while a listed owner has nothing pending). The endpoint tables quoted
   from 03-api.md are data (`tests/platform/contracts/spec/endpoints-ctr01.json`); a later package that adds endpoints
   (e.g. guest control endpoints, if CTR-02 lists them) adds `spec/endpoints-<package>.json` and never edits CTR-01's
   test files. Streaming rows have `request_kind: "stream"` and may name schemas outside `api/` (`ws/`); repeatable
   query parameters are arrays of text (03 §5 `terminal=`, §6.1 `cursor=`).
10. Each endpoint has `auth` (`anon`, `browser`, `anon_or_browser`, `root`, `device`, `device_enroll`) and `rights`
   from the vocabulary of 03 §3 plus the special cases of the tables (`per_entry_type`, `per_event_kind`, `owner`,
   `root`, `invite_proof`, `device_eligible`, `device_addressed`).
11. **Read-log resources.** Every authenticated, non-streaming GET has a `read_resource` (e.g. `org.log`,
   `device.state`); `audit-actions.json` contains exactly `read.<resource>` for these (03 §1 "every authenticated
   GET appends `read.<resource>`"), the hub route declaration uses the same name (ADR-027 §4).
12. Endpoint names: kebab case; `device-*` only for `/v1/device/…` (CTR-03's file prefix). The browser endpoints
   `/v1/device-requests…` are therefore named `requests-create` / `requests-get`.

## Content where the specification is silent
13. `/v1/config.limits` = the client-relevant limits of 03 §12 and 03 §1 as integers (`request_body_bytes`,
   `event_batch_bytes`, `events_per_request`, `event_ct_bytes`, `terminal_ct_bytes`, `whiteboard_text_bytes`,
   `session_certificate_seconds`, `invite_user_seconds`, `invite_device_seconds`, `invite_max_seconds`,
   `device_request_seconds`, `subagent_max_depth`, `subagent_max_running`, `subagent_max_total`).
14. `GET /v1/orgs/{org}` additionally returns `platform_access` (ADR-026): the browser must know it before assigning
   a platform device (03 §4.6.1 step 1). Device lists carry `capacity_low` (03 §4.6.2).
15. Rows (`api/objects.json`): task, session, VM, operation, notification, usage and metric sample with the columns of
   02 §2 that the hub may show (04 §7); operations omit the dedupe tag. Task lists and task detail use the same
   `task_row` (superset of the list fields in 03 §4.6).
16. Login begin options carry no `challenge`: the browser uses SHA-256 of its statement (04 §5.4). Registration
   options fix ES256 (`alg -7`) as the only algorithm, `residentKey`/`userVerification` `required`, PRF and timeout
   300000; `excludeCredentials` is optional (adding a passkey).
17. `size_exceeded` has the statuses 400 (a field limit such as E8) and 413 (request body) because 03 §11 lists it
   under both; `error-codes.json` carries `statuses` lists for every code (the first is the usual one). 04 §8 codes
   without an explicit status get 400 as 03 §11 says; the five client-only codes have no status.
18. Audit "invitations" (02 §2.9) are made concrete as `invite.lookup`, `invite.create`, `invite.revoke`,
   `invite.register` (registered user bound), `invite.release` (24 h orphan cleanup), `invite.use`,
   `invite.expire`; the credential key rotation of 03 §4.8 gets `credential.rotate`. Pattern actions are expanded
   (`log.<type>`, `session.<state>`, `operation.<state>`, `device_request.<state>`, `platform.<entry type>`,
   `read.<resource>`, `read.session.<tool>`). `read.session.<tool>` covers every MCP tool except
   `request_operation`.
19. `event-kinds.json` has one row per allowed (kind, scope, author) combination with the right and the header rules
   stated by 04 §4.3 / 03 §10 (`empty`, `set`, `scope_id`, `target`, `author`, `device`, `entry`, `operation`,
   `read_target`); fields not stated are left open. `session-states.json` encodes the transitions of 02 §2.4.1
   (chain, working ⇄ idle, idle → suspended ⇄ resuming → working, every non-terminal state → stopping and → failed).

20. **Closed lists inside the schemas.** The error body's `error` is the enum of hub-sent codes (`hub_error_code`),
   `state_reason` of tasks and sessions is `session_failure_reason`, operation `error_code` is `operation_error_code`;
   tests keep them equal to the files in `enums/`. Transition `reason`s stay a code pattern (non-failure transitions
   have reasons the specification does not list).
21. **Platform-log entries** are signed by the root, which has no principal ID: `by` is the constant
   `"platform-root"` (the key ID of root-signed requests, 03 §1) and the `root` entry's `pub` is the single Ed25519
   key (b64u, 32 bytes), not the `{kem,sig}` pair.
22. `POST /v1/passkeys/register/finish` is `oneOf` [first passkey with `identity` + `recovery` + `cred_wrap`, further
   passkey with none of them]. Registration and login options pin `prf.eval.first` to SHA-256("agent.dfx.swiss/prf/v1")
   (04 §3); WebAuthn `user.name`/`displayName` are the user ID text (the hub knows no names). `POST /v1/orgs` needs the
   three profile events (`org.profile`, `wg.profile`, `member.profile`, 04 §5.3); the order of `[task.meta, prompt]`
   in `POST /v1/tasks` and the kinds of the org-creation events are hub checks (no `prefixItems` in the whitelist).
23. Size limits the specification does not state: credential record `ct` ≤ 64 KiB, invitation package ≤ 64 KiB
   (several hundred workgroups), device request/response `ct` ≤ 256 KiB, identity bundle ≤ 1 KiB, audit `detail`
   values ≤ 256 printable ASCII characters (CTR-02 replaces the generic detail with per-action schemas). Monitoring
   `step` ≥ 15 s (03 §4.11). `GET /v1/device/pull` has the rights `device:wg` and `device:org` ("device:wg / org").
24. The 04 §8 codes `prf_length`, `recovery_code_length`, `invite_code_length`, `hpke_short` and `decrypt_failed`
   keep `origin: hub` with 400 as 03 §11 states literally, although in practice clients raise them; the five
   client-only codes are exactly the list of 03 §11.

## Harness and gates
25. The Python harness is `tests/platform/contracts/harness.py` (imported as `tests.platform.contracts.harness`;
   other suites use `Contracts.load()` / `Contracts.validate()`). The TypeScript harness `contracts/tools/validate.mjs`
   exports `loadContracts()`/`check()` for WEB-01 and is run by the pytest test `test_ts_harness.py`, so
   `make platform-ci` covers it without a new Makefile target. Missing `node` or `contracts/node_modules` fails the
   test (`pytest.fail`); `tools/wp/start.sh` already runs `npm ci` in `contracts/`.
26. `ajv` is pinned exactly (8.20.0) in `contracts/package.json` (development dependency of this directory only, own
   lock file). The GitHub workflow `platform-hub.yml` gets Node and `npm ci --prefix contracts` before the pytest
   step (additive).
27. The agent copy: the merge of this package's agent-core part already syncs `contracts/` into `agent` through the
   merge script's fallback (rsync + VERSION check, commit `Sync the contracts from agent-core.`), because
   `scripts/sync-contracts.sh` does not exist in `agent` yet. The agent part of CTR-01 then adds the script and its
   test and runs the script against the integration state; a byte-identical copy makes that a no-op.
