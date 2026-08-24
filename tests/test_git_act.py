"""Pure git_act runner fakes. No Store / Postgres."""

from __future__ import annotations

import json

import pytest

from agent_cli.git_act import GitActError, measure_mergeable, push_branch
from agent_cli.runtime import Completed

pytestmark = pytest.mark.no_pg

CWD = "/tmp/repo"
SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FORCE_FLAGS = ("--force", "--force-with-lease", "-f")
PUSH_ARGV = ["git", "-C", CWD, "push", "--", "origin", "HEAD:refs/heads/feat-x"]


def _config(argv: list[str]) -> Completed | None:
    if "config" not in argv or "--get" not in argv:
        return None
    key = argv[-1]
    if key == "branch.feat-x.remote":
        return Completed(0, "origin\n", "")
    if key == "branch.feat-x.merge":
        return Completed(0, "refs/heads/feat-x\n", "")
    return Completed(1, "", "")


def _assert_git_c(argv: list[str]) -> None:
    assert argv[:3] == ["git", "-C", CWD]
    for flag in FORCE_FLAGS:
        assert flag not in argv


def test_push_ahead_one_pushes_without_force() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        _assert_git_c(argv)
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/feat-x\n", "")
        cfg = _config(argv)
        if cfg is not None:
            return cfg
        if "fetch" in argv:
            assert argv == ["git", "-C", CWD, "fetch", "--", "origin"]
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0\t1\n", "")
        if argv == PUSH_ARGV:
            return Completed(0, "", "")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, SHA + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    got = push_branch(cwd=CWD, runner=runner)
    assert got == SHA
    assert ["git", "-C", CWD, "fetch", "--", "origin"] in calls
    assert PUSH_ARGV in calls
    fetch_at = calls.index(["git", "-C", CWD, "fetch", "--", "origin"])
    push_at = calls.index(PUSH_ARGV)
    assert fetch_at < push_at
    for argv in calls:
        for flag in FORCE_FLAGS:
            assert flag not in argv


def test_push_ahead_zero_skips_push() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        _assert_git_c(argv)
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/feat-x\n", "")
        cfg = _config(argv)
        if cfg is not None:
            return cfg
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0 0\n", "")
        if "push" in argv:
            raise AssertionError("must not push when ahead==0")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, "abc1234\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    assert push_branch(cwd=CWD, runner=runner) == "abc1234"
    assert not any("push" in a for a in calls)


def test_push_protected_branch_develop() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "develop\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError):
        push_branch(cwd=CWD, runner=runner)
    assert not any("push" in a for a in calls)


def test_push_dirty_porcelain() -> None:
    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, " M file.py\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="uncommitted changes"):
        push_branch(cwd=CWD, runner=runner)


def test_push_no_upstream() -> None:
    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv:
            return Completed(1, "", "no upstream configured")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="no upstream"):
        push_branch(cwd=CWD, runner=runner)


def test_push_behind_errors_no_push() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/feat-x\n", "")
        cfg = _config(argv)
        if cfg is not None:
            return cfg
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "1\t0\n", "")
        if "push" in argv:
            raise AssertionError("must not push when behind")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="branch is behind upstream"):
        push_branch(cwd=CWD, runner=runner)
    assert not any("push" in a for a in calls)


def test_push_upstream_origin_develop_refused() -> None:
    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv:
            return Completed(0, "origin/develop\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError):
        push_branch(cwd=CWD, runner=runner)


def test_mergeable_open_empty_checks() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/1",
                        "number": 1,
                    }
                ),
                "",
            )
        if "checks" in argv:
            return Completed(0, "[]", "")
        raise AssertionError(f"unexpected argv: {argv}")

    evidence = measure_mergeable(cwd=CWD, runner=runner)
    assert "mergeable" in evidence
    assert "checks=ok" in evidence
    assert "number=1" in evidence


def test_mergeable_all_success() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "open",
                        "url": "https://example.invalid/p/2",
                        "number": 2,
                    }
                ),
                "",
            )
        if "checks" in argv:
            return Completed(
                0,
                json.dumps(
                    [
                        {"name": "ci", "state": "SUCCESS"},
                        {"name": "lint", "state": "SUCCESS"},
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    evidence = measure_mergeable(cwd=CWD, runner=runner)
    assert "checks=ok" in evidence


def test_mergeable_conflicting() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "CONFLICTING",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/3",
                        "number": 3,
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="CONFLICTING"):
        measure_mergeable(cwd=CWD, runner=runner)


def test_mergeable_check_failure() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/4",
                        "number": 4,
                    }
                ),
                "",
            )
        if "checks" in argv:
            return Completed(
                0,
                json.dumps([{"name": "ci", "state": "FAILURE"}]),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="check ci is FAILURE"):
        measure_mergeable(cwd=CWD, runner=runner)


def test_mergeable_check_pending() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/5",
                        "number": 5,
                    }
                ),
                "",
            )
        if "checks" in argv:
            return Completed(
                0,
                json.dumps([{"name": "ci", "state": "PENDING"}]),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="check ci is PENDING"):
        measure_mergeable(cwd=CWD, runner=runner)


def test_mergeable_check_skipped() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/6",
                        "number": 6,
                    }
                ),
                "",
            )
        if "checks" in argv:
            return Completed(
                0,
                json.dumps([{"name": "ci", "state": "SKIPPED"}]),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="check ci is SKIPPED"):
        measure_mergeable(cwd=CWD, runner=runner)


def test_mergeable_missing_number() -> None:
    def runner(argv: list[str]) -> Completed:
        if "pr" in argv and "view" in argv:
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/7",
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="missing number"):
        measure_mergeable(cwd=CWD, runner=runner)
