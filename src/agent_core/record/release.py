"""The release rule over the record (RECORD-PROTOCOL.md section 9), as a pure function.

The rule reads the tasks of every team and of both kinds; the visibility of section 3 does not
limit it, and its answer names no task.
"""

from __future__ import annotations

from .objects import DROPPED, IN_PROGRESS, READY_FOR_REVIEW, Task
from .state import StateView

TASK_MISSING = "task_missing"
TASK_NOT_READY = "task_not_ready"
QUESTION_UNANSWERED = "question_unanswered"
_READY = (IN_PROGRESS, READY_FOR_REVIEW)


def linked_tasks(state: StateView, repository: str, pull_request: int) -> tuple[Task, ...]:
    """The tasks whose `links` contain `<repository>#<pull_request>` and whose status is not `dropped`."""
    ids = state.linked_tasks.get(f"{repository}#{pull_request}")
    if ids is None:
        return ()
    tasks = []
    for object_id in sorted(ids):
        task = state.objects[object_id]
        if not isinstance(task, Task):
            raise ValueError("the link index names an object that is not a task")
        if task.status != DROPPED:
            tasks.append(task)
    return tuple(tasks)


def release_answer(state: StateView, repository: str, pull_request: int) -> tuple[bool, str | None]:
    """`released` and the first failed condition of section 9, checked in the order of its table.

    `question_unanswered` is always met: orders and questions are not built (DESIGN.md §24.11).
    """
    tasks = linked_tasks(state, repository, pull_request)
    if not tasks:
        return False, TASK_MISSING
    if any(task.assignee is None or task.status not in _READY for task in tasks):
        return False, TASK_NOT_READY
    return True, None
