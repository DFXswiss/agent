# GST-03 decisions (hooks, PreToolUse policy, prompt origin)

Small decisions of the hook package (05-runner.md sections 5.2–5.5, 4.6 nos. 2 and 7; 03-api.md sections 7.1, 7.2,
8.2; ADR-008, ADR-020, ADR-025; ADR-027 section 4; CTR-02 decisions 21–23, 36, 37).

## Where the decisions are made
1. **The policy runs in the control process (root), not in the API process.** The session directories are owned by
   the session user with mode 0700; the API process (user `platform`) cannot look into them, so `realpath` there would
   silently stop at the first unreadable directory and miss every symlink in the workspace (ATK-34). The API process
   authenticates, parses and validates the hook body and sends only the keys the policy reads (`file_path`, `path`,
   `pattern`, `glob`) over `api.sock` (op `hook.pre_tool_use`); the control process evaluates, journals and answers.
   Bodies above 1 MiB of policy input are denied (`input_invalid`) instead of being sent.
2. Ops added in this package (`control_routes/hooks.py`, `control_routes/prompts.py`): `hook.pre_tool_use`,
   `hook.post_tool_use` (only `Write`/`Edit` bodies, so a compromised API process cannot forge transcript records of
   other tools), `file.changed`, `prompt.check`. `session.hook` and `guest.error` of the other events go through the
   existing `journal.append` op (GST-01 decision 10).

## Policy (05 section 5.3)
3. **Three path candidates.** Claude Code may open a path as given (kernel: physical resolution) or normalise it
   lexically first (Node `path.resolve`) and let the kernel follow symlinks afterwards. Rules 5 and 6 require the
   lexical form, `resolve(path)` and `resolve(lexical(path))` to be inside. Example: `chain/../../../x` is inside
   physically but outside lexically and is denied.
4. **Resolver.** `policy.resolve` follows symlinks per component like `os.path.realpath` (at most 40 links, the
   kernel's limit). A component that does not exist ends the physical walk; the rest is appended as text (deepest
   existing parent, 05 section 4.6 no. 2); a later `..` resumes the physical walk. Dangling symlinks are followed to
   their target. Every other error (permission, loop, file used as directory, name too long, `readlink` failure)
   makes the path unresolvable → deny rule 4. `os.path.realpath` itself is not used because its non-strict mode
   ignores such errors and would treat an unreadable symlink as a plain directory.
5. **Hard links (ATK-35).** An existing non-directory with `st_nlink > 1` (checked on both resolved candidates) is
   treated as outside (rules 5/6): its other
   names cannot be found, and the session cannot create hard links itself (no exec; `fs.protected_hardlinks`). This
   also denies a file hard-linked twice inside the workspace (deterministic, rare).
6. **Strict vs. inclusive.** Write/Edit need a path strictly below the workspace (the workspace directory itself is
   not a file target); Read/Glob/Grep may name the workspace itself. The own harness file is allowed for reads only
   if every candidate equals it (a symlink in the workspace to the harness is denied – the harness is passed to
   Claude Code at start anyway).
7. **Protected names are case-insensitive and segment-based** (CTR-02 decision 37): any segment below the workspace
   equal (casefold) to `.claude` or `.git` – also as the last segment (a `.git` file is a gitdir pointer) – or to
   `.claude.json`, `.mcp.json`, `CLAUDE.md`, `.gitconfig`. Reads of these names inside the workspace stay allowed
   (rule 6 has no protection list).
8. **Values.** A path value that is not a non-empty string, contains NUL, starts with `~` or has surrounding
   whitespace (JavaScript `trim()` set including U+FEFF, or Python `strip()`) is unresolvable: Claude Code trims
   paths before use and may expand `~` to the session home, so ` /etc/passwd` or `CLAUDE.md ` must never be read as
   a relative or unprotected name (review H1). Glob/Grep patterns with surrounding whitespace are denied by rule 3.
   A name the file system cannot represent (lone surrogate) is unresolvable. With both `file_path` and `path`
   present each is checked. Missing keys default to the workspace.
9. **Patterns (rule 3).** Besides a leading `/` or `~`, a `..` segment and NUL, brace alternatives that start with
   `/` or `~` (`{/etc,x}`) or are `..` (`{a,..}`) and backslash-escaped variants (`\.\./`, `\/etc`) are denied. This
   may deny a few harmless patterns such as `a{,/b}`; the rule stays deterministic. A non-string pattern is denied by
   rule 3. Grep's `pattern` is a content regex and not checked.
10. Deny texts and the block reason are constants in `policy.py`; a test keeps them equal to
    `contracts/mcp/hook-deny-reasons.json` and the schema `mcp/objects.json#/$defs/deny_reason`. The platform tool list
    (rule 1) is checked against `mcp/INDEX.json`. Allow answers carry no reason (schema).

## Journal and audit
11. PreToolUse: `session.hook {event, tool, decision, rule, reason?, path?}` with the physical path (journal only;
    omitted above 4096 characters). The tool name is journaled only when it matches `[A-Za-z0-9_.:-]{1,128}` (the
    schema pattern); otherwise it is left out and the deny reason is the unnamed sentence. The runner (RUN-04) turns a
    deny into the audit item `hook.denied {tool, rule}` – never the path. An unnamed tool has no `tool` field; RUN-04
    must use a placeholder there.
12. Other events: `session.hook {event, tool?, decision: "none"}`; an unreadable body adds `reason: "hook body
    invalid"`. `SubagentStart` adds `guest.error subagent_hook_seen`.
13. PostToolUse `Write`/`Edit` journal `session.tool` (05 section 5.5): input = JSON of `tool_input` (sorted keys),
    result = `tool_response` text, each cut at 64 Ki characters; `input_summary` = `file_path` (≤ 256); `is_error`
    false (failures arrive as `PostToolUseFailure`); `duration_ms` from the body when it is a valid count, else 0. Only
    with a valid `tool_use_id`. **Interface for GST-05:** the transcript reader must not journal a second
    `session.tool` for a `Write`/`Edit` `tool_use_id` already recorded by the hook.
14. **File hint `file_changed(session, path)`** (`hooks.file_changed` → op `file.changed`): queues the absolute path
    (relative paths joined to the workspace) in `control_routes.hooks.FILE_HINTS` (per session, ordered, without
    repeats, at most 1024). **Interface for GST-05:** the scanner (or its `hook_addons/file_changes.py` through its
    own op) drains it with `FILE_HINTS.take(session_id)`, reads the files as root and journals `session.file`.

## Prompt origin (05 section 5.4)
15. **`prompt_registry.prompt_id` (schema change).** `prompt_accepted {prompt_id}` (03 section 7.2) needs the ID of
    the accepted prompt, which 02 section 5 does not store. `schema.sql` gets `prompt_id TEXT NOT NULL` as part of
    the primary key (`session_id`, `prompt_id`) and an index on (`session_id`, `sha256`, `used_at`); `user_version` stays 1 because no golden image with the old schema was released
    (the golden-cache hash covers `src/agent_guest/`, so every image is rebuilt).
16. Registration rules: same `prompt_id` + same normalised text → idempotent (runner retry); same `prompt_id` + other
    text → `409 id_conflict`; an already accepted `prompt_id` → `409 state_conflict` (never accepted twice). Rows are
    keyed by (`session_id`, `prompt_id`) and never replaced (an index on the hash serves the check), so a repeated
    prompt text registered under several IDs is accepted once per registration, oldest first, and an accepted ID can
    never be re-armed. Unknown session `404`, stopped/failed `409 state_conflict`.
17. `normalise` also unwraps when only surrounding whitespace is outside the `<pasted_content>` wrapper (the TUI may
    add a line break); the match uses `fullmatch`, so `$` never matches before a trailing newline. Registration and
    check hash the same normalised text. Hook bodies with lone surrogates are unreadable and fail closed (review H2);
    the hash itself uses `surrogatepass` so a registration never crashes.
18. `prompt_refused` needs a `prompt_id` (schema), but a refused text has none: a fresh UUIDv7 is used. Acceptance
    sets the guest session state to `working` and journals only `prompt_accepted` (RUN-04 maps it to the `working`
    transition; a separate `session.state working` record would duplicate the transition).

## Hook transport and failure behaviour
19. One route per event with a raw body: the handler reads at most 16 MiB and parses strictly. A body that is too large,
    not JSON, not encodable text or not matching its request schema fails closed (CTR-02 decision 36); a too large
    body up to 64 MiB is read and dropped so that the client still receives the answer, beyond that the connection
    is closed. Authentication comes first: a missing/unknown token answers `401` before the body is read (counted in
    `STATS.unauthorized`). Requests without `Content-Length` or with `Transfer-Encoding` are refused by the shared
    HTTP layer (`400`) before any hook logic; Claude Code always sends a length.
20. **Failures of PreToolUse and UserPromptSubmit fail closed** (review M1): if the control process is unreachable or
    an op fails (journal unavailable, oversized message), the answer is deny `input_invalid` / block, not an error.
    Every other event answers `503` (fail-open by Claude Code; layers 1 and 2 hold). Counted in `hooks.STATS` and
    journaled as `guest.error hook_handler_failed` when the journal is reachable; the runner derives
    `agent_runner_session_hook_errors_total` from these records.
21. **Fault switch.** `AGENT_GUEST_FAULT_HOOKS=500` is effective only when the golden's install record
    `/var/lib/agent-golden/install.json` (written by `images/provision/install.sh`) has `"test_mode": true`; a
    production golden (`false`, missing, unreadable) ignores it (tests prove it). In fault mode every authenticated
    hook answers `500 {"error":"unavailable"}` and journals `session.hook {decision:"none", reason:"fault injected"}`
    plus `guest.error hook_handler_failed` (evidence for ATS-01). New configuration names in `config.py` (GST-01 file,
    minimal addition because unknown `AGENT_GUEST_*` names refuse the start): `AGENT_GUEST_FAULT_HOOKS` (empty or
    `500`) and `AGENT_GUEST_GOLDEN_INSTALL_FILE`. **Interface for ATS-01:** set the variable in a drop-in of
    `agent-guest-api.service` in a TEST_MODE golden.

## Hook addons (ADR-027 section 4)
22. `hook_addons/<name>.py:ADDON` is a `hooks.HookAddon(events, handle)`; `handle(api_context, HookCall)` returns an
    optional text. Addons run after the decision, in module order, for the events they list (`Stop` included, for
    GST-06/RUN-11). Only PreToolUse, PostToolUse and an accepted UserPromptSubmit (`call.decorate`) attach the text as
    `additionalContext`; elsewhere a text is dropped and reported, so an addon (pings, RUN-14) must not mark anything
    delivered when `call.decorate` is false. Several texts are joined by a blank line; an answer that would violate
    its response schema is sent undecorated. A raising addon is logged, journaled (`guest.error hook_handler_failed`)
    and skipped; the decision never changes. A malformed `ADDON` refuses the API process start (`registry_invalid`).

## Residual points (review, low)
23a. Glob/Grep on an allowed directory do not check hard links or symlinks of the files below it; the session cannot
    create links (no exec, Write creates regular files, file import refuses links), and layer 2 still applies.
23b. ATK-36: unknown-token calls are counted but neither journaled (no session to attribute them to; no guest error
    code exists for it) nor rate-limited: only processes inside the VM reach port 7700 and sessions reach nothing
    else (nftables). Left to the attack suite (ATS-01) to judge.
23c. `file.changed` accepts only paths lexically inside the session workspace; GST-05 must still resolve them safely
    when reading as root.

## Shared files touched
23. `config.py` (decision 21), `schema.sql` (decision 15) and one assertion of `tests/platform/guest/test_server.py`
    (`test_registry_of_this_package` pinned the API routes to `GET /v1/health` only; it now checks containment, since
    every later package adds routes). No other GST-01 file changes.
