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

## Guest
Version 0.8.0: the contracts the guest bundles change. The runner sends `known` only to guests from 0.8.0
(`guest_features.KNOWN_EXPORT`); an older guest cannot request `a38_review` (its bundled contracts refuse the
parameter), so it never receives the new result field.
