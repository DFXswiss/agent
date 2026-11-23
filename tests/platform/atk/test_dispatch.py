"""Unit tests of the host-side dispatcher (agent_doubles.atk.dispatch) against real model doubles on loopback."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from agent_doubles.atk.dispatch import Dispatcher
from agent_doubles.model import Access, ModelDouble, packaged_scenario

TOKEN = "dispatch-test-token"
MESSAGE = {
    "model": "m",
    "max_tokens": 16,
    "stream": False,
    "messages": [{"role": "user", "content": "Primary working directory: /srv/sessions/x/work"}],
    "tools": [{"name": "Write"}],
}


@pytest.fixture
def dispatcher() -> Iterator[Dispatcher]:
    started = Dispatcher().start()
    yield started
    started.stop()


@pytest.fixture
def double() -> Iterator[ModelDouble]:
    started = ModelDouble(packaged_scenario("atk-baseline"), access=Access("openrouter", TOKEN)).start()
    yield started
    started.stop()


def call(
    dispatcher: Dispatcher, method: str, path: str, body: object | None = None
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", dispatcher.port, timeout=10)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    data = None if body is None else json.dumps(body).encode()
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    found = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, payload, found


def test_requests_reach_the_double_of_their_key(dispatcher: Dispatcher, double: ModelDouble) -> None:
    assert dispatcher.register("s-1", double) == "/d/s-1"
    status, payload, headers = call(dispatcher, "POST", "/d/s-1/v1/messages?beta=true", MESSAGE)
    assert status == 200, payload
    assert json.loads(payload)["content"][1]["name"] == "Write"
    assert headers["content-length"] == str(len(payload))
    exchange = double.exchanges[-1]
    assert (exchange["path"], exchange["query"]) == ("/v1/messages", "beta=true")
    assert double.state()["consumed"] == ["write"]
    assert dispatcher.unknown == []


def test_the_bare_prefix_forwards_to_the_root_and_get_works(dispatcher: Dispatcher, double: ModelDouble) -> None:
    dispatcher.register("s-2", double)
    status, _payload, _headers = call(dispatcher, "GET", "/d/s-2")
    assert status == 404  # the double itself refuses the unknown route "/"
    assert double.exchanges[-1]["path"] == "/"


def test_unknown_keys_and_paths_are_refused_and_recorded(dispatcher: Dispatcher, double: ModelDouble) -> None:
    dispatcher.register("s-3", double)
    dispatcher.unregister("s-3")
    dispatcher.unregister("never-registered")
    assert call(dispatcher, "POST", "/d/s-3/v1/messages", MESSAGE)[0] == 404
    assert call(dispatcher, "GET", "/v1/messages")[0] == 404
    assert dispatcher.unknown == ["/d/s-3/v1/messages", "/v1/messages"]
    assert double.exchanges == []


def test_an_unreachable_double_answers_502(dispatcher: Dispatcher) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    @dataclass
    class Gone:
        url: str

    dispatcher.register("gone", Gone(f"http://127.0.0.1:{port}"))
    status, payload, _headers = call(dispatcher, "POST", "/d/gone/v1/messages", MESSAGE)
    assert (status, payload) == (502, b"model double unreachable\n")


@pytest.mark.parametrize("key", ["", "a/b", "x" * 65, "s 1"])
def test_invalid_keys_are_refused(dispatcher: Dispatcher, key: str) -> None:
    with pytest.raises(ValueError, match="invalid dispatcher key"):
        dispatcher.register(key, ModelDouble(packaged_scenario("atk-baseline")))
