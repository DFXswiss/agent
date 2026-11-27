"""Which task a job belongs to: job -> pull request -> task -> workgroup (decisions D-12 and D-13, spec 16 section
17.3).

`attribute(job, config, pulls, runs)` answers an `Attribution` (the task, its workgroup and organization, and the
session that bears the job's runtime) or a `Refusal` with a closed reason; a refused job is not acquired and stays at
GitHub. By event:

- `pull_request`: the number in the ref (`refs/pull/<n>/merge`) -> the pull request's link (`ref_unreadable` when
  the ref names no pull request);
- `push`: the run's head commit -> the pull requests GitHub associates with it (the merged one first) -> the first
  that has a link (`push_unattributable` without `push_attribution`, `github_unavailable` when GitHub does not
  answer);
- `schedule`, `workflow_dispatch`: the fixed task of the repository;
- any other event: `event_not_served`.

A link counts only when a session of agent.dfx.swiss created the pull request (`github.create_pull_request`, origin
`device`): a pull request a person opened on GitHub, or merely linked to a task, is not taken
(`pull_request_not_from_agent`). A repository without an entry in the configuration is `repository_not_served`.
The runtime of a pull request's job is borne by the session that created it (the hub books it to that session's task
and workgroup); the fixed task's by its configured main session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_runner.ci.jobs import Job, pull_number
from agent_runner.ci.repos import CiConfig

PULL_REQUEST = "pull_request"
PUSH = "push"
REPOSITORY_EVENTS = frozenset({"schedule", "workflow_dispatch"})
BASES = ("pull_request", "commit_pull_request", "repository_task")
REASONS = (
    "message_invalid",
    "repository_not_served",
    "event_not_served",
    "ref_unreadable",
    "push_unattributable",
    "github_unavailable",
    "pull_request_not_from_agent",
)


@dataclass(frozen=True)
class Owner:
    """The task a job runs for, its workgroup and organization, and the session that bears its runtime."""

    org_id: str
    wg_id: str
    task_id: str
    session_id: str


@dataclass(frozen=True)
class Attribution:
    owner: Owner
    basis: str
    number: int | None = None


@dataclass(frozen=True)
class Refusal:
    reason: str


class Pulls(Protocol):
    async def find(self, repo: str, number: int) -> Owner | None:
        """The owner of a pull request a session of this platform created, None otherwise."""


class RunsUnavailable(Exception):
    """GitHub could not answer a read of the run or of its pull requests."""


class Runs(Protocol):
    async def head_sha(self, repo: str, run_id: int) -> str: ...

    async def pulls_of(self, repo: str, sha: str) -> Sequence[int]:
        """The pull requests GitHub associates with the commit, the merged one first."""


async def attribute(  # noqa: PLR0911 - one return per row of the module docstring
    job: Job, config: CiConfig, pulls: Pulls, runs: Runs
) -> Attribution | Refusal:
    """The task of `job` (module docstring)."""
    repository = config.repository(job.full_name)
    if repository is None:
        return Refusal("repository_not_served")
    if job.event in REPOSITORY_EVENTS:
        owner = Owner(repository.org_id, repository.wg_id, repository.task_id, repository.session_id)
        return Attribution(owner, "repository_task")
    if job.event == PULL_REQUEST:
        number = pull_number(job.ref)
        if number is None:
            return Refusal("ref_unreadable")
        return await _linked(pulls, repository.repo, [number], PULL_REQUEST)
    if job.event != PUSH:
        return Refusal("event_not_served")
    if not config.push_attribution:
        return Refusal("push_unattributable")
    try:
        sha = await runs.head_sha(repository.repo, job.run_id)
        numbers = await runs.pulls_of(repository.repo, sha)
    except RunsUnavailable:
        return Refusal("github_unavailable")
    return await _linked(pulls, repository.repo, numbers, "commit_pull_request")


async def _linked(pulls: Pulls, repo: str, numbers: Sequence[int], basis: str) -> Attribution | Refusal:
    """The first of `numbers` a session of this platform created, else `pull_request_not_from_agent`."""
    for number in numbers:
        found = await pulls.find(repo, number)
        if found is not None:
            return Attribution(found, basis, number)
    return Refusal("pull_request_not_from_agent")
