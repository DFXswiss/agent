"""U-RUN-github: the three-way merge of `github.merge_base` (ADR-084 section 4, `agent_runner.github.merge3`): tree
entries (one side, both sides, deletions, modes, links, protected names, file against directory) and lines (clean
merges, equal changes, conflicts in the diff3 style with trimmed common lines, missing final line feeds, add/add)
and the formal marker check."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_runner.github import gitobj, merge3

F, X, L, S = gitobj.FILE_MODE, gitobj.EXEC_MODE, gitobj.LINK_MODE, gitobj.SUBMODULE_MODE
LABELS = merge3.Labels("feature", "main")


def sha(n: int) -> str:
    return f"{n:040x}"


def test_entries_take_the_changed_side_and_keep_equal_changes() -> None:
    o = {"same": (F, sha(1)), "ours": (F, sha(2)), "theirs": (F, sha(3)), "gone_a": (F, sha(4)), "gone_b": (F, sha(5))}
    o["both_gone"] = (F, sha(6))
    o["equal"] = (F, sha(7))
    a = {"same": (F, sha(1)), "ours": (F, sha(20)), "theirs": (F, sha(3)), "gone_b": (F, sha(5)), "equal": (F, sha(70))}
    a["new_a"] = (F, sha(8))
    b = {"same": (F, sha(1)), "ours": (F, sha(2)), "theirs": (F, sha(30)), "gone_a": (F, sha(4)), "equal": (F, sha(70))}
    b["new_b"] = (X, sha(9))
    plan = merge3.merge_entries(o, a, b)
    assert plan.result == {
        "same": (F, sha(1)),
        "ours": (F, sha(20)),
        "theirs": (F, sha(30)),
        "equal": (F, sha(70)),
        "new_a": (F, sha(8)),
        "new_b": (X, sha(9)),
    }
    assert (plan.content, plan.conflicts) == ([], {})


def test_entries_both_changed_go_to_the_line_merge_with_the_head_as_placeholder() -> None:
    o = {"f": (F, sha(1))}
    plan = merge3.merge_entries(o, {"f": (F, sha(2)), "add": (F, sha(5))}, {"f": (F, sha(3)), "add": (F, sha(6))})
    assert plan.content == ["add", "f"]
    assert plan.result == {"f": (F, sha(2)), "add": (F, sha(5))}


def test_entries_mode_changes() -> None:
    o = {"one": (F, sha(1)), "two": (F, sha(2))}
    a = {"one": (F, sha(10)), "two": (X, sha(20)), "new": (F, sha(4))}
    b = {"one": (X, sha(10)), "two": (F, sha(20)), "new": (X, sha(4))}
    plan = merge3.merge_entries(o, a, b)
    assert plan.result["one"] == (X, sha(10))  # same content, only the base changed the mode
    assert plan.result["two"] == (X, sha(20))  # same content, only the head changed the mode
    assert plan.result["new"] == (F, sha(4))  # added on both sides with different modes: the head's, listed
    assert plan.conflicts == {"new": "mode"}


def test_entries_modify_delete_keeps_the_modified_side() -> None:
    o = {"a": (F, sha(1)), "b": (F, sha(2))}
    plan = merge3.merge_entries(o, {"a": (F, sha(10))}, {"b": (F, sha(20))})
    assert plan.result == {"a": (F, sha(10)), "b": (F, sha(20))}
    assert plan.conflicts == {"a": "modify_delete", "b": "modify_delete"}


def test_entries_links_submodules_and_protected_names_are_decided_not_edited() -> None:
    o = {
        "link": (L, sha(1)),
        "sub": (S, sha(2)),
        "x": (F, sha(4)),
        ".claude/gone": (F, sha(6)),
        "AGENTS.md": (F, sha(7)),
    }
    a = {"link": (L, sha(10)), "sub": (S, sha(20)), "x": (L, sha(40)), ".claude/gone": (F, sha(5))}
    b = {"link": (L, sha(11)), "x": (F, sha(41)), "AGENTS.md": (F, sha(71)), "CLAUDE.md": (F, sha(8))}
    a["CLAUDE.md"] = (F, sha(9))
    plan = merge3.merge_entries(o, a, b)
    assert plan.conflicts == {
        ".claude/gone": "protected",  # protected, modified and deleted: the modified entry
        "AGENTS.md": "protected",  # deleted by the head, modified by the base: the base's
        "link": "protected",
        "sub": "protected",  # the base deleted the submodule the head moved: the head's
        "x": "protected",
    }
    assert plan.result == {
        ".claude/gone": (F, sha(5)),
        "AGENTS.md": (F, sha(71)),
        "link": (L, sha(10)),
        "sub": (S, sha(20)),
        "x": (L, sha(40)),
        "CLAUDE.md": (F, sha(9)),
    }
    assert plan.content == ["CLAUDE.md"]  # a protected text added on both sides: the line merge decides
    o = {"l": (L, sha(1)), "AGENTS.md": (F, sha(2))}
    plan = merge3.merge_entries(o, dict(o), {"AGENTS.md": (F, sha(9))})
    assert plan.result == {"AGENTS.md": (F, sha(9))}  # changed on one side only: merged at entry level
    assert plan.conflicts == {}


def test_entries_file_against_directory_keeps_the_head() -> None:
    plan = merge3.merge_entries({}, {"p": (F, sha(1))}, {"p/inner": (F, sha(2)), "p/deep/x": (F, sha(3))})
    assert plan.result == {"p": (F, sha(1))}
    assert plan.conflicts == {"p/deep/x": "type", "p/inner": "type"}
    plan = merge3.merge_entries({}, {"q/inner": (F, sha(1))}, {"q": (F, sha(2))})
    assert plan.result == {"q/inner": (F, sha(1))}
    assert plan.conflicts == {"q": "type"}


def test_entries_file_against_directory_settles_each_clash_once() -> None:
    plan = merge3.merge_entries({"d": (F, sha(1))}, {"d/x": (F, sha(2))}, {"d": (F, sha(3))})
    assert plan.result == {"d/x": (F, sha(2))}
    assert plan.conflicts == {"d": "type"}  # modified by the base, replaced by a directory in the head: listed
    plan = merge3.merge_entries({}, {"q/i": (F, sha(1)), "q/j": (F, sha(2))}, {"q": (F, sha(3))})
    assert plan.result == {"q/i": (F, sha(1)), "q/j": (F, sha(2))}
    assert plan.conflicts == {"q": "type"}


def test_text_detection() -> None:
    assert merge3.is_text("grüße\n".encode())
    assert not merge3.is_text(b"a\0b")
    assert not merge3.is_text(b"\xff\xfe")
    assert not merge3.is_text(b"a" * (merge3.TEXT_LIMIT + 1))


def test_line_merge_combines_changes_in_different_places() -> None:
    base = b"one\ntwo\nthree\nfour\nfive\n"
    ours = b"ONE\ntwo\nthree\nfour\nfive\n"
    theirs = b"one\ntwo\nthree\nfour\nFIVE\nsix\n"
    assert merge3.merge_text(base, ours, theirs, LABELS) == (b"ONE\ntwo\nthree\nfour\nFIVE\nsix\n", False)


def test_line_merge_takes_an_equal_change_once() -> None:
    base = b"a\nb\nc\n"
    assert merge3.merge_text(base, b"a\nB\nc\n", b"a\nB\nc\n", LABELS) == (b"a\nB\nc\n", False)
    assert merge3.merge_text(base, b"a\nb\nc\n", b"a\nb\nc\nd\n", LABELS) == (b"a\nb\nc\nd\n", False)
    assert merge3.merge_text(base, b"a\nc\n", base, LABELS) == (b"a\nc\n", False)


def test_line_merge_writes_conflicts_in_the_diff3_style() -> None:
    base = b"start\nvalue = 1\nend\n"
    merged, conflicted = merge3.merge_text(base, b"start\nvalue = 2\nend\n", b"start\nvalue = 3\nend\n", LABELS)
    assert conflicted
    assert merged == (
        b"start\n<<<<<<< pull request (feature)\nvalue = 2\n||||||| common ancestor\nvalue = 1\n=======\n"
        b"value = 3\n>>>>>>> base (main)\nend\n"
    )


def test_line_merge_moves_common_lines_out_of_a_conflict() -> None:
    base = b"x\n"
    merged, conflicted = merge3.merge_text(base, b"same\nours\ntail\n", b"same\ntheirs\ntail\n", LABELS)
    assert conflicted
    assert merged.startswith(b"same\n<<<<<<< ")
    assert merged.endswith(b">>>>>>> base (main)\ntail\n")
    assert b"||||||| common ancestor\nx\n=======" in merged


def test_line_merge_terminates_sides_without_a_final_line_feed() -> None:
    merged, conflicted = merge3.merge_text(b"v1", b"v2", b"v3", LABELS)
    assert conflicted
    assert merged == (
        b"<<<<<<< pull request (feature)\nv2\n||||||| common ancestor\nv1\n=======\nv3\n>>>>>>> base (main)\n"
    )


def test_line_merge_add_add_without_ancestor() -> None:
    merged, conflicted = merge3.merge_text(None, b"a\n", b"b\n", LABELS)
    assert conflicted
    assert b"||||||| common ancestor\n=======\n" in merged
    assert merge3.merge_text(None, b"same\n", b"same\n", LABELS) == (b"same\n", False)


def test_line_merge_keeps_crlf_lines() -> None:
    base = b"a\r\nb\r\nc\r\n"
    assert merge3.merge_text(base, b"A\r\nb\r\nc\r\n", b"a\r\nb\r\nC\r\n", LABELS) == (b"A\r\nb\r\nC\r\n", False)


@pytest.mark.parametrize(
    ("text", "strict", "found"),
    [
        (b"<<<<<<< pull request (x)\na\n=======\nb\n>>>>>>> base (y)\n", True, True),
        (b"ok\n=======\nok\n", True, True),
        (b"ok\n||||||| x\n", True, True),
        (b"ok\n======== long rule\n", True, False),
        (b"plain\n", True, False),
        (b"<<<<<<< a\nx\n>>>>>>> b\n", False, True),
        (b"<<<<<<< a\nx\n", False, False),
        (b"=======\n", False, False),
        (b"<<<<<<<<\n>>>>>>>>\n", False, False),
        (b"a\r\n=======\r\n", True, True),
    ],
)
def test_marker_check(text: bytes, strict: bool, found: bool) -> None:
    assert merge3.has_markers(text, strict=strict) is found


def _variant(rng: random.Random, lines: list[str]) -> list[str]:
    out = list(lines)
    for _ in range(rng.randint(0, 3)):
        at = rng.randint(0, len(out))
        action = rng.choice(("insert", "delete", "change"))
        if action == "insert" or not out:
            out.insert(at, rng.choice(TOKENS))
        elif action == "delete":
            del out[min(at, len(out) - 1)]
        else:
            out[min(at, len(out) - 1)] = rng.choice(TOKENS)
    return out


TOKENS = ["}\n", "\n", "end\n", "x = 1\n", "x = 2\n", "return\n"]
"""Repeated lines (closing braces, blank lines): where line matching is ambiguous."""


def _ending(rng: random.Random, lines: list[str], crlf: bool) -> str:
    text = "".join(line.replace("\n", "\r\n") if crlf else line for line in lines)
    return text[: -2 if crlf else -1] if text and rng.random() < 0.2 else text


def test_line_merge_never_merges_what_git_reports_as_a_conflict(tmp_path: Path) -> None:
    """Differential check against `git merge-file --diff3` (ADR-084 "Consequences"): a conflict of git is a conflict
    here, and a clean merge here equals git's result."""
    git = shutil.which("git")
    if git is None:
        pytest.fail("the git binary is needed for the differential test")
    rng = random.Random(4801)
    agreed = 0
    for _ in range(2000):
        base = [rng.choice([f"line{index}\n", *TOKENS]) for index in range(rng.randint(0, 10))]
        ours, theirs = _variant(rng, base), _variant(rng, base)
        crlf = rng.random() < 0.2
        texts = {
            name: _ending(rng, lines, crlf) for name, lines in (("ours", ours), ("base", base), ("theirs", theirs))
        }
        files = {}
        for name, text in texts.items():
            files[name] = tmp_path / name
            files[name].write_bytes(text.encode())
        done = subprocess.run(
            [git, "merge-file", "-p", "--diff3", str(files["ours"]), str(files["base"]), str(files["theirs"])],
            capture_output=True,
            check=False,
        )
        merged, conflicted = merge3.merge_text(
            texts["base"].encode(), texts["ours"].encode(), texts["theirs"].encode(), LABELS
        )
        if done.returncode != 0:
            assert conflicted, texts
        if not conflicted:
            assert done.returncode == 0, texts
            assert merged == done.stdout, texts
            agreed += 1
    assert agreed > 500  # the cases exercise clean merges, not only conflicts
