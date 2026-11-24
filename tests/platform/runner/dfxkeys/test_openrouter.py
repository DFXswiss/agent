"""U-RUN-dfxkeys: the OpenRouter management client of the issuer (ADR-080 section 7) - routes and documents against
the model double, malformed answers of a provider (`httpx.MockTransport`), and errors that never carry a key."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent_runner.dfxkeys.openrouter import KeyMeta, Management, ManagementError, parse_key, to_micro, to_usd

from .kit import MANAGEMENT, double

GOOD = {"hash": "a" * 64, "name": "n", "disabled": False, "limit": 1.5, "usage": 0.25}


def mocked(handler: Callable[[httpx.Request], httpx.Response]) -> Management:
    return Management(
        upstream="http://provider.invalid/api/",
        key=lambda: MANAGEMENT,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def answer(status: int, body: object = None, *, raw: bytes | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {MANAGEMENT}"
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=body)

    return handle


def test_amounts_and_documents() -> None:
    assert to_micro(1.5) == 1_500_000
    assert to_micro(0) == 0
    for bad in (-1, True, "1", float("nan"), float("inf"), None):
        assert to_micro(bad) is None
    assert to_usd(2_500_000) == 2.5
    assert parse_key(GOOD) == KeyMeta("a" * 64, "n", False, 1_500_000, 250_000)
    assert parse_key({**GOOD, "limit": None}).limit_micro is None
    malformed: list[object] = [
        [],
        {**GOOD, "hash": "short"},
        {**GOOD, "hash": 5},
        {**GOOD, "name": None},
        {**GOOD, "disabled": "no"},
        {**GOOD, "usage": -1},
        {**GOOD, "limit": "x"},
    ]
    for document in malformed:
        with pytest.raises(ManagementError, match="invalid"):
            parse_key(document)


def test_the_routes_against_the_double() -> None:
    async def main() -> None:
        with double() as started:
            management = Management(upstream=started.url, key=lambda: MANAGEMENT)
            meta, secret = await management.create("dfx-x", 1_250_000)
            assert (meta.name, meta.limit_micro, meta.usage_micro, meta.disabled) == ("dfx-x", 1_250_000, 0, False)
            assert secret.startswith("sk-or-v1-")
            assert await management.named("dfx-x") == [meta]
            assert await management.named("dfx-y") == []
            assert await management.get(meta.hash) == meta
            changed = await management.patch(meta.hash, {"limit": 2.0, "disabled": True})
            assert (changed.limit_micro, changed.disabled) == (2_000_000, True)
            await management.delete(meta.hash)
            await management.delete(meta.hash)  # already gone
            assert await management.get(meta.hash) is None

    asyncio.run(main())


@pytest.mark.parametrize(
    ("call", "handler", "reason"),
    [
        ("get", answer(500, {}), "status_500"),
        ("get", answer(401, {}), "status_401"),
        ("get", answer(200, raw=b"{"), "invalid"),
        ("get", answer(200, [1]), "invalid"),
        ("get", answer(200, {"data": {}}), "invalid"),
        ("named", answer(200, {"data": "x"}), "invalid"),
        ("named", answer(200, {"data": [{}]}), "invalid"),
        ("create", answer(201, {"data": GOOD}), "invalid"),
        ("create", answer(201, {"data": GOOD, "key": ""}), "invalid"),
        ("create", answer(201, {"data": GOOD, "key": "has space"}), "invalid"),
        ("create", answer(201, {"data": GOOD, "key": 7}), "invalid"),
        ("delete", answer(500, raw=b""), "status_500"),
    ],
)
def test_malformed_or_failed_answers(
    call: str, handler: Callable[[httpx.Request], httpx.Response], reason: str
) -> None:
    async def main() -> None:
        management = mocked(handler)
        calls: dict[str, Callable[[], Any]] = {
            "get": lambda: management.get("a" * 64),
            "named": lambda: management.named("n"),
            "create": lambda: management.create("n", 0),
            "delete": lambda: management.delete("a" * 64),
        }
        with pytest.raises(ManagementError) as caught:
            await calls[call]()
        assert caught.value.reason == reason
        assert MANAGEMENT not in str(caught.value)

    asyncio.run(main())


def test_a_provider_that_never_stops_paging_is_bounded() -> None:
    async def main() -> None:
        pages: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            pages.append(str(request.url.params["offset"]))
            return httpx.Response(200, json={"data": [GOOD]})

        assert len(await mocked(handle).named("n")) == 100
        assert pages[:3] == ["0", "1", "2"]
        assert len(pages) == 100

    asyncio.run(main())


def test_transport_errors_and_an_unusable_key() -> None:
    async def main() -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        with pytest.raises(ManagementError, match="ConnectTimeout"):
            await mocked(broken).get("a" * 64)
        management = Management(upstream="http://provider.invalid", key=lambda: None)
        with pytest.raises(ManagementError, match="key_unusable"):
            await management.get("a" * 64)
        assert management.url("/x") == "http://provider.invalid/v1/keys/x"

    asyncio.run(main())
