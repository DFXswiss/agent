"""U-DBL-github (KOR-65): OAuth App tokens of the double's second device-flow client and the organisation policies
(OAuth App access restrictions, classic tokens forbidden), seeded or set through ``POST /_double/organizations``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agent_doubles.github.deviceflow import DEFAULT_OAUTH_CLIENT_ID, GRANT_TYPE
from agent_doubles.github.repos import SeedError, World

REPO = "/repos/octo-org/hello"
JSON = {"accept": "application/json"}


def oauth_token(gh: Any, scope: str = "repo read:org") -> str:
    """A token of the OAuth App client through the device flow, authorized by ``octo-user``."""
    started = gh.client.request(
        "POST", "/login/device/code", {"client_id": DEFAULT_OAUTH_CLIENT_ID, "scope": scope}, JSON
    )
    grant = started.json()
    gh.control("POST", "device", {"user_code": grant["user_code"], "action": "authorize", "login": "octo-user"})
    gh.clock.now += 10
    body = {"client_id": DEFAULT_OAUTH_CLIENT_ID, "device_code": grant["device_code"], "grant_type": GRANT_TYPE}
    answer = gh.client.request("POST", "/login/oauth/access_token", body, JSON).json()
    assert answer["scope"] == ",".join(scope.split())
    token: str = answer["access_token"]
    return token


def test_an_oauth_token_reaches_the_users_repositories_by_its_scopes(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    data = seed()
    data["users"][0]["collaborator"] = ["octo-org/hello"]
    gh = start_github(data)
    token = oauth_token(gh)
    assert token.startswith("gho_")
    assert gh.double.world.tokens[token].client == DEFAULT_OAUTH_CLIENT_ID
    user = gh.api("GET", "/user", token=token)
    assert user.headers["x-oauth-scopes"] == "repo, read:org"
    assert gh.api("GET", REPO, token=token).status == 200
    assert gh.api("GET", "/repos/octo-org/hidden", token=token).status == 404


def test_organisation_policies_refuse_oauth_apps_and_classic_tokens(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    data = seed()
    data["users"][0]["collaborator"] = ["octo-org/hello", "octo-org/second"]
    data["users"][0]["tokens"].append({"token": "classic-all", "kind": "classic", "scopes": ["repo"]})
    data["organizations"] = [{"login": "octo-org", "oauth_restricted": True}]
    gh = start_github(data)
    token = oauth_token(gh)
    refused = gh.api("GET", REPO, token=token)
    assert refused.status == 403
    assert "OAuth App access restrictions" in refused.json()["message"]
    # a public repository stays readable, its writes are refused
    assert gh.api("GET", "/repos/octo-org/second", token=token).status == 200
    write = gh.api("POST", "/repos/octo-org/second/git/blobs", {"content": "x"}, token=token)
    assert write.status == 403
    approved = gh.control("POST", "organizations", {"login": "octo-org", "oauth_approved": [DEFAULT_OAUTH_CLIENT_ID]})
    assert approved.json() == {
        "login": "octo-org",
        "oauth_restricted": True,
        "oauth_approved": [DEFAULT_OAUTH_CLIENT_ID],
        "classic_forbidden": False,
    }
    assert gh.api("GET", REPO, token=token).status == 200
    assert gh.api("GET", REPO, token="classic-all").status == 200
    gh.control("POST", "organizations", {"login": "octo-org", "classic_forbidden": True})
    forbidden = gh.api("GET", REPO, token="classic-all")
    assert forbidden.status == 403
    assert "forbids access via a personal access token (classic)" in forbidden.json()["message"]
    assert gh.api("GET", REPO, token=token).status == 200
    assert gh.api("GET", REPO).status == 200  # App tokens are not touched by these policies
    # an organisation without repositories in the world changes nothing
    assert gh.control("POST", "organizations", {"login": "other-org", "classic_forbidden": True}).status == 200
    assert gh.api("GET", "/repos/other-org/none", token="classic-all").status == 404
    assert gh.control("POST", "organizations", {"login": "-bad-"}).status == 400
    assert gh.state()["failures"] == []


def test_organisation_seed_values() -> None:
    world = World()
    found = world.set_organization({"login": "Some-Org", "oauth_restricted": True, "oauth_approved": ["c1"]})
    assert world.organization("some-org") is found
    assert found.refuses_oauth("c2")
    assert not found.refuses_oauth("c1")
    kept = world.set_organization({"login": "some-org", "classic_forbidden": True})
    assert (kept.oauth_restricted, kept.oauth_approved, kept.classic_forbidden) == (True, ["c1"], True)
    assert world.organization("unknown") is None
    with pytest.raises(SeedError, match="organization: unknown keys"):
        world.seed({"organizations": [{"login": "x", "extra": 1}]})
    with pytest.raises(SeedError, match="invalid organization login"):
        world.set_organization({"login": "-x"})
