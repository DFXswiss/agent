# Local CI report `dfx-local-ci/v1`

This is the frozen comment payload that records a **full local CI run**
(`ci:full` equivalent) for a pull request. `agent local-ci verify` parses it
and decides pass or fail. Do not invent a second format.

Private GitHub repositories must attach this block to the ready comment.
Public repositories keep GitHub Actions as the CI gate and omit the block.

## Markers

A pull-request comment contains **exactly one** pair of HTML comments:
`<!-- DFX-LOCAL-CI:v1 -->` and `<!-- /DFX-LOCAL-CI:v1 -->`. Between them
sits one fenced JSON object whose language tag is `json`. Nothing else may
sit between the markers.

## Payload

Every key is required. Unknown keys are rejected.

| Key | Rule |
|---|---|
| `schema` | Exactly `dfx-local-ci/v1` |
| `repo` | `owner/name` |
| `head` | 40-character lowercase hex SHA of the pull-request head |
| `private` | JSON boolean. `true` for the private-repo local-CI gate |
| `recorded_at` | UTC `YYYY-MM-DDTHH:MM:SSZ` |
| `required` | Unique kebab-case ids. This is the full `ci:full` job set. Empty only when the repository has no pull-request CI jobs |
| `runs` | One object per id that ran. Empty only when `required` is empty |

Each run object:

| Key | Rule |
|---|---|
| `id` | kebab-case, unique, must match an entry in `required` for that job |
| `name` | Human job name |
| `command` | Exact local command that was executed |
| `result` | `pass` \| `fail` \| `error` \| `timeout` |
| `exit_code` | Integer. `pass` requires `0` |
| `duration_s` | Number ≥ 0 |
| `timeout_s` | Number > 0. The job timeout |

There is no `verdict` field. The script computes it.

## Verdict

`agent local-ci verify` exits `0` only when:

1. The comment parses.
2. `private` is `false` (`not_applicable`), **or**
3. `private` is `true` and every `required` id has a run with `result=pass`,
   `exit_code=0`, and `duration_s <= timeout_s`. An empty `required` list
   (no pull-request CI jobs in the repository) is a pass.

`--require-ids a,b,c` additionally demands that `required` is exactly that set.

Parse errors exit with `agent: …`. A computed fail exits `1` after printing
`local-ci fail …`.

## Commands

```
agent local-ci verify [--file PATH] [--require-ids id,id] [--json]
agent local-ci parse [--file PATH] [--json]
agent local-ci render --file payload.json
```

Without `--file`, the comment or JSON is read from stdin.
