"""U-DBL-github: lifecycle, control routes, merges in the history and binary changes."""

from __future__ import annotations

import base64
import threading
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.github.repos import SeedError
from agent_doubles.github.server import GitHubDouble

REPO = "/repos/octo-org/hello"


def test_context_manager_and_serve_forever() -> None:
    with GitHubDouble() as double:
        assert DoubleClient(double.url).state()["double"] == "github"
    foreground = GitHubDouble()
    thread = threading.Thread(target=foreground.serve_forever)
    thread.start()
    try:
        assert DoubleClient(foreground.url).state()["requests"] == 0
    finally:
        foreground.stop()
        thread.join()


def test_control_route_errors(github: Any) -> None:
    assert github.control("GET", "nothing").status == 404
    wrong = github.control("GET", "seed")
    assert (wrong.status, wrong.headers["allow"]) == (405, "POST")
    assert github.control("POST", "repos/octo-org/hello").status == 405
    too_long = github.client.request("POST", "/_double/seed", b"{}", {"content-length": "x"})
    assert too_long.status == 400
    assert github.client.request("POST", "/_double/seed", b"{nope").json() == {"error": "body is not JSON"}
    refused = github.control("POST", "seed", [])
    assert (refused.status, refused.json()) == (400, {"error": "seed must be an object"})
    with pytest.raises(SeedError):
        github.double.seed("x")
    assert github.state()["requests"] == 0


def test_clock_reset_and_failures(github: Any) -> None:
    before = github.double.clock()
    advanced = github.control("POST", "clock", {"advance": 30})
    assert advanced.json() == {"now": before + 30}
    assert github.control("POST", "clock", {"advance": 0.5}).json()["now"] == before + 30.5
    bodies: list[Any] = [{"advance": -1}, {"advance": True}, {"advance": "1"}, []]
    for body in bodies:
        assert github.control("POST", "clock", body).status == 400
    github.api("GET", "/nowhere")
    assert github.double.failures == ["unhandled route GET /nowhere"]
    state = github.control("POST", "reset").json()
    assert (state["ok"], state["requests"], state["failures"]) == (True, 0, [])
    assert github.client.journal() == []
    assert github.api("GET", "/user").status == 401
    assert github.control("GET", "repos/octo-org/hello").status == 404


def test_merge_commits_and_binary_files(github: Any) -> None:
    main = github.get(f"{REPO}/commits/main")
    feature = github.get(f"{REPO}/commits/feature")
    png = base64.b64encode(b"\x89PNG\0\1").decode()
    blob = github.api("POST", f"{REPO}/git/blobs", {"content": png, "encoding": "base64"}).json()["sha"]
    tree = github.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "base_tree": feature["commit"]["tree"]["sha"],
            "tree": [{"path": "logo.png", "mode": "100644", "type": "blob", "sha": blob}],
        },
    ).json()["sha"]
    merge = github.api(
        "POST", f"{REPO}/git/commits", {"message": "Merge.", "tree": tree, "parents": [main["sha"], feature["sha"]]}
    ).json()
    assert [p["sha"] for p in merge["parents"]] == [main["sha"], feature["sha"]]
    assert github.api("PATCH", f"{REPO}/git/refs/heads/feature", {"sha": merge["sha"]}).status == 200
    compare = github.get(f"{REPO}/compare/main...feature")
    assert (compare["status"], compare["ahead_by"]) == ("ahead", 2)
    github.api("POST", f"{REPO}/pulls", {"title": "Logo", "head": "feature", "base": "main"})
    files = {f["filename"]: f for f in github.get(f"{REPO}/pulls/1/files")}
    assert "patch" not in files["logo.png"]
    assert files["logo.png"]["additions"] == 0
    assert "patch" in files["src/app.py"]
    assert github.get(f"{REPO}/pulls/1")["commits"] == 2


def test_seeded_deletion(start_github: Any) -> None:
    gh = start_github(
        {
            "users": [{"login": "octo-user", "tokens": ["double-user-token"]}],
            "repositories": [
                {
                    "full_name": "octo-org/hello",
                    "branches": [
                        {"name": "main", "commits": [{"files": {"a": "1", "b": "2"}}, {"files": {"a": None}}]}
                    ],
                }
            ],
            "installations": [{"account": "octo-org", "repository_selection": "all", "users": ["octo-user"]}],
        }
    )
    tree = gh.get(f"{REPO}/git/trees/main")
    assert [e["path"] for e in tree["tree"]] == ["b"]


def test_seed_through_the_control_route(start_github: Any) -> None:
    gh = start_github(None)
    seeded = gh.control("POST", "seed", {"users": [{"login": "late", "tokens": ["late-token"]}]})
    assert (seeded.status, seeded.json()["ok"]) == (200, True)
    assert gh.get("/user", token="late-token")["login"] == "late"
