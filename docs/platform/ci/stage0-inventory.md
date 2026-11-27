# Stage 0 – inventory of the self-hosted CI runs

Stage 0 of [spec/16-ci-runners.md §12.2](../spec/16-ci-runners.md#122-stages-research-1-8), after the decisions of
2026-11-27 ([§13](../spec/16-ci-runners.md#13-open-decisions)). This document fixes **what** is inventoried, **how**
and **which result decides what**. It holds no production data: the filled-in inventory (organisations, repositories,
devices, labels, settings) is kept in the operator's internal infrastructure documentation ([OPS] of spec 16 §16.5),
because this repository is public ([KONZEPT Teil D Punkt 5](../KONZEPT.md#teil-d--pflege)).

## 1. Scope after the decisions

| Decision | Consequence for the inventory |
|---|---|
| D-1 (a), (b) | Linux jobs first; macOS jobs are listed but move only after the spike (spec 16 §12.4) |
| D-1 (c) | Host-bound jobs (deploy, drift, reconcile, key and connector installation on a device) are listed separately: they move to the signed pull deployment, not into a CI VM |
| D-9 (b) | One scale set per repository: the unit of the inventory is the repository, not the runner group |
| D-12 | A job runs on agent.dfx.swiss only for a pull request a session created, the push after its merge, and scheduled runs and deploys under the repository's fixed task. For every repository: who opens pull requests today, and which workflows run on `schedule` / `workflow_dispatch` |
| D-13 | For every repository: the workgroup it belongs to (its fixed task is created there) |

## 2. What is recorded per repository

| Field | How it is read (read-only; nothing is changed in stage 0) |
|---|---|
| Organisation, repository, visibility | `GET /orgs/{org}/repos` (`visibility`, `archived`) |
| Actions enabled, allowed actions | `GET /repos/{owner}/{repo}/actions/permissions` |
| Self-hosted labels per workflow and job | the `runs-on` of every file in `.github/workflows/` (default branch and long-lived branches) |
| Triggers per workflow | `on:` of the same files: `pull_request`, `pull_request_target`, `push` (branches), `schedule`, `workflow_dispatch`, others |
| Job class (spec 16 §1.2) | from the job's steps: Linux portable, Linux build, macOS, privileged, host-bound |
| Current runners and their labels | `GET /repos/{owner}/{repo}/actions/runners` and `GET /orgs/{org}/actions/runners` (status, labels, busy) |
| Label gap (spec 16 §1.5) | labels requested by a workflow that no online runner carries |
| Fork pull requests | `GET /repos/{owner}/{repo}/actions/permissions/fork-pr-contributor-approval` and whether forks exist |
| Secrets and environments | names only, never values: `GET /repos/{owner}/{repo}/actions/secrets`, `GET /repos/{owner}/{repo}/environments` (deployment branch policy), organisation secrets visible to the repository |
| `GITHUB_TOKEN` default | `GET /repos/{owner}/{repo}/actions/permissions/workflow` (`default_workflow_permissions`) |
| Protected branches | `GET /repos/{owner}/{repo}/branches?protected=true` |
| Minutes and duration | the usage report of the organisation (M-3) and the run history of the last 30 days: jobs per week, p50/p95 duration per job |
| amd64 need | jobs that pull or build `linux/amd64` images or run amd64-only tools |
| Workgroup and fixed task | which agent.dfx.swiss workgroup carries the repository (D-12, D-13) |

Per organisation: plan (D-9: Free), installed GitHub Apps, organisation runner groups (none on Free besides the
default group).

## 3. How it is done

1. Read with a fine-grained token of a person with read access only (Administration: read, Actions: read,
   Metadata: read, Secrets: read for names), or with the app "agent CI" once installed. Nothing is written.
2. Fill the fields of §2 into the internal inventory, one row per repository and one per job.
3. Classify every job by spec 16 §1.2 and by the chain of D-12: which event, which task it would belong to, or
   "not taken" with the reason of [§17.3](../spec/16-ci-runners.md#173-attribution-job--pull-request--task--workgroup-d-12).
4. Mark the jobs that would be refused today because their pull requests are opened by persons on GitHub: they keep
   their current runner until the repository's pull requests come from agent.dfx.swiss, or they run on GitHub-hosted
   runners where that is allowed.
5. Review the result with the operator; then choose the test repository of stage 1.

## 4. What the result decides

| Finding | Decides |
|---|---|
| Repositories with only Linux portable jobs, pull requests from agent.dfx.swiss, no secrets in PR workflows | candidates for stage 1 and 2 |
| `push` workflows on develop/main | whether the app needs `Actions: read` and `Pull requests: read` (open point of spec 16 §13) |
| Secrets readable by PR workflows, `GITHUB_TOKEN` write by default | preconditions of spec 16 §9.6 to be fixed before the repository moves |
| Host-bound jobs per device | the scope of the signed pull deployment (D-1 (c)) |
| macOS jobs | input of the spike and M-14 (D-1 (b)) |
| Label gap | interim solution, decided separately (spec 16 §15) |
| Heavy jobs above the `xlarge` size | D-11 |

## 5. Public facts already known

From this repository only (no inventory data): `DFXswiss/agent` uses GitHub-hosted runners only
(`.github/workflows/platform-runner.yml`); its platform workflows run on one branch. The public repositories with
self-hosted CI named in spec 16 §1.1 are inventoried like every other repository.
