"""Pure git_act runner fakes. No Store / Postgres."""

from __future__ import annotations

import json

import pytest

from agent_cli.git_act import GitActError, measure_mergeable, push_branch
from agent_cli.runtime import Completed

pytestmark = pytest.mark.no_pg

CWD = "/tmp/repo"
SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ORIGIN = "https://github.com/owner/repo.git"
REPO = "owner/repo"
BRANCH = "feat-x"
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


def _apply_rules(url: str, rules: list[tuple[str, str]]) -> str:
    best: tuple[str, str] | None = None
    for base, old in rules:
        if url.startswith(old) and (best is None or len(old) > len(best[1])):
            best = (base, old)
    if best is None:
        return url
    return best[0] + url[len(best[1]) :]


def _origin_resolution(
    argv: list[str],
    *,
    url: str = ORIGIN,
    instead_of: list[tuple[str, str]] | None = None,
    push_instead_of: list[tuple[str, str]] | None = None,
) -> Completed | None:
    """Simulate git remote get-url (rewrites already applied, as real Git does)."""
    if argv[:3] != ["git", "-C", CWD]:
        return None
    if argv == ["git", "-C", CWD, "rev-parse", "--abbrev-ref", "HEAD"]:
        return Completed(0, BRANCH + "\n", "")
    if "remote" in argv and "get-url" in argv:
        effective = url
        effective = _apply_rules(effective, instead_of or [])
        if "--push" in argv:
            effective = _apply_rules(effective, push_instead_of or [])
        return Completed(0, effective + "\n", "")
    return None


def _assert_git_c(argv: list[str]) -> None:
    assert argv[:3] == ["git", "-C", CWD]
    for flag in FORCE_FLAGS:
        assert flag not in argv


def _mergeable_view_argv(selector: str = BRANCH, repo: str = REPO) -> list[str]:
    return ["gh", "pr", "view", selector, "--repo", repo]


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
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/develop\n", "")
        if "config" in argv and "--get" in argv:
            key = argv[-1]
            if key == "branch.feat-x.remote":
                return Completed(0, "origin\n", "")
            if key == "branch.feat-x.merge":
                return Completed(0, "refs/heads/develop\n", "")
            return Completed(1, "", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="protected branch"):
        push_branch(cwd=CWD, runner=runner)


def test_push_upstream_feat_main_not_protected() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/feat/main\n", "")
        if "config" in argv and "--get" in argv:
            key = argv[-1]
            if key == "branch.feat-x.remote":
                return Completed(0, "origin\n", "")
            if key == "branch.feat-x.merge":
                return Completed(0, "refs/heads/feat/main\n", "")
            return Completed(1, "", "")
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0 0\n", "")
        if argv[-1] == "HEAD":
            return Completed(0, SHA + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    assert push_branch(cwd=CWD, runner=runner) == SHA
    assert not any(len(a) > 3 and a[3] == "push" for a in calls)


def test_mergeable_open_empty_checks() -> None:
    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            assert argv[argv.index("--repo") + 1] == REPO
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/1",
                        "number": 1,
                        "headRefOid": SHA,
                    }
                ),
                "",
            )
        if argv[:4] == ["gh", "pr", "checks", "1"] and "--repo" in argv:
            assert argv[argv.index("--repo") + 1] == REPO
            return Completed(0, "[]", "")
        raise AssertionError(f"unexpected argv: {argv}")

    evidence = measure_mergeable(cwd=CWD, runner=runner)
    assert "mergeable" in evidence
    assert "checks=ok" in evidence
    assert "number=1" in evidence


def test_mergeable_all_success() -> None:
    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "open",
                        "url": "https://example.invalid/p/2",
                        "number": 2,
                        "headRefOid": SHA,
                    }
                ),
                "",
            )
        if "checks" in argv:
            assert "--repo" in argv and argv[argv.index("--repo") + 1] == REPO
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
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "CONFLICTING",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/3",
                        "number": 3,
                        "headRefOid": SHA,
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="CONFLICTING"):
        measure_mergeable(cwd=CWD, runner=runner)


def test_mergeable_check_failure() -> None:
    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/4",
                        "number": 4,
                        "headRefOid": SHA,
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
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/5",
                        "number": 5,
                        "headRefOid": SHA,
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
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/6",
                        "number": 6,
                        "headRefOid": SHA,
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


def test_mergeable_head_mismatch() -> None:
    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
            return Completed(
                0,
                json.dumps(
                    {
                        "mergeable": "MERGEABLE",
                        "state": "OPEN",
                        "url": "https://example.invalid/p/8",
                        "number": 8,
                        "headRefOid": SHA,
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="does not match"):
        measure_mergeable(cwd=CWD, runner=runner, expected_head="bbbbbbb")


def test_mergeable_missing_number() -> None:
    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[:5] == _mergeable_view_argv():
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


def test_mergeable_rejects_credential_origin_without_leaking() -> None:
    secret = "super-secret-token"

    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(
            argv, url=f"https://x-access-token:{secret}@github.com/owner/repo.git"
        )
        if origin is not None:
            return origin
        raise AssertionError("gh must not run when origin is unsafe")

    with pytest.raises(GitActError, match="must not contain credentials") as excinfo:
        measure_mergeable(cwd=CWD, runner=runner)
    assert secret not in str(excinfo.value)


def test_mergeable_passes_explicit_repo_and_branch_for_container_context() -> None:
    """Regression: gh must receive --repo and a PR selector so docker-exec needs no cwd."""
    gh_calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        origin = _origin_resolution(argv)
        if origin is not None:
            return origin
        if argv[0] == "gh":
            gh_calls.append(list(argv))
            if "view" in argv:
                assert argv[1:4] == ["pr", "view", BRANCH]
                assert "--repo" in argv and argv[argv.index("--repo") + 1] == REPO
                return Completed(
                    0,
                    json.dumps(
                        {
                            "mergeable": "MERGEABLE",
                            "state": "OPEN",
                            "url": "https://example.invalid/p/9",
                            "number": 9,
                            "headRefOid": SHA,
                        }
                    ),
                    "",
                )
            if "checks" in argv:
                assert "--repo" in argv and argv[argv.index("--repo") + 1] == REPO
                return Completed(0, "[]", "")
        raise AssertionError(f"unexpected argv: {argv}")

    evidence = measure_mergeable(cwd=CWD, runner=runner)
    assert "number=9" in evidence
    assert gh_calls
    for call in gh_calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == REPO
        assert "-C" not in call
        assert "--workdir" not in call


def test_mergeable_uses_explicit_task_pr_without_git() -> None:
    """Known task PR target (fork upstream) must not consult origin or cwd."""
    fork_target = "upstream/product"
    gh_calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        if argv and argv[0] == "git":
            raise AssertionError("explicit repo+number must not call git")
        if argv[0] == "gh":
            gh_calls.append(list(argv))
            if "view" in argv:
                assert argv[:6] == ["gh", "pr", "view", "42", "--repo", fork_target]
                return Completed(
                    0,
                    json.dumps(
                        {
                            "mergeable": "MERGEABLE",
                            "state": "OPEN",
                            "url": "https://example.invalid/p/42",
                            "number": 42,
                            "headRefOid": SHA,
                        }
                    ),
                    "",
                )
            if "checks" in argv:
                assert argv[:4] == ["gh", "pr", "checks", "42"]
                assert argv[argv.index("--repo") + 1] == fork_target
                return Completed(0, "[]", "")
        raise AssertionError(f"unexpected argv: {argv}")

    evidence = measure_mergeable(
        cwd="/nonexistent/executor/cwd",
        runner=runner,
        repo=fork_target,
        number=42,
    )
    assert "number=42" in evidence
    assert gh_calls
    for call in gh_calls:
        assert call[call.index("--repo") + 1] == fork_target
