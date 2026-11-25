"""Three-way merge without the `git` binary (ADR-084 section 4, 05-runner.md section 12.1 `github.merge_base`).

The runner never runs `git` (ADR-015). Bringing a pull request up to date with its base merges three trees: the merge
base O, the pull request's head A ("ours") and the base B ("theirs").

**Entries** (`merge_entries(o, a, b)`, leaf entries `path -> (mode, sha)` as `gitobj.flatten` lists them):

- equal on both sides -> that side (also when both deleted it);
- changed on one side only -> that side (a deletion as well);
- changed on both sides:
  - both regular files (and the ancestor absent or a regular file) with different content -> `content`: the line
    merge decides (also for a protected name, whose result never reaches the workspace); the same content with
    different modes -> the mode of the side that changed it, the head's when both did (listed as `mode`);
  - a regular file modified on one side and deleted on the other -> the modified entry, listed as `modify_delete`;
  - anything else (a symbolic link or submodule on any side, a regular file against a link, a protected name
    modified on one side and deleted on the other) -> the head's entry (the base's when the head deleted it),
    listed as `protected`: the workspace holds neither links, submodules nor protected names, so the choice is
    recorded as a decision of the merge commit (`head` or `base`), never edited;
- a path that is a file on one side and a directory on the other keeps the head's entries; the base's entries below
  (or the base's file above) are dropped and listed as `type` - never silently.

**Lines** (`merge_text(o, a, b, labels)`): the classic diff3 algorithm. The matching blocks of O against A and O
against B (`difflib.SequenceMatcher`, no junk heuristic) are intersected into stable regions where all three agree;
only a region with an **anchor** - a non-blank line that occurs exactly once in each version - is trusted (repeated
lines such as blank lines or closing braces can be matched in more than one way, and a merge must never become clean
through such a choice), and a clean merge must be the same when the lines are matched from the end; between the
trusted regions a hunk changed on one side takes that side, changed equally on both sides takes it once, changed
differently is a conflict. Lines common to the start and end of both sides of a conflict are moved out of it. A
conflict is written in the diff3 style with the common ancestor, every marker on its own line:

    <<<<<<< pull request (<head ref>)
    ...head...
    ||||||| common ancestor
    ...ancestor...
    =======
    ...base...
    >>>>>>> base (<base ref>)

A side whose last line has no line feed gets one before the next marker. Text = valid UTF-8 without a NUL byte and
at most `TEXT_LIMIT` bytes per version; anything else changed on both sides keeps the head's version (`binary`).

**Markers** (`marker_lines`): the formal check before a merge commit (ADR-084 section 4): in a file the preparation
listed as conflicting any line starting with `<<<<<<<`, `|||||||` or `>>>>>>>` or exactly `=======`; in any other file
with new content a line starting with `<<<<<<< ` together with one starting with `>>>>>>> `.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from agent_runner.github import gitobj, tarsafe

TEXT_LIMIT = 1024 * 1024
START, ANCESTOR, MIDDLE, END = b"<<<<<<<", b"|||||||", b"=======", b">>>>>>>"
Entry = tuple[str, str]


@dataclass(frozen=True)
class Labels:
    head: str
    base: str

    def start(self) -> bytes:
        return START + f" pull request ({self.head})\n".encode()

    def ancestor(self) -> bytes:
        return ANCESTOR + b" common ancestor\n"

    def end(self) -> bytes:
        return END + f" base ({self.base})\n".encode()


@dataclass
class Plan:
    """The outcome of `merge_entries`."""

    result: dict[str, Entry] = field(default_factory=dict)
    """Entries decided without looking at content (paths of `content` are absent)."""
    content: list[str] = field(default_factory=list)
    """Regular files changed on both sides: the line merge decides (the head's entry until then)."""
    conflicts: dict[str, str] = field(default_factory=dict)
    """Listed conflicts that were decided here (`mode`, `modify_delete`, `type`, `protected`)."""


def _regular(entry: Entry | None) -> bool:
    return entry is not None and entry[0] in gitobj.REGULAR_MODES


def _both_changed(path: str, o: Entry | None, a: Entry | None, b: Entry | None, plan: Plan) -> None:
    if _regular(a) and _regular(b) and (o is None or _regular(o)):
        ours, theirs = cast(Entry, a), cast(Entry, b)
        if ours[1] != theirs[1]:
            plan.content.append(path)
            plan.result[path] = ours
            return
        ancestor = None if o is None else o[0]
        plan.result[path] = (theirs[0] if ours[0] == ancestor else ours[0], ours[1])
        if ancestor not in (ours[0], theirs[0]):
            plan.conflicts[path] = "mode"
        return
    kept = cast(Entry, a if a is not None else b)  # never both absent: that is equal on both sides
    plan.result[path] = kept
    simple = (a is None or b is None) and _regular(kept) and (o is None or _regular(o))
    plan.conflicts[path] = "modify_delete" if simple and not tarsafe.protected(path) else "protected"


def _parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(1, len(parts))]


def _types(plan: Plan, a: Mapping[str, Entry]) -> None:
    """A path that is a file and a directory at once keeps the head's side (module docstring)."""
    paths = set(plan.result)
    clashes = sorted(path for path in paths if any(parent in paths for parent in _parents(path)))
    for path in clashes:
        parents = [item for item in _parents(path) if item in paths]
        if not parents or path not in paths:
            continue  # the clash was settled by an earlier drop
        drop = path if parents[0] in a else parents[0]
        del plan.result[drop]
        paths.discard(drop)
        plan.conflicts[drop] = "type"


def merge_entries(o: Mapping[str, Entry], a: Mapping[str, Entry], b: Mapping[str, Entry]) -> Plan:
    """The tree-level merge (module docstring). Linear in the number of paths (each with its ancestors)."""
    plan = Plan()
    for path in sorted(set(o) | set(a) | set(b)):
        ours, theirs, base = a.get(path), b.get(path), o.get(path)
        if ours == theirs:
            chosen = ours
        elif ours == base:
            chosen = theirs
        elif theirs == base:
            chosen = ours
        else:
            _both_changed(path, base, ours, theirs, plan)
            continue
        if chosen is not None:
            plan.result[path] = chosen
    _types(plan, a)
    plan.content.sort()
    return plan


def is_text(data: bytes) -> bool:
    if len(data) > TEXT_LIMIT or b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


Region = tuple[int, int, int, int, int, int]


def _sync_regions(base: Sequence[bytes], a: Sequence[bytes], b: Sequence[bytes]) -> list[Region]:
    """Ranges `(base start, base end, a start, a end, b start, b end)` where all three agree, with a final empty
    region at the ends."""
    am = difflib.SequenceMatcher(None, base, a, autojunk=False).get_matching_blocks()
    bm = difflib.SequenceMatcher(None, base, b, autojunk=False).get_matching_blocks()
    regions: list[Region] = []
    ia = ib = 0
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        start, end = max(abase, bbase), min(abase + alen, bbase + blen)
        if start < end:
            asub, bsub = amatch + (start - abase), bmatch + (start - bbase)
            regions.append((start, end, asub, asub + end - start, bsub, bsub + end - start))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    anchors = _unique(base) & _unique(a) & _unique(b)
    kept = [region for region in regions if any(line in anchors for line in base[region[0] : region[1]])]
    kept.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    return kept


def _unique(lines: Sequence[bytes]) -> set[bytes]:
    """Lines that occur exactly once and are not blank: the only lines that can anchor a stable region."""
    seen: dict[bytes, int] = {}
    for line in lines:
        seen[line] = seen.get(line, 0) + 1
    return {line for line, count in seen.items() if count == 1 and line.strip()}


def _lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _terminated(lines: Sequence[bytes]) -> list[bytes]:
    out = list(lines)
    if out and not out[-1].endswith((b"\n", b"\r")):
        out[-1] += b"\n"
    return out


def _conflict(base: list[bytes], a: list[bytes], b: list[bytes], labels: Labels) -> list[bytes]:
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    mid_a, mid_b = a[head : len(a) - tail], b[head : len(b) - tail]
    out = list(a[:head])
    out += [labels.start(), *_terminated(mid_a), labels.ancestor(), *_terminated(base)]
    out += [MIDDLE + b"\n", *_terminated(mid_b), labels.end()]
    out += a[len(a) - tail :]
    return out


def _merge_lines(base: list[bytes], ours: list[bytes], theirs: list[bytes], labels: Labels) -> tuple[list[bytes], bool]:
    out: list[bytes] = []
    conflicted = False
    iz = ia = ib = 0
    for zmatch, zend, amatch, aend, bmatch, bend in _sync_regions(base, ours, theirs):
        part_z, part_a, part_b = base[iz:zmatch], ours[ia:amatch], theirs[ib:bmatch]
        if part_b in (part_a, part_z):
            out += part_a
        elif part_a == part_z:
            out += part_b
        else:
            conflicted = True
            out += _conflict(part_z, part_a, part_b, labels)
        out += base[zmatch:zend]
        iz, ia, ib = zend, aend, bend
    return out, conflicted


def merge_text(o: bytes | None, a: bytes, b: bytes, labels: Labels) -> tuple[bytes, bool]:
    """The line merge of three versions (module docstring): `(merged bytes, conflicted)`. A clean merge must come out
    the same when the lines are matched from the end (the reversed sequences); otherwise the alignment was ambiguous and
    the whole differing part is one conflict."""
    base, ours, theirs = _lines(o or b""), _lines(a), _lines(b)
    out, conflicted = _merge_lines(base, ours, theirs, labels)
    if not conflicted:
        back, again = _merge_lines(base[::-1], ours[::-1], theirs[::-1], labels)
        if again or back[::-1] != out:
            out, conflicted = _conflict(base, ours, theirs, labels), True
    return b"".join(out), conflicted


def _marker(line: bytes, strict: bool) -> bool:
    if strict:
        return line.startswith((START, ANCESTOR, END)) or line == MIDDLE
    return line.startswith((START + b" ", END + b" "))


def _bodies(data: bytes) -> list[bytes]:
    return [line.rstrip(b"\r") for line in data.split(b"\n")]


def marker_lines(data: bytes) -> set[bytes]:
    """The lines of a version that the strict check would take for markers (CR ignored)."""
    return {line for line in _bodies(data) if _marker(line, True)}


def has_markers(data: bytes, *, strict: bool, known: frozenset[bytes] = frozenset()) -> bool:
    """Whether a file still holds conflict markers (module docstring, "Markers"); a line in `known` (it occurs in the
    head's or the base's version) is no marker."""
    found = [line for line in _bodies(data) if _marker(line, strict) and line not in known]
    if strict:
        return bool(found)
    return any(line.startswith(START) for line in found) and any(line.startswith(END) for line in found)
