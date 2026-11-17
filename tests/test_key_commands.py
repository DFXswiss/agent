"""Client side of the key protocol: key files, the pinned hub key and the signed whoami call.

Every test uses a fake transport and a fixed clock; nothing touches the network
or PostgreSQL.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import stat
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from agent_cli import keys
from agent_cli import main as agent_main

pytestmark = pytest.mark.no_pg

NOW = "2026-11-17T10:00:00Z"
HUB_URL = "https://hub.example"
HUB_HOST = "hub.example"
HUB_ID = "hub-test-1"
CLIENT_ID = "example-laptop-1"
TARGET = "/api/key/whoami"
HUB_PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
OTHER_PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
WHOAMI = {
    "key": CLIENT_ID,
    "kind": "ed25519",
    "principal": "agent",
    "handle": "example-staff",
    "team": "example",
    "device": "laptop-1",
    "scopes": ["record:read", "record:write", "device:controller"],
    "hub_key": HUB_ID,
}
BASE64URL_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
FIELD_PRIME = 2**255 - 19
ORDER_EIGHT_Y = 2707385501144840649318225287225658788936804267575313519463743609750303402022
BAD_IDS = (
    "ab",
    "a" * 64,
    "-abc",
    "abc-",
    "Abc",
    "ab_c",
    "ab c",
    "abc\n",
    "../abc",
    "äbc",
    "",
)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_text(private: Ed25519PrivateKey) -> str:
    return b64(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def shifted(seconds: int) -> str:
    base = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def non_canonical(text: str) -> str:
    """The same bytes with unused trailing bits set: decodes, but is not canonical."""
    index = BASE64URL_ALPHABET.index(text[-1])
    return text[:-1] + BASE64URL_ALPHABET[index | 1]


def y_key(y: int, sign: bool) -> str:
    value = y | (1 << 255) if sign else y
    return b64(value.to_bytes(32, "little"))


HUB_PUBLIC = public_text(HUB_PRIVATE)
BAD_PUBLIC_KEYS = (
    HUB_PUBLIC + "=",
    non_canonical(HUB_PUBLIC),
    b64(bytes(31)),
    b64(bytes(33)),
    HUB_PUBLIC.replace(HUB_PUBLIC[0], "+"),
    "",
    y_key(0, False),
    y_key(1, False),
    y_key(1, True),
    y_key(FIELD_PRIME - 1, False),
    y_key(ORDER_EIGHT_Y, False),
    y_key(ORDER_EIGHT_Y, True),
    y_key(FIELD_PRIME - ORDER_EIGHT_Y, False),
    y_key(FIELD_PRIME, False),
    y_key(FIELD_PRIME + 2, False),
    y_key((1 << 255) - 1, False),
)


class FakeHub:
    """The hub side of a whoami call: checks the signed request and signs its answer."""

    def __init__(self, client_public: str) -> None:
        self.client_public = Ed25519PublicKey.from_public_bytes(unb64(client_public))
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body = json.dumps(WHOAMI).encode("utf-8")
        self.hub_id = HUB_ID
        self.hub_time: str | None = NOW
        self.signer = HUB_PRIVATE
        self.signed = True
        self.signed_status: int | None = None
        self.signed_body: bytes | None = None
        self.signed_nonce: str | None = None
        self.signed_time: str | None = None
        self.extra_headers: list[tuple[str, str]] = []
        self.compress = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.check_request(request)
        headers = [("Content-Type", "application/json"), *self.extra_headers]
        if self.signed:
            hub_time = request.headers["Agent-Time"] if self.hub_time is None else self.hub_time
            text = "\n".join(
                [
                    "agent-response-v1",
                    request.headers["Agent-Key"],
                    request.headers["Agent-Nonce"] if self.signed_nonce is None else self.signed_nonce,
                    request.headers["Agent-Time"] if self.signed_time is None else self.signed_time,
                    str(self.status if self.signed_status is None else self.signed_status),
                    hub_time,
                    hashlib.sha256(self.body if self.signed_body is None else self.signed_body).hexdigest(),
                ]
            )
            headers += [
                ("Agent-Hub-Key", self.hub_id),
                ("Agent-Hub-Time", hub_time),
                ("Agent-Hub-Signature", b64(self.signer.sign(text.encode("utf-8")))),
            ]
        body = self.body
        if self.compress:
            body = gzip.compress(body)
            headers.append(("Content-Encoding", "gzip"))
        return httpx.Response(self.status, headers=headers, content=body)

    def check_request(self, request: httpx.Request) -> None:
        assert request.method == "GET"
        assert request.url.host == HUB_HOST
        assert request.url.raw_path == TARGET.encode("ascii")
        assert request.content == b""
        for name in ("Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature"):
            assert len(request.headers.get_list(name)) == 1
        assert request.headers["Agent-Key"] == CLIENT_ID
        assert len(unb64(request.headers["Agent-Nonce"])) == 16
        assert b64(unb64(request.headers["Agent-Nonce"])) == request.headers["Agent-Nonce"]
        text = "\n".join(
            [
                "agent-request-v1",
                request.headers["Agent-Key"],
                "GET",
                HUB_HOST,
                TARGET,
                request.headers["Agent-Time"],
                request.headers["Agent-Nonce"],
                EMPTY_SHA,
            ]
        )
        self.client_public.verify(unb64(request.headers["Agent-Signature"]), text.encode("utf-8"))


@pytest.fixture
def agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agent-home"
    monkeypatch.setenv("AGENT_HOME", str(home))
    return home


@pytest.fixture
def hub(agent_home: Path) -> FakeHub:
    client_public = keys.create_key(agent_home, CLIENT_ID)
    keys.pin_hub_key(agent_home, HUB_ID, HUB_PUBLIC, False)
    return FakeHub(client_public)


def call(agent_home: Path, hub: FakeHub, now: str, hub_url: str) -> dict:
    pin = keys.load_hub_pin(agent_home)
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        return keys.whoami(agent_home, CLIENT_ID, pin, hub_url, client, lambda: now)


def refused(agent_home: Path, hub: FakeHub) -> str:
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    message = str(caught.value)
    assert message.startswith(keys.NOT_VERIFIED)
    assert "example-staff" not in message
    return message


def run_cli(*argv: str) -> None:
    agent_main.main(["key", *argv])


def cli_refused(*argv: str) -> str:
    with pytest.raises(SystemExit) as caught:
        run_cli(*argv)
    return str(caught.value)


def seed_text(agent_home: Path, key_id: str) -> str:
    return (agent_home / "keys" / f"{key_id}.key").read_text(encoding="ascii").removesuffix("\n")


# Key creation


def test_create_sets_directory_and_file_modes(agent_home: Path) -> None:
    previous = os.umask(0)
    try:
        public = keys.create_key(agent_home, CLIENT_ID)
    finally:
        os.umask(previous)
    directory = agent_home / "keys"
    path = directory / f"{CLIENT_ID}.key"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    seed = unb64(seed_text(agent_home, CLIENT_ID))
    assert len(seed) == 32
    assert b64(seed) == seed_text(agent_home, CLIENT_ID)
    assert public_text(Ed25519PrivateKey.from_private_bytes(seed)) == public


def test_create_tightens_an_existing_keys_directory(agent_home: Path) -> None:
    directory = agent_home / "keys"
    directory.mkdir(parents=True)
    os.chmod(directory, 0o755)
    keys.create_key(agent_home, CLIENT_ID)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_create_never_overwrites_an_existing_key(agent_home: Path) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    before = seed_text(agent_home, CLIENT_ID)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert "already exists" in message
    assert before not in message
    assert seed_text(agent_home, CLIENT_ID) == before


def test_create_prints_only_the_public_key(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli("create", "--id", CLIENT_ID)
    captured = capsys.readouterr()
    seed = seed_text(agent_home, CLIENT_ID)
    public = public_text(Ed25519PrivateKey.from_private_bytes(unb64(seed)))
    assert captured.out == f"{public}\n"
    assert seed not in captured.out
    assert seed not in captured.err


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_create_refuses_an_id_outside_the_rule_and_writes_nothing(agent_home: Path, bad_id: str) -> None:
    message = cli_refused("create", "--id", bad_id)
    assert "nothing was written" in message or "--id is required" in message
    assert not agent_home.exists()


def test_create_accepts_the_shortest_and_longest_id(agent_home: Path) -> None:
    keys.create_key(agent_home, "a1b")
    keys.create_key(agent_home, "a" * 63)
    assert (agent_home / "keys" / "a1b.key").is_file()
    assert (agent_home / "keys" / f"{'a' * 63}.key").is_file()


def test_list_shows_ids_and_public_keys(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    second = keys.create_key(agent_home, "example-runner-1")
    first = keys.create_key(agent_home, CLIENT_ID)
    capsys.readouterr()
    run_cli("list")
    captured = capsys.readouterr()
    assert captured.out == f"{CLIENT_ID}  {first}\nexample-runner-1  {second}\n"
    assert seed_text(agent_home, CLIENT_ID) not in captured.out


def test_list_without_keys_prints_nothing(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli("list")
    assert capsys.readouterr().out == ""
    assert not agent_home.exists()


# Pinning the hub key


def test_pin_hub_writes_the_file_with_mode_0600(agent_home: Path) -> None:
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    path = agent_home / "hub-key.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"id": HUB_ID, "public_key": HUB_PUBLIC}
    pin = keys.load_hub_pin(agent_home)
    assert pin.key_id == HUB_ID
    assert public_text_of(pin.public_key) == HUB_PUBLIC


def public_text_of(public_key: Ed25519PublicKey) -> str:
    return b64(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_pin_hub_refuses_an_invalid_id_and_writes_nothing(agent_home: Path, bad_id: str) -> None:
    cli_refused("pin-hub", "--id", bad_id, "--public-key", HUB_PUBLIC)
    assert not (agent_home / "hub-key.json").exists()
    assert not agent_home.exists()


@pytest.mark.parametrize("bad_key", BAD_PUBLIC_KEYS)
def test_pin_hub_refuses_an_invalid_key_and_writes_nothing(agent_home: Path, bad_key: str) -> None:
    cli_refused("pin-hub", "--id", HUB_ID, "--public-key", bad_key)
    assert not (agent_home / "hub-key.json").exists()
    assert not agent_home.exists()


def test_pin_hub_never_replaces_silently(agent_home: Path) -> None:
    other_public = public_text(OTHER_PRIVATE)
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    message = cli_refused("pin-hub", "--id", "hub-test-2", "--public-key", other_public)
    assert "--replace" in message
    pin = keys.load_hub_pin(agent_home)
    assert pin.key_id == HUB_ID
    assert public_text_of(pin.public_key) == HUB_PUBLIC

    run_cli("pin-hub", "--id", "hub-test-2", "--public-key", other_public, "--replace")
    path = agent_home / "hub-key.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pin = keys.load_hub_pin(agent_home)
    assert pin.key_id == "hub-test-2"
    assert public_text_of(pin.public_key) == other_public


def test_pin_hub_replace_refuses_an_invalid_key_and_keeps_the_pin(agent_home: Path) -> None:
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    cli_refused("pin-hub", "--id", HUB_ID, "--public-key", y_key(1, False), "--replace")
    assert public_text_of(keys.load_hub_pin(agent_home).public_key) == HUB_PUBLIC


# The signed whoami call


def test_whoami_accepts_the_signed_answer_to_its_request(agent_home: Path, hub: FakeHub) -> None:
    assert call(agent_home, hub, NOW, HUB_URL) == WHOAMI
    assert len(hub.requests) == 1
    request = hub.requests[0]
    assert request.headers["Agent-Time"] == NOW
    assert "Content-Encoding" not in request.headers


def test_whoami_uses_a_new_nonce_for_every_request(agent_home: Path, hub: FakeHub) -> None:
    call(agent_home, hub, NOW, HUB_URL)
    call(agent_home, hub, NOW, HUB_URL)
    nonces = {request.headers["Agent-Nonce"] for request in hub.requests}
    assert len(nonces) == 2


def test_whoami_refuses_an_answer_without_hub_headers(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    refused(agent_home, hub)


def test_whoami_shows_the_error_of_an_unsigned_refusal_marked_unverified(
    agent_home: Path, hub: FakeHub
) -> None:
    hub.signed = False
    hub.status = 401
    hub.body = b'{"error": "key_unknown"}'
    message = refused(agent_home, hub)
    assert "unverified" in message
    assert "key_unknown" in message


def test_whoami_shows_no_unsigned_body_that_is_not_a_plain_refusal(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    hub.status = 401
    hub.body = b'{"error": "\\u001b[31mspoofed"}'
    message = refused(agent_home, hub)
    assert "spoofed" not in message


def test_whoami_refuses_a_wrong_hub_key_id(agent_home: Path, hub: FakeHub) -> None:
    hub.hub_id = "hub-other-1"
    refused(agent_home, hub)


def test_whoami_refuses_a_wrong_signature(agent_home: Path, hub: FakeHub) -> None:
    hub.signer = OTHER_PRIVATE
    refused(agent_home, hub)


def test_whoami_refuses_a_signed_answer_to_another_request_of_the_same_key(
    agent_home: Path, hub: FakeHub
) -> None:
    earlier = shifted(-10)
    assert call(agent_home, hub, earlier, HUB_URL) == WHOAMI
    first_request = hub.requests[0]
    first_answer = hub(first_request)

    def replay(request: httpx.Request) -> httpx.Response:
        hub.requests.append(request)
        return httpx.Response(
            first_answer.status_code,
            headers=list(first_answer.headers.multi_items()),
            content=first_answer.content,
        )

    with pytest.raises(keys.KeyProtocolError) as caught:
        pin = keys.load_hub_pin(agent_home)
        with httpx.Client(transport=httpx.MockTransport(replay)) as client:
            keys.whoami(agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: NOW)
    assert str(caught.value).startswith(keys.NOT_VERIFIED)
    replayed_to = hub.requests[-1]
    assert replayed_to.headers["Agent-Nonce"] != first_request.headers["Agent-Nonce"]
    assert replayed_to.headers["Agent-Time"] != first_request.headers["Agent-Time"]


def test_whoami_refuses_an_answer_signed_for_another_nonce(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_nonce = b64(bytes(16))
    refused(agent_home, hub)


def test_whoami_refuses_an_answer_signed_for_another_request_time(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_time = shifted(-5)
    refused(agent_home, hub)


def test_whoami_refuses_a_changed_body(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_body = hub.body
    hub.body = json.dumps({**WHOAMI, "handle": "someone-else"}).encode("utf-8")
    message = refused(agent_home, hub)
    assert "someone-else" not in message


def test_whoami_refuses_a_changed_status(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_status = 404
    refused(agent_home, hub)


@pytest.mark.parametrize("seconds", [120, -120])
def test_whoami_accepts_a_hub_time_120_seconds_off(agent_home: Path, hub: FakeHub, seconds: int) -> None:
    hub.hub_time = shifted(seconds)
    assert call(agent_home, hub, NOW, HUB_URL) == WHOAMI


@pytest.mark.parametrize("seconds", [121, -121, 3600])
def test_whoami_refuses_a_hub_time_outside_the_window(agent_home: Path, hub: FakeHub, seconds: int) -> None:
    hub.hub_time = shifted(seconds)
    refused(agent_home, hub)


@pytest.mark.parametrize(
    "hub_time",
    ["2026-11-17T10:00:00.5Z", "2026-11-17T10:00:00+00:00", "2026-02-30T10:00:00Z"],
)
def test_whoami_refuses_a_malformed_hub_time(agent_home: Path, hub: FakeHub, hub_time: str) -> None:
    hub.hub_time = hub_time
    refused(agent_home, hub)


def test_whoami_refuses_an_answer_with_content_encoding(agent_home: Path, hub: FakeHub) -> None:
    hub.compress = True
    message = refused(agent_home, hub)
    assert "Content-Encoding" in message


def test_whoami_refuses_an_answer_with_identity_content_encoding(agent_home: Path, hub: FakeHub) -> None:
    hub.extra_headers = [("Content-Encoding", "identity")]
    message = refused(agent_home, hub)
    assert "Content-Encoding" in message


def test_whoami_refuses_a_repeated_hub_header(agent_home: Path, hub: FakeHub) -> None:
    hub.extra_headers = [("Agent-Hub-Key", HUB_ID)]
    refused(agent_home, hub)


def test_whoami_reports_a_signed_refusal_as_verified(agent_home: Path, hub: FakeHub) -> None:
    hub.status = 403
    hub.body = b'{"error": "scope_missing"}'
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    assert str(caught.value) == "hub refused the request: HTTP 403, error scope_missing"


def test_whoami_does_not_follow_an_unsigned_redirect(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    hub.status = 302
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/api/key/whoami")]
    refused(agent_home, hub)
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


def test_whoami_does_not_follow_a_signed_redirect(agent_home: Path, hub: FakeHub) -> None:
    hub.status = 307
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/api/key/whoami")]
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    assert str(caught.value) == "hub refused the request: HTTP 307"
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


def test_whoami_without_a_pinned_hub_key_sends_nothing(agent_home: Path) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == "hub key is not pinned"


def test_cli_whoami_without_a_pinned_hub_key_sends_nothing(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys.create_key(agent_home, CLIENT_ID)

    def no_store(**kwargs: object) -> None:
        raise AssertionError("the store must not be opened")

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(agent_main, "open_store", no_store)
    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused("whoami", "--id", CLIENT_ID) == "agent: hub key is not pinned"


class FakeStore:
    def __init__(self, hub_url: str) -> None:
        self.hub_url = hub_url
        self.closed = False

    def meta(self, key: str) -> str | None:
        assert key == "hub_url"
        return self.hub_url

    def close(self) -> None:
        self.closed = True


def test_cli_whoami_prints_the_verified_answer(
    agent_home: Path,
    hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore(HUB_URL)
    real_client = httpx.Client

    def client_with_fake_transport(**kwargs: object) -> httpx.Client:
        assert kwargs.get("follow_redirects") is False
        return real_client(transport=httpx.MockTransport(hub), **kwargs)

    monkeypatch.setattr(agent_main, "open_store", lambda **kwargs: store)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    monkeypatch.setattr(httpx, "Client", client_with_fake_transport)
    run_cli("whoami", "--id", CLIENT_ID)
    assert json.loads(capsys.readouterr().out) == WHOAMI
    assert store.closed


def test_cli_whoami_prints_nothing_for_an_unverified_answer(
    agent_home: Path,
    hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hub.signer = OTHER_PRIVATE
    real_client = httpx.Client
    monkeypatch.setattr(agent_main, "open_store", lambda **kwargs: FakeStore(HUB_URL))
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(hub), **kwargs),
    )
    message = cli_refused("whoami", "--id", CLIENT_ID)
    assert message.startswith(f"agent: {keys.NOT_VERIFIED}")
    assert capsys.readouterr().out == ""


# The host line and the configured hub URL


@pytest.mark.parametrize(
    ("hub_url", "base_url", "host"),
    [
        ("https://HUB.Example", "https://hub.example", "hub.example"),
        ("https://hub.example:443/", "https://hub.example", "hub.example"),
        ("http://hub.example:80", "http://hub.example", "hub.example"),
        ("https://hub.example:8443", "https://hub.example:8443", "hub.example:8443"),
        ("http://[::1]:8080", "http://[::1]:8080", "[::1]:8080"),
        ("https://bücher.example", "https://xn--bcher-kva.example", "xn--bcher-kva.example"),
    ],
)
def test_host_line_follows_the_configured_hub_url(hub_url: str, base_url: str, host: str) -> None:
    origin = keys.hub_origin(hub_url)
    assert origin.base_url == base_url
    assert origin.host == host


@pytest.mark.parametrize(
    "hub_url",
    [
        "https://user@hub.example",
        "https://user:secret@hub.example",
        "https://hub.example/prefix",
        "https://hub.example/?x=1",
        "https://hub.example?",
        "https://hub.example#part",
        "ftp://hub.example",
        "hub.example",
    ],
)
def test_hub_url_with_more_than_scheme_host_and_port_is_refused(hub_url: str) -> None:
    with pytest.raises(keys.KeyProtocolError):
        keys.hub_origin(hub_url)


def test_whoami_signs_the_normalised_host_of_the_configured_hub(agent_home: Path, hub: FakeHub) -> None:
    assert call(agent_home, hub, NOW, "https://HUB.Example:443/") == WHOAMI
    assert hub.requests[0].url.host == HUB_HOST


def test_whoami_refuses_a_hub_url_with_a_path_before_sending(agent_home: Path, hub: FakeHub) -> None:
    with pytest.raises(keys.KeyProtocolError):
        call(agent_home, hub, NOW, "https://hub.example/prefix")
    assert hub.requests == []


# Encodings


def test_request_and_response_texts_have_no_trailing_line_feed() -> None:
    request = keys.request_text(CLIENT_ID, "GET", HUB_HOST, TARGET, NOW, "n", EMPTY_SHA)
    response = keys.response_text(CLIENT_ID, "n", NOW, 200, NOW, EMPTY_SHA)
    assert request == f"agent-request-v1\n{CLIENT_ID}\nGET\n{HUB_HOST}\n{TARGET}\n{NOW}\nn\n{EMPTY_SHA}"
    assert response == f"agent-response-v1\n{CLIENT_ID}\nn\n{NOW}\n200\n{NOW}\n{EMPTY_SHA}"
    assert keys.sha256_hex(b"") == EMPTY_SHA


@pytest.mark.parametrize("bad_key", BAD_PUBLIC_KEYS)
def test_decode_public_key_refuses_bad_values(bad_key: str) -> None:
    with pytest.raises(ValueError):
        keys.decode_public_key(bad_key)


def test_decode_public_key_accepts_a_real_key() -> None:
    assert public_text_of(keys.decode_public_key(HUB_PUBLIC)) == HUB_PUBLIC
