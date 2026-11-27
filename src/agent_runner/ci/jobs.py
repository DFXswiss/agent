"""A job of the scale set as the listener reports it (`actions/scaleset` `JobMessageBase`, spec 16 section 17.2).

`job_of` reads one message of the listener into a `Job`, or None when a field is missing or has the wrong shape (the
controller then refuses the job, reason `message_invalid`). `ref_of` takes the ref out of `jobWorkflowRef`
(`<owner>/<repo>/.github/workflows/<file>@<ref>`); `pull_number` reads the number of a pull request out of a ref
`refs/pull/<n>/merge` or `refs/pull/<n>/head`, the refs GitHub runs `pull_request` workflows on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
PULL_REF = re.compile(r"refs/pull/([1-9][0-9]{0,9})/(?:merge|head)")
NUMBER_MAX = 2**31 - 1
LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Job:
    """One job GitHub offers or assigned to the scale set."""

    request_id: int
    owner: str
    repo: str
    job_id: str
    run_id: int
    workflow_ref: str
    event: str
    labels: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        """`<owner>/<repo>` as GitHub spells it."""
        return f"{self.owner}/{self.repo}"

    @property
    def ref(self) -> str | None:
        return ref_of(self.workflow_ref)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def job_of(message: Mapping[str, Any]) -> Job | None:
    """The job of a listener message (module docstring), None when it does not fit."""
    request_id, run_id = _integer(message.get("request_id")), _integer(message.get("run_id"))
    owner, repo = message.get("owner"), message.get("repo")
    job_id, ref, event = message.get("job_id"), message.get("workflow_ref"), message.get("event")
    labels = message.get("labels", [])
    if (
        request_id is None
        or run_id is None
        or not isinstance(owner, str)
        or OWNER.fullmatch(owner) is None
        or not isinstance(repo, str)
        or NAME.fullmatch(repo) is None
        or repo in (".", "..")
        or not isinstance(job_id, str)
        or not 0 < len(job_id) <= 64
        or not isinstance(ref, str)
        or len(ref) > 1024
        or not isinstance(event, str)
        or not 0 < len(event) <= 64
        or not isinstance(labels, list)
        or not all(isinstance(label, str) and LABEL.fullmatch(label) for label in labels)
    ):
        return None
    return Job(request_id, owner, repo, job_id, run_id, ref, event, tuple(labels))


def ref_of(workflow_ref: str) -> str | None:
    """The ref after the last `@` of a `jobWorkflowRef`, None without one."""
    _path, at, ref = workflow_ref.rpartition("@")
    return ref if at and ref else None


def pull_number(ref: str | None) -> int | None:
    """The pull request number of a `refs/pull/<n>/merge|head` ref, None for every other ref."""
    found = PULL_REF.fullmatch(ref or "")
    if found is None:
        return None
    number = int(found.group(1))
    return number if number <= NUMBER_MAX else None
