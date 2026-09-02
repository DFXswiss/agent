from __future__ import annotations

import httpx
import pytest

from agent_cli.hub import Hub, HubError


def test_request_invalid_json_response_raises_hub_error_not_json_decode_error() -> None:
    """Regression test: a 2xx response with a non-JSON body used to leak a raw
    json.JSONDecodeError out of Hub.request, which callers like _sync_once (via
    hub.pull) do not catch, killing whatever loop called it instead of logging a
    HubError like every other hub-communication failure."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hub = Hub("https://hub.example", "tok", client=client)
        with pytest.raises(HubError, match="invalid JSON"):
            hub.pull({})


def test_request_recursion_error_response_raises_hub_error_not_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: CPython's C-accelerated json decoder still bounds
    recursion by C stack depth, not just sys.getrecursionlimit() - a
    pathologically deeply-nested response body (adversarial or buggy hub)
    can raise RecursionError, a RuntimeError subclass the previous except
    tuple (JSONDecodeError/UnicodeDecodeError/ValueError) didn't catch. That
    would have escaped every caller's HubError/StoreError guard the same way
    a raw json.JSONDecodeError used to before this method existed."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="[1]")

    def raise_recursion_error(self: httpx.Response) -> None:
        raise RecursionError("Stack overflow while decoding a JSON array")

    monkeypatch.setattr(httpx.Response, "json", raise_recursion_error)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hub = Hub("https://hub.example", "tok", client=client)
        with pytest.raises(HubError, match="invalid JSON"):
            hub.pull({})


def test_request_recursion_error_on_error_response_body_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: the row-side sibling of the test above, for _detail()
    - called only for an HTTP >= 400 response to build the error message.
    _detail() had its own separate response.json() call with its own except
    tuple (bare ValueError), never updated when the success-path tuple above
    gained RecursionError. A >= 400 response with a pathologically nested
    error body used to crash Hub.request with an uncaught RecursionError
    instead of falling back to the raw response text like every other
    unparseable error body already does."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request, raw text")

    def raise_recursion_error(self: httpx.Response) -> None:
        raise RecursionError("Stack overflow while decoding a JSON array")

    monkeypatch.setattr(httpx.Response, "json", raise_recursion_error)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hub = Hub("https://hub.example", "tok", client=client)
        with pytest.raises(HubError, match="bad request, raw text"):
            hub.pull({})
