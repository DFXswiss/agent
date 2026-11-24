# CTR-02 decisions (contracts II: guest control protocol, journal, MCP, operations, audit details, model catalog)

Small decisions taken while writing `contracts/guest/`, `contracts/mcp/`, `contracts/ops/`, `contracts/audit/` and the
model catalog from `03-api.md` sections 7–9 and 11, `05-runner.md` sections 5, 8, 9, 10.5, 12 and `02-data-model.md`
sections 2.9 and 5. Conventions of CTR-01 (`contracts/README.md`, `decisions/CTR-01.md`) apply unchanged.

## Layout and indexes
1. **Own indexes instead of `api/INDEX.json`.** `api/INDEX.json` lists the hub's HTTP endpoints (signed requests, read
   log, rights). The guest control protocol is another transport (unix socket, bearer control token) and even reuses
   the path `GET /v1/health`, so its endpoints are listed in `guest/INDEX.json`; journal kinds in
   `guest/journal/INDEX.json`, MCP methods/tools/hooks in `mcp/INDEX.json`, operation types in `ops/INDEX.json`. The
   audit details need no index: the file name is the action (`audit/<action>.json`). `api/INDEX.json` is unchanged
   (`POST /v1/device/operations`, 03 section 8.3, belongs to CTR-03).
2. **Libraries plus thin message files.** Reused bodies live once in `guest/objects.json`, `mcp/objects.json`,
   `ops/objects.json` (and the journal bodies in `guest/journal/record.json#/$defs`); every message file is a
   `$ref` to its definition, because references must point at a named `$defs` entry (CTR-01 rule). Thus the journal
   record binds `kind` to its body (`oneOf` of one variant per kind), `tools/call` binds `name` to the tool's
   arguments, `platform.query` binds `tool` to its arguments.
3. **List results are objects** (CTR-01 decision 7) also for the MCP tools that 03 section 8.1 writes as arrays:
   `{"workgroups":[…]}`, `{"members":[…]}`, `{"tasks":[…],"next_before"}`, `{"messages":[…],"next_after"}`,
   `{"entries":[…]}`, `{"notifications":[…]}`, `{"operations":[…]}`.

## Guest control protocol (03 section 7.1)
4. **Errors** use the hub's body shape `{error, detail?}` with a closed guest list: `request_invalid`, `unauthorized`,
   `not_found`, `already_assigned`, `not_assigned`, `id_conflict`, `state_conflict`, `size_exceeded`, `path_invalid`,
   `sandbox_failed`, `unavailable` (`guest/error.response.json`).
5. **Health** has three exact variants: `unassigned` (no IDs), `assigned` (org and wg), `broken` (with or without IDs).
   `boot_id` and `claude_session_id` accept UUIDs of any version (Linux and Claude Code generate v4).
6. **`POST /v1/sessions`**: `model.env` is closed to the model variables of 05 section 10.3 (`ANTHROPIC_MODEL`,
   `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`,
   `CLAUDE_CODE_MAX_OUTPUT_TOKENS`); every other variable is fixed by the guest, so the runner cannot inject one.
   `limits`: `memory_max` bytes, `cpu_quota` percent of one CPU (systemd `CPUQuota`), `tasks_max`, `runtime_max_sec`.
   `depth` 0–2 (ADR-017). Prompt texts ≤ 256 Ki characters (event limit).
7. **Credential provisioning**: `auth_header` ∈ {`x-api-key`, `authorization`}; `auth_value` printable ASCII without line
   breaks (no header injection), ≤ 16 KiB; `extra_headers` a map of printable values.
8. **File export** (03 section 7.1 names a manifest and `X-Manifest-Sha256` but no format): the tar stream's **first
   member** is `.agent-export-manifest.json` = canonical JSON of `guest/session-files-export.response.json`
   (`{files:[{path,size,sha256}], skipped:[{path,reason}]}`, reasons `symlink`, `special`, `unreadable`); the header is
   its `sha256_hex`, so a truncated or altered stream is detected. The runner removes the member before use.
   Import query: `dir` (workspace-relative) and `replace` as text `true|false`.
9. **Journal long poll**: `limit` 1–1000 (like logs), `wait` 0–25 s as text.
10. **Pending answers**: `{status:"ok", result}` with the tool's output schema (any of the twelve) or
    `{status:"error", error}` with the MCP tool error.

## Journal (03 section 7.2, 02 section 5)
11. `metrics` is a journal kind in 02 section 5 without a body in 03 section 7.2: its body is `{at, vm, sessions}` with
    the fields of `GET /v1/metrics`. `metrics` and `guest.error` are VM-level kinds (`session_id` may be null); all
    other kinds require a session.
12. **Additions**: `session.state.harness_version` (05 section 8 says it is journaled in `session.state`),
    `session.output.truncated` (assistant text is cut at 64 Ki characters to stay inside the 256 KiB event limit),
    `session.hook.rule` (the runner needs the rule number for the audit item `hook.denied`, 05 section 5.3).
13. `session.state` variants: a session state with optional code `reason`; `failed` with a reason of
    `enums/session-failure-reasons.json` (required); `prompt_accepted {prompt_id}`; `prompt_refused {prompt_id, reason}`
    with reasons `unregistered` (UserPromptSubmit found no registered hash) and `delivery_failed` (05 section 6.2).
14. `session.file`: `delete` has `sha256` and `size` null. `session.terminal` frames are `[t_ms, code, data]` with code
    `o` or `m` (05 section 4.6 item 5 marker); the whitelist has no `prefixItems`, so the position of each element is
    a producer rule (items are `anyOf` integer/code/text, exactly three); the 16 KiB per record is checked by the
    producer.
15. `guest.error.code` is a closed list: the two codes the specification names (`subagent_hook_seen`, `gateway_route`)
    plus `hook_handler_failed`, `transcript_unreadable`, `recorder_failed`, `scanner_failed`, `journal_write_failed`,
    `pending_expired`, `session_unit_failed`. New codes are additive changes (ADR-027 section 3).
16. `operation.request.params` is any object: the guest forwards unknown types and invalid parameters (05 section 12
    step 1) and the runner records them as `rejected`.

## MCP (03 section 8)
17. **Tool errors** (`isError`): `{error, message}` with the closed codes `params_invalid` (argument schema),
    `not_found`, `forbidden` (outside the read scope of ADR-024), `unavailable` (runner/hub not reachable), `timeout`;
    `message` is a printable ASCII sentence. An unknown tool name is the JSON-RPC error `-32602`; the JSON-RPC error
    codes are the five standard ones.
18. `request_operation`/`get_operation` result is a `oneOf` by status: `succeeded` (result), `duplicate`
    (`duplicate_of` + original result), `failed|rejected|denied` (error), `pending` (optional `duplicate_of`).
    Operation errors are `{code, message?, pointers?, retryable?}` (`ops/error.json`).
19. `get_whiteboard` requires `task_id` exactly for level `task`; `get_subagent_result` returns `final_text` exactly in
    state `stopped`; `list_workgroups.my_access` is null for workgroups the task owner has no access to.
20. `initialize`: params closed except `clientInfo` (third-party, see 22); result capabilities exactly
    `{tools:{listChanged:false}}`. `tools/list` returns exactly the twelve tools.

## Hooks (03 section 8.2, 05 sections 5.2–5.5)
21. **Responses are exact**: PreToolUse `allow` (no reason) or `deny` with a fixed reason; optional notice on both.
    UserPromptSubmit `{}`, notice, or the block with the verbatim reason (never with a notice). PostToolUse `{}` or
    notice. Every other event: `{}`. The notice is a pattern: count 1–999999, kinds distinct in the fixed order
    `whiteboard.entry, subagent.finished, operation.finished`, separated by `, ` (`mcp/hook-deny-reasons.json#/notice`).
22. **Hook requests are open third-party input.** If the guest refused a body because Claude Code added a field, the
    hook would fail and Claude Code would continue (fail-open, 05 section 5.5) – for UserPromptSubmit that would skip the
    prompt-origin check. The schemas therefore check the listed fields (common fields and per-event fields known for the
    pinned version) and ignore unknown ones: `additionalProperties: {}` with `"$comment": "open: …"`. The harness
    accepts such objects only with that marker; `test_ctr02.py` pins the places (the 18 hook requests and MCP
    `clientInfo`). `contracts/README.md` documents the rule.
23. **Deny sentences** (05 section 5.3 gives only rule 2 verbatim): one fixed English sentence per rule outcome in
    `mcp/hook-deny-reasons.json` (`tool_forbidden` template with the tool name if it matches `[A-Za-z0-9_.:-]{1,128}`,
    else `tool_forbidden_unnamed`; `pattern_outside_workspace`; `path_unresolvable`; `write_outside_workspace`;
    `write_protected`; `read_outside_workspace`). Rule 1 and 7 allow. The schema enum equals the file (test).

## Operations (03 section 9)
24. **Dedupe fields** are exactly the normalised field sets of the crypto reference (`agent_hub.crypto.dedupe`, CRY-01:
    `session`, `ref`, `parent_sha`, `tree_sha`, `body` = sha256, `prompt` = sha256(purpose ‖ 0x00 ‖ prompt)); a test
    keeps `ops/dedupe-fields.json`, the reference and `vectors/dedupe.json` equal. Windows in seconds. With an
    idempotency key the window is that of the type; a type without dedupe fields uses 86400 s.
25. **Branch names** follow `git check-ref-format --branch` completely, which is what 03 section 9 refers to: besides
    the listed rules also no space, no `//`, no `/.`, no leading `.`, no trailing `.`, not `@` or `HEAD`. The schema
    refuses the forbidden characters and first characters; sequence rules are data (`ops/rules.json#/branch`) with
    valid/invalid examples and a reference reading in the test. Refs (`checkout.ref`, `create_branch.from`) are branch
    or tag names or 7–40 hex digits.
26. **Paths** (`commit.paths`, journal paths): relative, no empty/`.`/`..` segment, not `.git` or under `.git/`, no
    control character, no backslash, ≤ 4096 characters (`ops/rules.json#/path`).
27. **Protected-branch patterns** match like fnmatch with `*` not crossing `/` (`release/*` protects `release/1.2`, not
    `release/1.2/hotfix`).
28. **Secret patterns** are search regexes in `ops/rules.json#/secret_patterns`; their examples are stored as fragments
    joined by the reader so that the public repository contains no literal token (push protection, hygiene).
29. Results: `get_pull_request` has a required `truncated` flag (03 section 9 "flagged truncated"), GitHub timestamps
    without milliseconds, logins with an optional `[bot]` suffix, `body` null allowed; `create_pull_request.existing`
    is only ever `true`. `task.set_title` title: 1–120 characters without CR/LF.

## Audit details (02 section 2.9)
30. One schema per action of `enums/audit-actions.json` (133). Every detail is a **flat** object of strings (printable
    ASCII ≤ 256), integers, booleans or null, so it always fits the generic `detail` of the audit row
    (`api/org-audit.response.json`, unchanged); a test validates every valid fixture against that generic schema.
    Contents are metadata only: e.g. `hook.denied {tool, rule}` (never the path), `operation.<state> {type,
    error_code, duplicate_of, local_check}` (never parameters), `event.append {kind, scope, scope_seq, ct_bytes}`,
    `read.<resource>` `{}` (the target is in `target_kind`/`target_id`), `read.session.<tool> {result_count}`.

## Model catalog (05 section 10.5)
31. `contracts/model-catalog.json` is the schema, `contracts/model-catalog.example.json` the example (data, no
    `$schema`). Access keys are the `model_access` values `platform`, `api_key`, `subscription`; the platform default is
    required, the others may be null (Claude Code default). Prices are USD per million tokens. The example lists
    `deepseek/deepseek-v4.1-flash-1017` (1 048 576 context, 393 216 output, 0.15/0.60/0.003, cache write = input price,
    from the OpenRouter model list of the model-access spike) and the current Claude models (1 000 000 context, 128 000
    output, list prices including cache write = 1.25 × input). Rules a schema cannot state (allowed ⊆ models, default ∈
    allowed) are checked by the test.

## Harness
32. The CTR-01 harness gets `is_open_input`/`open_objects` (decision 22) and the mutation test validates the extra
    field instead of refusing it for such roots. Everything else is discovered automatically.
33. Ajv strict mode accepts all new schemas; the files were produced by a workspace-internal generator and are
    committed as the authoritative source (the schemas, not the generator, are the contract).

## After the independent review
34. **Existing pull request** (03 section 9): MCP `status:"duplicate"` without `duplicate_of` is valid when the executor
    found an open PR (`result.existing:true`); `duplicate_of` is optional in the duplicate variant.
35. **Credential provisioning** (amends 7): `upstream_base_url` is `https://…` or `http://` to a loopback address
    (doubles in tests); `extra_headers` is a list of `{name, value}` with lower-case token names (no CR/LF injection),
    because the keyword whitelist has no `propertyNames`; the guest refuses the reserved names of
    `guest/INDEX.json#/reserved_headers` (authorization, x-api-key, host, …).
36. **Hook bodies fail closed** (amends 22): hook request schemas have no upper length limits (a long prompt or tool
    name never makes a body invalid). If a body still does not match its schema, the guest answers PreToolUse with
    `deny` and the reason `input_invalid` ("The tool call could not be checked and is denied.", rule 4),
    UserPromptSubmit with the block, every other event with `{}`; it never answers with an error.
37. **Paths**: a `.git` segment is refused anywhere in the path and case-insensitively (`sub/.git/config`,
    `.GIT/hooks/x`); the guest applies the same segment rules to `files/import` and `files/export` paths.
38. **Branches**: `.lock` is refused at the end of every component (`a.lock/b`), as git does.
39. **Secret patterns** keep the prefixes of 03 section 9 and require at least 16 token characters after the prefix
    (shorter strings are documentation naming a prefix, not a credential).
40. **Notice** pattern lists the seven ordered kind combinations exactly (no repetition, fixed order).
41. **`request_operation.type`** and `operation.request.type` are any text of 1–128 characters, so that even a malformed
    type is forwarded and recorded as `rejected` (`unknown_type`, 05 section 12 step 1).
42. `platform.query` never carries `get_notifications` (answered locally by the guest, 05 section 9). Task titles
    exclude all control characters and U+2028/U+2029.
43. Not changed (recorded): cross-field consistency (`session.hook` event/decision/rule, `depth` vs
    `parent_session_id`) and the position typing of terminal frames are producer rules (no `if`/`prefixItems` in the
    whitelist). `session.status` (03 section 10) is an event kind the runner derives from `session.state failed` and
    `StopFailure` hook records; no own journal kind. The guest-local closed lists (guest error codes, `guest.error`
    codes, MCP tool error codes) stay in the schemas because they never reach the hub or the web (03 section 11 lists
    hub-visible codes); `test_ctr02.py` pins them through fixtures.
