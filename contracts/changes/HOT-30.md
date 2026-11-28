# HOT-30 – contract changes

ADR-027 section 3 (additive). A task can change the title, the text, the draft state and the labels of a pull request
in a repository it may use.

## Operation types (`ops/`)

| Type | Params | Result | Dedupe |
|---|---|---|---|
| `github.update_pull_request` | `repo`, `number`, `title?` (1-256), `body?` (≤ 65536), `draft?` (boolean), `labels?` (as `github.update_issue`, replaces every label), `github_identity?`; at least one of `title`, `body`, `draft`, `labels` (formal check of the runner) | `number`, `url`, `title`, `state`, `draft`, `labels` | none (setting values is idempotent) |

The parameters are a closed object without `state`, `merge`, `merge_method`, `event` or `head_sha`: the type can
neither close, merge nor approve a pull request (merging stays `github.merge`, approving is no operation).

`ops/INDEX.json`, `ops/objects.json` (`params_github_update_pull_request`, `result_github_update_pull_request`, the
`result` union), `ops/classes.json` (class `write`) and `events/objects.json` (`body_operation_params`) list the new
type; fixtures under `fixtures/ops/github.update_pull_request.{params,result}/` and
`fixtures/events/operation.params/valid/variant-22.json`.

The guest validates operation types against its bundled contracts, so the type needs the guest version 0.8.0 (a new
golden); an older guest refuses it locally as `unknown_type`.
