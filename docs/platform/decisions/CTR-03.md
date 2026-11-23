# CTR-03 decisions (contracts Ib: device API, outbox, live channels, events, credentials, requests, logs)

Small decisions taken while writing the device side of `contracts/` from `03-api.md` sections 1, 5, 6, 8.3, 10,
`04-crypto.md` sections 3 and 4.3–4.9, `05-runner.md` section 11 and `02-data-model.md`. The conventions of CTR-01
(`contracts/README.md`, `decisions/CTR-01.md`) and CTR-02 apply unchanged.

## Layout
1. **Libraries plus thin files** (as CTR-02): references must point at a named `$defs` entry, so every directory has an
   `objects.json` library (`events/`, `outbox/`, `ws/device/`, `ws/sse/`) and one thin message file per kind/type that
   `$ref`s its definition. Bodies nobody else references (`credentials/`, `requests/`, `log/`) are written directly.
2. **Kind-restricted envelopes** live in `events/objects.json`: `event_other` (every kind but `session.terminal`,
   ≤ 256 KiB), `event_terminal` (≤ 64 KiB), `event_device` (device-authored kinds, the outbox item `event`) and
   `event_operation_params` (`params_event` of `POST /v1/device/operations`). Their headers copy the exact key set of
   `common.json#/$defs/event_header` and differ only in `kind` (tested). `common.json` is not changed.
3. **Session event bodies are the journal bodies** (03 section 10): `events/objects.json#/$defs/body_session_*` are
   `$ref`s to `guest/journal/record.json` (CTR-02). `operation.params` binds `type` to `ops/objects.json` params,
   `platform.read` binds `tool` to the MCP tool input of `mcp/objects.json` (the eleven read tools; `request_operation`
   is not a read).
4. **Live channels**: `ws/INDEX.json` lists the query schema and every frame type per direction. SSE frames are
   described as `{event, data}` (the wire form is `event: <type>\ndata: <json>`), device frames as the JSON object
   `{type, …}` of 03 section 6.2. `INDEX.json` rows of the streams name the query schema as `request` and the frame
   union from the hub as `response`; the device heartbeat is `ws/device/heartbeat.json`.
5. `events/kinds.json` repeats per kind the scopes and author kinds of `enums/event-kinds.json`, the ciphertext limit
   (E8) and whether `GET /v1/device/pull` delivers it (all but `session.terminal`); the test keeps it equal to the
   matrix (CON-03).

## Device API (03 section 6.1)
6. `POST /v1/device/enroll`: `oneOf` org device (`entry` = accept entry with `sig` and `inv_sig`, no `enroll_sig`)
   and platform device (`enroll_sig` + `inv_sig`, no `entry`). `capacity` is the device capacity of 02 section 2.1
   (`common.json#/$defs/capacity`); `os`/`arch` are short lower-case tokens (`macos`, `arm64`). Response: `active`
   with `org_id` or `pending_certification` without.
7. `GET /v1/device/state`: exactly two variants. `last_origin_seq` 0 = nothing stored yet (origin_seq starts at 1).
   `rotation_pending` = org IDs with `rotation_pending` (the specification writes `[…]`). `resume_from` is optional,
   all other session fields required (`parent_session_id`, `model_id_pref` nullable). `pings[].level` is null for
   `subagent.finished`/`operation.finished`; `ref` is the entry, child session or operation ID.
   `platform_budget.spent_today_by_org` maps org ID → USD (number) like `usd_per_org_day`.
8. `POST /v1/device/outbox`: 1–500 items; the 4 MiB and 250 ms limits are hub checks. Result entries: `rejected`
   requires `error` (a hub error code), the other statuses carry none.
9. `GET /v1/device/pull`: `cursor` is repeatable (array of `scope_id:seq`); response `{events:[{scope_id, scope_seq,
   received_at, event}], more}` – `more` tells the runner to pull again (the specification says "until empty").
   Events are `event_other`: a pull never carries `session.terminal`.
10. `GET /v1/device/tasks/{task}` operations use the field names of 03 section 6.1 literally (`id`, not
    `operation_id`); sessions are full `session_row`s.
11. `GET /v1/device/logs`: `org` required. Both log responses have the shape of the browser log responses.
12. `GET /v1/device/requests/{id}` → `{envelope}`; `POST …/responses` → `{}`. The request state is not repeated
    (the runner keeps its own expiry, 05 section 11.1).
13. `POST /v1/device/operations` (03 section 8.3): request `oneOf` `local_check` `passed` (no `error_code`) /
    `rejected` (`error_code` required); `dedupe` is `{epoch, tag}` with the 32-byte HMAC tag (04 section 4.10) or
    null. Response variants: `accepted` (optional `child_session_id`), `duplicate` (`duplicate_of`, `original`
    `{state, result_event_id?}`), `denied`/`rejected` (`error_code`); every variant may carry `replayed` (03 section 1
    idempotency, `operation_id` is the client ID).

## Outbox bodies
14. `session_transition`: `failed` requires a session failure reason; other states carry a code reason or null.
15. `operation_transition`: `executing`; `succeeded` and `failed` require `result_event_id` (the runner writes
    `operation.result` first, 03 section 9 "Result delivery"); `failed` also `error_code` + `retryable`; `expired`
    with `hub_unreachable` or `timeout` and an optional result event.
16. `metrics` samples are the `api/objects.json` metric sample plus `entity_kind`/`entity_id` (device/session UUID or
    `vm-…`). `quota.used_percent` 0–1000 (overuse is reported, not clamped).
17. `audit`: one `oneOf` variant per action of `enums/outbox-audit-actions.json`; the detail schema of
    `audit/<action>.json` (CTR-02) is **inlined** (a top-level schema cannot be referenced, `$ref` only to `$defs`) and
    a test keeps the copy equal to the source. `target_id` is an ID-like token (`[A-Za-z0-9_.:-]`, ≤ 128), never a path
    or text, so the item schema alone keeps content out of the clear audit row (review finding).

## Bodies
18. Event bodies: names ≤ 200 characters, descriptions ≤ 4096, `member.profile.display_name` and
    `device.profile.label` ≤ 100, `task.meta.title` single line ≤ 120 (as `task.set_title`), prompt text ≤ 64 Ki
    characters (like whiteboards; well inside the 256 KiB ciphertext limit), `attachments` exactly `[]` (reserved).
    `settings.workgroup`: lower-case `owner/name` (≤ 500 entries), branch patterns `[A-Za-z0-9._/*+-]` of ≤ 200
    characters (≤ 100 entries). Text limits are in characters (the whitelist has no byte rule); the producer also
    keeps the sealed body within E8 (`size_exceeded` otherwise), so 64 Ki characters of 4-byte text can be refused.
    `idempotency_key` is the MCP definition (`mcp/objects.json`).
    `session.status`: `failed` with a failure reason, otherwise a code; optional flat `details`.
    `operation.result`: `succeeded` with `result`, `failed|rejected|denied|expired` with the operation `error`.
19. Credentials: tokens and keys are printable ASCII without spaces (≤ 4096); `claude_subscription.email` is required
    and nullable (05 section 11.2: only when the CLI reports it); `mailbox.imap_password` any text ≤ 1024.
20. Device requests: `github.connect {}`, `claude.login {mode}` (`manual` = setup-token relay, `automatic` =
    mailbox automation) or the follow-up `{code}`, `credential.check {kind}` (`anthropic_api_key` | `claude_subscription`),
    `mailbox.check {}`. The request `mode` decides: `automatic` runs the mailbox automation only on a device with
    `LOGIN_AUTOMATION=on` and an owner `mailbox` credential; otherwise the runner falls back to the relay and says so
    with `authorize {url, automation:"failed"}` – the only automation-failure signal (`login_automation_failed` is a
    client-only code shown by the web). `failed.code` is the closed list `requests/objects.json#/$defs/failure_code`
    (`timeout`, `busy`, `credential_missing`, `code_invalid`, `login_cli_failed`, `token_invalid`, `upstream_error`,
    `upstream_unreachable`, `device_shutdown`); the web catalog maps them. Responses by `step`: `device_code`/`done {login}`/`failed {code}`;
    `authorize {url, automation?:"failed"}`/`progress {stage}`/`done {email?}`/`failed {code}`; checks
    `done {valid}`/`failed {code}`. URLs are `https` or `http` on the loopback host only (hermetic doubles), so the web
    never renders a `javascript:` or foreign plain-http link. Responses never contain a secret.
21. Log files: one schema per membership type (13) and platform type (8), thin over `common.json` bodies (`invite`
    = user or device body; `inv_sig` exactly on `accept`). The invitation package is the plaintext of 04 section 4.6
    (`keys` ≥ 1, scopes `org`/`wg` only). The session statement is the five-line text of
    `agent_core.passkey.certificate` (origin `http(s)://host[:port]`, 65-byte point starting with 0x04, 32-byte hub
    value, time without milliseconds) as one anchored pattern.

## Known limits of the schema subset
22a. Maps keyed by IDs (`wg_names`, `delivered_prompt_event_ids`, `spent_today_by_org`, `heads`) cannot constrain
    their keys or size (no `propertyNames`/`maxProperties` in the whitelist); producers and the reference
    (`agent_hub.crypto.invite.check_package`) check the keys. `GET /v1/device/pull?kinds=` is a syntactic list like
    `GET /v1/scopes/{scope_id}/events`; unknown kinds simply match nothing. The org device's `accept` entry is the
    generic accept body; `kind = "device"` and `subject = device_id` are hub checks. Open code texts remain where the
    specification lists no values: non-failure transition reasons, quota `severity`.
22b. Device-authored event headers are pinned to `scope: "wg"` with a set `wg` (04 section 4.3: all of them are
    workgroup kinds).

## Live channels
22. Hints carry only IDs and sequence numbers (tested). `session.control.action` is `interrupt|stop` (the browser
    actions; `suspend` is runner-internal). `ping` has one variant per level (`org`: no wg/task, `workgroup`: wg,
    `task`: wg and task). `log {org_id, seq}` only for membership logs; platform-log changes reach devices through
    `reconcile`.
23. SSE: `task`/`operation`/`notification` data are the rows of `api/objects.json`; `session` = `{session,
    transition|null}`; `metrics` = `{samples:[{entity_kind, entity_id, sample}]}`; `resync.reason` ∈
    `buffer_overflow`, `replay_limit` (more than 1000 events after a cursor), `hub_restart`. Query: `org` required,
    `terminal` 1–3 session IDs, `cursors` comma list (≤ 256), `monitoring` only `"1"`.

## Vector defects fixed (ADR-027 section 3, `contracts/changes/CTR-03.md`)
24. The CRY-01 vectors were not valid instances of the specified formats: the enrollment body used a hardware
    capacity `{cpus, memory_mib}` instead of the device capacity of 02 section 2.1, and the `prompt` event body lacked
    `attachments: []` (03 section 10), and the first `claude.login` request body was `{}` instead of
    `{"mode":"manual"}`. The change is in the generator source `src/agent_hub/crypto/vectors.py` (CRY-01) and named in
    `contracts/changes/CTR-03.md`. `agent_hub.crypto.vectors` now builds both as specified and
    `contracts/vectors/{enroll,event,request}.json` are regenerated; the cryptographic rules are unchanged.
25. The membership-/platform-log vectors of CRY-02 are not merged yet; their schema check is added together with them
    (the files `log/membership/<type>.json` and `log/platform/<type>.json` are ready).

## Generation
26. Schemas and fixtures were produced by a scratch generator and reviewed as files; fixtures follow CTR-01 (valid
    `basic.json` + variants, derived `missing-…`/`extra-field`/`wrong-type-…` + targeted invalid cases).
