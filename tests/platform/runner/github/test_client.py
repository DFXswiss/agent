"""U-RUN-github: the GitHub HTTP client against the GitHub double (05-runner.md section 12.1: headers, version,
timeouts, redirects, `Retry-After`, `x-ratelimit-remaining`, retryable failures)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_runner.github import client as client_module
from agent_runner.github.client import (
    GitHubClient,
    GitHubError,
    GitHubOptions,
    GitHubSettings,
    RateGate,
    codeload_allowed,
    settings_for,
)

from .conftest import REPO, TOKEN, GitHub


class Sleeps:
    def __init__(self, github: GitHub | None = None) -> None:
        self.calls: list[float] = []
        self.github = github

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.github is not None:
            self.github.clock.now += seconds


def make(github: GitHub, token: str | None = TOKEN, **kw: Any) -> tuple[GitHubClient, Sleeps]:
    sleeps = Sleeps(github)
    built = GitHubClient(
        api_url=github.url,
        web_url=github.url,
        token=token,
        wall=github.clock,
        sleep=sleeps,
        gate=kw.pop("gate", RateGate()),
        **kw,
    )
    return built, sleeps


def run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


def test_api_requests_carry_the_fixed_headers_and_the_token(github: GitHub) -> None:
    async def body() -> None:
        client, _ = make(github)
        async with client:
            answer = await client.request("GET", f"/repos/{REPO}")
        assert answer.status == 200
        assert answer.data["full_name"] == REPO
        (record,) = github.double.journal.records()
        headers = record["headers"]
        assert headers["accept"] == "application/vnd.github+json"
        assert headers["x-github-api-version"] == "2022-11-28"
        assert headers["authorization"] != f"Bearer {TOKEN}"  # the journal redacts it
        assert github.failures() == []

    run(body)


def test_refusals_are_classified(github: GitHub) -> None:
    async def body() -> None:
        client, _ = make(github)
        async with client:
            with pytest.raises(GitHubError) as missing:
                await client.request("GET", "/repos/octo-org/hidden")
            assert (missing.value.kind, missing.value.status, missing.value.retryable) == ("not_found", 404, False)
            with pytest.raises(GitHubError) as exists:
                await client.request(
                    "POST", f"/repos/{REPO}/git/refs", body={"ref": "refs/heads/main", "sha": "0" * 40}
                )
            assert exists.value.kind == "unprocessable"
            assert exists.value.message
        stranger, _ = make(github, token="unknown-token")
        async with stranger:
            with pytest.raises(GitHubError) as revoked:
                await stranger.request("GET", "/user")
            assert revoked.value.kind == "unauthorized"

    run(body)


def test_a_write_without_the_installation_permission_is_forbidden(start_github: Any) -> None:
    from .conftest import seed  # noqa: PLC0415

    world = seed()
    world["installations"][0]["permissions"] = {"contents": "read", "metadata": "read"}
    github = start_github(world=world)

    async def body() -> None:
        client, _ = make(github)
        async with client:
            with pytest.raises(GitHubError) as refused:
                await client.request("POST", f"/repos/{REPO}/git/refs", body={"ref": "refs/heads/x", "sha": "0" * 40})
        assert (refused.value.kind, refused.value.retryable) == ("forbidden", False)

    run(body)


@pytest.mark.parametrize(
    ("fault", "kind", "retry_after", "cause"),
    [
        ({"kind": "error", "status": 502}, "server_error", None, ""),
        ({"kind": "retry_after", "retry_after": 7}, "rate_limited", 7.0, ""),
        ({"kind": "secondary_rate_limit"}, "rate_limited", 60.0, ""),
        ({"kind": "secondary_rate_limit", "retry_after": 12}, "rate_limited", 12.0, ""),
        ({"kind": "primary_rate_limit", "reset_in": 600}, "rate_limited", 600.0, ""),
        ({"kind": "disconnect"}, "unreachable", None, "RemoteProtocolError"),
    ],
)
def test_retryable_failures(
    github: GitHub, fault: dict[str, Any], kind: str, retry_after: float | None, cause: str
) -> None:
    github.faults({"route": "GET /repos/{owner}/{repo}", "times": 1, **fault})

    async def body() -> None:
        client, _ = make(github)
        async with client:
            with pytest.raises(GitHubError) as failed:
                await client.request("GET", f"/repos/{REPO}")
            assert (failed.value.kind, failed.value.retryable, failed.value.retry_after) == (kind, True, retry_after)
            assert failed.value.cause == cause
        again, _ = make(github)  # a fresh gate: the fault fired once
        async with again:
            assert (await again.request("GET", f"/repos/{REPO}")).status == 200

    run(body)


def test_a_primary_limit_resetting_within_a_minute_is_waited_for_once(github: GitHub) -> None:
    github.faults({"route": "GET /repos/{owner}/{repo}", "kind": "primary_rate_limit", "reset_in": 20, "times": 1})

    async def body() -> None:
        client, sleeps = make(github)
        async with client:
            assert (await client.request("GET", f"/repos/{REPO}")).status == 200
        assert len(sleeps.calls) == 1
        assert 19 <= sleeps.calls[0] <= 21
        assert github.paths() == [f"GET /repos/{REPO}", f"GET /repos/{REPO}"]

    run(body)


def test_a_primary_limit_that_persists_after_the_wait_fails_retryable(github: GitHub) -> None:
    github.faults({"route": "GET /repos/{owner}/{repo}", "kind": "primary_rate_limit", "reset_in": 5, "times": 2})

    async def body() -> None:
        client, _ = make(github)
        async with client:
            with pytest.raises(GitHubError) as failed:
                await client.request("GET", f"/repos/{REPO}")
        assert (failed.value.kind, failed.value.retryable) == ("rate_limited", True)

    run(body)


def test_an_exhausted_limit_holds_the_next_request_until_the_reset(github: GitHub) -> None:
    github.faults({"route": "GET /repos/{owner}/{repo}", "kind": "exhaust", "reset_in": 30, "times": 1})

    async def body() -> None:
        gate = RateGate()
        client, sleeps = make(github, gate=gate)
        async with client:
            assert (await client.request("GET", f"/repos/{REPO}")).status == 200
            assert sleeps.calls == []
            assert (await client.request("GET", "/user")).status == 200
        assert len(sleeps.calls) == 1
        assert 29 <= sleeps.calls[0] <= 31
        # the gate belongs to the owner: a later client of the same owner waits too, far resets fail retryable
        gate.blocked_until = github.clock() + 3600
        later, _ = make(github, gate=gate)
        async with later:
            with pytest.raises(GitHubError) as failed:
                await later.request("GET", "/user")
        assert failed.value.kind == "rate_limited"
        assert failed.value.retry_after is not None
        assert failed.value.retry_after > 3000

    run(body)


def test_items_follow_the_next_links_on_the_configured_host(start_github: Any) -> None:
    from .conftest import seed  # noqa: PLC0415

    world = seed()
    names = [f"octo-org/r{n:03d}" for n in range(101)]
    world["repositories"] += [{"full_name": name, "branches": [{"name": "main", "commits": [{}]}]} for name in names]
    world["installations"][0]["repositories"] += names
    github = start_github(world=world)

    async def body() -> None:
        client, _ = make(github)
        async with client:
            found = await client.items("/user/installations/31/repositories", key="repositories")
        assert len(found) == 103
        assert github.journal() == [
            "GET /user/installations/31/repositories?per_page=100",
            "GET /user/installations/31/repositories?per_page=100&page=2",
        ]

    run(body)


def answering(*answers: httpx.Response) -> httpx.MockTransport:
    queue = list(answers)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    transport = httpx.MockTransport(handle)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def mock_client(transport: httpx.MockTransport, api: str = "https://api.example/api/v3") -> GitHubClient:
    async def no_sleep(_seconds: float) -> None:
        return None

    return GitHubClient(
        api_url=api,
        web_url="https://ghe.example",
        token=TOKEN,
        wall=lambda: 0.0,
        sleep=no_sleep,
        gate=RateGate(),
        transport=transport,
    )


def test_items_refuse_foreign_next_links_and_malformed_pages() -> None:
    async def body() -> None:
        foreign = httpx.Response(200, json=[1], headers={"link": '<https://evil.example/x?page=2>; rel="next"'})
        async with mock_client(answering(foreign)) as client:
            with pytest.raises(GitHubError) as refused:
                await client.items("/user/repos")
            assert refused.value.kind == "invalid_response"
        same_host = httpx.Response(
            200, json={"items": [1]}, headers={"link": '<https://other.example/api/v3/user/repos?page=2>; rel="next"'}
        )
        last = httpx.Response(200, json={"items": [2]})
        transport = answering(same_host, last)
        async with mock_client(transport) as client:
            assert await client.items("/user/repos", key="items") == [1, 2]
        # only path and query of the link are used, on the configured host
        assert str(transport.seen[1].url) == "https://api.example/api/v3/user/repos?page=2"  # type: ignore[attr-defined]
        async with mock_client(answering(httpx.Response(200, json={"items": None}))) as client:
            with pytest.raises(GitHubError):
                await client.items("/user/repos", key="items")
        looping = [
            httpx.Response(200, json=[], headers={"link": '<https://api.example/api/v3/user/repos?page=2>; rel="next"'})
            for _ in range(2)
        ]
        async with mock_client(answering(*looping)) as client:
            with pytest.raises(GitHubError):
                await client.items("/user/repos", max_pages=2)

    run(body)


def test_answers_that_are_not_usable_are_invalid() -> None:
    async def body() -> None:
        cases = [
            httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
            httpx.Response(200, content=b"{"),
            httpx.Response(418, content=b"not json"),
            httpx.Response(200, content=b"x" * (client_module.MAX_BODY_BYTES + 1)),
        ]
        for case in cases:
            async with mock_client(answering(case)) as client:
                with pytest.raises(GitHubError) as failed:
                    await client.request("GET", "/x")
                assert (failed.value.kind, failed.value.retryable) == ("invalid_response", False)
        async with mock_client(answering(httpx.Response(204))) as client:
            assert (await client.request("DELETE", "/x")).data is None

    run(body)


def test_web_forms_post_without_token_and_classify_refusals(github: GitHub) -> None:
    github.faults({"route": "POST /login/device/code", "kind": "error", "status": 503, "times": 1})

    async def body() -> None:
        client, _ = make(github)
        async with client:
            with pytest.raises(GitHubError) as failed:
                await client.web_form("/login/device/code", {"client_id": "Iv1.0000double0000"})
            assert (failed.value.kind, failed.value.retryable) == ("server_error", True)
            answer = await client.web_form("/login/device/code", {"client_id": "Iv1.0000double0000"})
        assert answer.data["user_code"]
        assert all("authorization" not in r["headers"] for r in github.double.journal.records())

    run(body)


def test_download_follows_one_redirect_to_codeload_without_the_token(start_github: Any) -> None:
    github = start_github(public=None)

    async def body() -> None:
        client, _ = make(github)
        chunks: list[bytes] = []
        async with client:
            size = await client.download(f"/repos/{REPO}/tarball/main", chunks.append, max_bytes=10_000_000)
            assert size == len(b"".join(chunks)) > 0
            with pytest.raises(GitHubError) as too_large:
                await client.download(f"/repos/{REPO}/tarball/main", chunks.append, max_bytes=10)
            assert too_large.value.kind == "invalid_response"
            with pytest.raises(GitHubError) as missing:
                await client.download(f"/repos/{REPO}/tarball/nope", chunks.append, max_bytes=10)
            assert missing.value.kind == "not_found"
        assert github.failures() == []  # the double fails a codeload request that carries a token

    run(body)


def test_download_refuses_other_redirects_and_reads_direct_answers() -> None:
    async def body() -> None:
        moved = httpx.Response(302, headers={"location": "https://evil.example/codeload/x"})
        async with mock_client(answering(moved)) as client:
            with pytest.raises(GitHubError):
                await client.download("/t", lambda _b: None, max_bytes=10)
        direct = httpx.Response(200, content=b"abc")
        got: list[bytes] = []
        async with mock_client(answering(direct)) as client:
            assert await client.download("/t", got.append, max_bytes=10) == 3
        codeload = httpx.Response(302, headers={"location": "https://codeload.ghe.example/o/r/legacy.tar.gz/x"})
        gone = httpx.Response(410, content=b"{")
        async with mock_client(answering(codeload, gone)) as client:
            with pytest.raises(GitHubError) as failed:
                await client.download("/t", got.append, max_bytes=10)
            assert failed.value.kind == "invalid_response"

        def broken(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async with mock_client(httpx.MockTransport(broken)) as client:
            with pytest.raises(GitHubError) as down:
                await client.download("/t", got.append, max_bytes=10)
            assert (down.value.kind, down.value.cause) == ("unreachable", "ConnectError")

    run(body)


@pytest.mark.parametrize(
    ("location", "allowed"),
    [
        ("https://codeload.github.com/o/r/legacy.tar.gz/abc", True),
        ("http://codeload.github.com/o/r/legacy.tar.gz/abc", False),
        ("https://github.com/codeload/o/r/legacy.tar.gz/abc", True),
        ("https://github.com/o/r/archive.tar.gz", False),
        ("https://user:pw@codeload.github.com/x", False),
        ("https://evil.example/codeload/x", False),
        ("/codeload/x", False),
    ],
)
def test_codeload_allowed(location: str, allowed: bool) -> None:
    assert codeload_allowed("https://github.com", location) is allowed


def test_error_kinds_are_closed() -> None:
    with pytest.raises(ValueError, match="unknown GitHub error kind"):
        GitHubError("teapot")


def test_settings_come_from_the_configuration_once_per_daemon() -> None:
    async def host_sleep(_seconds: float) -> None:
        return None

    config = SimpleNamespace(
        github_api_url="https://api.github.com/", github_web_url="https://github.com", github_client_id="Iv1.x"
    )
    context = SimpleNamespace(config=config, host=SimpleNamespace(sleep=host_sleep))
    built = settings_for(context)
    assert isinstance(built, GitHubSettings)
    assert (built.api_url, built.web_url, built.client_id, built.sleep) == (
        "https://api.github.com",
        "https://github.com",
        "Iv1.x",
        host_sleep,
    )
    assert settings_for(context) is built
    assert built.gate("a") is built.gate("a")
    other = SimpleNamespace(config=config, host=context.host, github_options=GitHubOptions(wall=lambda: 5.0))
    assert settings_for(other).wall() == 5.0


def test_refusal_messages_include_the_validation_items_and_no_token_means_no_authorization() -> None:
    async def body() -> None:
        errors = {"message": "Validation Failed", "errors": ["x", {"field": "head"}, {"message": "m", "field": 3}]}
        transport = answering(httpx.Response(422, json=errors), httpx.Response(200, json={}))
        async with mock_client(transport) as client:
            with pytest.raises(GitHubError) as failed:
                await client.request("POST", "/x", body={})
            assert failed.value.message == "Validation Failed; field:head; m"
        anonymous = GitHubClient(
            api_url="https://api.example",
            web_url="https://ghe.example",
            token=None,
            wall=lambda: 0.0,
            sleep=Sleeps(),
            gate=RateGate(),
            transport=transport,
        )
        async with anonymous:
            await anonymous.request("GET", "/meta")
        assert "authorization" not in transport.seen[1].headers  # type: ignore[attr-defined]

    run(body)
