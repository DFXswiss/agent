# ADR-102: Sessions query the DFX production database through the debug endpoint with the platform's operation `dfx.debug` – the owner's sealed debug sign-in, released to workgroups, used by the device itself, read only

Status: Proposed (2026-11-27)

## Context
- The client (2026-11-27, DBG-01): "ich will eine agent session die zugriff auf debug hat". A person investigates DFX
  production data today with the skill `dfx-debug` of DFXswiss/tools: a CLI signs in with a wallet that has the Debug
  role (address and signature give a JWT) and sends a structured query DTO (table, select, WHERE tree, group, order,
  limit, offset) to `POST /v1/support/debug` of the DFX API. The server decides the allowed tables and columns
  (`DebugAllowedColumns`), masks personal data, counts the persons whose sensitive data an account reads per UTC day
  (table `pii_read`, refusal `PII_READ_LIMIT`) and can be switched off (process `SupportDebug`). The CLI's only write
  is `poll` (`POST /v1/payIn/poll`).
- Rules: [§10 point 3](../KONZEPT.md#k-10-b3) (a session executes nothing itself, everything is a platform
  operation), [E-31](../KONZEPT.md#e-31) (a person's access is used only where the person switches it on),
  [E-05](../KONZEPT.md#e-05) (a refusal names its remedy), A15/E-19 (a personal connection is never used on behalf of
  another person, [ADR-070](070-personal-connections-follow-the-instruction.md)); [ADR-099](099-mailboxes-read-by-the-platform.md) §1: what the device
  can do itself with a credential the session must not see is a platform operation.
- Built before: credential records sealed under the owner's `CK` (04-crypto.md §4.4), the runner's credential cache,
  the acting user of a turn (`credentials.acting`), pre-checks of operation types (`<area>/op_prechecks.py`), audit
  items of the runner through the outbox (`mail.read`), the release `use {wgs}` of a mailbox (ADR-099 §3).

## Decision

### 1. An operation, executed by the device
The debug endpoint is used by the runner on the device, never by a session or in a VM: the operation type
`dfx.debug` (class `read`, no dedupe: every query is a new read). The device opens the sign-in for the one query,
signs in, keeps the JWT in memory for that query only, sends the DTO and answers the result to the session. Neither the
signature nor the address nor the token reaches a session, a VM, an event, a log line or an audit row.

### 2. The debug sign-in as a platform credential
- A new credential kind `dfx_debug`, one record per user (primary, like `anthropic_api_key`), sealed under the user's
  `CK`; the hub never sees its content. Body (`contracts/credentials/dfx_debug.json`): `address`, `signature`
  (printable ASCII), `api_url` (`https://` host, optional port, up to eight path segments: the API base with its
  version) and the release `use {wgs}` (§3).
- Agent tool: `agent-platform credentials set-dfx-debug --api-url <url> [--release <wg>]...` reads the address and
  the signature from standard input, one per line, never from the command line; without `--release` the stored
  release stays. `agent-platform credentials release-dfx-debug --wg <wg>... | --none` changes only the release.
  `credentials list` shows the API URL and the released workgroups, never the address or the signature;
  `credentials delete --kind dfx_debug` removes it. The hub's change notices learn the kind (migration 0042); a `CK`
  rotation carries up to five records now.

### 3. Who may use it (E-31, A15)
A query uses the sign-in only when, at the pre-check and again at the execution:
- the session belongs to a task of the sign-in's owner (`session.owner_id`), and the person whose instruction
  started the turn is that owner (`operation.actor_id`): another writer of a shared task, a turn of unknown origin and
  the platform's own continuation use nobody's sign-in (`rights_denied`);
- the owner's record verifies and opens on the device (`not_found` otherwise, naming the command that stores it);
- its **sealed** release names the session's workgroup and the owner has explicit `read` or `write` access to that
  workgroup on the verified membership log now (`rights_denied` otherwise, naming the release command).
The result of an operation is sealed in the task's events under the workgroup key, so every reader of the workgroup
sees what was read: the release per workgroup is the owner's decision about that audience. Nothing is released by
default. A hub that cannot be reached is `hub_unreachable`.

### 4. The query
- Parameters (`contracts/ops/objects.json#/$defs/params_dfx_debug`): the DTO of the CLI and the endpoint,
  unchanged – `table`, `select` (1–100 items `column`, `jsonb` with `jsonbPath`, `aggregate`), `where` (a tree of
  `leaf`, `and`/`or` with 1–5 children and `not`), `groupBy` and `orderBy` (≤ 20 each), `limit` (1–10000,
  required), `offset` (0–1000000). The runner adds the bounds the endpoint enforces anyway (depth ≤ 5, ≤ 200 nodes,
  ≤ 50 leaves, a leaf's value by its operator) as formal checks (`params_invalid` with pointers); which tables and
  columns exist stays the endpoint's allowlist – the platform copies it into the tool's description, not into a
  check, so a change of the allowlist needs no platform release.
- Requests: `POST {api_url}/auth/signIn {address, signature}` → `accessToken`; `POST {api_url}/support/debug` with the
  DTO and the bearer token → `{keys, rows}`. 30 s per request, no redirects, `https` only.
- Result: `{table, keys, rows, rows_total, truncated}`; the last rows are left out (`truncated`) until the answer is
  at most 200 KiB.
- **Read only.** There is no write type and no write path in the client: `poll` (`POST /payIn/poll`) does not exist on
  the platform; it would come only with an explicit order, as a type of its own of class `write`.

### 5. The session's tool
Besides `request_operation {type: "dfx.debug", params}`, the guest offers the MCP tool `dfx_debug` whose arguments are
the same DTO. It is a shorthand of `request_operation`: the call is checked against the operation's parameter schema,
journaled as `operation.request` and answered with the operation's outcome, so records, audit, rights profiles (the
tool stands for the type `dfx.debug` in `ops/classes.json`) and the operation budget apply unchanged. Its description
carries the text of the skill `dfx-debug`: the allowed tables, the classes of allowed and refused columns, the
filter-only and presence-only columns and examples. Guest version 0.8.0 (an older guest refuses the type locally).

### 6. Refusals, limits and audit
- The endpoint's refusals reach the session in plain words (E-05): `400` → `params_invalid` with the endpoint's
  message (e.g. a column outside the allowlist); `403 PII_READ_LIMIT` → `limit_exceeded` with the endpoint's message,
  when the count starts again (00:00 UTC) and how to avoid it (no sensitive columns); `403 Endpoint disabled` →
  `upstream_error` naming the kill switch (process `SupportDebug`), never retried; other `401`/`403` →
  `upstream_error` naming the Debug role; a refused sign-in → `upstream_error` naming the owner's remedy (renew the
  signature); `429`/`5xx` → `upstream_error`, retryable; no answer in 30 s → `timeout`, retryable. A message passes
  only as printable ASCII, bounded, with the sign-in's address and signature replaced by `[hidden]`.
- The runner writes the audit item `dfx.read` (target kind `credential`, target the sign-in record) for every attempt
  that reached the API: `{operation_id, owner_id, table, filter, rows, result}` – `filter` is the WHERE tree with its
  columns and operators and without any value (`and(amlCheck =, not(mail =))`, ≤ 256 characters), `rows` the number
  of rows answered, `result` one of `ok`, `params_invalid`, `limit_exceeded`, `switched_off`, `upstream_error`,
  `timeout`. Never a result value, never a filter value.

### 7. Rights profiles
`dfx.debug` is an operation type of class `read`: the built-in roles keep it; a profile whose operation list leaves it
out forbids the tool and the type (`rights_denied`, ADR-094 §3).

## Consequences
- The hub learns that a user keeps a debug sign-in (kind and version), never its content; it learns from the audit when
  a session queried which table with which filter shape and how many rows came back.
- The endpoint attributes every query to the owner's Debug account: its daily count of persons and its own audit log
  apply to the queries of the owner's sessions exactly as to the owner's CLI.
- The files named `agents.md` (`docs/platform/agents.md`, `web/public/agents.md`) are byte-identical copies of
  `src/agent_platform/guide.md`; they follow it with this change.
- Not part of this decision: a settings section of the web client for the sign-in (the agent tool stores and releases
  it; the section follows the pattern of "Mailboxes"), the release of a sign-in to other people's tasks (it would need
  the hub's mirror of ADR-099 §3) and the write `poll`.

## Alternatives considered
- The CLI in the work VM with the credentials injected: the signature and the token would sit in a VM (§10 point 3,
  ADR-099 §1).
- Releasing the sign-in to every session of a workgroup like a mailbox: the sign-in is a person's access to production
  data with a per-person daily limit; using it only for the owner's own instructions keeps the attribution of every
  query to the person whose account the endpoint counts.
- Copying the endpoint's allowlist into the runner's checks: it changes with the backend; the endpoint refuses
  precisely and the refusal reaches the session.

## Deviation from DESIGN.md
None.
