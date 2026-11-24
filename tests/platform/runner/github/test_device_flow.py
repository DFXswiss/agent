"""U-RUN-github: `github.connect` (05-runner.md section 11.4, ADR-015) through the real request frame, the hub kit and
the GitHub double's device flow (states `authorization_pending`, `slow_down`, `expired_token`, `access_denied`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import ANY

import httpx
import pytest

from agent_runner.credentials import cache
from agent_runner.github.client import GitHubOptions
from agent_runner.github.device_flow import scopes
from agent_runner.requests.service import RequestService, service_for

from ..credentials.kit import ALICE, CLIENT_ID, WALL, Env, kit_env, seal_request
from ..settle import settle as settle_tasks
from .conftest import GitHub

Decide = Callable[[int], None]


async def settle(service: RequestService) -> None:
    await settle_tasks(lambda: [f.task for f in service.flows.values()], "the request flows")


def connect(
    dsn: str, github: GitHub | None, decide: Decide, check: Callable[[Env, str], Awaitable[None]], **kw: Any
) -> None:
    async def main() -> None:
        url = github.url if github is not None else "https://github.mock"
        wall = github.clock if github is not None else (lambda: WALL)
        async with kit_env(dsn, github_url=url, github_wall=wall) as env:
            if "transport" in kw:
                env.context.github_options = GitHubOptions(wall=wall, transport=kw["transport"])
            if kw.get("client_id", True) is not True:
                env.context.config.github_client_id = kw["client_id"]
            env.context.config.github_oauth_client_id = kw.get("oauth")
            if kw.get("ck", True):
                await env.add_ck()
            count = 0

            def on_sleep(seconds: float) -> None:
                nonlocal count
                count += 1
                if github is not None:
                    github.clock.now += seconds
                decide(count)

            env.clock.on_sleep = on_sleep
            service = service_for(env.context)
            assert service is not None
            request_id = env.hub.add_request(seal_request("github.connect", kw.get("body", {})))
            await service.process(request_id)
            await settle(service)
            await check(env, request_id)

    asyncio.run(main())


def test_connect_relays_the_device_code_stores_the_credential_and_answers_done(store_dsn: str, github: GitHub) -> None:
    def decide(count: int) -> None:
        if count == 2:
            github.decide("authorize", login="octo-user")

    async def check(env: Env, request_id: str) -> None:
        bodies = env.hub.bodies(request_id)
        assert len(bodies) == 2
        first, last = bodies
        assert first["step"] == "device_code"
        assert first["verification_uri"] == "https://github.example/login/device"
        assert first["expires_in"] == 900
        assert first["user_code"] == github.double.device.grants()[-1]["user_code"]
        assert last == {
            "step": "done",
            "login": "octo-user",
            "scopes": [],
            "source": "device_flow",
            "identity_id": env.hub.records[(ALICE, "github")]["header"]["id"],
        }
        assert [h["final"] for h, _b in env.hub.responses(request_id)] == [False, True]
        record = env.hub.records[(ALICE, "github")]
        assert record["header"]["author"] == env.context.identity.device.device_id
        async with cache.opened(env.context, ALICE, "github") as body:
            assert (body["login"], body["user_id"], body["scopes"], body["source"]) == (
                "octo-user",
                101,
                [],
                "device_flow",
            )
            token = body["token"]
        assert token
        assert token not in env.log()
        assert token not in json.dumps(bodies)
        assert github.paths() == [
            "POST /login/device/code",
            "POST /login/oauth/access_token",
            "POST /login/oauth/access_token",
            "GET /user",
        ]
        assert env.clock.sleeps == [5, 5]
        assert github.failures() == []
        assert f"github_connect done request_id={request_id} source=device_flow scopes=-\n" in env.log()

    connect(store_dsn, github, decide, check)


def test_slow_down_lengthens_the_poll_interval(store_dsn: str, github: GitHub) -> None:
    def decide(count: int) -> None:
        if count == 1:
            github.decide("slow_down", count=1)
        if count == 3:
            github.decide("authorize", login="octo-user")

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id)[-1]["login"] == "octo-user"
        assert env.clock.sleeps == [5, 10, 10]

    connect(store_dsn, github, decide, check)


@pytest.mark.parametrize(("action", "code"), [("expire", "timeout"), ("deny", "upstream_error")])
def test_an_expired_or_denied_authorization_ends_failed(store_dsn: str, github: GitHub, action: str, code: str) -> None:
    def decide(count: int) -> None:
        if count == 1:
            github.decide(action)

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id)[-1] == {"step": "failed", "code": code}
        assert (ALICE, "github") not in env.hub.records
        assert f"github_connect failed request_id={request_id} code={code}" in env.log()

    connect(store_dsn, github, decide, check)


def test_poll_network_errors_are_polled_again_up_to_the_limit(store_dsn: str, github: GitHub) -> None:
    github.faults({"route": "POST /login/oauth/access_token", "kind": "disconnect", "times": 2})

    def decide(count: int) -> None:
        if count == 1:
            github.decide("authorize", login="octo-user")

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id)[-1]["step"] == "done"
        assert env.clock.sleeps == [5, 5, 5]

    connect(store_dsn, github, decide, check)


def test_a_poll_that_stays_unreachable_ends_upstream_unreachable(store_dsn: str, github: GitHub) -> None:
    github.faults({"route": "POST /login/oauth/access_token", "kind": "error", "status": 502, "times": 6})

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id)[-1] == {"step": "failed", "code": "upstream_unreachable"}
        assert len(env.clock.sleeps) == 6
        line = f"github_connect failed request_id={request_id} code=upstream_unreachable step=poll kind=server_error"
        assert f"{line} status=502\n" in env.log()

    connect(store_dsn, github, lambda _c: None, check)


def test_a_device_code_request_that_keeps_failing_ends_upstream_unreachable(store_dsn: str, github: GitHub) -> None:
    github.faults({"route": "POST /login/device/code", "kind": "error", "status": 502, "times": 3})

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "upstream_unreachable"}]
        assert env.clock.sleeps == [2, 8]
        log = env.log()
        for attempt in (1, 2):
            line = f"github_connect retry request_id={request_id} attempt={attempt} step=device_code kind=server_error"
            assert f"{line} status=502\n" in log
        assert (
            f"github_connect failed request_id={request_id} code=upstream_unreachable step=device_code "
            "kind=server_error status=502\n" in log
        )
        assert github.double.device.grants() == []

    connect(store_dsn, github, lambda _c: None, check)


def test_a_device_code_request_whose_connection_drops_once_is_sent_again(store_dsn: str, github: GitHub) -> None:
    """KOR-16: one connection closed without an answer (the double journals the request, the client reads nothing)
    costs one attempt, not the connect."""
    github.faults({"route": "POST /login/device/code", "kind": "disconnect", "times": 1})

    def decide(count: int) -> None:
        if count == 2:
            github.decide("authorize", login="octo-user")

    async def check(env: Env, request_id: str) -> None:
        bodies = env.hub.bodies(request_id)
        assert [body["step"] for body in bodies] == ["device_code", "done"]
        assert bodies[0]["user_code"] == github.double.device.grants()[-1]["user_code"]
        assert env.clock.sleeps[0] == 2
        assert (
            f"github_connect retry request_id={request_id} attempt=1 step=device_code kind=unreachable "
            "cause=RemoteProtocolError\n" in env.log()
        )
        assert github.double.state()["ok"] is True
        assert github.paths()[:2] == ["POST /login/device/code"] * 2

    connect(store_dsn, github, decide, check)


def test_a_rate_limited_device_code_request_is_not_sent_again(store_dsn: str, github: GitHub) -> None:
    github.faults({"route": "POST /login/device/code", "kind": "retry_after", "retry_after": 1, "times": 1})

    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "upstream_unreachable"}]
        assert "code=upstream_unreachable step=device_code kind=rate_limited status=429\n" in env.log()
        assert "github_connect retry" not in env.log()
        assert env.clock.sleeps == []

    connect(store_dsn, github, lambda _c: None, check)


def test_a_wrong_client_id_ends_upstream_error(store_dsn: str, github: GitHub) -> None:
    async def check(env: Env, request_id: str) -> None:
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "upstream_error"}]

    connect(store_dsn, github, lambda _c: None, check, client_id="Iv1.wrong")


# --- answers the double does not give (mock transport) ----------------------------------------------------------


def github_mock(**answers: Any) -> httpx.MockTransport:
    """Answers by path; a list is consumed in order, a callable is called with the request."""

    def handle(request: httpx.Request) -> httpx.Response:
        found = answers[request.url.path.strip("/").replace("/", "_")]
        if isinstance(found, list):
            found = found.pop(0) if len(found) > 1 else found[0]
        if callable(found):
            return found(request)  # type: ignore[no-any-return]
        status, body = found
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


CODE = {
    "device_code": "dc",
    "user_code": "ABCD-1234",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 5,
}
TOKEN = {"access_token": "gho_mocktoken", "token_type": "bearer", "scope": "repo, read:user,BAD SCOPE"}
USER = {"login": "octo-user", "id": 101}


def mocked(
    dsn: str, transport: httpx.MockTransport, *, ck: bool = True, client_id: Any = True, body: Any = None
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    async def check(env: Env, request_id: str) -> None:
        found.extend(env.hub.bodies(request_id))

    connect(dsn, None, lambda _c: None, check, transport=transport, ck=ck, client_id=client_id, body=body or {})
    return found


def failed(code: str) -> dict[str, str]:
    return {"step": "failed", "code": code}


def test_mock_happy_path_records_the_granted_scopes(store_dsn: str) -> None:
    transport = github_mock(
        login_device_code=(200, CODE),
        login_oauth_access_token=[(200, {"error": "slow_down"}), (200, TOKEN)],
        user=(200, USER),
    )
    done = {
        "step": "done",
        "login": "octo-user",
        "scopes": ["read:user", "repo"],
        "source": "device_flow",
        "identity_id": ANY,
    }
    assert mocked(store_dsn, transport)[-1] == done


def test_scopes() -> None:
    assert scopes("repo, read:user,BAD SCOPE,,") == ["read:user", "repo"]
    assert scopes(None) == []


@pytest.mark.parametrize(
    "answer",
    [
        [1],
        {**CODE, "user_code": "lower"},
        {**CODE, "verification_uri": "http://github.com/login/device"},
        {**CODE, "device_code": ""},
        {**CODE, "expires_in": True},
        {**CODE, "interval": "5"},
    ],
)
def test_an_unusable_device_code_answer_ends_upstream_error(store_dsn: str, answer: Any) -> None:
    transport = github_mock(login_device_code=(200, answer))
    assert mocked(store_dsn, transport) == [failed("upstream_error")]


def test_a_missing_interval_defaults_to_five_seconds(store_dsn: str) -> None:
    code = {key: value for key, value in CODE.items() if key != "interval"}
    transport = github_mock(login_device_code=(200, code), login_oauth_access_token=(200, TOKEN), user=(200, USER))
    assert mocked(store_dsn, transport)[-1]["step"] == "done"


@pytest.mark.parametrize(
    ("poll", "user", "code"),
    [
        ((200, {"access_token": "has space"}), (200, USER), "token_invalid"),
        ((200, {"error": "incorrect_device_code"}), (200, USER), "upstream_error"),
        ((200, [1]), (200, USER), "upstream_error"),
        ((400, {"error": "bad"}), (200, USER), "upstream_error"),
        ((200, TOKEN), (200, {"login": "bad login", "id": 1}), "upstream_error"),
        ((200, TOKEN), (200, {"login": "octo-user", "id": 0}), "upstream_error"),
        ((200, TOKEN), (200, [1]), "upstream_error"),
        ((200, TOKEN), (401, {"message": "Bad credentials"}), "upstream_error"),
        ((200, TOKEN), (502, {}), "upstream_unreachable"),
    ],
)
def test_unusable_tokens_and_users_end_failed(store_dsn: str, poll: Any, user: Any, code: str) -> None:
    transport = github_mock(login_device_code=(200, CODE), login_oauth_access_token=poll, user=user)
    assert mocked(store_dsn, transport)[-1] == failed(code)


def test_the_flow_ends_with_timeout_when_the_device_code_expires_locally(store_dsn: str) -> None:
    transport = github_mock(
        login_device_code=(200, {**CODE, "expires_in": 6}),
        login_oauth_access_token=(200, {"error": "authorization_pending"}),
    )
    assert mocked(store_dsn, transport)[-1] == failed("timeout")


def test_storing_needs_the_owners_ck_and_the_hub(store_dsn: str) -> None:
    def transport() -> httpx.MockTransport:
        return github_mock(login_device_code=(200, CODE), login_oauth_access_token=(200, TOKEN), user=(200, USER))

    assert mocked(store_dsn, transport(), ck=False)[-1] == failed("credential_missing")
    assert mocked(store_dsn, transport(), client_id=None) == [failed("flow_unavailable")]


def test_a_hub_refusing_or_unreachable_while_storing_ends_failed(store_dsn: str) -> None:
    async def main(mode: str) -> list[dict[str, Any]]:
        async with kit_env(store_dsn, github_url="https://github.mock") as env:
            env.context.github_options = GitHubOptions(
                wall=lambda: WALL,
                transport=github_mock(
                    login_device_code=(200, CODE), login_oauth_access_token=(200, TOKEN), user=(200, USER)
                ),
            )
            await env.add_ck()
            if mode == "refused":
                env.hub.put_refusals.append((403, {"error": "forbidden"}))
            elif mode == "read_refused":
                env.hub.get_refusals.append((403, {"error": "forbidden"}))
            else:
                env.hub.get_refusals.append((0, {}))
            service = service_for(env.context)
            assert service is not None
            request_id = env.hub.add_request(seal_request("github.connect", {}))
            await service.process(request_id)
            await settle(service)
            assert "store_failed" in env.log() or mode != "refused"
            return env.hub.bodies(request_id)

    assert asyncio.run(main("refused"))[-1] == failed("upstream_error")
    assert asyncio.run(main("unreachable"))[-1] == failed("upstream_unreachable")
    assert asyncio.run(main("read_refused"))[-1] == failed("upstream_error")


def _stored(dsn: str, lost: list[str], read_refusals: list[tuple[int, Any]] | None = None) -> tuple[list[Any], Any]:
    """Run the mocked flow with lost credential writes; the bodies and the hub kit."""
    out: list[Any] = []

    async def main() -> None:
        async with kit_env(dsn, github_url="https://github.mock") as env:
            env.context.github_options = GitHubOptions(
                wall=lambda: WALL,
                transport=github_mock(
                    login_device_code=(200, CODE), login_oauth_access_token=(200, TOKEN), user=(200, USER)
                ),
            )
            await env.add_ck()
            env.hub.put_lost.extend(lost)
            env.hub.get_refusals.extend(read_refusals or [])
            service = service_for(env.context)
            assert service is not None
            request_id = env.hub.add_request(seal_request("github.connect", {}))
            await service.process(request_id)
            await settle(service)
            out.extend([env.hub.bodies(request_id), env.hub, env.log(), request_id])

    asyncio.run(main())
    return out[0], out[1:]


@pytest.mark.parametrize("lost", [["before"], ["after"], ["after", "before", ""]])
def test_a_store_lost_in_transport_is_sent_again_and_the_connect_succeeds(store_dsn: str, lost: list[str]) -> None:
    """KOR-18: the user has confirmed the code; a lost write (before or after the hub stored it) is repeated with the
    same record bytes, so the connect ends done with exactly one stored version."""
    bodies, (hub, log, _request_id) = _stored(store_dsn, lost)
    assert bodies[-1]["step"] == "done"
    assert hub.records[(ALICE, "github")]["header"]["version"] == 1
    puts = [r for r in hub.requests if r.method == "PUT"]
    assert len(puts) == len([mode for mode in lost if mode]) + 1
    assert len({r.raw for r in puts}) == 1
    assert "runner.credentials store_retry step=write attempt=1 reason=hub_unreachable cause=ReadError\n" in log
    assert "github_connect failed" not in log


def test_a_store_that_stays_lost_names_the_step_and_the_cause(store_dsn: str) -> None:
    bodies, (hub, log, request_id) = _stored(store_dsn, ["before"] * 4)
    assert bodies[-1] == failed("upstream_unreachable")
    assert len([r for r in hub.requests if r.method == "PUT"]) == 4
    assert "store_retry step=write attempt=3 " in log
    line = (
        f"github_connect failed request_id={request_id} code=upstream_unreachable step=store kind=hub "
        "reason=hub_unreachable cause=ReadError\n"
    )
    assert line in log


def test_a_refused_store_names_the_step_and_the_code(store_dsn: str) -> None:
    bodies, (_hub, log, request_id) = _stored(store_dsn, [], [(403, {"error": "forbidden"})])
    assert bodies[-1] == failed("upstream_error")
    assert "store_retry" not in log
    line = f"request_id={request_id} code=upstream_error step=store kind=hub status=403 error=forbidden"
    assert f"github_connect failed {line}" in log


def test_an_answer_that_does_not_verify_while_storing_is_not_sent_again(store_dsn: str) -> None:
    bodies, (_hub, log, request_id) = _stored(store_dsn, [], [(0, {})])
    assert bodies[-1] == failed("upstream_unreachable")
    assert "store_retry" not in log
    assert (
        f"request_id={request_id} code=upstream_unreachable step=store kind=hub reason=response_signature_invalid"
        in log
    )


def test_the_handler_is_registered_for_github_connect() -> None:
    from agent_runner import registry  # noqa: PLC0415

    assert "github.connect" in registry.request_handlers()
    assert CLIENT_ID.startswith("Iv1.")
