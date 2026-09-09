# Local CI report `dfx-local-ci/v1`

This is the frozen comment payload that records a **full local CI run**
(`ci:full` equivalent) for a pull request. An authorized omitted job is
recorded as `result=not_applicable` without executing the command.
`agent local-ci verify` parses it and decides pass or fail. Do not invent a
second format.

This document defines the report format and legacy verifier behavior only; it
does not define process adoption or CI applicability. Adoption must be claimed
by the target repository's written rules. A38 adopters follow the central
[A38 standard](a38.md) and [guard guide](a38-guard.md): private visibility alone
is not opt-in or permission to skip GitHub CI, and private local code-gate
equivalence requires trusted-base opt-in through a valid A38 manifest,
assessment against the canonical active policy, and a separate live join against
the actual latest report-like GitHub comment by the PR author. Public A38 adopters
must publish and validate this author report in addition to their cumulative
GitHub CI. Repositories that do not adopt A38 retain their existing written
rules; A38 also retains independent
GitHub-only checks, technical merge restrictions, review gates, and human merge.

The wire verifier's `private: false` result remains `not_applicable` as specified
below. That legacy result is not A38 public-report validation; use the canonical
A38 policy verification and guard process for that assessment.

## Markers

A pull-request comment contains **exactly one** pair of HTML comments:
`<!-- DFX-LOCAL-CI:v1 -->` and `<!-- /DFX-LOCAL-CI:v1 -->`. Between them
sits one fenced JSON object whose language tag is `json`. Nothing else may
sit between the markers.

## Payload

Every key below is required except the optional `readme_only` and
`markdown_only` booleans. Unknown keys are rejected.

| Key | Rule |
|---|---|
| `schema` | Exactly `dfx-local-ci/v1` |
| `repo` | `owner/name` |
| `head` | 40-character lowercase hex SHA of the pull-request head |
| `private` | JSON boolean. `true` for the private-repo local-CI gate |
| `recorded_at` | UTC `YYYY-MM-DDTHH:MM:SSZ` |
| `required` | Unique kebab-case ids. This is the full `ci:full` job set. Empty only when the repository has no pull-request CI jobs |
| `runs` | One object per required id (executed or authorized omitted). Empty only when `required` is empty |
| `readme_only` (optional) | JSON boolean. When present and `true`, authorizes `result=not_applicable` runs with `exit_code=0` (A38 policy verify also requires the job id in `readme_only.omit_jobs`; the guard independently confirms the PR file inventory). Omit the key when false. |
| `markdown_only` (optional) | JSON boolean. When present and `true`, authorizes `result=not_applicable` runs with `exit_code=0` for every required job (full local-suite skip when every changed path ends with `.md`; the guard independently confirms the PR file inventory). Omit the key when false. |

Each run object:

| Key | Rule |
|---|---|
| `id` | kebab-case, unique, must match an entry in `required` for that job |
| `name` | Human job name |
| `command` | The configured command (executed, or recorded without execution when `not_applicable`) |
| `result` | `pass` \| `fail` \| `error` \| `timeout` \| `not_applicable` |
| `exit_code` | Integer |
| `duration_s` | Number ≥ 0 |
| `timeout_s` | Number > 0. The job timeout |

There is no `verdict` field. The script computes it.

## Verdict

`agent local-ci verify` exits `0` only when:

1. The comment parses.
2. `private` is `false` (legacy wire verdict `not_applicable`; distinct from a
   run's `result=not_applicable`), **or**
3. `private` is `true` and every `required` id has a run that is either
   `result=pass` with `exit_code=0` and `duration_s <= timeout_s`, or
   `result=not_applicable` authorized by `readme_only: true` or
   `markdown_only: true` with `exit_code=0` (duration versus timeout need
   not apply to those omitted runs). An empty `required` list (no
   pull-request CI jobs in the repository) is a pass.

`--require-ids a,b,c` additionally demands that `required` is exactly that set.
`--expect-head SHA` demands the payload head matches. `--expect-private` demands
`private` is true and rejects the legacy private-false `not_applicable` verdict.

Parse errors exit with `agent: …`. A computed fail exits `1` after printing
`local-ci fail …`.

## Commands

```
agent local-ci verify [--file PATH] [--require-ids id,id] [--expect-head SHA] [--expect-private] [--json]
agent local-ci parse [--file PATH] [--json]
agent local-ci render [--file PATH]
```

Without `--file`, the comment or JSON is read from stdin.
