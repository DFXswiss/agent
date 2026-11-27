"""Jobs, the controller's configuration and the chain job -> pull request -> task -> workgroup (spec 16 section 17.3,
decisions D-12 and D-13)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runner.ci.attribution import Attribution, Owner, Refusal, RunsUnavailable, attribute
from agent_runner.ci.jobs import Job, job_of, pull_number, ref_of
from agent_runner.ci.repos import ConfigError, load, parse

from .kit import FIXED, PR_OWNER, REPO, SHA, config, config_data, job_message, write_config

# -- jobs ------------------------------------------------------------------------------------------------------------


def test_a_job_message_is_read() -> None:
    job = job_of(job_message())
    assert job == Job(
        42,
        "example-org",
        "example-repo",
        "job-42",
        942,
        "example-org/example-repo/.github/workflows/ci.yml@refs/pull/7/merge",
        "pull_request",
        ("agent-ci-example-repo",),
    )
    assert job.full_name == REPO
    assert job.ref == "refs/pull/7/merge"
    assert job_of({k: v for k, v in job_message().items() if k != "labels"}) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": 0},
        {"request_id": True},
        {"run_id": "1"},
        {"owner": "bad owner"},
        {"owner": 5},
        {"repo": ".."},
        {"repo": "bad/repo"},
        {"repo": None},
        {"job_id": ""},
        {"job_id": "x" * 65},
        {"job_id": 5},
        {"workflow_ref": 5},
        {"workflow_ref": "x" * 1025},
        {"event": ""},
        {"event": 5},
        {"labels": "agent-ci"},
        {"labels": ["bad label"]},
        {"labels": [5]},
    ],
)
def test_a_message_that_does_not_fit_is_no_job(changes: dict[str, Any]) -> None:
    assert job_of(job_message(**changes)) is None


def test_refs_and_pull_numbers() -> None:
    assert ref_of("o/r/.github/workflows/x.yml@refs/heads/main") == "refs/heads/main"
    assert ref_of("no-ref") is None
    assert ref_of("o/r/x.yml@") is None
    assert pull_number("refs/pull/12/merge") == 12
    assert pull_number("refs/pull/12/head") == 12
    assert pull_number("refs/heads/main") is None
    assert pull_number(None) is None
    assert pull_number("refs/pull/9999999999/merge") is None


# -- configuration ---------------------------------------------------------------------------------------------------


def test_the_configuration_is_read(tmp_path: Path) -> None:
    loaded = load(write_config(tmp_path / "ci.json"))
    assert loaded.client_id == "Iv23-test"
    assert loaded.api_url == "https://api.github.test"
    assert loaded.push_attribution is True
    assert loaded.repository("EXAMPLE-ORG/Example-Repo") is loaded.repositories[0]
    assert loaded.repository("example-org/other") is None
    defaults = parse({k: v for k, v in config_data().items() if k not in ("api_url", "push_attribution")})
    assert defaults.api_url == "https://api.github.com"
    assert defaults.push_attribution is False
    same_org = parse(config_data(repositories=[_repo(), _repo(repo="Example-Org/second", scale_set="agent-ci-second")]))
    assert same_org.organisations == ["example-org"]
    assert same_org.ready
    local = parse(config_data(api_url="http://127.0.0.1:8080/"))
    assert local.api_url == "http://127.0.0.1:8080"


def _repo(**changes: Any) -> dict[str, Any]:
    entry = dict(config_data()["repositories"][0])
    entry.update(changes)
    return entry


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        ([], ""),
        (config_data(app=None), "/app"),
        (config_data(api_url="ftp://x"), "/api_url"),
        (config_data(api_url=5), "/api_url"),
        (config_data(push_attribution="yes"), "/push_attribution"),
        (config_data(repositories=[]), "/repositories"),
        (config_data(repositories=[5]), "/repositories/0"),
        (config_data(repositories=[_repo(installation_id=0)]), "/repositories/0/installation_id"),
        (config_data(repositories=[_repo(installation_id=True)]), "/repositories/0/installation_id"),
        (config_data(repositories=[_repo(size="huge")]), "/repositories/0/size"),
        (config_data(repositories=[_repo(repo="no-slash")]), "/repositories/0/repo"),
        (config_data(repositories=[_repo(repo="o/..")]), "/repositories/0/repo"),
        (config_data(repositories=[_repo(repo="")]), "/repositories/0/repo"),
        (config_data(repositories=[_repo(scale_set="Upper")]), "/repositories/0/scale_set"),
        (config_data(repositories=[_repo(task_id="not-a-uuid")]), "/repositories/0/task_id"),
        (config_data(repositories=[_repo(), _repo(repo=REPO.upper(), scale_set="other")]), "/repositories/1/repo"),
        (config_data(repositories=[_repo(), _repo(repo="o/second")]), "/repositories/1/scale_set"),
        (config_data(app={"client_id": "bad id", "key_file": "/k"}), "/app/client_id"),
        (config_data(listener=""), "/listener"),
    ],
)
def test_a_configuration_that_does_not_fit_is_refused(data: Any, pointer: str) -> None:
    with pytest.raises(ConfigError) as refused:
        parse(data)
    assert refused.value.pointer == pointer


def test_an_unreadable_configuration_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(broken)


# -- attribution -----------------------------------------------------------------------------------------------------


class Pulls:
    def __init__(self, known: dict[int, Owner]) -> None:
        self.known = known
        self.asked: list[tuple[str, int]] = []

    async def find(self, repo: str, number: int) -> Owner | None:
        self.asked.append((repo, number))
        return self.known.get(number)


class Runs:
    def __init__(self, numbers: Sequence[int] = (), fail: bool = False) -> None:
        self.numbers = list(numbers)
        self.fail = fail

    async def head_sha(self, repo: str, run_id: int) -> str:
        if self.fail:
            raise RunsUnavailable("server_error")
        return SHA

    async def pulls_of(self, repo: str, sha: str) -> Sequence[int]:
        assert sha == SHA
        return self.numbers


def _attribute(message: dict[str, Any], pulls: Pulls, runs: Runs | None = None, **changes: Any) -> Any:
    job = job_of(message)
    assert job is not None
    return asyncio.run(attribute(job, config(**changes), pulls, runs or Runs()))


def test_a_pull_request_job_belongs_to_the_task_of_its_pull_request() -> None:
    pulls = Pulls({7: PR_OWNER})
    assert _attribute(job_message(), pulls) == Attribution(PR_OWNER, "pull_request", 7)
    assert pulls.asked == [(REPO, 7)]


def test_a_pull_request_a_person_opened_is_not_taken() -> None:
    assert _attribute(job_message(), Pulls({})) == Refusal("pull_request_not_from_agent")


def test_a_pull_request_job_without_a_pull_ref_is_not_taken() -> None:
    assert _attribute(job_message(ref="refs/heads/feature"), Pulls({7: PR_OWNER})) == Refusal("ref_unreadable")


def test_a_push_after_the_merge_belongs_to_the_merged_pull_request() -> None:
    message = job_message(event="push", ref="refs/heads/develop")
    found = _attribute(message, Pulls({8: PR_OWNER}), Runs([5, 8]))
    assert found == Attribution(PR_OWNER, "commit_pull_request", 8)
    assert _attribute(message, Pulls({}), Runs([5])) == Refusal("pull_request_not_from_agent")
    assert _attribute(message, Pulls({}), Runs(fail=True)) == Refusal("github_unavailable")
    assert _attribute(message, Pulls({8: PR_OWNER}), push_attribution=False) == Refusal("push_unattributable")


def test_scheduled_runs_and_deploys_run_under_the_fixed_task_of_the_repository() -> None:
    for event in ("schedule", "workflow_dispatch"):
        assert _attribute(job_message(event=event, ref="refs/heads/main"), Pulls({})) == Attribution(
            FIXED, "repository_task"
        )


def test_other_events_and_repositories_are_not_taken() -> None:
    assert _attribute(job_message(event="pull_request_target"), Pulls({})) == Refusal("event_not_served")
    assert _attribute(job_message(repo="other-repo"), Pulls({})) == Refusal("repository_not_served")
