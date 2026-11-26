"""Rights profiles of sessions and the built-in agent roles (ADR-094 sections 1 and 2; `contracts/ops/classes.json`).

A **profile** is an optional restriction of one session: the operation types it may request and the reach of its read
tools (`all` as its human, ADR-089; `workgroup` only its own workgroup; `task` only its own task). No profile means
every right (interpretation A1, rule E-11); nothing here is ever assigned by default.

Every operation type has exactly one **class** (`CLASSES`, a copy of `contracts/ops/classes.json`; a test holds both
equal and refuses a type without a class). The built-in profiles (`BUILTIN`, the roles of Idee 42) are derived from the
classes, so a new type falls into the roles by its class without a list being edited.

A profile is resolved into a `Profile` **when it is assigned** and stored as a snapshot (`as_json`): a later change of a
definition never widens or narrows a running assignment. `within(child, parent)` is the one rule "a child never has
more than its parent".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

READ, WRITE, RUN, DELEGATE, ASK = "read", "write", "run", "delegate", "ask"
SCAN, REVIEW, MERGE = "scan", "review", "merge"
CLASS_NAMES = (READ, WRITE, RUN, DELEGATE, ASK, SCAN, REVIEW, MERGE)

CLASSES: Mapping[str, str] = MappingProxyType(
    {
        "browser.close": RUN,
        "browser.open": RUN,
        "github.checkout": READ,
        "github.comment": WRITE,
        "github.commit": WRITE,
        "github.create_branch": WRITE,
        "github.create_issue": WRITE,
        "github.create_pull_request": WRITE,
        "github.get_issue": READ,
        "github.get_pull_request": READ,
        "github.list_identities": READ,
        "github.list_reviews": READ,
        "github.get_checks": READ,
        "github.merge": MERGE,
        "github.list_issues": READ,
        "github.list_pull_requests": READ,
        "github.list_repositories": READ,
        "github.merge_base": WRITE,
        "github.push": WRITE,
        "github.request_connection": READ,
        "github.update_issue": WRITE,
        "human.approval": ASK,
        "human.question": ASK,
        "mail.find": READ,
        "review.verdict": REVIEW,
        "security.report": SCAN,
        "subagent.instruct": DELEGATE,
        "subagent.request": DELEGATE,
        "subagent.stop": DELEGATE,
        "task.set_title": WRITE,
        "test.run": RUN,
        "workvm.release": RUN,
        "workvm.request": RUN,
    }
)
"""The class of every operation type (`contracts/ops/classes.json`)."""

REACH_ALL, REACH_WORKGROUP, REACH_TASK = "all", "workgroup", "task"
REACHES = (REACH_TASK, REACH_WORKGROUP, REACH_ALL)
"""The reaches of the read tools, narrowest first (`task` ⊂ `workgroup` ⊂ `all`)."""

ROLE_CLASSES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "read_only": frozenset({READ, DELEGATE, ASK}),
        "development": frozenset({READ, WRITE, RUN, DELEGATE, ASK}),
        "security": frozenset({READ, RUN, SCAN, DELEGATE, ASK}),
        "review": frozenset({READ, REVIEW, RUN, DELEGATE, ASK}),
        "merge": frozenset({READ, MERGE, DELEGATE, ASK}),
        "analysis": frozenset({READ, DELEGATE, ASK}),
    }
)
"""The built-in roles by the classes they allow (ADR-094 section 2; `analysis`: the process analysis, ADR-097 section
2 - the classes of `read_only`, and the session start names the analysis brief of the harness)."""

ANALYSIS = "analysis"

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
SCOPE_BUILTIN, SCOPE_ORG, SCOPE_WG = "builtin", "org", "wg"


@dataclass(frozen=True)
class Profile:
    """A resolved profile: its name, the operation types it allows and the reach of its reads."""

    name: str
    operations: frozenset[str]
    reads: str

    def allows(self, op_type: str) -> bool:
        return op_type in self.operations

    def as_json(self) -> dict[str, Any]:
        """The stored snapshot `{name, operations (sorted), reads}`."""
        return {"name": self.name, "operations": sorted(self.operations), "reads": self.reads}


def of_classes(classes: Iterable[str]) -> frozenset[str]:
    """Every operation type whose class is one of `classes`."""
    wanted = frozenset(classes)
    return frozenset(op_type for op_type, found in CLASSES.items() if found in wanted)


BUILTIN: Mapping[str, Profile] = MappingProxyType(
    {name: Profile(name, of_classes(classes), REACH_ALL) for name, classes in ROLE_CLASSES.items()}
)


def analysis(reads: str) -> Profile:
    """The snapshot an analysis task gets at its creation (ADR-097 section 2): the built-in `analysis` with the reach
    `reads` (`all` for an analysis of the organization, `workgroup` for one of the task's workgroup)."""
    return Profile(ANALYSIS, BUILTIN[ANALYSIS].operations, reads)


def from_json(value: Mapping[str, Any] | None) -> Profile | None:
    """The profile of a stored snapshot (None for none)."""
    if value is None:
        return None
    return Profile(str(value["name"]), frozenset(str(item) for item in value["operations"]), str(value["reads"]))


def allows(profile: Profile | None, op_type: str) -> bool:
    """True when a session with `profile` may request `op_type` (no profile: every type, A1)."""
    return profile is None or profile.allows(op_type)


def within(child: Profile, parent: Profile | None) -> bool:
    """True when `child` allows nothing its parent does not (ADR-094 section 1)."""
    if parent is None:
        return True
    return child.operations <= parent.operations and REACHES.index(child.reads) <= REACHES.index(parent.reads)


def invalid_operations(operations: Iterable[str]) -> list[int]:
    """The indexes of entries that are no known operation type."""
    return [index for index, item in enumerate(operations) if item not in CLASSES]
