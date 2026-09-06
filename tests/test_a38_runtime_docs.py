"""Runtime-bound A38 documentation and policy-link tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest

from agent_cli import a38_guard as guard

pytestmark = pytest.mark.no_pg

RUNTIME = "d" * 40
HEAD = "a" * 40
BASE = "b" * 40
TARGET = "example/consumer"
FORK = "contributor/fork"


def _pull(*, state: str = "open", head_repo: str = TARGET) -> guard.PullSnapshot:
    return guard.PullSnapshot(
        repo=TARGET,
        number=7,
        state=state,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref="develop",
        private=True,
        author_id=101,
        author_login="author",
        head_repo=head_repo,
    )


def _assessment(**kwargs) -> guard.Assessment:
    return guard.assess_from_parts(
        pull=kwargs.pop("pull", _pull()),
        policy={"mode": "enforce", "jobs": []},
        policy_error=None,
        workflow_problems=[],
        author_comment=None,
        runtime_revision=RUNTIME,
        **kwargs,
    )


def test_consumer_needs_no_docs_and_central_urls_use_runtime_sha() -> None:
    assessment = _assessment()
    expected_root = f"https://github.com/DFXswiss/agent/blob/{RUNTIME}"
    assert assessment.standard_url == f"{expected_root}/docs/a38.md"
    assert assessment.guard_docs_url == f"{expected_root}/docs/a38-guard.md"
    assert assessment.policy_url == (
        f"https://github.com/{TARGET}/blob/{BASE}/.github/a38.json"
    )
    assert HEAD not in assessment.standard_url
    assert BASE not in assessment.standard_url
    assert TARGET not in assessment.standard_url
    assert assessment.standard_url in assessment.comment_body
    assert assessment.guard_docs_url in assessment.comment_body
    assert assessment.policy_url in assessment.comment_body
    payload = assessment.to_json()
    assert payload["standard_url"] == assessment.standard_url
    assert payload["guard_docs_url"] == assessment.guard_docs_url
    assert payload["policy_url"] == assessment.policy_url


def test_policy_url_tracks_approved_head_policy_in_fork() -> None:
    assessment = _assessment(
        pull=_pull(head_repo=FORK),
        policy_repo=FORK,
        policy_sha=HEAD,
    )
    assert assessment.policy_sha == HEAD
    assert assessment.policy_url == (
        f"https://github.com/{FORK}/blob/{HEAD}/.github/a38.json"
    )
    assert HEAD not in assessment.standard_url
    assert assessment.standard_url.endswith(f"/{RUNTIME}/docs/a38.md")


@pytest.mark.parametrize("revision", ["", "develop", "v1", "A" * 40, "f" * 39])
def test_explicit_runtime_revision_must_be_lowercase_full_sha(revision: str) -> None:
    with pytest.raises(guard.GuardError, match="A38_RUNTIME_REVISION"):
        guard.resolve_runtime_revision({"A38_RUNTIME_REVISION": revision})


def test_explicit_runtime_revision_is_accepted_without_git() -> None:
    assert guard.resolve_runtime_revision({"A38_RUNTIME_REVISION": RUNTIME}) == RUNTIME


def test_packaged_runtime_without_explicit_revision_fails_without_parent_walk(
    tmp_path: Path,
) -> None:
    module = tmp_path / "site-packages" / "agent_cli" / "a38_guard.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed package\n")
    with pytest.raises(guard.GuardError, match="installed runtime"):
        guard.resolve_runtime_revision({}, module_file=module)


def test_source_fallback_is_root_anchored_and_removes_all_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trusted-runtime"
    module = root / "src" / "agent_cli" / "a38_guard.py"
    module.parent.mkdir(parents=True)
    module.write_text("# source checkout\n")
    (root / ".git").mkdir()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.chdir(consumer)

    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = f"{root}\n" if argv[-1] == "--show-toplevel" else f"{RUNTIME}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    hostile_env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_DIR": str(consumer / ".git"),
        "GIT_WORK_TREE": str(consumer),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(consumer),
        "GIT_EXEC_PATH": str(consumer),
        "GH_TOKEN": "must-not-reach-git",
        "GITHUB_TOKEN": "must-not-reach-git-either",
    }
    with mock.patch.object(guard.subprocess, "run", side_effect=fake_run):
        assert guard.resolve_runtime_revision(hostile_env, module_file=module) == RUNTIME

    assert len(calls) == 2
    for argv, options in calls:
        assert options["cwd"] == root
        assert f"--git-dir={root / '.git'}" in argv
        assert f"--work-tree={root}" in argv
        assert str(root) in argv
        assert options["env"]["PATH"] == hostile_env["PATH"]
        assert all(not key.startswith("GIT_") for key in options["env"])
        assert "GH_TOKEN" not in options["env"]
        assert "GITHUB_TOKEN" not in options["env"]
        assert str(consumer) not in argv


def test_source_fallback_rejects_mismatched_git_top_level(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    module = root / "src" / "agent_cli" / "a38_guard.py"
    module.parent.mkdir(parents=True)
    module.write_text("# source checkout\n")
    (root / ".git").mkdir()
    completed = subprocess.CompletedProcess([], 0, stdout=f"{tmp_path}\n", stderr="")
    with mock.patch.object(guard.subprocess, "run", return_value=completed):
        with pytest.raises(guard.GuardError, match="top-level does not match"):
            guard.resolve_runtime_revision({}, module_file=module)


def test_closed_pull_does_not_resolve_runtime_provenance() -> None:
    with mock.patch.object(
        guard, "resolve_runtime_revision", side_effect=AssertionError("must not resolve")
    ):
        assessment = guard.assess_pull(
            mock.Mock(), TARGET, 7, pull=_pull(state="closed")
        )
    assert assessment.ok is True
    assert assessment.closed is True
    assert assessment.comment_body == ""


def test_ignored_event_does_not_resolve_runtime_provenance() -> None:
    api = mock.Mock()
    api.resolve_own_user.return_value = (999, "guard")
    payload = {"action": "created", "issue": {"number": 7}}
    with mock.patch.object(
        guard, "resolve_runtime_revision", side_effect=AssertionError("must not resolve")
    ):
        result = guard.reconcile_event(
            api,
            event_name="issue_comment",
            payload=payload,
            runtime_env={},
        )
    assert result["ok"] is True
    assert result["status"] == "ignored"


def test_empty_all_open_does_not_resolve_runtime_provenance(capsys) -> None:
    api = mock.Mock()
    api.paginate.return_value = []
    with mock.patch.object(
        guard, "resolve_runtime_revision", side_effect=AssertionError("must not resolve")
    ):
        code = guard.main(
            ["reconcile", "--repo", TARGET, "--all-open", "--dry-run", "--json"],
            env={},
            api=api,
        )
    assert code == 0
    assert '"results": []' in capsys.readouterr().out
