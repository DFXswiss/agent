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
