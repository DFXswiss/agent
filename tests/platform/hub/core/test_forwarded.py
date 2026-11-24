"""U-HUB-config, U-HUB-hygiene (A-11): the source address of the anonymous rate limit behind trusted reverse proxies.

Without `AGENT_HUB_TRUSTED_PROXIES` no forwarding header is read; with it, only a peer that is a trusted proxy may name
the client, and only through the configured header, read from the right (a client cannot spoof its source)."""

from __future__ import annotations

from dataclasses import replace
from ipaddress import ip_network

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_hub.config import ConfigError, load_config
from agent_hub.http.forwarded import client_address
from agent_hub.http.pipeline import source_address
from tests.platform.hub.conftest import HubHarness, hub_environ

PROXIES = (ip_network("127.0.0.1/32"), ip_network("192.0.2.0/25"), ip_network("::1/128"))


def headers(name: str, *values: str) -> list[tuple[bytes, bytes]]:
    return [(b"host", b"hub.test"), *((name.encode(), value.encode()) for value in values)]


@pytest.mark.parametrize(
    ("peer", "values", "expected"),
    [
        # an untrusted peer is the source, whatever it sends
        ("192.0.2.200", ("198.51.100.1",), "192.0.2.200"),
        ("testclient", ("198.51.100.1",), "testclient"),
        # a trusted peer without the header
        ("127.0.0.1", (), "127.0.0.1"),
        # the rightmost address that is not a trusted proxy
        ("127.0.0.1", ("198.51.100.1",), "198.51.100.1"),
        ("127.0.0.1", ("198.51.100.99, 198.51.100.1",), "198.51.100.1"),
        ("127.0.0.1", ("198.51.100.99", "198.51.100.1, 192.0.2.3"), "198.51.100.1"),
        ("::ffff:127.0.0.1", ("198.51.100.1",), "198.51.100.1"),
        ("::1", ("2001:db8::5",), "2001:db8::5"),
        ("127.0.0.1", ("[2001:db8::5]:4711",), "2001:db8::5"),
        ("127.0.0.1", ("198.51.100.1:5678",), "198.51.100.1"),
        ("127.0.0.1", ("::ffff:198.51.100.1",), "198.51.100.1"),
        # only trusted hops: the leftmost of them
        ("127.0.0.1", ("192.0.2.2, 192.0.2.3",), "192.0.2.2"),
        # a malformed entry ends the walk at the last trusted hop
        ("127.0.0.1", ("198.51.100.1, garbage",), "127.0.0.1"),
        ("127.0.0.1", ("198.51.100.1, garbage, 192.0.2.4",), "192.0.2.4"),
        ("127.0.0.1", ("[2001:db8::5",), "127.0.0.1"),
        ("127.0.0.1", ("",), "127.0.0.1"),
        ("127.0.0.1", ("198.51.100.1, , 192.0.2.4",), "192.0.2.4"),
    ],
)
def test_x_forwarded_for(peer: str, values: tuple[str, ...], expected: str) -> None:
    assert client_address(peer, headers("x-forwarded-for", *values), PROXIES, "x-forwarded-for") == expected


FORWARDED_CASES = [
    ("for=198.51.100.1", "198.51.100.1"),
    ('for="198.51.100.1";proto=https;by=192.0.2.1', "198.51.100.1"),
    ('for=198.51.100.99, For="[2001:db8:cafe::17]:4711"', "2001:db8:cafe::17"),
    ("for=198.51.100.1, for=192.0.2.9", "198.51.100.1"),
    ("for=unknown", "127.0.0.1"),
    ("for=_hidden, for=192.0.2.9", "192.0.2.9"),
    ("proto=https", "127.0.0.1"),
]


@pytest.mark.parametrize(("value", "expected"), FORWARDED_CASES)
def test_rfc_7239_forwarded(value: str, expected: str) -> None:
    assert client_address("127.0.0.1", headers("forwarded", value), PROXIES, "forwarded") == expected


def test_several_forwarded_lines_are_one_list() -> None:
    lines = headers("forwarded", "for=198.51.100.9", "for=198.51.100.1;proto=https", "for=192.0.2.9")
    assert client_address("127.0.0.1", lines, PROXIES, "forwarded") == "198.51.100.1"


def test_only_the_configured_header_is_read() -> None:
    both = [(b"x-forwarded-for", b"198.51.100.1"), (b"forwarded", b"for=198.51.100.2")]
    assert client_address("127.0.0.1", both, PROXIES, "x-forwarded-for") == "198.51.100.1"
    assert client_address("127.0.0.1", both, PROXIES, "forwarded") == "198.51.100.2"
    assert client_address("127.0.0.1", both, (), "forwarded") == "127.0.0.1"


def test_source_address_outside_the_pipeline() -> None:
    assert source_address({"type": "http", "client": ("192.0.2.1", 1)}) == "192.0.2.1"
    assert source_address({"type": "http", "client": None}) == "unknown"
    assert source_address({"type": "http", "client": ("192.0.2.1", 1), "agent_hub.source": "198.51.100.1"}) == (
        "198.51.100.1"
    )


def test_configuration() -> None:
    config = load_config(hub_environ())
    assert (config.trusted_proxies, config.forwarded_header) == ((), "x-forwarded-for")
    config = load_config(hub_environ(AGENT_HUB_TRUSTED_PROXIES="127.0.0.1, 192.0.2.0/25,::1"))
    assert config.trusted_proxies == PROXIES
    config = load_config(hub_environ(AGENT_HUB_TRUSTED_PROXIES="127.0.0.1", AGENT_HUB_FORWARDED_HEADER="Forwarded"))
    assert config.forwarded_header == "forwarded"
    assert load_config(hub_environ(AGENT_HUB_TRUSTED_PROXIES="")).trusted_proxies == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"AGENT_HUB_TRUSTED_PROXIES": "127.0.0.1,,192.0.2.1"}, "AGENT_HUB_TRUSTED_PROXIES must be a comma-separated"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "proxy.example"}, "AGENT_HUB_TRUSTED_PROXIES must be a comma-separated"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "192.0.2.1/24"}, "AGENT_HUB_TRUSTED_PROXIES must be a comma-separated"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "0.0.0.0/0"}, "AGENT_HUB_TRUSTED_PROXIES must not trust every address"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "::/0"}, "AGENT_HUB_TRUSTED_PROXIES must not trust every address"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "::ffff:127.0.0.1"}, "AGENT_HUB_TRUSTED_PROXIES must name IPv4-mapped"),
        ({"AGENT_HUB_TRUSTED_PROXIES": "::ffff:0:0/96"}, "AGENT_HUB_TRUSTED_PROXIES must name IPv4-mapped"),
        (
            {"AGENT_HUB_TRUSTED_PROXIES": "127.0.0.1", "AGENT_HUB_FORWARDED_HEADER": "x-real-ip"},
            "AGENT_HUB_FORWARDED_HEADER must be x-forwarded-for or forwarded",
        ),
        ({"AGENT_HUB_FORWARDED_HEADER": "forwarded"}, "AGENT_HUB_FORWARDED_HEADER needs AGENT_HUB_TRUSTED_PROXIES"),
    ],
)
def test_configuration_errors(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(hub_environ(None, **overrides))
    assert str(caught.value).startswith(message)
    assert all(value not in str(caught.value) for value in overrides.values() if value not in message)


def peer(hub: HubHarness, address: str) -> TestClient:
    """A client of the running hub from `address` (the harness entered the lifespan)."""
    return TestClient(hub.app, base_url=hub.config.public.origin, client=(address, 40000))


def trust(hub: HubHarness, monkeypatch: pytest.MonkeyPatch, header: str = "x-forwarded-for") -> None:
    context = hub.app.state.hub
    config = replace(context.config, trusted_proxies=PROXIES, forwarded_header=header)
    monkeypatch.setattr(hub.app.state, "hub", replace(context, config=config))


def exhaust(client: TestClient, forwarded: str | None = None, header: str = "x-forwarded-for") -> None:
    extra = {} if forwarded is None else {header: forwarded}
    for _ in range(30):
        assert client.get("/v1/health", headers=extra).status_code == 200


def test_by_default_every_client_behind_a_proxy_shares_the_proxy_bucket(hub: HubHarness) -> None:
    proxy = peer(hub, "127.0.0.1")
    exhaust(proxy, "198.51.100.1")
    assert proxy.get("/v1/health", headers={"x-forwarded-for": "198.51.100.2"}).status_code == 429


def test_behind_a_trusted_proxy_each_client_has_its_own_bucket(
    hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust(hub, monkeypatch)
    proxy = peer(hub, "127.0.0.1")
    exhaust(proxy, "198.51.100.1")
    assert proxy.get("/v1/health", headers={"x-forwarded-for": "198.51.100.1"}).status_code == 429
    assert proxy.get("/v1/health", headers={"x-forwarded-for": "198.51.100.2"}).status_code == 200
    assert proxy.get("/v1/health").status_code == 200


def test_an_untrusted_peer_cannot_spoof_its_source(hub: HubHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    trust(hub, monkeypatch)
    direct = peer(hub, "192.0.2.200")
    for number in range(30):
        assert direct.get("/v1/health", headers={"x-forwarded-for": f"198.51.100.{number}"}).status_code == 200
    assert direct.get("/v1/health", headers={"x-forwarded-for": "198.51.100.200"}).status_code == 429


def test_a_client_behind_a_trusted_proxy_cannot_spoof_by_prepending(
    hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust(hub, monkeypatch)
    proxy = peer(hub, "127.0.0.1")
    for number in range(30):
        spoofed = f"198.51.100.{number}, 203.0.113.5"
        assert proxy.get("/v1/health", headers={"x-forwarded-for": spoofed}).status_code == 200
    assert proxy.get("/v1/health", headers={"x-forwarded-for": "198.51.100.99, 203.0.113.5"}).status_code == 429
    # the client's own `Forwarded` header is not read when the proxies speak X-Forwarded-For
    blocked = {"x-forwarded-for": "203.0.113.5", "forwarded": "for=198.51.100.77"}
    assert proxy.get("/v1/health", headers=blocked).status_code == 429


def test_forwarded_header_and_websocket_source(hub: HubHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    trust(hub, monkeypatch, "forwarded")
    proxy = peer(hub, "127.0.0.1")
    exhaust(proxy, "for=198.51.100.1", "forwarded")
    assert proxy.get("/v1/health", headers={"forwarded": "for=198.51.100.1"}).status_code == 429
    assert proxy.get("/v1/health", headers={"x-forwarded-for": "198.51.100.1"}).status_code == 200
    # the upgrade takes its token from the forwarded source (and is then refused: unsigned)
    limiter = hub.app.state.hub.limiter
    for _ in range(29):
        assert limiter.hit("anonymous", "198.51.100.3") is None
    upgrade = {"forwarded": "for=198.51.100.3"}
    with pytest.raises(WebSocketDisconnect) as closed, proxy.websocket_connect("/v1/device/ws", headers=upgrade):
        pass
    assert closed.value.code == 1008
    assert limiter.hit("anonymous", "198.51.100.3") is not None
