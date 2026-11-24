"""U-RUN-github: `github.connect {token}` (KOR-25): the device checks an existing GitHub token with `GET /user`, takes
the scopes from `X-OAuth-Scopes`, stores the credential with `source: "token"` and answers
`done {login, scopes, source}`; refusals end with `token_refused`, `scope_missing`, `sso_required`, `token_invalid`
or `upstream_unreachable`. No `GITHUB_CLIENT_ID` is needed and the token is never logged or answered."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import ANY

import httpx
import pytest

from agent_runner.credentials import cache

from ..credentials.kit import ALICE, Env
from .test_device_flow import connect, failed, mocked

TOKEN = "double-pat-classic0token"  # not a secret: only compared by the mock
USER = {"login": "octo-user", "id": 4242}


def user_answer(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/user"
        return httpx.Response(status, json=USER if body is None else body, headers=headers or {})

    transport = httpx.MockTransport(handle)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def stored(dsn: str, transport: httpx.MockTransport, body: dict[str, Any]) -> tuple[list[Any], Any, str]:
    found: list[Any] = []

    async def check(env: Env, request_id: str) -> None:
        record = None
        if (ALICE, "github") in env.hub.records:
            async with cache.opened(env.context, ALICE, "github") as opened:
                record = dict(opened)
        found.extend([env.hub.bodies(request_id), record, env.log()])

    connect(dsn, None, lambda _c: None, check, transport=transport, client_id=None, body=body)
    return found[0], found[1], found[2]


def test_a_classic_token_is_checked_stored_and_answered_without_a_client_id(store_dsn: str) -> None:
    transport = user_answer(headers={"x-oauth-scopes": "repo, read:org, gist"})
    bodies, record, log = stored(store_dsn, transport, {"token": TOKEN})
    done = {
        "step": "done",
        "login": "octo-user",
        "scopes": ["gist", "read:org", "repo"],
        "source": "token",
        "identity_id": ANY,
    }
    assert bodies == [done]
    assert record is not None
    assert {key: record[key] for key in ("token", "login", "user_id", "scopes", "source")} == {
        "token": TOKEN,
        "login": "octo-user",
        "user_id": 4242,
        "scopes": ["gist", "read:org", "repo"],
        "source": "token",
    }
    seen = transport.seen  # type: ignore[attr-defined]
    assert [(r.method, r.url.path, r.headers["authorization"]) for r in seen] == [("GET", "/user", f"Bearer {TOKEN}")]
    assert TOKEN not in log
    assert TOKEN not in json.dumps(bodies)
    assert "github_connect done request_id=" in log
    assert " source=token scopes=gist,read:org,repo\n" in log
    assert "client_id_missing" not in log


@pytest.mark.parametrize(
    ("headers", "scopes"),
    [({}, []), ({"x-oauth-scopes": "public_repo"}, ["public_repo"])],
)
def test_fine_grained_and_public_repo_tokens_are_accepted(
    store_dsn: str, headers: dict[str, str], scopes: list[str]
) -> None:
    bodies = mocked(store_dsn, user_answer(headers=headers), client_id=None, body={"token": TOKEN})
    assert bodies == [{"step": "done", "login": "octo-user", "scopes": scopes, "source": "token", "identity_id": ANY}]


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (200, {"x-oauth-scopes": "read:org, gist"}, "scope_missing"),
        (200, {"x-oauth-scopes": ""}, "scope_missing"),
        (401, {}, "token_refused"),
        (
            403,
            {"x-github-sso": "required; url=https://github.example/orgs/o/sso?authorization_request=x"},
            "sso_required",
        ),
        (403, {}, "upstream_error"),
        (502, {}, "upstream_unreachable"),
    ],
)
def test_refused_tokens_end_failed_and_store_nothing(
    store_dsn: str, status: int, headers: dict[str, str], code: str
) -> None:
    body = {"message": "Bad credentials"} if status >= 400 else None
    bodies, record, log = stored(store_dsn, user_answer(status, body, headers), {"token": TOKEN})
    assert bodies == [failed(code)]
    assert record is None
    assert f"code={code} step=user kind=" in log
    assert TOKEN not in log
    assert "authorization_request" not in log


def test_a_network_error_ends_upstream_unreachable(store_dsn: str) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    bodies = mocked(store_dsn, httpx.MockTransport(broken), body={"token": TOKEN})
    assert bodies == [failed("upstream_unreachable")]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"token": "has space"}, "token_invalid"),
        ({"token": ""}, "token_invalid"),
        ({"token": 7}, "token_invalid"),
        ({"token": "x" * 4097}, "token_invalid"),
        ({"token": TOKEN, "mode": "x"}, "upstream_error"),
        ({"mode": "x"}, "upstream_error"),
    ],
)
def test_malformed_bodies_end_failed_before_github_is_asked(store_dsn: str, body: dict[str, Any], code: str) -> None:
    transport = user_answer(headers={"x-oauth-scopes": "repo"})
    assert mocked(store_dsn, transport, body=body) == [failed(code)]
    assert transport.seen == []  # type: ignore[attr-defined]


def test_unusable_user_answers_and_a_missing_key_end_failed(store_dsn: str) -> None:
    scoped = {"x-oauth-scopes": "repo"}
    assert mocked(store_dsn, user_answer(headers=scoped), ck=False, body={"token": TOKEN}) == [
        failed("credential_missing")
    ]
    assert mocked(store_dsn, user_answer(200, {"login": "bad login", "id": 1}, scoped), body={"token": TOKEN}) == [
        failed("upstream_error")
    ]
