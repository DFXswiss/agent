# contracts

Single source of truth for every cross-component format of the platform (`docs/platform/spec/07-testing.md`
section 4, ADR-027 section 3): JSON Schemas of the hub, device, guest and MCP messages, closed enums, crypto test
vectors and golden fixtures. The contract packages author this directory in `agent-core`. The `agent` repository
holds a byte-identical copy that is updated only by `scripts/sync-contracts.sh <agent-core checkout>` (the merge of
an agent-core change that touches `contracts/` syncs it automatically) and is **never edited by hand**.

`VERSION` is derived: the SHA-256 over the manifest lines `<sha256 of the file>  <path>\n` of every file in this
directory except `VERSION`, sorted by POSIX path relative to `contracts/` (directories `node_modules`,
`__pycache__`, `.pytest_cache` and files `.DS_Store` are skipped). `agent-core` regenerates it with
`make contracts-version`; `tests/platform/test_contracts_version.py` fails in both repositories while it is stale,
and the end-to-end orchestrator refuses to start when the two checkouts differ (CON-04).

## Layout

| Path | Content | Owner |
|---|---|---|
| `common.json` | named formats (`uuid`, `time`, `b64u`, fixed-length `b64u_<n>`, key IDs, query texts), the closed code lists used inside schemas, and the **single definition** of the signed structures of `04-crypto.md` section 4: event envelope and header, wrap, membership- and platform-log entries (one exact body per type), credential record, device request/response envelopes, error body | CTR-01 (later changes per ADR-027 §3) |
| `api/INDEX.json` | every HTTP endpoint of `03-api.md` sections 4, 5, 6.1, 6.2: method, path, auth, rights, path parameters, schema files, read-log resource | CTR-01 (device rows: CTR-03) |
| `api/<name>.request.json`, `api/<name>.response.json` | one pair per endpoint; `api/objects.json` shared rows; `api/error.response.json` the error body | CTR-01 (browser), CTR-03 (`device-*`) |
| `enums/` | closed lists: error codes with HTTP statuses, operation error codes, session failure reasons, outbox item kinds and audit actions, audit actions, session and task states, event-kind matrix | CTR-01 |
| `ws/`, `outbox/`, `events/`, `credentials/`, `requests/`, `log/` | live channels, outbox items, the plaintext **bodies** per event kind, credential kind and request kind; files there reference the envelopes of `common.json` instead of redefining them | CTR-03 |
| `ws/INDEX.json`, `ws/sse/`, `ws/device/` | `GET /v1/stream` and `GET /v1/device/ws`: query schema, one data schema per frame type, the frame unions (`ws/sse/frame.json`, `ws/device/hint.json`) and the device heartbeat | CTR-03 |
| `events/kinds.json`, `events/<kind>.json`, `events/objects.json` | event kinds with body schema, scopes, authors and ciphertext limit (consistent with `enums/event-kinds.json`); bodies; kind-restricted envelopes (device kinds, `operation.params`, all but terminal, terminal) | CTR-03 |
| `outbox/item.json`, `outbox/<item_kind>.json`, `outbox/objects.json` | items of `POST /v1/device/outbox` and their bodies per item kind | CTR-03 |
| `credentials/<kind>.json`, `requests/<kind>.{request,response}.json` | plaintext bodies of credential records and of device requests and responses | CTR-03 |
| `log/membership/<type>.json`, `log/platform/<type>.json`, `log/invitation-package.json`, `log/session-statement.json` | one schema per membership- and platform-log entry type (thin over `common.json`), the invitation package plaintext, the session statement text | CTR-03 |
| `guest/`, `mcp/`, `ops/`, `audit/`, `model-catalog.json` | guest control protocol and journal, MCP, operation types, audit details, model catalog | CTR-02 |
| `guest/INDEX.json`, `guest/journal/INDEX.json` | guest control endpoints (03-api.md section 7.1) and journal kinds with their schemas | CTR-02 |
| `mcp/INDEX.json`, `mcp/hook-deny-reasons.json` | JSON-RPC methods, platform tools, hook events; the fixed deny sentences, block reason and notice template | CTR-02 |
| `ops/INDEX.json`, `ops/dedupe-fields.json`, `ops/rules.json` | operation types; dedupe fields and windows; formal rules a schema cannot state (branch, path, secret patterns, limits) with examples | CTR-02 |
| `audit/<action>.json` | `detail` of the audit rows of each action of `enums/audit-actions.json` (flat metadata) | CTR-02 |
| `model-catalog.example.json` | example catalog with the platform default (data, not a schema) | CTR-02 |
| `vectors/`, `tools/make_vectors.py` | crypto test vectors and their generator | CRY-01/CRY-02 |
| `tasks/title.json` | cases of the task title derivation (`06-web.md` section 4.2): `prompt` → `title`, the whitespace and line-break sets in `about`; hand-maintained data (no generator), consumed by the web core and the runner | WCR-01 |
| `fixtures/<schema path without .json>/{valid,invalid}/*.json` | examples per schema | the owner of the schema |
| `tools/validate.mjs`, `package.json` | TypeScript-side harness (Ajv) | CTR-01 |
| `changes/<package id>.md` | note of every later contract change (ADR-027 section 3) | the changing package |

## Rules

- JSON Schema **Draft 2020-12**. Every schema file starts with `$schema`, `$id` (`https://agent.dfx.swiss/contracts/`
  + its path; an identifier, never fetched), `title` and `description`. A JSON file is a schema exactly when its top
  level declares `$schema`; other JSON files (enums, `INDEX.json`, fixtures, vectors) are data.
- References are relative (`../common.json#/$defs/uuid`, `objects.json#/$defs/task_row`, `#/$defs/x`) and always point
  at a named definition under `$defs`.
- `additionalProperties: false` on every object with `properties`; maps keyed by IDs (no `properties`) carry a value
  schema in `additionalProperties`. Unknown fields are refused (`400 request_invalid`).
- Exception for **third-party input** only (Claude Code hook bodies `mcp/hooks/*.request.json`, the MCP `clientInfo`):
  an object may be open with `additionalProperties: {}` if it carries `"$comment": "open: <producer>"`. The listed
  properties are still checked; unknown fields are ignored so that a new Claude Code field never makes a hook fail
  (and thereby fail open). `tests/platform/contracts/test_ctr02.py` pins the places where this is allowed.
- Integers are JSON integers: booleans and floats (also `4.0`) are refused. Implementations check this explicitly
  where their language blurs it (Python `bool` is an `int`; the harness redefines `integer`).
- `pattern` is anchored (`^…$`) and restricted to the common subset of Python `re` and ECMA-262 (explicit character
  classes, groups `(…)`/`(?:…)`, alternation, quantifiers); no `\d`, `\w`, `\s`, `\b`, `\Z`, lookaround or named
  groups (the harness checks it). `$` matches only at the very end of the string: Python implementations must not let
  it match before a trailing line feed (use `\Z` or `fullmatch` internally).
- Bytes are canonical b64u without padding (`common.json#/$defs/b64u`); fixed sizes use `b64u_<bytes>`.
- Query parameters are described as an object of text values (`request_kind: "query"` in `INDEX.json`); a repeatable
  parameter (`terminal=`, `cursor=`) is an array of text values. Path parameters are listed by `path_params` in
  `INDEX.json`, naming a definition of `common.json`. Streaming endpoints (`/v1/stream`, `/v1/device/ws`) have
  `request_kind: "stream"`; their `request`/`response` name the schemas of the query and of the frames.
- `INDEX.json` rows with `request`/`response` `null` are allowed only for an owner listed in `pending_owners`; an
  owner removes itself when it has delivered all of its schemas. The tables that `INDEX.json` is checked against are
  `tests/platform/contracts/spec/endpoints-<package>.json` (one file per contract package).
- List responses are objects with a named array (`{"tasks": [...], "next_before": ...}`), never bare arrays.
- Size limits come from `03-api.md` section 12 and `04-crypto.md` E8 (`maxLength` of b64u = encoded length of the byte
  limit).
- Every message schema has fixtures: at least one valid and one invalid example (missing required field, extra field,
  wrong type, plus targeted cases). Definition libraries (`common.json`, `api/objects.json`) are covered through the
  schemas that use them and the format table of `tests/platform/contracts/test_common.py`.

## Allowed schema keywords

The subset implemented by the guest's standard-library validator (GST-01). Any other keyword fails the harness.

Assertions and applicators:

- `type`
- `properties`
- `required`
- `additionalProperties`
- `enum`
- `const`
- `pattern`
- `minLength`
- `maxLength`
- `minimum`
- `maximum`
- `items`
- `minItems`
- `maxItems`
- `$ref`
- `$defs`
- `oneOf`
- `anyOf`

Annotations:

- `$schema`
- `$id`
- `title`
- `description`
- `$comment`

`type` is a single name (unions are written with `anyOf`). `$ref` may have sibling keywords (Draft 2020-12 semantics: both
apply). `format` is not used: formats are named `$defs` with a `pattern`.

## Harness

- Python: `tests/platform/contracts/` (`harness.py`: `Contracts.load()`, `Contracts.validate(path, instance)`); run by
  `make platform-test`. It checks the metaschema, `$id`, the keyword whitelist, closed objects, `$ref` resolution, the
  fixtures (plus generated mutations), `INDEX.json` against the endpoint tables of `03-api.md` and the enums against
  `03-api.md`, `04-crypto.md` and `02-data-model.md`.
- TypeScript: `node contracts/tools/validate.mjs` after `npm ci` in `contracts/` (Ajv, a development dependency of
  this directory only). It compiles every schema in strict mode and classifies every fixture; `make platform-test`
  runs it, the web tests import `loadContracts()` from it (WEB-01).
