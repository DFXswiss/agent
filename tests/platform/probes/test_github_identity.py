"""The GitHub identity probe (KOR-47 stage a, ADR-078 "Pre-study") against the GitHub double.

`probelib.github_identity.run` and the command `scripts/probes/probe_github_identity.py` talk HTTP to a started
double, with a classic token that may manage SSH signing keys and with tokens that may not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import probe_github_identity
import pytest
from probelib.github_identity import Account, Send, http_sender, run

from agent_doubles.github.server import GitHubDouble

REPO = "probe-user/probe"
NOW = 1_800_000_000.0
TOKENS = {
    "probe-admin": ["repo", "admin:ssh_signing_key"],
    "probe-write": ["repo", "write:ssh_signing_key"],
    "probe-repo": ["repo"],
}
CO_AUTHOR = Account("co-author", 202)


@pytest.fixture
def double() -> Iterator[GitHubDouble]:
    with GitHubDouble() as started:
        started.seed(
            {
                "users": [
                    {
                        "login": "probe-user",
                        "id": 201,
                        "name": "Probe User",
                        "tokens": [
                            {"token": token, "kind": "classic", "scopes": scopes} for token, scopes in TOKENS.items()
                        ],
                    },
                    {"login": "co-author", "id": 202},
                ],
                "repositories": [
                    {"full_name": REPO, "branches": [{"name": "main", "commits": [{"files": {"README.md": "x\n"}}]}]}
                ],
            }
        )
        yield started


def probe(double: GitHubDouble, token: str, **kwargs: Any) -> dict[str, Any]:
    report = run(http_sender(double.url, token), REPO, co_author=CO_AUTHOR, now=lambda: NOW, **kwargs)
    result: dict[str, Any] = report.json()
    return result


def test_a_token_that_may_sign(double: GitHubDouble) -> None:
    report = probe(double, "probe-admin", keep_ref=True)
    assert (report["account"], report["scopes"]) == ("probe-user", ["admin:ssh_signing_key", "repo"])
    results = report["results"]
    unsigned, attribution, signed = results["unsigned"], results["attribution"], results["ssh_signed"]
    assert (unsigned["verified"], unsigned["reason"]) == (False, "unsigned")
    assert attribution == {
        "sha": attribution["sha"],
        "verified": False,
        "reason": "unsigned",
        "author": "co-author",
        "committer": "probe-user",
        "ref": f"refs/heads/probe-identity-{int(NOW)}",
    }
    assert signed == {
        "status": 201,
        "sha": signed["expected_sha"],
        "expected_sha": signed["expected_sha"],
        "verified": True,
        "reason": "valid",
        "key_removed": True,
    }
    inspect = double.inspect("probe-user", "probe")
    assert inspect is not None
    assert inspect["refs"][f"refs/heads/probe-identity-{int(NOW)}"] == attribution["sha"]
    assert double.world.signing_keys == {}
    # A second run in the same second finds its branch taken.
    again = probe(double, "probe-admin", keep_ref=True)["results"]["attribution"]
    assert (again["sha"], again["ref"]) == (attribution["sha"], "HTTP 422")
    assert double.failures == []


def test_a_token_that_may_add_but_not_delete_keys(double: GitHubDouble) -> None:
    signed = probe(double, "probe-write")["results"]["ssh_signed"]
    assert (signed["verified"], signed["reason"], signed["sha"]) == (True, "valid", signed["expected_sha"])
    assert signed["key_removed"] is False
    assert list(double.world.signing_keys) == [signed["key_id"]]
    assert "ref" not in probe(double, "probe-write")["results"]["attribution"]


def test_a_token_without_the_signing_scope(double: GitHubDouble) -> None:
    report = probe(double, "probe-repo")
    assert report["scopes"] == ["repo"]
    assert report["results"]["ssh_signed"] == {"skipped": "the token lacks write:ssh_signing_key"}
    assert report["results"]["unsigned"]["reason"] == "unsigned"
    assert double.world.signing_keys == {}


def test_refused_requests(double: GitHubDouble) -> None:
    double.seed({"faults": [{"route": "POST /user/ssh_signing_keys", "kind": "error", "status": 502}]})
    assert probe(double, "probe-admin")["results"]["ssh_signed"] == {"skipped": "POST /user/ssh_signing_keys: HTTP 502"}
    double.seed({"faults": [{"route": "POST /repos/{owner}/{repo}/git/commits", "kind": "error", "skip": 2}]})
    assert probe(double, "probe-admin")["results"]["ssh_signed"] == {"status": 502, "key_removed": True}
    with pytest.raises(RuntimeError, match=r"GET /repos/probe-user/none: HTTP 404"):
        run(http_sender(double.url, "probe-admin"), "probe-user/none", co_author=CO_AUTHOR)
    with pytest.raises(RuntimeError, match=r"GET /user: HTTP 401"):
        run(http_sender(double.url, "unknown"), REPO, co_author=CO_AUTHOR)


def test_unexpected_answers(double: GitHubDouble) -> None:
    """Answers without ``verification`` or a failing commit lookup are reported, not fatal; an unknown co-author is
    attributed to nobody."""
    real = http_sender(double.url, "probe-repo")

    def send(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        if method == "GET" and "/commits/" in path and "/git/" not in path:
            return 500, None, {}
        status, payload, headers = real(method, path, body)
        if method == "POST" and isinstance(payload, dict):
            payload.pop("verification", None)
        return status, payload, headers

    tampered: Send = send
    results = run(tampered, REPO, co_author=CO_AUTHOR).json()["results"]
    assert {k: v for k, v in results["attribution"].items() if k != "sha"} == {
        "verified": None,
        "reason": None,
        "status": 500,
    }
    ghost = run(real, REPO, co_author=Account("ghost", 999)).json()["results"]["attribution"]
    assert (ghost["author"], ghost["committer"]) == (None, "probe-user")


def test_command_line(double: GitHubDouble, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("probe-admin\n", encoding="ascii")
    args = ["--repo", REPO, "--token-file", str(token_file), "--co-author", "co-author:202", "--api", double.url]
    assert probe_github_identity.main(args) == 0
    out = capsys.readouterr().out
    assert "probe-admin" not in out
    report = json.loads(out)
    assert report["results"]["ssh_signed"]["verified"] is True
    assert report["results"]["attribution"]["author"] == "co-author"
    assert probe_github_identity.main([*args, "--keep-ref"]) == 0
    assert "ref" in json.loads(capsys.readouterr().out)["results"]["attribution"]
    failed = ["--repo", "probe-user/none", "--token-file", str(token_file), "--co-author", "x:1", "--api", double.url]
    assert probe_github_identity.main(failed) == 1
    out = capsys.readouterr().out
    assert json.loads(out) == {"error": "GET /repos/probe-user/none: HTTP 404"}
    assert "probe-admin" not in out
    with pytest.raises(SystemExit) as exit_info:
        probe_github_identity.main([*args[:4], "--co-author", "co-author"])
    assert exit_info.value.code == 2
    assert "probe-admin" not in capsys.readouterr().err
