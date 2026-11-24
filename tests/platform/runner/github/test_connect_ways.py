"""U-RUN-github: the ways of "Connect GitHub" (KOR-65, ADR-015 addendum) against the GitHub double: the OAuth App's
device flow is preferred when it is set up (scopes `repo workflow`, an OAuth token that reaches every repository of
the account), `{app}` names the device flow, a flow that is not set up is `flow_unavailable` (never a dead
`upstream_error`), and the live channel announces the ways of the device."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from agent_runner.credentials import cache
from agent_runner.github.device_flow import OAUTH_SCOPES
from agent_runner.sync.ws import features

from ..credentials.kit import ALICE, CLIENT_ID, Env
from .conftest import GitHub
from .test_device_flow import connect

OAUTH_ID = "Ov23double00000000"


def authorize(github: GitHub) -> Callable[[int], None]:
    def decide(count: int) -> None:
        if count == 1:
            github.decide("authorize", login="octo-user")

    return decide


def form(github: GitHub) -> dict[str, list[str]]:
    (first,) = [r for r in github.double.journal.records() if r["path"] == "/login/device/code"]
    return parse_qs(first["body"])


def test_the_oauth_app_is_preferred_and_asks_for_repo_and_workflow(store_dsn: str, github: GitHub) -> None:
    async def check(env: Env, request_id: str) -> None:
        last = env.hub.bodies(request_id)[-1]
        assert last == {"step": "done", "login": "octo-user", "scopes": ["repo", "workflow"], "source": "device_flow"}
        assert form(github) == {"client_id": [OAUTH_ID], "scope": [OAUTH_SCOPES]}
        async with cache.opened(env.context, ALICE, "github") as body:
            assert body["token"].startswith("gho_")
            assert body["scopes"] == ["repo", "workflow"]
        assert f"github_connect device_flow request_id={request_id} app=oauth_app\n" in env.log()

    connect(store_dsn, github, authorize(github), check, oauth=OAUTH_ID)


def test_the_github_app_is_used_when_asked_for(store_dsn: str, github: GitHub) -> None:
    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id)[-1]["step"] == "done"
        assert form(github) == {"client_id": [CLIENT_ID]}
        async with cache.opened(env.context, ALICE, "github") as body:
            assert body["token"].startswith("ghu_")

    connect(store_dsn, github, authorize(github), check, oauth=OAUTH_ID, body={"app": "github_app"})


@pytest.mark.parametrize(
    ("body", "oauth", "client_id"),
    [
        ({"app": "oauth_app"}, None, CLIENT_ID),
        ({"app": "github_app"}, OAUTH_ID, None),
        ({}, None, None),
    ],
)
def test_a_flow_that_is_not_set_up_is_flow_unavailable(
    store_dsn: str, github: GitHub, body: dict[str, str], oauth: str | None, client_id: str | None
) -> None:
    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "flow_unavailable"}]
        assert github.paths() == []
        assert "github_connect client_id_missing app=" in env.log()

    connect(store_dsn, github, lambda _count: None, check, oauth=oauth, client_id=client_id, body=body)


@pytest.mark.parametrize("body", [{"app": "gitlab"}, {"app": "oauth_app", "token": "x-token"}, {"other": 1}])
def test_a_body_outside_the_contract_is_upstream_error(store_dsn: str, github: GitHub, body: dict[str, object]) -> None:
    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "upstream_error"}]
        assert github.paths() == []

    connect(store_dsn, github, lambda _count: None, check, oauth=OAUTH_ID, body=body)


@pytest.mark.parametrize(
    ("config", "found"),
    [
        (None, ("log.admit", "log.org_assign", "vm.choice", "github.ways")),
        (
            SimpleNamespace(github_oauth_client_id=None, github_client_id=None, operator_github_token_file=None),
            ("log.admit", "log.org_assign", "vm.choice", "github.ways"),
        ),
        (
            SimpleNamespace(github_oauth_client_id=OAUTH_ID, github_client_id=None, operator_github_token_file=None),
            ("log.admit", "log.org_assign", "vm.choice", "github.ways", "github.device_flow", "github.oauth_app"),
        ),
        (
            SimpleNamespace(github_oauth_client_id=None, github_client_id=CLIENT_ID, operator_github_token_file="t"),
            ("log.admit", "log.org_assign", "vm.choice", "github.ways", "github.device_flow", "github.operator"),
        ),
    ],
)
def test_the_live_channel_announces_the_github_ways(config: object, found: tuple[str, ...]) -> None:
    context = SimpleNamespace() if config is None else SimpleNamespace(config=config)
    assert features(context) == found
