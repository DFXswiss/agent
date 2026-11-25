# KOR-44 – contract changes

ADR-027 section 3. Additive only (ADR-085, 13-work-vm.md section 13, 04-crypto.md section 4.13).

- `ops/browser.{open,close}.{params,result}.json`, `ops/INDEX.json` (`dedupe: false`): the operations `browser.open`
  `{url?, idle_seconds?}` and `browser.close` `{}` (`ops/objects.json` `browser_id`, `browser_url`,
  `browser_close_reason`, `params_browser_*`, `result_browser_*`, both results in `result`).
- `events/browser.action.json`, `events/browser.state.json`, `events/kinds.json`, `enums/event-kinds.json`, the kind
  enums of `common.json` (`event_kind`, `event_header_other`) and `events/objects.json` (`header_device`), the
  `operation.params` variants of both operations: the event kinds `browser.action` (a watched kind: `device_pull:
  false` like `session.terminal`; `header_browser_action`, `event_browser_action` for the stream frame) and
  `browser.state` (with `implicit`, `operation_id` null for an implicit open). Scope `wg`, author the device, right
  `device_assigned_wg_session`, ciphertext limit 256 KiB. The `browser.action` picture is a JPEG of at most 960 px
  width and 100 KiB decoded.
- `mcp/tools/browser_*.{input,output}.json`, `mcp/INDEX.json` (`via: workvm.call`, no read audit),
  `mcp/tools-call.params.json`, `mcp/tools-list.result.json`, `guest/journal/record.json` (`workvm.call` variants
  and the tool list of `platform.refused`): the fifteen browser tools `browser_navigate`, `browser_snapshot`,
  `browser_click`, `browser_hover`, `browser_type`, `browser_select`, `browser_press`, `browser_scroll`,
  `browser_wait`, `browser_evaluate`, `browser_upload`, `browser_dialog`, `browser_tabs`, `browser_download`,
  `browser_screenshot`; one answer `mcp/objects.json#/$defs/output_browser`, the target `browser_target`. "Exactly
  one of" (`browser_navigate`, `browser_click`, `browser_download`) and "at least one of" (`browser_wait`) are
  checked by the runner (`params_invalid`): a tool input may not be a top-level `oneOf`/`anyOf`.
- `common.json` `browser_frame` (04-crypto.md section 4.13; `ct` 29 bytes to 512 KiB + 28 bytes decoded).
- `api/device-browser-live.{request,response}.json`, `api/INDEX.json`: `POST
  /v1/device/sessions/{session}/browser/live` (device, `device:session`) `{frame?}` → `{until, kbps}`;
  `enums/error-codes.json` and `common.json` `hub_error_code`: `not_watched` (409).
- `ws/sse/query.json`: `browser` (repeatable, at most 3) and `browser_kbps` (100-10000). Stream frames
  `browser_action` and `browser_live` (`ws/sse/browser_*.json`, `ws/sse/objects.json`, `ws/INDEX.json`); device hint
  `browser {session_id}` (`ws/device/browser.json`, `ws/device/objects.json`).
- `audit/read.stream.browser.json`, `enums/audit-actions.json`: `read.stream.browser`, one row per watched session
  of an opened stream.
- `api/scope-events-get.{request,response}.json`: newest-first paging of `GET /v1/scopes/{scope_id}/events` with
  `before=<scope_seq>` (not together with `after`, `400 request_invalid`): the `limit` matching events below it in
  descending order and `next_before` (the smallest returned `scope_seq` while older ones exist, else null), so a long
  browser record is read page by page from its end. Without `before` the answer is unchanged.
- `vectors/browserframe.json` (new section of `agent_hub.crypto.vectors`); `vectors/log-events.json` with the two
  kinds.

## Compatibility

Hub first: an older hub refuses the event kinds (`kind_not_allowed`), the operation types (`unknown_type`) and the
live route (404), so the runner writes and posts them only after the hub knows them. An older web ignores the new
stream frames and kinds.
