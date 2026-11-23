# GST-07 – contract changes

ADR-027 section 3 (defect fixes). Consumers: the guest MCP server (`tools/list`, argument check) and the runner's
answer to `platform.query` (RUN-08, not yet implemented).

## `mcp/objects.json#/$defs/input_get_whiteboard`: plain object instead of `oneOf` (defect)

`tools/list` hands the dereferenced input schema of every tool to Claude Code, which passes it to the model API as the
tool's `input_schema`. The model API refuses `oneOf`, `anyOf` and `allOf` at the top level of a tool input schema, so
the two-branch `oneOf` of `get_whiteboard` would make every model request of a session fail once the tool is
registered (GST-07). The definition is now a closed object `{level: org|workgroup|task, task_id?: uuid}` with
`level` required. The rule "`task_id` exactly for level `task`" (03-api.md section 8.1) is checked by the guest
(tool error `params_invalid`, nothing forwarded) and by the runner; the description of the definition says so.

The same definition is referenced by `guest/journal/record.json` (`platform.query`), `events/objects.json`
(`platform.read`) and `mcp/tools-call.params.json`; they accept the same wider set. The guest never forwards a call
that breaks the rule.

Fixtures: `invalid/org-with-id.json` and `invalid/task-without-id.json` are removed (schema-valid now, refused by the
implementations and tested there); new `valid/org.json`, `invalid/level-unknown.json`, `invalid/task-id-invalid.json`.

`tests/platform/contracts/test_ctr02.py`: every tool input definition is a closed plain object; the whiteboard rule is
documented as left to the implementations.

## `guest/journal/record.json#/$defs/body_operation_request`: `local_check`, `local_error` (additive)

05-runner.md section 12 step 1 forwards a request the guest found invalid "with `local_check:"rejected"`", but the
journal body had no field for it. New optional fields:

- `local_check` (`passed` | `rejected`): the result of the guest's schema check of `type` against `ops/INDEX.json` and
  of `params` against `ops/<type>.params.json`. `rejected` is binding for the runner: it registers the operation at the
  hub with `local_check:"rejected"` and never executes it, because the session was already told `rejected`. A guest
  rejection can only make the outcome stricter. `passed` is a hint only; the runner repeats the formal checks on the
  plaintext (03-api.md section 9).
- `local_error` (`ops/objects.json#/$defs/error`): the error the guest returned (`unknown_type`, or `params_invalid`
  with pointers), sent together with `rejected`.

Fixtures: valid `local-rejected`, `local-passed`; invalid `local-check-unknown`, `local-error-code-unknown`.
`test_ctr02.py` lists both fields as additions to the specified body.
