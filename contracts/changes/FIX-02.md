# FIX-02 – contract changes

ADR-027 section 3 (additive). Decisions: [docs/platform/decisions/FIX-02.md](../../docs/platform/decisions/FIX-02.md).

## Guest `files/export` (`guest/session-files-export.*`)
- Request: new optional `known`, at most 100 000 keys `^[0-9a-f]{32}$` (the first 32 hex digits of SHA-256 over the
  file's path, NUL and its `sha256_hex`). The route takes a body of up to 4 MiB.
- Manifest: new `omitted` (sorted paths), present exactly when the request named `known`: files listed in `files`
  whose content the archive leaves out because their key was known.

## Operation `github.comment` (`ops/objects.json`)
- Parameters: new optional `a38_review` (boolean): post only when the A38 guard would count the comment, else
  `params_invalid` with the guard's reasons.
- Result: new optional `a38 {run, head, signal?, timings_ms {queued, account, check, post, total}}`; new `$defs`
  `duration_ms`.

## Guest journal `session.state` (`guest/journal/record.json`)
- New optional `stop_error` of the state change, only with `idle` after Claude Code's `StopFailure`: the error class
  (`rate_limit`, `overloaded`, `server_error`, `authentication_failed`, `billing_error`, `invalid_request`,
  `model_not_found`, `max_output_tokens`, `unknown`). The runner continues a turn ended by `rate_limit`, `overloaded`
  or `server_error` (`model.retry`); an older runner ignores the field.

## Guest
Version 0.8.0: the contracts the guest bundles change. The runner sends `known` only to guests from 0.8.0
(`guest_features.KNOWN_EXPORT`); an older guest cannot request `a38_review` (its bundled contracts refuse the
parameter), so it never receives the new result field.
