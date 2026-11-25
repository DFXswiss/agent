"""The merge commit of `github.commit` with `merge: true` (ADR-084 section 4, 03-api.md section 9, 05-runner.md section
12.1): the tree against the head, the verification against the stored preparation and the formal marker check.

**Tree** (`plan`): the head's tree changed by the workspace snapshot as for every commit (`commit.new_tree`, without
`paths`), then

- the merged mode of every regular file the merge changed (a mode the base changed survives; a file decided `base`
  takes the base's mode);
- for every entry the workspace does not hold - a protected name, a symbolic link, a submodule (`hidden`) - the merge
  result of the preparation, or for a conflict of kind `protected` the side its decision names (`head` or `base`).

**Verification** (`conflicts_unresolved`, the message names up to 20 paths): every path the merge changed without a
conflict must hold its merge result (same blob, or absent when the merge deleted it) unless a decision `head`,
`deleted` or `edited` says otherwise and the tree matches it; every conflict without markers needs a decision that the
tree matches (`head`/`base`: the blob of that side or its absence, `deleted`: absent, `edited`: present and not
hidden); a decision of a `content` conflict is checked the same way; a file of the head the merge did not touch may be
missing only with the decision `deleted`; any other decision for a path the merge did not change is refused. So a
"merge" whose tree drops the base's or the head's files - an empty or foreign workspace, an ignored binary conflict -
is never committed.

**New content** (`counted`): a changed path whose blob is neither in the head's nor in the base's tree nor a hidden
entry of the preparation (`github.merge_base` uploaded those). Only these
count for the limits, are scanned for secrets and are uploaded; every other blob is at GitHub already.

**Markers** (`marked`, `conflict_markers`): every `content` conflict file present in the tree is checked strictly
against the marker-like lines its head's and base's versions already contain (stored with the preparation); every
other file with new content is checked for a `<<<<<<< ` and a `>>>>>>> ` line, and only such a file has its head's and
base's versions read (`suspects`, then `marked` again with their lines).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runner.github import commit, gitobj, merge3, snapshot, tarsafe
from agent_runner.github.merge_prep import Entry, Prep
from agent_runner.operations.extensions import CheckFailed

MAX_NAMED = 20
UNRESOLVED = (
    "The merge commit does not match the prepared merge at: {paths}. Resolve these paths and record a decision for each"
    " with decisions: [{{path, choice: head|base|deleted|edited}}]."
)
MARKED = "Conflict markers are left in: {paths}. Remove them before the merge commit."
Files = Mapping[str, tuple[str, str]]


@dataclass(frozen=True)
class MergePlan:
    plan: commit.Plan
    new: dict[str, tuple[str, str]]
    counted: frozenset[str]


def named(paths: Sequence[str]) -> str:
    more = f" and {len(paths) - MAX_NAMED} more" if len(paths) > MAX_NAMED else ""
    return ", ".join(paths[:MAX_NAMED]) + more


def _tuple(entry: Entry) -> tuple[str, str] | None:
    return None if entry is None else (entry[0], entry[1])


def hidden(path: str, entry: tuple[str, str] | None) -> bool:
    """An entry the workspace never holds: a protected name, a link or a submodule."""
    return tarsafe.protected(path) or (entry is not None and entry[0] not in gitobj.REGULAR_MODES)


def _same(actual: tuple[str, str] | None, wanted: tuple[str, str] | None) -> bool:
    if actual is None or wanted is None:
        return actual is wanted
    return actual[1] == wanted[1]


def _set(new: dict[str, tuple[str, str]], path: str, entry: tuple[str, str] | None) -> None:
    if entry is None:
        new.pop(path, None)
    else:
        new[path] = entry


def _apply(new: dict[str, tuple[str, str]], head: Files, prep: Prep, decisions: Mapping[str, str]) -> None:
    for path, raw in prep.changed.items():
        entry, kind = _tuple(raw), prep.conflicts.get(path)
        if kind == "protected":
            continue
        if hidden(path, entry) or (entry is None and hidden(path, head.get(path))):
            _set(new, path, entry)
        elif entry is not None and path in new and new[path][0] in gitobj.REGULAR_MODES:
            new[path] = (entry[0], new[path][1])  # the merged mode
    for path, kind in prep.conflicts.items():
        choice = decisions.get(path)
        if kind == "protected" and choice in ("head", "base"):
            _set(new, path, _tuple(prep.sides[path][str(choice)]))
            continue
        side = _tuple(prep.sides[path]["base"]) if choice == "base" and path in prep.sides else None
        if side is not None and path in new and new[path][1] == side[1]:
            new[path] = side


def _check(
    path: str, choice: str | None, actual: tuple[str, str] | None, prep: Prep, sides: tuple[Files, Files]
) -> bool:
    found = prep.sides.get(path)
    if choice in ("head", "base"):
        wanted = _tuple(found[str(choice)]) if found is not None else sides[choice == "base"].get(path)
        return _same(actual, wanted)
    if choice == "deleted":
        return actual is None
    return choice == "edited" and actual is not None and not hidden(path, actual)


def unresolved(
    new: Mapping[str, tuple[str, str]], sides: tuple[Files, Files], prep: Prep, decisions: Mapping[str, str]
) -> list[str]:
    """The paths that do not match the preparation and its decisions (module docstring, "Verification")."""
    head = sides[0]
    bad = [
        path
        for path in decisions
        if path not in prep.changed
        and path not in prep.conflicts
        and not (path in head and decisions[path] == "deleted")
    ]
    # a file of the head the merge left alone may only disappear with the decision `deleted`
    for path, entry in head.items():
        untouched = path not in prep.changed and path not in prep.conflicts and not hidden(path, entry)
        if untouched and path not in new and decisions.get(path) != "deleted":
            bad.append(path)
    for path in sorted(set(prep.changed) | set(prep.conflicts)):
        actual, choice, kind = new.get(path), decisions.get(path), prep.conflicts.get(path)
        if kind == "content" and choice is None:
            continue
        if kind is None and choice is None:
            if not _same(actual, _tuple(prep.changed[path])):
                bad.append(path)
            continue
        if not _check(path, choice, actual, prep, sides):
            bad.append(path)
    return sorted(set(bad))


def plan(head: Files, theirs: Files, snap: snapshot.Snapshot, prep: Prep, decisions: Mapping[str, str]) -> MergePlan:
    """The merge commit's tree, verified (module docstring); `CheckFailed` otherwise."""
    new = commit.new_tree(head, snap, None)
    _apply(new, head, prep, decisions)
    bad = unresolved(new, (head, theirs), prep, decisions)
    if bad:
        raise CheckFailed("conflicts_unresolved", message=UNRESOLVED.format(paths=named(bad)))
    known = {sha for _, sha in head.values()} | {sha for _, sha in theirs.values()}
    known |= {raw[1] for path, raw in prep.changed.items() if raw is not None and hidden(path, _tuple(raw))}
    counted = frozenset(path for path, entry in new.items() if head.get(path) != entry and entry[1] not in known)
    sizes = {path: snap.files[path].size for path in counted}
    try:
        found = commit.finish(head, new, sizes, counted)
    except ValueError:
        raise CheckFailed(
            "conflicts_unresolved", message="A path of the merge commit is a file and a directory at once."
        ) from None
    return MergePlan(found, new, counted)


def marked(
    scratch: Any, merged: MergePlan, prep: Prep, known: Mapping[str, frozenset[bytes]] | None = None
) -> tuple[list[str], list[str]]:
    """`(paths with markers, suspects)`: `content` conflicts strictly; other new content for a marker pair, a suspect
    when no `known` lines are given for it yet (module docstring, "Markers")."""
    bad, suspects = [], []
    for path in sorted(merged.counted):
        data = snapshot.read_blob(scratch, merged.new[path][1])
        if prep.conflicts.get(path) == "content":
            lines = frozenset(line.encode("latin-1") for line in prep.markers.get(path, []))
            if merge3.has_markers(data, strict=True, known=lines):
                bad.append(path)
            continue
        if not merge3.has_markers(data, strict=False):
            continue
        if known is None or path not in known:
            suspects.append(path)
        elif merge3.has_markers(data, strict=False, known=known[path]):
            bad.append(path)
    return bad, suspects


def refuse_marked(paths: Sequence[str]) -> None:
    if paths:
        raise CheckFailed("conflict_markers", message=MARKED.format(paths=named(list(paths))))
