"""The operation types the hub knows and their duplicate windows (03-api.md section 9, `contracts/ops/`).

`OPERATION_TYPES` is check H3 of the registration (03-api.md section 8.3): a type outside it is `rejected` with
`unknown_type`. Every known type is enabled in v1 (there is no switch to disable one). `WINDOWS` holds the duplicate
window of every type with a dedupe field set (`contracts/ops/dedupe-fields.json`); a type without one has duplicate
protection only through an idempotency key, whose window is `IDEMPOTENCY_WINDOW` (the hub only sees the tag and cannot
tell the two cases apart, 04-crypto.md section 4.10). Tests compare both with the contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType

OPERATION_TYPES = frozenset(
    {
        "github.list_repositories",
        "github.checkout",
        "github.get_pull_request",
        "github.create_branch",
        "github.commit",
        "github.push",
        "github.create_pull_request",
        "github.comment",
        "subagent.request",
        "task.set_title",
        "workvm.request",
        "workvm.release",
        "test.run",
        "browser.open",
        "browser.close",
        "github.list_issues",
        "github.get_issue",
        "github.create_issue",
        "github.update_issue",
        "github.list_pull_requests",
        "github.request_connection",
        "human.question",
        "human.approval",
        "github.list_identities",
        "github.merge_base",
    }
)
SUBAGENT_REQUEST = "subagent.request"
HUMAN_TYPES = frozenset({"human.question", "human.approval"})
"""Operations that ask a person (KOR-39): only a main session may request them (check H4)."""
WINDOWS: Mapping[str, int] = MappingProxyType(
    {
        "github.checkout": 600,
        "github.create_branch": 86400,
        "github.commit": 86400,
        "github.push": 86400,
        "github.create_pull_request": 604800,
        "github.comment": 86400,
        "github.create_issue": 86400,
        "subagent.request": 600,
    }
)
IDEMPOTENCY_WINDOW = 86400


def window(op_type: str) -> timedelta:
    """How long an accepted operation of the type holds its dedupe slot."""
    return timedelta(seconds=WINDOWS.get(op_type, IDEMPOTENCY_WINDOW))
