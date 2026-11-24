"""U-DBL-github: every REST route of the GitHub double against its real HTTP server (05-runner.md §12.1)."""

from __future__ import annotations

import base64
import gzip
import io
import tarfile
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.github.objects import EMPTY_TREE, object_id
from agent_doubles.github.server import ROUTES, match_route

REPO = "/repos/octo-org/hello"
OTHER = "double-other-token"


def head(gh: Any, branch: str = "main") -> str:
    sha: str = gh.get(f"{REPO}/git/ref/heads/{branch}")["object"]["sha"]
    return sha


# --- headers, credentials, rights --------------------------------------------------------------------


def test_api_headers_and_rate_limit(github: Any) -> None:
    response = github.api("GET", "/user")
    assert response.status == 200
    assert response.headers["x-github-api-version-selected"] == "2022-11-28"
    assert response.headers["x-github-media-type"] == "github.v3; format=json"
    assert response.headers["x-github-request-id"].startswith("DBL0:")
    assert response.headers["x-ratelimit-limit"] == "5000"
    assert response.headers["x-ratelimit-remaining"] == "4999"
    assert response.headers["x-ratelimit-used"] == "1"
    assert response.headers["x-ratelimit-resource"] == "core"
    reset = int(response.headers["x-ratelimit-reset"])
    assert reset == int(github.clock.now) + 3600
    assert github.api("GET", "/user").headers["x-ratelimit-remaining"] == "4998"
    assert github.api("GET", "/user", token=OTHER).headers["x-ratelimit-remaining"] == "4999"
    github.clock.now += 3600
    again = github.api("GET", "/user")
    assert again.headers["x-ratelimit-remaining"] == "4999"
    assert int(again.headers["x-ratelimit-reset"]) == reset + 3600
    assert github.state()["ok"] is True


def test_user(github: Any) -> None:
    user = github.get("/user")
    assert (user["login"], user["id"], user["name"], user["type"]) == ("octo-user", 101, "Octo User", "User")
    assert user["url"] == f"{github.double.url}/users/octo-user"
    other = github.get("/user", token=OTHER)
    assert (other["login"], other["email"], other["name"]) == ("other-user", "other@example.invalid", None)


def test_missing_and_wrong_headers_are_failures(github: Any) -> None:
    client: DoubleClient = github.client
    bare = client.request("GET", "/user", headers={"authorization": "Bearer double-user-token"})
    assert bare.status == 200
    state = github.state()
    assert state["ok"] is False
    assert state["failures"] == [
        "GET /user: missing X-GitHub-Api-Version",
        "GET /user: Accept is not application/vnd.github+json",
    ]
    old = github.api("GET", "/user", headers={"x-github-api-version": "2020-01-01"})
    assert old.status == 400
    assert old.json()["message"] == "API version '2020-01-01' is not supported."
    assert "unsupported X-GitHub-Api-Version '2020-01-01'" in github.state()["failures"][-1]


def test_credentials(github: Any) -> None:
    missing = github.api("GET", "/user", token=None)
    assert (missing.status, missing.json()["message"]) == (401, "Requires authentication")
    assert github.state()["failures"] == ["GET /user: request without credentials"]
    github.control("POST", "reset")
    github.double.seed({"users": [{"login": "u", "tokens": ["t1"]}]})
    bad = github.api("GET", "/user", token="unknown")
    assert (bad.status, bad.json()) == (
        401,
        {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest", "status": "401"},
    )
    legacy = github.api("GET", "/user", token=None, headers={"authorization": "token t1"})
    assert legacy.json()["login"] == "u"
    assert github.state()["ok"] is True


def test_repository_access_follows_installations(github: Any) -> None:
    repo = github.get(REPO)
    assert (repo["full_name"], repo["private"], repo["visibility"], repo["default_branch"]) == (
        "octo-org/hello",
        True,
        "private",
        "main",
    )
    assert repo["owner"]["type"] == "Organization"
    assert repo["permissions"]["push"] is True
    assert github.get("/repos/OCTO-ORG/Hello")["full_name"] == "octo-org/hello"
    for path, token in (
        ("/repos/octo-org/hidden", "double-user-token"),
        (REPO, OTHER),
        ("/repos/octo-org/missing", "double-user-token"),
    ):
        response = github.api("GET", path, token=token)
        assert response.status == 404
        assert response.json()["message"] == "Not Found"
    assert github.state()["ok"] is True


def test_write_needs_the_installation_permission(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    data = seed()
    data["installations"][0]["permissions"] = {"contents": "read", "issues": "read", "pull_requests": "read"}
    gh = start_github(data)
    assert gh.get(REPO)["permissions"]["push"] is False
    main = head(gh)
    for method, path, body in (
        ("POST", f"{REPO}/git/refs", {"ref": "refs/heads/x", "sha": main}),
        ("PATCH", f"{REPO}/git/refs/heads/main", {"sha": main}),
        ("POST", f"{REPO}/git/blobs", {"content": "x"}),
        ("POST", f"{REPO}/git/trees", {"tree": []}),
        ("POST", f"{REPO}/git/commits", {"message": "m", "tree": EMPTY_TREE}),
        ("POST", f"{REPO}/pulls", {"title": "t", "head": "feature", "base": "main"}),
        ("POST", f"{REPO}/issues/1/comments", {"body": "b"}),
    ):
        response = gh.api(method, path, body)
        assert (response.status, response.json()["message"]) == (403, "Resource not accessible by integration")
    assert gh.state()["ok"] is True


# --- installations -----------------------------------------------------------------------------------


def test_installations_and_their_repositories(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    data = seed()
    data["installations"].append({"id": 7, "account": "octo-user", "repository_selection": "all"})
    data["repositories"].append({"full_name": "octo-user/own", "branches": [{"name": "main", "commits": [{}]}]})
    gh = start_github(data)
    listed = gh.get("/user/installations")
    assert listed["total_count"] == 2
    assert [(i["id"], i["account"]["login"], i["target_type"]) for i in listed["installations"]] == [
        (7, "octo-user", "User"),
        (31, "octo-org", "Organization"),
    ]
    assert listed["installations"][1]["permissions"] == {
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    selected = gh.get("/user/installations/31/repositories")
    assert selected["repository_selection"] == "selected"
    assert [r["full_name"] for r in selected["repositories"]] == ["octo-org/hello", "octo-org/second"]
    assert selected["repositories"][1]["private"] is False
    every = gh.get("/user/installations/7/repositories")
    assert [r["full_name"] for r in every["repositories"]] == ["octo-user/own"]
    assert gh.get("/repos/octo-user/own")["owner"]["type"] == "User"
    assert gh.get("/user/installations", token=OTHER) == {"total_count": 0, "installations": []}
    for path in ("/user/installations/99/repositories", "/user/installations/31/repositories"):
        assert gh.api("GET", path, token=OTHER if "31" in path else "double-user-token").status == 404


def test_personal_tokens_without_the_app(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    """KOR-27: a personal token is refused the installation routes, lists and reaches its own and its collaborator
    repositories (write access, `X-OAuth-Scopes`); `/user/repos` of an App user token lists its installations'."""
    data = seed()
    data["users"].append(
        {"login": "op-account", "personal_tokens": ["personal-token"], "collaborator": [REPO[7:], "octo-org/none"]}
    )
    data["repositories"].append({"full_name": "op-account/own", "branches": [{"name": "main", "commits": [{}]}]})
    gh = start_github(data)
    for path in ("/user/installations", "/user/installations/31/repositories"):
        refused = gh.api("GET", path, token="personal-token")
        assert refused.status == 403
        assert refused.json()["message"].startswith("You must authenticate with an access token authorized")
    user = gh.api("GET", "/user", token="personal-token")
    assert (user.status, user.headers["x-oauth-scopes"]) == (200, "repo")
    assert "x-oauth-scopes" not in gh.api("GET", "/user").headers
    listed = gh.get("/user/repos", token="personal-token")
    assert [r["full_name"] for r in listed] == ["octo-org/hello", "op-account/own"]
    assert gh.get(REPO, token="personal-token")["permissions"]["push"] is True
    assert gh.api("GET", "/repos/octo-org/second", token="personal-token").status == 404
    assert gh.api("GET", "/repos/octo-org/none", token="personal-token").status == 404
    created = gh.api("POST", f"{REPO}/git/refs", {"ref": "refs/heads/op", "sha": head(gh)}, token="personal-token")
    assert created.status == 201
    assert [r["full_name"] for r in gh.get("/user/repos")] == ["octo-org/hello", "octo-org/second"]
    assert gh.get("/user/repos", token=OTHER) == []
    assert gh.state()["ok"] is True


def test_pagination_with_link_headers(start_github: Callable[..., Any]) -> None:
    names = [f"octo-org/r{i:02d}" for i in range(5)]
    gh = start_github(
        {
            "users": [{"login": "octo-user", "tokens": ["double-user-token"]}],
            "repositories": [{"full_name": n, "branches": [{"name": "main", "commits": [{}]}]} for n in names],
            "installations": [{"id": 1, "account": "octo-org", "repository_selection": "all", "users": ["octo-user"]}],
        }
    )
    path = "/user/installations/1/repositories"
    first = gh.api("GET", f"{path}?per_page=2")
    assert [r["full_name"] for r in first.json()["repositories"]] == names[:2]
    assert first.json()["total_count"] == 5
    base = f"{gh.double.url}{path}"
    assert first.headers["link"] == (f'<{base}?per_page=2&page=2>; rel="next", <{base}?per_page=2&page=3>; rel="last"')
    middle = gh.api("GET", f"{path}?per_page=2&page=2")
    assert middle.headers["link"] == (
        f'<{base}?per_page=2&page=1>; rel="prev", <{base}?per_page=2&page=1>; rel="first", '
        f'<{base}?per_page=2&page=3>; rel="next", <{base}?per_page=2&page=3>; rel="last"'
    )
    last = gh.api("GET", f"{path}?per_page=2&page=3")
    assert [r["full_name"] for r in last.json()["repositories"]] == names[4:]
    assert "next" not in last.headers["link"]
    beyond = gh.api("GET", f"{path}?per_page=2&page=9")
    assert beyond.json()["repositories"] == []
    assert f'<{base}?per_page=2&page=3>; rel="prev"' in beyond.headers["link"]
    whole = gh.api("GET", f"{path}?per_page=abc&page=0")
    assert len(whole.json()["repositories"]) == 5
    assert "link" not in whole.headers
    capped = gh.api("GET", f"{path}?per_page=1000")
    assert len(capped.json()["repositories"]) == 5


# --- commits, tarball --------------------------------------------------------------------------------


def test_commit_resolution(github: Any) -> None:
    main = head(github)
    by_branch = github.get(f"{REPO}/commits/main")
    assert by_branch["sha"] == main
    assert by_branch["commit"]["message"] == "Second commit."
    assert by_branch["stats"] == {"total": 1, "additions": 1, "deletions": 0}
    assert by_branch["files"][0]["filename"] == "README.md"
    assert by_branch["files"][0]["patch"] == "@@ -1 +1,2 @@\n hello\n+world"
    assert by_branch["author"] is None
    for ref in (main, main[:7], "v1.0", "heads/main", "refs/heads/main", "tags/v1.0"):
        assert github.get(f"{REPO}/commits/{ref}")["sha"] == main
    first = by_branch["parents"][0]["sha"]
    root = github.get(f"{REPO}/commits/{first}")
    assert root["parents"] == []
    assert sorted(f["filename"] for f in root["files"]) == ["README.md", "src/app.py"]
    for ref in ("nope", "0000000", main[:6]):
        missing = github.api("GET", f"{REPO}/commits/{ref}")
        assert (missing.status, missing.json()["message"]) == (422, f"No commit found for SHA: {ref}")


def test_tarball_redirects_to_codeload(github: Any) -> None:
    main = head(github)
    redirect = github.api("GET", f"{REPO}/tarball/main")
    assert redirect.status == 302
    assert redirect.body == b""
    location = urlsplit(redirect.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == github.double.url
    assert location.path == f"/codeload/octo-org/hello/legacy.tar.gz/{main}"
    assert location.query.startswith("token=DL")
    download = github.client.request("GET", f"{location.path}?{location.query}")
    assert download.status == 200
    assert download.headers["content-type"] == "application/x-gzip"
    assert download.headers["content-disposition"] == f"attachment; filename=octo-org-hello-{main[:7]}.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(download.body)) as archive:
        assert archive.pax_headers == {"comment": main}
        names = archive.getnames()
        assert names == [
            f"octo-org-hello-{main[:7]}",
            f"octo-org-hello-{main[:7]}/README.md",
            f"octo-org-hello-{main[:7]}/src",
            f"octo-org-hello-{main[:7]}/src/app.py",
        ]
        readme = archive.extractfile(names[1])
        assert readme is not None
        assert readme.read() == b"hello\nworld\n"
    # Without the token, with a token for another commit, or for an unknown ref: not found.
    first = github.get(f"{REPO}/commits/main")["parents"][0]["sha"]
    for path in (location.path, f"/codeload/octo-org/hello/legacy.tar.gz/{first}?{location.query}"):
        assert github.client.request("GET", path).status == 404
    assert github.client.request("GET", "/codeload/octo-org/hello/legacy.tar.gz/nope").status == 404
    assert github.client.request("GET", "/codeload/octo-org/none/legacy.tar.gz/main").status == 404
    assert github.api("GET", f"{REPO}/tarball/nope").status == 404
    assert github.state()["ok"] is True


def test_public_tarball_needs_no_token_and_codeload_refuses_credentials(github: Any) -> None:
    redirect = github.api("GET", "/repos/octo-org/second/tarball/main")
    location = urlsplit(redirect.headers["location"])
    assert location.query == ""
    leaked = github.client.request("GET", location.path, headers={"authorization": "Bearer double-user-token"})
    assert leaked.status == 200
    assert gzip.decompress(leaked.body)
    assert github.state()["failures"] == [
        "codeload request carries an Authorization header (credential forwarded on redirect)"
    ]


def test_public_url_is_written_into_answers(start_github: Callable[..., Any]) -> None:
    gh = start_github(public_url="http://192.0.2.10:7000/")
    location = gh.api("GET", f"{REPO}/tarball/main").headers["location"]
    assert location.startswith("http://192.0.2.10:7000/codeload/octo-org/hello/legacy.tar.gz/")
    assert gh.get(REPO)["url"] == "http://192.0.2.10:7000/repos/octo-org/hello"
    assert gh.get(REPO)["ssh_url"] == "git@192.0.2.10:7000:octo-org/hello.git"


def test_html_url_names_web_links_only(start_github: Callable[..., Any]) -> None:
    """`html_url` (an https site the clients never call, as GitHub answers) replaces the base of web links only; the
    redirect to codeload, the device verification page and the API URLs keep the served base."""
    gh = start_github(public_url="http://192.0.2.10:7000/", html_url="https://github.example/")
    location = gh.api("GET", f"{REPO}/tarball/main").headers["location"]
    assert location.startswith("http://192.0.2.10:7000/codeload/octo-org/hello/legacy.tar.gz/")
    repo = gh.get(REPO)
    assert (repo["url"], repo["html_url"]) == (
        "http://192.0.2.10:7000/repos/octo-org/hello",
        "https://github.example/octo-org/hello",
    )
    grant = gh.client.request(
        "POST", "/login/device/code", {"client_id": "Iv1.0000double0000"}, {"accept": "application/json"}
    ).json()
    assert grant["verification_uri"] == "http://192.0.2.10:7000/login/device"


# --- refs --------------------------------------------------------------------------------------------


def test_refs(github: Any) -> None:
    main = head(github)
    ref = github.get(f"{REPO}/git/ref/heads/main")
    assert ref["ref"] == "refs/heads/main"
    assert ref["object"] == {"sha": main, "type": "commit", "url": f"{github.double.url}{REPO}/git/commits/{main}"}
    assert github.get(f"{REPO}/git/ref/tags/v1.0")["object"]["sha"] == main
    assert github.api("GET", f"{REPO}/git/ref/heads/none").status == 404
    created = github.api("POST", f"{REPO}/git/refs", {"ref": "refs/heads/topic/one", "sha": main})
    assert created.status == 201
    assert created.headers["location"] == created.json()["url"]
    assert github.get(f"{REPO}/git/ref/heads/topic/one")["object"]["sha"] == main
    exists = github.api("POST", f"{REPO}/git/refs", {"ref": "refs/heads/main", "sha": main})
    assert (exists.status, exists.json()["message"]) == (422, "Reference already exists")
    for body in (
        {"ref": "heads/x", "sha": main},
        {"ref": "refs/heads/a..b", "sha": main},
        {"ref": "refs/heads/sp ace", "sha": main},
        {"sha": main},
    ):
        response = github.api("POST", f"{REPO}/git/refs", body)
        assert (response.status, response.json()["message"]) == (422, "Reference name is not well formed")
    for sha in ("0" * 40, EMPTY_TREE, None):
        response = github.api("POST", f"{REPO}/git/refs", {"ref": "refs/heads/y", "sha": sha})
        assert (response.status, response.json()["message"]) == (422, "Object does not exist")
    assert github.state()["ok"] is True


def test_ref_update_is_fast_forward_only(github: Any) -> None:
    main = head(github)
    feature = head(github, "feature")
    first = github.get(f"{REPO}/commits/main")["parents"][0]["sha"]
    behind = github.api("PATCH", f"{REPO}/git/refs/heads/main", {"sha": first, "force": False})
    assert (behind.status, behind.json()["message"]) == (422, "Update is not a fast forward")
    forward = github.api("PATCH", f"{REPO}/git/refs/heads/main", {"sha": feature})
    assert forward.status == 200
    assert forward.json()["object"]["sha"] == feature
    same = github.api("PATCH", f"{REPO}/git/refs/heads/main", {"sha": feature, "force": False})
    assert same.status == 200
    missing = github.api("PATCH", f"{REPO}/git/refs/heads/none", {"sha": main})
    assert (missing.status, missing.json()["message"]) == (422, "Reference does not exist")
    unknown = github.api("PATCH", f"{REPO}/git/refs/heads/main", {"sha": "f" * 40})
    assert (unknown.status, unknown.json()["message"]) == (422, "Object does not exist")
    assert github.state()["ok"] is True
    forced = github.api("PATCH", f"{REPO}/git/refs/heads/main", {"sha": first, "force": True})
    assert forced.status == 422
    assert github.state()["failures"] == [
        "PATCH /repos/{owner}/{repo}/git/refs/{ref}: forced ref update of refs/heads/main"
    ]
    assert head(github) == feature


# --- git data ----------------------------------------------------------------------------------------


def test_blobs(github: Any) -> None:
    text = github.api("POST", f"{REPO}/git/blobs", {"content": "hello\n", "encoding": "utf-8"})
    assert text.status == 201
    assert text.json() == {
        "url": f"{github.double.url}{REPO}/git/blobs/{object_id('blob', b'hello\n')}",
        "sha": object_id("blob", b"hello\n"),
    }
    data = bytes(range(200))
    encoded = base64.b64encode(data).decode()
    binary = github.api("POST", f"{REPO}/git/blobs", {"content": encoded, "encoding": "base64"})
    sha = binary.json()["sha"]
    assert sha == object_id("blob", data)
    fetched = github.get(f"{REPO}/git/blobs/{sha}")
    assert fetched["size"] == 200
    assert fetched["encoding"] == "base64"
    assert all(len(line) <= 60 for line in fetched["content"].splitlines())
    assert base64.b64decode(fetched["content"]) == data
    assert github.api("POST", f"{REPO}/git/blobs", {"content": "x", "encoding": "utf8"}).status == 201
    assert github.api("POST", f"{REPO}/git/blobs", {"content": "x"}).status == 201
    for body, message in (
        ({}, '"content" wasn\'t supplied.'),
        ({"content": "a", "encoding": "base64"}, "Invalid Base64"),
        ({"content": "x", "encoding": "latin1"}, "encoding must be utf-8 or base64"),
    ):
        response = github.api("POST", f"{REPO}/git/blobs", body)
        assert response.status == 422
        assert message in response.json()["message"]
    assert github.api("GET", f"{REPO}/git/blobs/{EMPTY_TREE}").status == 404
    assert github.state()["ok"] is True


def test_trees_with_base_tree_and_deletions(github: Any) -> None:
    main = github.get(f"{REPO}/commits/main")
    base_tree = main["commit"]["tree"]["sha"]
    listing = github.get(f"{REPO}/git/trees/{base_tree}?recursive=1")
    assert [(e["path"], e["mode"], e["type"]) for e in listing["tree"]] == [
        ("README.md", "100644", "blob"),
        ("src", "040000", "tree"),
        ("src/app.py", "100644", "blob"),
    ]
    assert listing["tree"][0]["size"] == 12
    assert "size" not in listing["tree"][1]
    assert listing["truncated"] is False
    top = github.get(f"{REPO}/git/trees/{base_tree}")
    assert [e["path"] for e in top["tree"]] == ["README.md", "src"]
    assert github.get(f"{REPO}/git/trees/main")["sha"] == base_tree
    assert github.get(f"{REPO}/git/trees/{main['sha']}")["sha"] == base_tree
    assert github.api("GET", f"{REPO}/git/trees/{'e' * 40}").status == 404
    assert github.api("GET", f"{REPO}/git/trees/nope").status == 404
    blob = github.api("POST", f"{REPO}/git/blobs", {"content": "#!/bin/sh\n"}).json()["sha"]
    created = github.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "base_tree": base_tree,
            "tree": [
                {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": None},
                {"path": "docs/guide.md", "mode": "100644", "type": "blob", "content": "guide\n"},
                {"path": "bin/run", "mode": "100755", "type": "blob", "sha": blob},
                {"path": "latest", "mode": "120000", "type": "blob", "content": "README.md"},
            ],
        },
    )
    assert created.status == 201
    tree = created.json()
    assert [(e["path"], e["mode"]) for e in tree["tree"]] == [
        ("README.md", "100644"),
        ("bin", "040000"),
        ("docs", "040000"),
        ("latest", "120000"),
    ]
    full = github.get(f"{REPO}/git/trees/{tree['sha']}?recursive=1")
    assert [e["path"] for e in full["tree"] if e["type"] == "blob"] == [
        "README.md",
        "bin/run",
        "docs/guide.md",
        "latest",
    ]
    # A tree entry replaces a whole directory; deleting a directory removes everything below it.
    docs_tree = next(e["sha"] for e in tree["tree"] if e["path"] == "docs")
    moved = github.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "base_tree": tree["sha"],
            "tree": [
                {"path": "bin", "mode": "040000", "type": "tree", "sha": None},
                {"path": "src", "mode": "040000", "type": "tree", "sha": docs_tree},
            ],
        },
    ).json()
    paths = [e["path"] for e in github.get(f"{REPO}/git/trees/{moved['sha']}?recursive=1")["tree"]]
    assert paths == ["README.md", "docs", "docs/guide.md", "latest", "src", "src/guide.md"]
    fresh = github.api(
        "POST", f"{REPO}/git/trees", {"tree": [{"path": "a", "mode": "100644", "type": "blob", "content": "a"}]}
    )
    assert fresh.json()["sha"] == object_id("tree", b"100644 a\0" + bytes.fromhex(object_id("blob", b"a")))
    assert github.state()["ok"] is True


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, '"tree" wasn\'t supplied.'),
        ({"tree": [], "base_tree": "f" * 40}, "base_tree is not a tree"),
        ({"tree": ["x"]}, "every entry needs a relative path"),
        ({"tree": [{"path": "/abs", "mode": "100644", "type": "blob", "content": ""}]}, "relative path"),
        ({"tree": [{"path": "a/../b", "mode": "100644", "type": "blob", "content": ""}]}, "relative path"),
        ({"tree": [{"path": "a", "mode": "160000", "type": "commit", "sha": "f" * 40}]}, "mode '160000'"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "tree", "content": ""}]}, "type 'tree'"),
        ({"tree": [{"path": "a", "mode": "040000", "type": "tree", "content": ""}]}, "content of a"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "blob", "content": 1}]}, "content of a"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "blob", "content": "", "sha": "x"}]}, "content of a"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "blob"}]}, "a needs sha or content"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "blob", "sha": "f" * 40}]}, "does not exist"),
        ({"tree": [{"path": "a", "mode": "100644", "type": "blob", "sha": 5}]}, "does not exist"),
        ({"tree": [{"path": "a", "mode": "040000", "type": "tree", "sha": EMPTY_TREE[:-1] + "0"}]}, "does not exist"),
        ({"tree": [{"path": "gone", "mode": "100644", "type": "blob", "sha": None}]}, "cannot delete missing path"),
    ],
)
def test_tree_refusals(github: Any, body: dict[str, Any], message: str) -> None:
    response = github.api("POST", f"{REPO}/git/trees", body)
    assert response.status == 422
    assert message in response.json()["message"]
    assert github.state()["ok"] is True


def test_tree_with_a_file_where_a_directory_is_needed(github: Any) -> None:
    response = github.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "tree": [
                {"path": "a", "mode": "100644", "type": "blob", "content": "x"},
                {"path": "a/b", "mode": "100644", "type": "blob", "content": "y"},
            ]
        },
    )
    assert response.status == 422
    assert "duplicate entry names" in response.json()["message"]


def test_commits(github: Any) -> None:
    main = head(github)
    tree = github.api("POST", f"{REPO}/git/trees", {"tree": []}).json()["sha"]
    assert tree == EMPTY_TREE
    created = github.api("POST", f"{REPO}/git/commits", {"message": "Empty it.", "tree": tree, "parents": [main]})
    assert created.status == 201
    commit = created.json()
    assert commit["parents"][0]["sha"] == main
    assert commit["author"] == commit["committer"]
    assert commit["author"]["name"] == "Octo User"
    assert commit["author"]["email"] == "101+octo-user@users.noreply.github.com"
    assert commit["verification"]["verified"] is False
    assert github.get(f"{REPO}/git/commits/{commit['sha']}") == commit
    rest = github.get(f"{REPO}/commits/{commit['sha']}")
    assert rest["author"]["login"] == "octo-user"
    assert [f["status"] for f in rest["files"]] == ["removed", "removed"]
    explicit = github.api(
        "POST",
        f"{REPO}/git/commits",
        {
            "message": "Explicit.",
            "tree": tree,
            "author": {"name": "Ann", "email": "other@example.invalid", "date": "2026-02-01T10:00:00+01:00"},
            "committer": {"name": "Cy", "email": "c@example.invalid"},
        },
    ).json()
    assert explicit["author"]["date"] == "2026-02-01T09:00:00Z"
    assert explicit["committer"] == {"name": "Cy", "email": "c@example.invalid", "date": "2026-02-01T09:00:00Z"}
    assert explicit["parents"] == []
    rest = github.get(f"{REPO}/commits/{explicit['sha']}")
    assert (rest["author"]["login"], rest["committer"]) == ("other-user", None)
    other = github.api("POST", "/repos/octo-org/second/git/commits", {"message": "m", "tree": tree}, token=OTHER)
    assert other.status == 404
    for body, message in (
        ({"tree": tree}, '"message" wasn\'t supplied.'),
        ({"message": "m", "tree": "f" * 40}, "Tree SHA does not exist"),
        ({"message": "m", "tree": tree, "parents": ["f" * 40]}, "Parent SHA does not exist"),
        ({"message": "m", "tree": tree, "parents": "x"}, "Parent SHA does not exist"),
        ({"message": "m", "tree": tree, "author": {"name": "n"}}, "Invalid author"),
        ({"message": "m", "tree": tree, "author": {"name": "n", "email": "e", "date": "x"}}, "Invalid author date"),
    ):
        response = github.api("POST", f"{REPO}/git/commits", body)
        assert response.status == 422
        assert message in response.json()["message"]
    assert github.api("GET", f"{REPO}/git/commits/{tree}").status == 404


def test_full_commit_chain_matches_local_tree_ids(github: Any) -> None:
    """The flow of the commit executor (05 §12.1): blobs → tree with base_tree → commit → push."""
    parent = head(github, "feature")
    parent_tree = github.get(f"{REPO}/git/commits/{parent}")["tree"]["sha"]
    listing = github.get(f"{REPO}/git/trees/{parent_tree}?recursive=1")["tree"]
    files = {e["path"]: (e["mode"], e["sha"]) for e in listing if e["type"] == "blob"}
    new = b"x = 3\n"
    blob = github.api("POST", f"{REPO}/git/blobs", {"content": base64.b64encode(new).decode(), "encoding": "base64"})
    files["src/app.py"] = ("100644", blob.json()["sha"])
    del files["README.md"]
    # Local computation of the expected root tree (what the runner does without git).
    sub = object_id("tree", b"100644 app.py\0" + bytes.fromhex(files["src/app.py"][1]))
    expected = object_id("tree", b"40000 src\0" + bytes.fromhex(sub))
    tree = github.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "base_tree": parent_tree,
            "tree": [
                {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": files["src/app.py"][1]},
                {"path": "README.md", "mode": "100644", "type": "blob", "sha": None},
            ],
        },
    ).json()
    assert tree["sha"] == expected
    commit = github.api("POST", f"{REPO}/git/commits", {"message": "Change.", "tree": expected, "parents": [parent]})
    sha = commit.json()["sha"]
    compare = github.get(f"{REPO}/compare/{parent}...{sha}")
    assert (compare["status"], compare["ahead_by"]) == ("ahead", 1)
    assert github.api("PATCH", f"{REPO}/git/refs/heads/feature", {"sha": sha, "force": False}).status == 200
    assert head(github, "feature") == sha


# --- compare -----------------------------------------------------------------------------------------


def test_compare_statuses(github: Any) -> None:
    main = head(github)
    feature = head(github, "feature")
    first = github.get(f"{REPO}/commits/main")["parents"][0]["sha"]
    identical = github.get(f"{REPO}/compare/{main}...{main}")
    assert (identical["status"], identical["ahead_by"], identical["behind_by"], identical["files"]) == (
        "identical",
        0,
        0,
        [],
    )
    ahead = github.get(f"{REPO}/compare/main...feature")
    assert (ahead["status"], ahead["ahead_by"], ahead["behind_by"], ahead["total_commits"]) == ("ahead", 1, 0, 1)
    assert ahead["commits"][0]["sha"] == feature
    assert ahead["merge_base_commit"]["sha"] == main
    assert ahead["files"][0]["patch"] == "@@ -1 +1 @@\n-x = 1\n+x = 2"
    behind = github.get(f"{REPO}/compare/{feature}..{first}")
    assert (behind["status"], behind["ahead_by"], behind["behind_by"]) == ("behind", 0, 2)
    tree = github.get(f"{REPO}/git/commits/{first}")["tree"]["sha"]
    side = github.api("POST", f"{REPO}/git/commits", {"message": "Side.", "tree": tree, "parents": [first]}).json()
    diverged = github.get(f"{REPO}/compare/{feature}...{side['sha']}")
    assert (diverged["status"], diverged["ahead_by"], diverged["behind_by"]) == ("diverged", 1, 2)
    assert diverged["merge_base_commit"]["sha"] == first
    orphan = github.api("POST", f"{REPO}/git/commits", {"message": "Root.", "tree": EMPTY_TREE}).json()["sha"]
    unrelated = github.api("GET", f"{REPO}/compare/{main}...{orphan}")
    assert (unrelated.status, unrelated.json()["message"]) == (404, f"No common ancestor between {main} and {orphan}.")
    for basehead in ("main", "main...nope", "nope...main"):
        assert github.api("GET", f"{REPO}/compare/{basehead}").status == 404


# --- pull requests and comments ----------------------------------------------------------------------


def test_pull_request_lifecycle(github: Any) -> None:
    created = github.api(
        "POST", f"{REPO}/pulls", {"title": "Feature", "head": "octo-org:feature", "base": "main", "draft": True}
    )
    assert created.status == 201
    pull = created.json()
    assert (pull["number"], pull["state"], pull["draft"], pull["title"], pull["body"]) == (
        1,
        "open",
        True,
        "Feature",
        None,
    )
    assert pull["html_url"] == f"{github.double.url}/octo-org/hello/pull/1"
    assert pull["head"]["sha"] == head(github, "feature")
    assert pull["head"]["label"] == "octo-org:feature"
    assert pull["base"]["sha"] == head(github)
    assert (pull["commits"], pull["additions"], pull["deletions"], pull["changed_files"]) == (1, 1, 1, 1)
    assert pull["user"]["login"] == "octo-user"
    assert pull["mergeable_state"] == "draft"
    assert created.headers["location"] == pull["url"]
    assert github.get(f"{REPO}/pulls/1") == pull
    files = github.get(f"{REPO}/pulls/1/files")
    assert [(f["filename"], f["status"], f["additions"], f["deletions"]) for f in files] == [
        ("src/app.py", "modified", 1, 1)
    ]
    again = github.api("POST", f"{REPO}/pulls", {"title": "Again", "head": "feature", "base": "main"})
    assert again.status == 422
    assert again.json()["errors"] == [
        {"resource": "PullRequest", "code": "custom", "message": "A pull request already exists for octo-org:feature."}
    ]
    listed = github.get(f"{REPO}/pulls?head=octo-org:feature&base=main&state=open")
    assert [p["number"] for p in listed] == [1]
    assert "mergeable" not in listed[0]
    assert github.get(f"{REPO}/pulls?head=someone:feature") == []
    assert github.get(f"{REPO}/pulls?base=develop") == []
    assert github.get(f"{REPO}/pulls?state=closed") == []
    assert [p["number"] for p in github.get(f"{REPO}/pulls?state=all")] == [1]
    bad_state = github.api("GET", f"{REPO}/pulls?state=merged")
    assert bad_state.status == 422
    assert bad_state.json()["errors"] == [{"resource": "Issue", "field": "state", "code": "invalid"}]
    assert github.api("GET", f"{REPO}/pulls/9").status == 404
    assert github.api("GET", f"{REPO}/pulls/9/files").status == 404
    assert github.get(f"{REPO}")["open_issues_count"] == 1


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"head": "feature", "base": "main"}, {"resource": "PullRequest", "field": "title", "code": "missing_field"}),
        (
            {"title": "t", "head": "none", "base": "main"},
            {"resource": "PullRequest", "field": "head", "code": "invalid"},
        ),
        (
            {"title": "t", "head": "x:feature", "base": "main"},
            {"resource": "PullRequest", "field": "head", "code": "invalid"},
        ),
        (
            {"title": "t", "head": "feature", "base": "none"},
            {"resource": "PullRequest", "field": "base", "code": "invalid"},
        ),
        (
            {"title": "t", "head": "main", "base": "feature"},
            {"resource": "PullRequest", "code": "custom", "message": "No commits between feature and main"},
        ),
    ],
)
def test_pull_request_validation(github: Any, body: dict[str, Any], error: dict[str, Any]) -> None:
    response = github.api("POST", f"{REPO}/pulls", body)
    assert response.status == 422
    assert response.json()["message"] == "Validation Failed"
    assert response.json()["errors"] == [error]


def test_pull_request_invalid_types(github: Any) -> None:
    for body in (
        {"title": "t", "head": "feature", "base": "main", "draft": "yes"},
        {"title": "t", "head": "feature", "base": "main", "body": 5},
    ):
        response = github.api("POST", f"{REPO}/pulls", body)
        assert (response.status, response.json()["message"]) == (422, "Invalid request.\n\nInvalid draft or body.")


def test_seeded_pull_requests_and_comments(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    data = seed()
    repo = data["repositories"][0]
    repo["pulls"] = [
        {"title": "Old", "head": "feature", "base": "main", "state": "closed", "user": "other-user", "body": "b"},
        {"title": "Open", "head": "feature", "user": "octo-user", "draft": True},
    ]
    repo["comments"] = [
        {"number": 2, "body": "first", "user": "other-user"},
        {"number": 2, "body": "second", "user": "octo-user"},
    ]
    gh = start_github(data)
    closed = gh.get(f"{REPO}/pulls/1")
    assert (closed["state"], closed["closed_at"] is not None, closed["mergeable"]) == ("closed", True, None)
    assert closed["author_association"] == "COLLABORATOR"
    assert [p["number"] for p in gh.get(f"{REPO}/pulls?state=all")] == [2, 1]
    created = gh.api("POST", f"{REPO}/issues/2/comments", {"body": "third"})
    assert created.status == 201
    comment = created.json()
    assert comment["issue_url"] == f"{gh.double.url}{REPO}/issues/2"
    assert comment["user"]["login"] == "octo-user"
    assert created.headers["location"] == comment["url"]
    listed = gh.api("GET", f"{REPO}/issues/2/comments?per_page=2")
    assert [c["body"] for c in listed.json()] == ["first", "second"]
    assert 'rel="next"' in listed.headers["link"]
    assert [c["body"] for c in gh.get(f"{REPO}/issues/2/comments?page=2&per_page=2")] == ["third"]
    assert gh.get(f"{REPO}/pulls/2")["comments"] == 3
    assert gh.get(f"{REPO}/issues/1/comments") == []
    assert gh.api("GET", f"{REPO}/issues/5/comments").status == 404
    assert gh.api("POST", f"{REPO}/issues/5/comments", {"body": "x"}).status == 404
    for body in ({}, {"body": ""}):
        response = gh.api("POST", f"{REPO}/issues/2/comments", body)
        assert response.status == 422
        assert response.json()["errors"] == [{"resource": "IssueComment", "field": "body", "code": "missing_field"}]
    inspected = gh.control("GET", "repos/octo-org/hello").json()
    assert [c["body"] for c in inspected["comments"]] == ["first", "second", "third"]
    assert [(p["number"], p["state"]) for p in inspected["pulls"]] == [(1, "closed"), (2, "open")]
    assert inspected["refs"]["refs/tags/v1.0"] == gh.get(f"{REPO}/commits/main")["sha"]
    assert gh.control("GET", "repos/octo-org/none").status == 404


# --- protocol errors ---------------------------------------------------------------------------------


def test_unknown_routes_and_bad_bodies_are_failures(github: Any) -> None:
    unknown = github.api("DELETE", f"{REPO}/git/refs/heads/main")
    assert unknown.status == 404
    assert github.api("PUT", "/nowhere").status == 404
    assert github.api("HEAD", "/user").status == 404
    bad_json = github.api("POST", f"{REPO}/git/blobs", b"{not json", headers={"content-type": "application/json"})
    assert (bad_json.status, bad_json.json()["message"]) == (400, "Problems parsing JSON")
    not_object = github.api("POST", f"{REPO}/git/blobs", [1])
    assert not_object.status == 400
    chunked = github.client.request("POST", "/login/device/code", b"", {"transfer-encoding": "chunked"})
    assert chunked.status == 411
    state = github.state()
    assert state["failures"] == [
        "unhandled route DELETE /repos/octo-org/hello/git/refs/heads/main",
        "unhandled route PUT /nowhere",
        "unhandled route HEAD /user",
        "POST /login/device/code: request body without content-length",
    ]
    assert state["requests"] == 6
    journal = github.client.journal()
    assert [e["route"] for e in journal if "route" in e] == ["POST /repos/{owner}/{repo}/git/blobs"] * 2
    assert journal[0]["note"] == "unhandled route DELETE /repos/octo-org/hello/git/refs/heads/main"
    assert journal[0]["headers"]["authorization"] == "Bearer <redacted 17 chars>"


def test_route_table() -> None:
    assert len({route.key for route in ROUTES}) == len(ROUTES)
    found = match_route("GET", "/repos/o/r/git/ref/heads/feature/x")
    assert found is not None
    assert found[1] == {"owner": "o", "repo": "r", "ref": "heads/feature/x"}
    assert match_route("GET", "/user/installations/x/repositories") is None
    assert match_route("POST", "/user") is None


def test_merge_base_follows_the_graph_not_the_dates(github: Any) -> None:
    first = github.get(f"{REPO}/commits/main")["parents"][0]["sha"]
    tree = github.get(f"{REPO}/git/commits/{first}")["tree"]["sha"]
    old = {"name": "Old", "email": "old@example.invalid", "date": "2001-01-01T00:00:00Z"}

    def commit(message: str, parents: list[str], **extra: Any) -> str:
        body = {"message": message, "tree": tree, "parents": parents, **extra}
        sha: str = github.api("POST", f"{REPO}/git/commits", body).json()["sha"]
        return sha

    base = commit("Back-dated base.", [first], author=old, committer=old)
    left = commit("Left.", [base])
    right = commit("Right.", [base])
    compare = github.get(f"{REPO}/compare/{left}...{right}")
    assert compare["merge_base_commit"]["sha"] == base
    assert (compare["status"], compare["ahead_by"], compare["behind_by"]) == ("diverged", 1, 1)


def test_comments_on_seeded_issues(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    data = seed()
    data["repositories"][0]["issues"] = [{"title": "Bug", "user": "other-user"}]
    data["repositories"][0]["comments"] = [{"number": 1, "body": "seeded", "user": "other-user"}]
    gh = start_github(data)
    created = gh.api("POST", f"{REPO}/issues/1/comments", {"body": "on an issue"}).json()
    assert created["html_url"].startswith(f"{gh.double.url}/octo-org/hello/issues/1#issuecomment-")
    assert [c["body"] for c in gh.get(f"{REPO}/issues/1/comments")] == ["seeded", "on an issue"]
    pull = gh.api("POST", f"{REPO}/pulls", {"title": "t", "head": "feature", "base": "main"}).json()
    assert pull["number"] == 2
    assert gh.api("GET", f"{REPO}/pulls/1").status == 404


# ---------------------------------------------------------------- credential revocation (KOR-40)


def test_revocation_forgets_the_tokens_without_credentials(github: Any) -> None:
    assert github.api("GET", "/user").status == 200
    answer = github.api(
        "POST", "/credentials/revoke", {"credentials": ["double-user-token", "ghu_unknown"]}, token=None
    )
    assert (answer.status, answer.json()) == (202, {})
    assert github.api("GET", "/user").status == 401
    assert github.state()["failures"] == []


def test_revocation_refuses_credentials_and_malformed_bodies(github: Any) -> None:
    assert github.api("POST", "/credentials/revoke", {"credentials": ["double-user-token"]}).status == 403
    assert github.state()["failures"] == ["POST /credentials/revoke: request with credentials"]
    bodies: list[dict[str, Any]] = [{}, {"credentials": []}, {"credentials": [1]}, {"credentials": "x"}]
    for body in bodies:
        assert github.api("POST", "/credentials/revoke", body, token=None).status == 422
    assert github.api("GET", "/user").status == 200
