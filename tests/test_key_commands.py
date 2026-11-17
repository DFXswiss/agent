"""Client side of the key protocol: key files, the pinned hub key, the signed whoami call
and the signed release query.

Every test uses a mocked hub (``httpx.MockTransport``) and a fixed clock;
nothing touches the network. Tests that open the store run against the real
PostgreSQL of the test session; every other test is marked ``no_pg``.
"""

from __future__ import annotations

import base64
import errno
import gzip
import hashlib
import json
import os
import stat
import string
from collections.abc import Callable, Iterator
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
from agent_cli.store import Store

no_pg = pytest.mark.no_pg

NOW = "2026-11-17T10:00:00Z"
HUB_URL = "https://hub.example"
HUB_HOST = "hub.example"
HUB_ID = "hub-test-1"
CLIENT_ID = "example-laptop-1"
OTHER_CLIENT_ID = "example-laptop-2"
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
# JSON nested deeper than the parser's recursion limit: json.loads raises RecursionError.
DEEP_JSON = b"[" * 200_000 + b"]" * 200_000


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


class BrokenStream(httpx.SyncByteStream):
    """An answer body whose connection breaks after the first chunk."""

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"key"'
        raise httpx.ReadError("connection reset by peer")


class FakeHub:
    """The hub side of a whoami call: checks the signed request and signs its answer."""

    def __init__(self, client_public: str) -> None:
        self.client_public = Ed25519PublicKey.from_public_bytes(unb64(client_public))
        self.target = TARGET
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
        self.signature: str | None = None
        self.drop_headers: set[str] = set()
        self.extra_headers: list[tuple[str, str]] = []
        self.compress = False
        self.error: httpx.HTTPError | None = None
        self.stream: httpx.SyncByteStream | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.check_request(request)
        if self.error is not None:
            raise self.error
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
            signature = b64(self.signer.sign(text.encode("utf-8"))) if self.signature is None else self.signature
            hub_headers = [
                ("Agent-Hub-Key", self.hub_id),
                ("Agent-Hub-Time", hub_time),
                ("Agent-Hub-Signature", signature),
            ]
            headers += [header for header in hub_headers if header[0] not in self.drop_headers]
        if self.stream is not None:
            return httpx.Response(self.status, headers=headers, stream=self.stream)
        body = self.body
        if self.compress:
            body = gzip.compress(body)
            headers.append(("Content-Encoding", "gzip"))
        return httpx.Response(self.status, headers=headers, content=body)

    def check_request(self, request: httpx.Request) -> None:
        assert request.method == "GET"
        assert request.url.host == HUB_HOST
        assert request.url.raw_path == self.target.encode("ascii")
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
                self.target,
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


def call_refused(agent_home: Path, hub: FakeHub) -> str:
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    return str(caught.value)


def refused(agent_home: Path, hub: FakeHub) -> str:
    message = call_refused(agent_home, hub)
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


def key_file(agent_home: Path, key_id: str) -> Path:
    return agent_home / "keys" / f"{key_id}.key"


def mock_hub_client(monkeypatch: pytest.MonkeyPatch, hub: FakeHub) -> None:
    """Every httpx.Client the CLI opens talks to ``hub`` through a mock transport."""
    real_client = httpx.Client

    def client_with_mock_transport(**kwargs: object) -> httpx.Client:
        assert kwargs.get("follow_redirects") is False
        return real_client(transport=httpx.MockTransport(hub), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_mock_transport)


def pair(agent_home: Path, hub_url: str) -> None:
    """Record the hub URL in the device's real store, as pairing does."""
    store = Store(agent_home)
    try:
        store.set_meta("hub_url", hub_url)
    finally:
        store.close()


def track_store_closes(monkeypatch: pytest.MonkeyPatch) -> list[Store]:
    """The stores closed from now on; each close still runs the real Store.close."""
    closed: list[Store] = []
    real_close = Store.close

    def tracking_close(self: Store) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(Store, "close", tracking_close)
    return closed


# Key creation


@no_pg
def test_create_sets_directory_and_file_modes(agent_home: Path) -> None:
    previous = os.umask(0)
    try:
        public = keys.create_key(agent_home, CLIENT_ID)
    finally:
        os.umask(previous)
    directory = agent_home / "keys"
    path = directory / f"{CLIENT_ID}.key"
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    seed = unb64(seed_text(agent_home, CLIENT_ID))
    assert len(seed) == 32
    assert b64(seed) == seed_text(agent_home, CLIENT_ID)
    assert public_text(Ed25519PrivateKey.from_private_bytes(seed)) == public


@no_pg
def test_create_tightens_an_existing_keys_directory(agent_home: Path) -> None:
    directory = agent_home / "keys"
    directory.mkdir(parents=True)
    os.chmod(directory, 0o755)
    keys.create_key(agent_home, CLIENT_ID)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@no_pg
def test_create_leaves_the_mode_of_an_existing_home(agent_home: Path) -> None:
    agent_home.mkdir(parents=True)
    os.chmod(agent_home, 0o750)
    keys.create_key(agent_home, CLIENT_ID)
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o750


@no_pg
def test_pin_hub_creates_a_missing_home_with_mode_0700(agent_home: Path) -> None:
    previous = os.umask(0)
    try:
        keys.pin_hub_key(agent_home, HUB_ID, public_text(HUB_PRIVATE), replace=False)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((agent_home / "hub-key.json").stat().st_mode) == 0o600


@no_pg
def test_load_hub_pin_refuses_a_pin_file_nested_too_deeply(agent_home: Path) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_bytes(DEEP_JSON)
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == f"{path} is not valid JSON"


@no_pg
def test_create_never_overwrites_an_existing_key(agent_home: Path) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    before = seed_text(agent_home, CLIENT_ID)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert "already exists" in message
    assert before not in message
    assert seed_text(agent_home, CLIENT_ID) == before


@no_pg
def test_create_refuses_an_existing_symlink_as_key_file(agent_home: Path, tmp_path: Path) -> None:
    directory = agent_home / "keys"
    directory.mkdir(parents=True)
    target = tmp_path / "symlink-target.key"
    key_file(agent_home, CLIENT_ID).symlink_to(target)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert message == (
        f"agent: key {CLIENT_ID} already exists at {key_file(agent_home, CLIENT_ID)}; nothing was written"
    )
    assert key_file(agent_home, CLIENT_ID).is_symlink()
    assert not target.exists()


@no_pg
def test_create_refuses_a_keys_directory_that_is_a_symlink(agent_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    agent_home.mkdir(parents=True)
    (agent_home / "keys").symlink_to(target, target_is_directory=True)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert message == f"agent: {agent_home / 'keys'} is a symbolic link; nothing was written"
    assert list(target.iterdir()) == []


@no_pg
def test_create_reports_a_keys_path_that_is_a_file(agent_home: Path) -> None:
    agent_home.mkdir(parents=True)
    (agent_home / "keys").write_text("not a directory", encoding="utf-8")
    message = cli_refused("create", "--id", CLIENT_ID)
    assert message.startswith(f"agent: key files under {agent_home}: ")
    assert (agent_home / "keys").read_text(encoding="utf-8") == "not a directory"


@no_pg
def test_create_refuses_a_key_file_created_concurrently(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process creates the key file between the existence check and the write."""
    directory = agent_home / "keys"
    real_chmod = os.chmod

    def chmod_then_concurrent_create(path: object, mode: int, *args: object, **kwargs: object) -> None:
        real_chmod(path, mode, *args, **kwargs)
        if Path(str(path)) == directory:
            key_file(agent_home, CLIENT_ID).write_bytes(b"written by another process\n")

    monkeypatch.setattr(os, "chmod", chmod_then_concurrent_create)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert message == (
        f"agent: key {CLIENT_ID} already exists at {key_file(agent_home, CLIENT_ID)}; nothing was written"
    )
    assert key_file(agent_home, CLIENT_ID).read_bytes() == b"written by another process\n"


@no_pg
def test_create_removes_a_partly_written_key_file(agent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_fchmod(fd: int, mode: int) -> None:
        raise OSError(errno.EIO, "disk failed")

    monkeypatch.setattr(os, "fchmod", failing_fchmod)
    message = cli_refused("create", "--id", CLIENT_ID)
    assert message == f"agent: key files under {agent_home}: [Errno {errno.EIO}] disk failed"
    assert not key_file(agent_home, CLIENT_ID).exists()
    assert list((agent_home / "keys").iterdir()) == []


@no_pg
def test_create_prints_only_the_public_key(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli("create", "--id", CLIENT_ID)
    captured = capsys.readouterr()
    seed = seed_text(agent_home, CLIENT_ID)
    public = public_text(Ed25519PrivateKey.from_private_bytes(unb64(seed)))
    assert captured.out == f"{public}\n"
    assert seed not in captured.out
    assert seed not in captured.err


@no_pg
@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_create_refuses_an_id_outside_the_rule_and_writes_nothing(agent_home: Path, bad_id: str) -> None:
    message = cli_refused("create", "--id", bad_id)
    assert "nothing was written" in message or "--id is required" in message
    assert not agent_home.exists()


@no_pg
def test_create_accepts_the_shortest_and_longest_id(agent_home: Path) -> None:
    keys.create_key(agent_home, "a1b")
    keys.create_key(agent_home, "a" * 63)
    assert (agent_home / "keys" / "a1b.key").is_file()
    assert (agent_home / "keys" / f"{'a' * 63}.key").is_file()


# Listing keys


@no_pg
def test_list_shows_ids_and_public_keys(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    second = keys.create_key(agent_home, "example-runner-1")
    first = keys.create_key(agent_home, CLIENT_ID)
    capsys.readouterr()
    run_cli("list")
    captured = capsys.readouterr()
    assert captured.out == f"{CLIENT_ID}  {first}\nexample-runner-1  {second}\n"
    assert seed_text(agent_home, CLIENT_ID) not in captured.out


@no_pg
def test_list_without_keys_prints_nothing(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli("list")
    assert capsys.readouterr().out == ""
    assert not agent_home.exists()


@no_pg
def test_list_skips_files_that_are_not_key_files(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    public = keys.create_key(agent_home, CLIENT_ID)
    directory = agent_home / "keys"
    (directory / "README").write_text("notes", encoding="utf-8")
    (directory / ".hidden").write_text("notes", encoding="utf-8")
    (directory / f"{CLIENT_ID}.key.bak").write_text("old", encoding="utf-8")
    (directory / "archive").mkdir()
    capsys.readouterr()
    run_cli("list")
    assert capsys.readouterr().out == f"{CLIENT_ID}  {public}\n"


@no_pg
@pytest.mark.parametrize("name", ["Bad_Name.key", "ab.key", ".key", "-abc.key"])
def test_list_refuses_a_key_file_named_outside_the_id_rule(agent_home: Path, name: str) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    path = agent_home / "keys" / name
    path.write_text(f"{b64(bytes(32))}\n", encoding="ascii")
    assert cli_refused("list") == f"agent: key file {path} has a name outside the id rule"


@no_pg
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"\xff\xfe\n",
        b"not base64!\n",
        f"{b64(bytes(31))}\n".encode("ascii"),
        f"{b64(bytes(33))}\n".encode("ascii"),
        f"{non_canonical(b64(bytes(32)))}\n".encode("ascii"),
        f"{b64(bytes(32))}\n\n".encode("ascii"),
    ],
)
def test_list_refuses_a_malformed_key_file(agent_home: Path, content: bytes) -> None:
    path = key_file(agent_home, CLIENT_ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert cli_refused("list") == (
        f"agent: key file {path} does not hold a 32-byte seed as base64url"
    )


@no_pg
def test_list_refuses_an_unreadable_key_file(agent_home: Path) -> None:
    path = key_file(agent_home, CLIENT_ID)
    path.mkdir(parents=True)
    assert cli_refused("list").startswith(f"agent: cannot read key file {path}: ")


@no_pg
def test_cli_list_reports_a_keys_path_that_is_a_file(agent_home: Path) -> None:
    agent_home.mkdir(parents=True, exist_ok=True)
    (agent_home / "keys").write_text("not a directory", encoding="utf-8")
    assert cli_refused("list").startswith(f"agent: key files under {agent_home}: ")


# Pinning the hub key


@no_pg
def test_pin_hub_writes_the_file_with_mode_0600(agent_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    assert capsys.readouterr().out == f"pinned hub key {HUB_ID}\n"
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


@no_pg
@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_pin_hub_refuses_an_invalid_id_and_writes_nothing(agent_home: Path, bad_id: str) -> None:
    cli_refused("pin-hub", "--id", bad_id, "--public-key", HUB_PUBLIC)
    assert not (agent_home / "hub-key.json").exists()
    assert not agent_home.exists()


@no_pg
@pytest.mark.parametrize("bad_key", BAD_PUBLIC_KEYS)
def test_pin_hub_refuses_an_invalid_key_and_writes_nothing(agent_home: Path, bad_key: str) -> None:
    cli_refused("pin-hub", "--id", HUB_ID, "--public-key", bad_key)
    assert not (agent_home / "hub-key.json").exists()
    assert not agent_home.exists()


@no_pg
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
    assert list(agent_home.glob(".hub-key.json.tmp.*")) == []


@no_pg
def test_pin_hub_replace_without_an_earlier_pin_writes_the_file(agent_home: Path) -> None:
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC, "--replace")
    path = agent_home / "hub-key.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keys.load_hub_pin(agent_home).key_id == HUB_ID


@no_pg
def test_pin_hub_replace_refuses_an_invalid_key_and_keeps_the_pin(agent_home: Path) -> None:
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    cli_refused("pin-hub", "--id", HUB_ID, "--public-key", y_key(1, False), "--replace")
    assert public_text_of(keys.load_hub_pin(agent_home).public_key) == HUB_PUBLIC


@no_pg
def test_pin_hub_refuses_an_existing_symlink_as_pin_file(agent_home: Path, tmp_path: Path) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    target = tmp_path / "pin-target.json"
    path.symlink_to(target)
    message = cli_refused("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC)
    assert message == f"agent: a hub key is already pinned in {path}; use --replace to replace it"
    assert path.is_symlink()
    assert not target.exists()


@no_pg
def test_pin_hub_replace_replaces_a_symlink_and_never_writes_its_target(agent_home: Path, tmp_path: Path) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    target = tmp_path / "pin-target.json"
    path.symlink_to(target)
    run_cli("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC, "--replace")
    assert not path.is_symlink()
    assert not target.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keys.load_hub_pin(agent_home).key_id == HUB_ID


@no_pg
def test_pin_hub_replace_that_fails_leaves_no_temporary_file(agent_home: Path) -> None:
    path = agent_home / "hub-key.json"
    path.mkdir(parents=True)
    message = cli_refused("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC, "--replace")
    assert message.startswith(f"agent: key files under {agent_home}: ")
    assert path.is_dir()
    assert list(agent_home.glob(".hub-key.json.tmp.*")) == []


# Loading the pinned hub key


@no_pg
def test_load_hub_pin_reports_a_pin_path_that_is_a_directory(agent_home: Path) -> None:
    path = agent_home / "hub-key.json"
    path.mkdir(parents=True)
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value).startswith(f"cannot read {path}: ")


@no_pg
def test_load_hub_pin_reports_a_pin_file_that_is_not_utf8(agent_home: Path) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_bytes(b'{"id": "\xff"}')
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value).startswith(f"cannot read {path}: ")


@no_pg
@pytest.mark.parametrize("content", ["", "{", "not json", '{"id": "hub-test-1",}'])
def test_load_hub_pin_refuses_a_pin_file_that_is_not_json(agent_home: Path, content: str) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == f"{path} is not valid JSON"


@no_pg
@pytest.mark.parametrize(
    "data",
    [
        [],
        "hub-test-1",
        {},
        {"id": HUB_ID},
        {"public_key": HUB_PUBLIC},
        {"id": HUB_ID, "public_key": HUB_PUBLIC, "extra": "x"},
        {"id": 1, "public_key": HUB_PUBLIC},
        {"id": HUB_ID, "public_key": None},
    ],
)
def test_load_hub_pin_refuses_a_pin_file_of_another_shape(agent_home: Path, data: object) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == f"{path} must hold exactly the strings id and public_key"


@no_pg
@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_load_hub_pin_refuses_a_pinned_id_outside_the_rule(agent_home: Path, bad_id: str) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_text(json.dumps({"id": bad_id, "public_key": HUB_PUBLIC}), encoding="utf-8")
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == f"{path} holds a hub key id outside the id rule"


@no_pg
@pytest.mark.parametrize("bad_key", BAD_PUBLIC_KEYS)
def test_load_hub_pin_refuses_an_invalid_pinned_public_key(agent_home: Path, bad_key: str) -> None:
    agent_home.mkdir(parents=True)
    path = agent_home / "hub-key.json"
    path.write_text(json.dumps({"id": HUB_ID, "public_key": bad_key}), encoding="utf-8")
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value).startswith(f"{path} holds an invalid hub public key (")


@no_pg
def test_cli_whoami_reports_a_malformed_pin_before_opening_the_store(agent_home: Path) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    path = agent_home / "hub-key.json"
    path.write_text("{", encoding="utf-8")
    assert cli_refused("whoami", "--id", CLIENT_ID) == f"agent: {path} is not valid JSON"


# The signed whoami call


@no_pg
def test_whoami_accepts_the_signed_answer_to_its_request(agent_home: Path, hub: FakeHub) -> None:
    assert call(agent_home, hub, NOW, HUB_URL) == WHOAMI
    assert len(hub.requests) == 1
    request = hub.requests[0]
    assert request.headers["Agent-Time"] == NOW
    assert "Content-Encoding" not in request.headers


@no_pg
def test_whoami_uses_a_new_nonce_for_every_request(agent_home: Path, hub: FakeHub) -> None:
    call(agent_home, hub, NOW, HUB_URL)
    call(agent_home, hub, NOW, HUB_URL)
    nonces = {request.headers["Agent-Nonce"] for request in hub.requests}
    assert len(nonces) == 2


@no_pg
def test_whoami_refuses_an_answer_without_hub_headers(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    refused(agent_home, hub)


@no_pg
def test_whoami_shows_the_error_of_an_unsigned_refusal_marked_unverified(
    agent_home: Path, hub: FakeHub
) -> None:
    hub.signed = False
    hub.status = 401
    hub.body = b'{"error": "key_unknown"}'
    message = refused(agent_home, hub)
    assert "unverified" in message
    assert "key_unknown" in message


@no_pg
def test_whoami_shows_no_unsigned_body_that_is_not_a_plain_refusal(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    hub.status = 401
    hub.body = b'{"error": "\\u001b[31mspoofed"}'
    message = refused(agent_home, hub)
    assert "spoofed" not in message


@no_pg
@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (401, b'{"error": "key_unknown"}', "HTTP 401, error key_unknown"),
        (403, b'{"error": "scope_missing"}', "HTTP 403, error scope_missing"),
        (401, b'{"error": "\\u001b[31mspoofed"}', "HTTP 401"),
        (401, b'{"error": "' + b"a" * 65 + b'"}', "HTTP 401"),
        (401, b'{"error": ""}', "HTTP 401"),
        (401, b'{"error": 1}', "HTTP 401"),
        (401, b'{"error": null}', "HTTP 401"),
        (401, b'{"error": "key_unknown", "detail": "spoofed"}', "HTTP 401"),
        (401, b"{}", "HTTP 401"),
        (401, b'["key_unknown"]', "HTTP 401"),
        (401, b'"key_unknown"', "HTTP 401"),
        (401, b"key_unknown", "HTTP 401"),
        (401, b"", "HTTP 401"),
        (401, b"\xff\xff", "HTTP 401"),
        (500, DEEP_JSON, "HTTP 500"),
        (200, b'{"error": "key_unknown"}', "HTTP 200"),
        (399, b'{"error": "key_unknown"}', "HTTP 399"),
    ],
)
def test_whoami_reports_an_unsigned_answer_as_unverified(
    agent_home: Path, hub: FakeHub, status: int, body: bytes, detail: str
) -> None:
    hub.signed = False
    hub.status = status
    hub.body = body
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (unsigned answer; unverified: {detail})"


@no_pg
@pytest.mark.parametrize("name", keys.HUB_HEADERS)
def test_whoami_refuses_an_answer_missing_one_hub_header(agent_home: Path, hub: FakeHub, name: str) -> None:
    hub.drop_headers = {name}
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} ({name} is missing or repeated)"


@no_pg
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Agent-Hub-Key", HUB_ID),
        ("Agent-Hub-Time", NOW),
        ("Agent-Hub-Signature", b64(bytes(64))),
    ],
)
def test_whoami_refuses_an_answer_repeating_one_hub_header(
    agent_home: Path, hub: FakeHub, name: str, value: str
) -> None:
    hub.extra_headers = [(name, value)]
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} ({name} is missing or repeated)"


@no_pg
def test_whoami_refuses_a_repeated_hub_header(agent_home: Path, hub: FakeHub) -> None:
    hub.extra_headers = [("Agent-Hub-Key", HUB_ID)]
    refused(agent_home, hub)


@no_pg
@pytest.mark.parametrize("hub_id", ["hub-other-1", "", "HUB-TEST-1"])
def test_whoami_refuses_a_wrong_hub_key_id(agent_home: Path, hub: FakeHub, hub_id: str) -> None:
    hub.hub_id = hub_id
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (Agent-Hub-Key is not the pinned hub key id)"


@no_pg
def test_whoami_refuses_a_wrong_signature(agent_home: Path, hub: FakeHub) -> None:
    hub.signer = OTHER_PRIVATE
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (the signature does not verify with the pinned hub key)"


@no_pg
@pytest.mark.parametrize(
    "signature",
    [
        "",
        "not base64!",
        b64(bytes(64)) + "=",
        b64(bytes(63)),
        b64(bytes(65)),
        b64(bytes(32)),
        non_canonical(b64(bytes(64))),
        "A" * 85,
    ],
)
def test_whoami_refuses_a_malformed_hub_signature(agent_home: Path, hub: FakeHub, signature: str) -> None:
    hub.signature = signature
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (Agent-Hub-Signature is not a 64-byte base64url value)"


@no_pg
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


@no_pg
def test_whoami_refuses_an_answer_signed_for_another_nonce(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_nonce = b64(bytes(16))
    refused(agent_home, hub)


@no_pg
def test_whoami_refuses_an_answer_signed_for_another_request_time(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_time = shifted(-5)
    refused(agent_home, hub)


@no_pg
def test_whoami_refuses_a_changed_body(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_body = hub.body
    hub.body = json.dumps({**WHOAMI, "handle": "someone-else"}).encode("utf-8")
    message = refused(agent_home, hub)
    assert "someone-else" not in message


@no_pg
def test_whoami_refuses_a_changed_status(agent_home: Path, hub: FakeHub) -> None:
    hub.signed_status = 404
    refused(agent_home, hub)


@no_pg
@pytest.mark.parametrize("seconds", [120, -120])
def test_whoami_accepts_a_hub_time_120_seconds_off(agent_home: Path, hub: FakeHub, seconds: int) -> None:
    hub.hub_time = shifted(seconds)
    assert call(agent_home, hub, NOW, HUB_URL) == WHOAMI


@no_pg
@pytest.mark.parametrize("seconds", [121, -121, 3600])
def test_whoami_refuses_a_hub_time_outside_the_window(agent_home: Path, hub: FakeHub, seconds: int) -> None:
    hub.hub_time = shifted(seconds)
    message = refused(agent_home, hub)
    assert message == (
        f"{keys.NOT_VERIFIED} (Agent-Hub-Time is more than 120 seconds from this device's clock)"
    )


@no_pg
@pytest.mark.parametrize(
    "hub_time",
    ["2026-11-17T10:00:00.5Z", "2026-11-17T10:00:00+00:00", "2026-02-30T10:00:00Z", ""],
)
def test_whoami_refuses_a_malformed_hub_time(agent_home: Path, hub: FakeHub, hub_time: str) -> None:
    hub.hub_time = hub_time
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (Agent-Hub-Time is not YYYY-MM-DDTHH:MM:SSZ)"


@no_pg
def test_whoami_refuses_an_answer_with_content_encoding(agent_home: Path, hub: FakeHub) -> None:
    hub.compress = True
    message = refused(agent_home, hub)
    assert "Content-Encoding" in message


@no_pg
def test_whoami_refuses_an_answer_with_identity_content_encoding(agent_home: Path, hub: FakeHub) -> None:
    hub.extra_headers = [("Content-Encoding", "identity")]
    message = refused(agent_home, hub)
    assert "Content-Encoding" in message


@no_pg
def test_verify_answer_refuses_an_answer_with_content_encoding() -> None:
    pin = keys.HubPin(key_id=HUB_ID, public_key=HUB_PRIVATE.public_key())
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.verify_answer(
            pin,
            key_id=CLIENT_ID,
            nonce=b64(bytes(16)),
            request_time=NOW,
            status=200,
            headers=httpx.Headers({"Content-Encoding": "identity"}),
            body=b"{}",
            now=NOW,
        )
    assert str(caught.value) == f"{keys.NOT_VERIFIED} (the answer carries a Content-Encoding)"


@no_pg
def test_whoami_refuses_an_answer_larger_than_1_mib(agent_home: Path, hub: FakeHub) -> None:
    hub.body = b" " * (keys.MAX_ANSWER_BYTES + 1)
    message = refused(agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (the answer is larger than 1 MiB)"


@no_pg
def test_whoami_accepts_an_answer_of_exactly_1_mib(agent_home: Path, hub: FakeHub) -> None:
    body = json.dumps(WHOAMI).encode("utf-8")
    hub.body = body + b" " * (keys.MAX_ANSWER_BYTES - len(body))
    assert len(hub.body) == keys.MAX_ANSWER_BYTES
    assert call(agent_home, hub, NOW, HUB_URL) == WHOAMI


@no_pg
def test_whoami_reports_a_connection_that_breaks_while_reading(agent_home: Path, hub: FakeHub) -> None:
    hub.stream = BrokenStream()
    assert call_refused(agent_home, hub) == "hub request failed: connection reset by peer"
    assert len(hub.requests) == 1


@no_pg
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_whoami_reports_a_transport_error(agent_home: Path, hub: FakeHub, error: httpx.HTTPError) -> None:
    hub.error = error
    assert call_refused(agent_home, hub) == f"hub request failed: {error}"
    assert len(hub.requests) == 1


@no_pg
def test_whoami_reports_a_signed_refusal_as_verified(agent_home: Path, hub: FakeHub) -> None:
    hub.status = 403
    hub.body = b'{"error": "scope_missing"}'
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    assert str(caught.value) == "hub GET /api/key/whoami → HTTP 403: scope_missing"


@no_pg
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, b'{"error": 7}'),
        (403, b'{"error": "scope_missing", "detail": "x"}'),
        (404, b'["scope_missing"]'),
        (500, b"internal error"),
        (500, b""),
        (500, DEEP_JSON),
        (502, b'{"error": "\\u001b[31mspoofed"}'),
    ],
)
def test_whoami_reports_a_signed_refusal_without_a_plain_error_value(
    agent_home: Path, hub: FakeHub, status: int, body: bytes
) -> None:
    hub.status = status
    hub.body = body
    assert call_refused(agent_home, hub) == f"hub GET /api/key/whoami → HTTP {status}: "


@no_pg
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "hub answer is not JSON"),
        (b"not json", "hub answer is not JSON"),
        (b'{"key": ', "hub answer is not JSON"),
        (b"\xff\xff", "hub answer is not JSON"),
        (DEEP_JSON, "hub answer is not JSON"),
        (b"[]", "hub answer is not a JSON object"),
        (b'"example-laptop-1"', "hub answer is not a JSON object"),
        (b"null", "hub answer is not a JSON object"),
        (b"42", "hub answer is not a JSON object"),
    ],
)
def test_whoami_refuses_a_verified_answer_that_is_not_a_json_object(
    agent_home: Path, hub: FakeHub, body: bytes, message: str
) -> None:
    hub.body = body
    assert call_refused(agent_home, hub) == message


@no_pg
def test_whoami_does_not_follow_an_unsigned_redirect(agent_home: Path, hub: FakeHub) -> None:
    hub.signed = False
    hub.status = 302
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/api/key/whoami")]
    refused(agent_home, hub)
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


@no_pg
def test_whoami_does_not_follow_a_signed_redirect(agent_home: Path, hub: FakeHub) -> None:
    hub.status = 307
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/api/key/whoami")]
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, NOW, HUB_URL)
    assert str(caught.value) == "hub GET /api/key/whoami → HTTP 307: "
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


@no_pg
@pytest.mark.parametrize(
    "clock_value",
    ["2026-11-17 10:00:00", "2026-11-17T10:00:00+00:00", "2026-13-01T10:00:00Z", ""],
)
def test_whoami_refuses_a_clock_without_a_valid_time_before_sending(
    agent_home: Path, hub: FakeHub, clock_value: str
) -> None:
    with pytest.raises(keys.KeyProtocolError) as caught:
        call(agent_home, hub, clock_value, HUB_URL)
    assert str(caught.value) == "the local clock did not give a time as YYYY-MM-DDTHH:MM:SSZ"
    assert hub.requests == []


@no_pg
def test_whoami_checks_the_clock_again_for_the_answer(agent_home: Path, hub: FakeHub) -> None:
    times = iter([NOW, "later"])
    pin = keys.load_hub_pin(agent_home)
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(keys.KeyProtocolError) as caught:
            keys.whoami(agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: next(times))
    assert str(caught.value) == "the local clock did not give a time as YYYY-MM-DDTHH:MM:SSZ"
    assert len(hub.requests) == 1


@no_pg
def test_whoami_without_a_key_file_sends_nothing(agent_home: Path, hub: FakeHub) -> None:
    pin = keys.load_hub_pin(agent_home)
    path = key_file(agent_home, OTHER_CLIENT_ID)
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(keys.KeyProtocolError) as caught:
            keys.whoami(agent_home, OTHER_CLIENT_ID, pin, HUB_URL, client, lambda: NOW)
    assert str(caught.value) == (
        f"no key {OTHER_CLIENT_ID} at {path}; run agent key create --id {OTHER_CLIENT_ID}"
    )
    assert hub.requests == []


@no_pg
def test_whoami_with_an_unreadable_key_file_sends_nothing(agent_home: Path, hub: FakeHub) -> None:
    pin = keys.load_hub_pin(agent_home)
    path = key_file(agent_home, OTHER_CLIENT_ID)
    path.mkdir()
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(keys.KeyProtocolError) as caught:
            keys.whoami(agent_home, OTHER_CLIENT_ID, pin, HUB_URL, client, lambda: NOW)
    assert str(caught.value).startswith(f"cannot read key file {path}: ")
    assert hub.requests == []


@no_pg
def test_whoami_with_a_malformed_key_file_sends_nothing(agent_home: Path, hub: FakeHub) -> None:
    pin = keys.load_hub_pin(agent_home)
    path = key_file(agent_home, CLIENT_ID)
    path.write_text("not-a-seed\n", encoding="ascii")
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(keys.KeyProtocolError) as caught:
            keys.whoami(agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: NOW)
    assert str(caught.value) == f"key file {path} does not hold a 32-byte seed as base64url"
    assert "not-a-seed" not in str(caught.value)
    assert hub.requests == []


@no_pg
def test_whoami_without_a_pinned_hub_key_sends_nothing(agent_home: Path) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.load_hub_pin(agent_home)
    assert str(caught.value) == "hub key is not pinned"


@no_pg
def test_whoami_refuses_an_id_outside_the_rule_without_claiming_a_write(
    agent_home: Path, hub: FakeHub
) -> None:
    pin = keys.load_hub_pin(agent_home)
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(keys.KeyProtocolError) as caught:
            keys.whoami(agent_home, "../x", pin, HUB_URL, client, lambda: NOW)
    assert "nothing was written" not in str(caught.value)
    assert str(caught.value).startswith("ID must be ")
    assert hub.requests == []


# The whoami command with the device's real store


def test_cli_whoami_without_a_pinned_hub_key_sends_nothing(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    opened: list[dict[str, object]] = []
    real_open_store = agent_main.open_store

    def recording_open_store(**kwargs: object) -> Store:
        opened.append(kwargs)
        return real_open_store(**kwargs)

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(agent_main, "open_store", recording_open_store)
    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused("whoami", "--id", CLIENT_ID) == "agent: hub key is not pinned"
    assert opened == []


def test_cli_whoami_prints_the_verified_answer(
    agent_home: Path,
    hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair(agent_home, HUB_URL)
    closed = track_store_closes(monkeypatch)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, hub)
    run_cli("whoami", "--id", CLIENT_ID)
    assert json.loads(capsys.readouterr().out) == WHOAMI
    assert len(closed) == 1
    assert len(hub.requests) == 1


def test_cli_whoami_prints_nothing_for_an_unverified_answer(
    agent_home: Path,
    hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hub.signer = OTHER_PRIVATE
    pair(agent_home, HUB_URL)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, hub)
    message = cli_refused("whoami", "--id", CLIENT_ID)
    assert message.startswith(f"agent: {keys.NOT_VERIFIED}")
    assert capsys.readouterr().out == ""


def test_cli_whoami_without_a_hub_url_sends_nothing(
    agent_home: Path, hub: FakeHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = track_store_closes(monkeypatch)

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused("whoami", "--id", CLIENT_ID) == "agent: device is not paired"
    assert len(closed) == 1
    assert hub.requests == []


# The release query

REPO = "example-org/example-repo"
RELEASE_TARGET = "/api/key/release?repository=example-org%2Fexample-repo&pull_request=42"
RELEASED = {
    "repository": REPO,
    "pull_request": 42,
    "released": True,
    "failed": None,
    "hub_key": HUB_ID,
}
RELEASED_LINE = (
    '{"failed": null, "hub_key": "hub-test-1", "pull_request": 42, '
    '"released": true, "repository": "example-org/example-repo"}\n'
)
FAILURES = ("task_missing", "task_not_ready", "question_unanswered")
REPO_MESSAGE = f"--repo must be {keys.REPOSITORY_RULE}; nothing was sent"
PR_MESSAGE = "--pr must be a number from 1 to 2147483647 without sign or leading zero; nothing was sent"
BAD_REPOS = (
    "",
    "example-org",
    "example-org/",
    "/example-repo",
    "/",
    "Example-org/example-repo",
    "example-org/Example-repo",
    "EXAMPLE-ORG/EXAMPLE-REPO",
    "-example/example-repo",
    "example-/example-repo",
    "ex--ample/example-repo",
    "-/example-repo",
    "a" * 40 + "/example-repo",
    "example_org/example-repo",
    "example.org/example-repo",
    "example-org/" + "a" * 101,
    "example-org/.",
    "example-org/..",
    "example-org/example-repo/extra",
    "example-org//example-repo",
    "example-org%2Fexample-repo",
    "example-org/example repo",
    "example-org/example+repo",
    "example-org/example%repo",
    "example-org/example-repo\n",
    "\nexample-org/example-repo",
    "exämple/example-repo",
    "example-org/exämple",
)
BAD_PULL_REQUESTS = (
    "",
    "0",
    "00",
    "01",
    "+1",
    "-1",
    "1.0",
    "1e3",
    "0x10",
    " 1",
    "1 ",
    "1\n",
    "4 2",
    "٣",
    "2147483648",
    "9999999999",
    "99999999999",
)


def release_body(**changes: object) -> bytes:
    return json.dumps({**RELEASED, **changes}).encode("utf-8")


def release_body_without(field: str) -> bytes:
    return json.dumps({name: value for name, value in RELEASED.items() if name != field}).encode("utf-8")


MALFORMED_RELEASE_ANSWERS = (
    # A repeated name is refused, not resolved to its last value.
    b'{"repository":"example-org/example-repo","pull_request":42,"released":false,"released":true,'
    b'"failed":null,"hub_key":"hub-test-1"}',
    b'{"repository":"example-org/example-repo","pull_request":42,"released":true,"failed":"task_missing",'
    b'"failed":null,"hub_key":"hub-test-1"}',
    # The fields in another order than the hub writes them.
    json.dumps(dict(reversed(list(RELEASED.items())))).encode("utf-8"),
    # An array of pairs is not an object.
    json.dumps([[name, value] for name, value in RELEASED.items()]).encode("utf-8"),
    # Not UTF-8, or UTF-8 with a byte order mark.
    json.dumps(RELEASED).encode("utf-16"),
    json.dumps(RELEASED).encode("utf-32"),
    b"\xef\xbb\xbf" + json.dumps(RELEASED).encode("utf-8"),
    *(release_body_without(field) for field in RELEASED),
    release_body(extra="x"),
    json.dumps({}).encode("utf-8"),
    release_body(repository=None),
    release_body(repository=1),
    release_body(repository=[REPO]),
    release_body(pull_request="42"),
    release_body(pull_request=42.0),
    release_body(pull_request=None),
    release_body(pull_request=[42]),
    release_body(released="true"),
    release_body(released=1),
    release_body(released=None),
    release_body(released=False, failed=0),
    release_body(released=False, failed=False),
    release_body(released=False, failed=[FAILURES[0]]),
    release_body(released=False, failed={"failed": FAILURES[0]}),
    release_body(hub_key=None),
    release_body(hub_key=1),
    release_body(failed=FAILURES[0]),
    release_body(failed=""),
    release_body(failed=False),
    release_body(released=False, failed=None),
    release_body(released=False, failed="unknown"),
    release_body(released=False, failed="TASK_MISSING"),
    release_body(released=False, failed="task_missing "),
    release_body(released=False, failed=""),
    release_body(repository="example-org/other-repo"),
    release_body(repository="Example-org/example-repo"),
    release_body(repository="example-org%2Fexample-repo"),
    release_body(pull_request=43),
    release_body(pull_request=-42),
    release_body(hub_key="hub-other-1"),
    release_body(hub_key="HUB-TEST-1"),
    b"[]",
    b"null",
    b'"released"',
    b"42",
    b"true",
    b"",
    b"not json",
    b"\xff\xff",
    b'{"repository": ',
    DEEP_JSON,
)


@pytest.fixture
def release_hub(hub: FakeHub) -> FakeHub:
    hub.target = RELEASE_TARGET
    hub.body = release_body()
    return hub


def call_release(
    agent_home: Path, hub: FakeHub, repository: str = REPO, pull_request: str = "42"
) -> dict:
    pin = keys.load_hub_pin(agent_home)
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        return keys.release_query(
            agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: NOW, repository, pull_request
        )


def release_refused(
    agent_home: Path, hub: FakeHub, repository: str = REPO, pull_request: str = "42"
) -> str:
    with pytest.raises(keys.KeyProtocolError) as caught:
        call_release(agent_home, hub, repository, pull_request)
    return str(caught.value)


@no_pg
def test_release_sends_the_exact_signed_request_and_accepts_a_release(
    agent_home: Path, release_hub: FakeHub
) -> None:
    assert call_release(agent_home, release_hub) == RELEASED
    assert len(release_hub.requests) == 1
    request = release_hub.requests[0]
    assert request.method == "GET"
    assert str(request.url) == HUB_URL + RELEASE_TARGET
    assert request.url.raw_path == RELEASE_TARGET.encode("ascii")
    assert request.content == b""
    for name in ("Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature"):
        assert len(request.headers.get_list(name)) == 1
    assert request.headers["Agent-Key"] == CLIENT_ID
    assert request.headers["Agent-Time"] == NOW
    assert request.headers["Accept-Encoding"] == "identity"
    nonce = request.headers["Agent-Nonce"]
    assert len(unb64(nonce)) == 16
    text = f"agent-request-v1\n{CLIENT_ID}\nGET\n{HUB_HOST}\n{RELEASE_TARGET}\n{NOW}\n{nonce}\n{EMPTY_SHA}"
    release_hub.client_public.verify(unb64(request.headers["Agent-Signature"]), text.encode("utf-8"))


@no_pg
@pytest.mark.parametrize("failed", FAILURES)
def test_release_returns_a_verified_answer_that_does_not_release(
    agent_home: Path, release_hub: FakeHub, failed: str
) -> None:
    release_hub.body = release_body(released=False, failed=failed)
    assert call_release(agent_home, release_hub) == {**RELEASED, "released": False, "failed": failed}


@no_pg
@pytest.mark.parametrize(
    ("repository", "pull_request", "target"),
    [
        ("a/b", "1", "/api/key/release?repository=a%2Fb&pull_request=1"),
        (
            "a" * 39 + "/" + "z" * 100,
            "2147483647",
            "/api/key/release?repository=" + "a" * 39 + "%2F" + "z" * 100 + "&pull_request=2147483647",
        ),
        ("a-b-c/my.repo_name-1", "10", "/api/key/release?repository=a-b-c%2Fmy.repo_name-1&pull_request=10"),
        ("0/...", "9", "/api/key/release?repository=0%2F...&pull_request=9"),
        ("x1/.github", "1000000000", "/api/key/release?repository=x1%2F.github&pull_request=1000000000"),
    ],
)
def test_release_accepts_the_bounds_of_the_rules(
    agent_home: Path, hub: FakeHub, repository: str, pull_request: str, target: str
) -> None:
    answer = {**RELEASED, "repository": repository, "pull_request": int(pull_request)}
    hub.target = target
    hub.body = json.dumps(answer).encode("utf-8")
    assert call_release(agent_home, hub, repository, pull_request) == answer
    assert hub.requests[0].url.raw_path == target.encode("ascii")


@no_pg
@pytest.mark.parametrize("body", MALFORMED_RELEASE_ANSWERS)
def test_release_refuses_a_verified_answer_of_another_form(
    agent_home: Path, release_hub: FakeHub, body: bytes
) -> None:
    release_hub.body = body
    assert release_refused(agent_home, release_hub) == keys.UNEXPECTED_FORM
    assert len(release_hub.requests) == 1


@no_pg
def test_release_refuses_a_pull_request_number_written_as_true(agent_home: Path, hub: FakeHub) -> None:
    hub.target = "/api/key/release?repository=example-org%2Fexample-repo&pull_request=1"
    hub.body = release_body(pull_request=True)
    assert release_refused(agent_home, hub, REPO, "1") == keys.UNEXPECTED_FORM


@no_pg
@pytest.mark.parametrize("repository", BAD_REPOS)
def test_release_refuses_an_invalid_repo_before_sending(
    agent_home: Path, release_hub: FakeHub, repository: str
) -> None:
    assert release_refused(agent_home, release_hub, repository, "42") == REPO_MESSAGE
    assert release_hub.requests == []


@no_pg
@pytest.mark.parametrize("pull_request", BAD_PULL_REQUESTS)
def test_release_refuses_an_invalid_pr_before_sending(
    agent_home: Path, release_hub: FakeHub, pull_request: str
) -> None:
    assert release_refused(agent_home, release_hub, REPO, pull_request) == PR_MESSAGE
    assert release_hub.requests == []


@no_pg
def test_release_checks_the_repo_before_the_pr(agent_home: Path, release_hub: FakeHub) -> None:
    assert release_refused(agent_home, release_hub, "Example/x", "0") == REPO_MESSAGE
    assert release_hub.requests == []


# Paths shared by whoami and the release query: the signed GET and its verification

COMMAND_REQUESTS = {
    "whoami": (TARGET, json.dumps(WHOAMI).encode("utf-8")),
    "release": (RELEASE_TARGET, release_body()),
}
COMMAND_ANSWERS = {"whoami": WHOAMI, "release": RELEASED}
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(params=sorted(COMMAND_REQUESTS))
def command(request: pytest.FixtureRequest, hub: FakeHub) -> str:
    hub.target, hub.body = COMMAND_REQUESTS[request.param]
    return request.param


def call_command(
    command: str, agent_home: Path, hub: FakeHub, now: str = NOW, handler: Handler | None = None
) -> dict:
    pin = keys.load_hub_pin(agent_home)
    transport = httpx.MockTransport(hub if handler is None else handler)
    with httpx.Client(transport=transport) as client:
        if command == "whoami":
            return keys.whoami(agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: now)
        return keys.release_query(agent_home, CLIENT_ID, pin, HUB_URL, client, lambda: now, REPO, "42")


def command_refused(
    command: str, agent_home: Path, hub: FakeHub, handler: Handler | None = None
) -> str:
    with pytest.raises(keys.KeyProtocolError) as caught:
        call_command(command, agent_home, hub, handler=handler)
    return str(caught.value)


@no_pg
def test_signed_get_accepts_the_signed_answer(agent_home: Path, hub: FakeHub, command: str) -> None:
    assert call_command(command, agent_home, hub) == COMMAND_ANSWERS[command]
    assert len(hub.requests) == 1


@no_pg
@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (400, b'{"error":"query_invalid"}', "query_invalid"),
        (403, b'{"error":"scope_missing"}', "scope_missing"),
        (403, b'{"error":"subject_not_allowed"}', "subject_not_allowed"),
        (404, b'{"error":"not_found"}', "not_found"),
        (404, b"", ""),
        (500, b"internal error", ""),
    ],
)
def test_signed_get_reports_a_signed_refusal_with_its_target(
    agent_home: Path, hub: FakeHub, command: str, status: int, body: bytes, detail: str
) -> None:
    hub.status = status
    hub.body = body
    message = command_refused(command, agent_home, hub)
    assert message == f"hub GET {COMMAND_REQUESTS[command][0]} → HTTP {status}: {detail}"


@no_pg
@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (400, b'{"error":"query_invalid"}', "HTTP 400, error query_invalid"),
        (401, b'{"error":"key_unknown"}', "HTTP 401, error key_unknown"),
        (403, b'{"error":"scope_missing"}', "HTTP 403, error scope_missing"),
        (403, b'{"error":"subject_not_allowed"}', "HTTP 403, error subject_not_allowed"),
        (404, b"", "HTTP 404"),
        (200, release_body(), "HTTP 200"),
    ],
)
def test_signed_get_reports_an_unsigned_answer_as_unverified(
    agent_home: Path, hub: FakeHub, command: str, status: int, body: bytes, detail: str
) -> None:
    hub.signed = False
    hub.status = status
    hub.body = body
    message = command_refused(command, agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (unsigned answer; unverified: {detail})"


@no_pg
def test_signed_get_refuses_an_answer_signed_by_another_key(
    agent_home: Path, hub: FakeHub, command: str
) -> None:
    hub.signer = OTHER_PRIVATE
    message = command_refused(command, agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (the signature does not verify with the pinned hub key)"


@no_pg
def test_signed_get_refuses_a_changed_body(agent_home: Path, hub: FakeHub, command: str) -> None:
    hub.signed_body = hub.body
    hub.body = hub.body + b" "
    message = command_refused(command, agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (the signature does not verify with the pinned hub key)"


@no_pg
@pytest.mark.parametrize("seconds", [121, -121])
def test_signed_get_refuses_a_stale_answer(
    agent_home: Path, hub: FakeHub, command: str, seconds: int
) -> None:
    hub.hub_time = shifted(seconds)
    message = command_refused(command, agent_home, hub)
    assert message == (
        f"{keys.NOT_VERIFIED} (Agent-Hub-Time is more than 120 seconds from this device's clock)"
    )


@no_pg
def test_signed_get_refuses_a_replayed_answer(agent_home: Path, hub: FakeHub, command: str) -> None:
    assert call_command(command, agent_home, hub, now=shifted(-10)) == COMMAND_ANSWERS[command]
    first_request = hub.requests[0]
    first_answer = hub(first_request)

    def replay(request: httpx.Request) -> httpx.Response:
        hub.requests.append(request)
        return httpx.Response(
            first_answer.status_code,
            headers=list(first_answer.headers.multi_items()),
            content=first_answer.content,
        )

    message = command_refused(command, agent_home, hub, handler=replay)
    assert message == f"{keys.NOT_VERIFIED} (the signature does not verify with the pinned hub key)"
    replayed_to = hub.requests[-1]
    assert replayed_to.headers["Agent-Nonce"] != first_request.headers["Agent-Nonce"]


@no_pg
def test_signed_get_does_not_follow_a_signed_redirect(agent_home: Path, hub: FakeHub, command: str) -> None:
    hub.status = 307
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/")]
    message = command_refused(command, agent_home, hub)
    assert message == f"hub GET {COMMAND_REQUESTS[command][0]} → HTTP 307: "
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


@no_pg
def test_signed_get_does_not_follow_an_unsigned_redirect(
    agent_home: Path, hub: FakeHub, command: str
) -> None:
    hub.signed = False
    hub.status = 302
    hub.body = b""
    hub.extra_headers = [("Location", "https://other.example/")]
    message = command_refused(command, agent_home, hub)
    assert message == f"{keys.NOT_VERIFIED} (unsigned answer; unverified: HTTP 302)"
    assert len(hub.requests) == 1
    assert hub.requests[0].url.host == HUB_HOST


@no_pg
def test_signed_get_reports_a_transport_error(agent_home: Path, hub: FakeHub, command: str) -> None:
    hub.error = httpx.ConnectError("connection refused")
    assert command_refused(command, agent_home, hub) == "hub request failed: connection refused"
    assert len(hub.requests) == 1


@no_pg
def test_signed_get_reports_a_connection_that_breaks_while_reading(
    agent_home: Path, hub: FakeHub, command: str
) -> None:
    hub.stream = BrokenStream()
    assert command_refused(command, agent_home, hub) == "hub request failed: connection reset by peer"


# The release command with the device's real store


def release_argv(repository: str = REPO, pull_request: str = "42") -> tuple[str, ...]:
    return ("release", "--id", CLIENT_ID, "--repo", repository, "--pr", pull_request)


def test_cli_release_prints_a_released_answer_and_exits_0(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair(agent_home, HUB_URL)
    closed = track_store_closes(monkeypatch)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, release_hub)
    run_cli(*release_argv())
    captured = capsys.readouterr()
    assert captured.out == RELEASED_LINE
    assert captured.err == ""
    assert len(closed) == 1
    assert len(release_hub.requests) == 1
    assert release_hub.requests[0].url.raw_path == RELEASE_TARGET.encode("ascii")


@pytest.mark.parametrize("failed", FAILURES)
def test_cli_release_exits_3_for_an_answer_that_does_not_release(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed: str,
) -> None:
    release_hub.body = release_body(released=False, failed=failed)
    pair(agent_home, HUB_URL)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, release_hub)
    with pytest.raises(SystemExit) as caught:
        run_cli(*release_argv())
    assert caught.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == (
        f'{{"failed": "{failed}", "hub_key": "hub-test-1", "pull_request": 42, '
        '"released": false, "repository": "example-org/example-repo"}\n'
    )
    assert captured.err == f"not released: {failed}\n"


def test_cli_release_refuses_a_verified_answer_of_another_form(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_hub.body = release_body(hub_key="hub-other-1")
    pair(agent_home, HUB_URL)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, release_hub)
    assert cli_refused(*release_argv()) == f"agent: {keys.UNEXPECTED_FORM}"
    assert capsys.readouterr().out == ""


def test_cli_release_prints_nothing_for_an_unverified_answer(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_hub.signer = OTHER_PRIVATE
    pair(agent_home, HUB_URL)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, release_hub)
    assert cli_refused(*release_argv()) == (
        f"agent: {keys.NOT_VERIFIED} (the signature does not verify with the pinned hub key)"
    )
    assert capsys.readouterr().out == ""


def test_cli_release_reports_a_signed_refusal(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_hub.status = 403
    release_hub.body = b'{"error":"subject_not_allowed"}'
    pair(agent_home, HUB_URL)
    monkeypatch.setattr(agent_main, "utcnow", lambda: NOW)
    mock_hub_client(monkeypatch, release_hub)
    assert cli_refused(*release_argv()) == (
        f"agent: hub GET {RELEASE_TARGET} → HTTP 403: subject_not_allowed"
    )
    assert capsys.readouterr().out == ""


def test_cli_release_without_a_pinned_hub_key_sends_nothing(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys.create_key(agent_home, CLIENT_ID)
    opened: list[dict[str, object]] = []
    real_open_store = agent_main.open_store

    def recording_open_store(**kwargs: object) -> Store:
        opened.append(kwargs)
        return real_open_store(**kwargs)

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(agent_main, "open_store", recording_open_store)
    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused(*release_argv()) == "agent: hub key is not pinned"
    assert opened == []


def test_cli_release_without_a_hub_url_sends_nothing(
    agent_home: Path, release_hub: FakeHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = track_store_closes(monkeypatch)

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused(*release_argv()) == "agent: device is not paired"
    assert len(closed) == 1
    assert release_hub.requests == []


@pytest.mark.parametrize(
    ("repository", "pull_request", "message"),
    [
        *((repository, "42", REPO_MESSAGE) for repository in BAD_REPOS if repository != ""),
        *((REPO, pull_request, PR_MESSAGE) for pull_request in BAD_PULL_REQUESTS if pull_request != ""),
    ],
)
def test_cli_release_checks_repo_and_pr_before_opening_the_store(
    agent_home: Path,
    release_hub: FakeHub,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    pull_request: str,
    message: str,
) -> None:
    (agent_home / "hub-key.json").unlink()
    opened: list[dict[str, object]] = []
    real_open_store = agent_main.open_store

    def recording_open_store(**kwargs: object) -> Store:
        opened.append(kwargs)
        return real_open_store(**kwargs)

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(agent_main, "open_store", recording_open_store)
    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert cli_refused(*release_argv(repository, pull_request)) == f"agent: {message}"
    assert opened == []
    assert release_hub.requests == []


@no_pg
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("release",), "agent: --id is required"),
        (("release", "--id", CLIENT_ID), "agent: --repo is required"),
        (("release", "--id", CLIENT_ID, "--pr", "42"), "agent: --repo is required"),
        (("release", "--id", CLIENT_ID, "--repo", REPO), "agent: --pr is required"),
        (("release", "--repo", REPO, "--pr", "42"), "agent: --id is required"),
        (("release", "--id", CLIENT_ID, "--repo", "", "--pr", "42"), "agent: --repo is required"),
        (("release", "--id", CLIENT_ID, "--repo", REPO, "--pr", ""), "agent: --pr is required"),
        (("release", "--id"), "agent: --id needs a value"),
        (("release", "--id", CLIENT_ID, "--repo"), "agent: --repo needs a value"),
        (("release", "--id", CLIENT_ID, "--repo", REPO, "--pr"), "agent: --pr needs a value"),
        ((*release_argv(), "--pr", "42"), f"agent: {agent_main.KEY_USAGE}"),
        ((*release_argv(), "--repo", REPO), f"agent: {agent_main.KEY_USAGE}"),
        ((*release_argv(), "--id", CLIENT_ID), f"agent: {agent_main.KEY_USAGE}"),
        ((*release_argv(), "--verbose"), f"agent: {agent_main.KEY_USAGE}"),
        ((*release_argv(), "--replace"), f"agent: {agent_main.KEY_USAGE}"),
        ((*release_argv(), "extra"), f"agent: {agent_main.KEY_USAGE}"),
        (("release", "--id", CLIENT_ID, f"--repo={REPO}", "--pr", "42"), f"agent: {agent_main.KEY_USAGE}"),
        (("release", "--id", CLIENT_ID, "--repository", REPO, "--pr", "42"), f"agent: {agent_main.KEY_USAGE}"),
    ],
)
def test_cli_release_refuses_malformed_arguments(
    agent_home: Path, argv: tuple[str, ...], expected: str
) -> None:
    assert cli_refused(*argv) == expected
    assert not agent_home.exists()


@no_pg
def test_key_usage_names_the_release_command() -> None:
    assert agent_main.KEY_USAGE == (
        "Usage: agent key create --id ID | list | "
        "pin-hub --id HUB_ID --public-key VALUE [--replace] | whoami --id ID | "
        "release --id ID --repo OWNER/NAME --pr NUMBER"
    )


# The host line and the configured hub URL


@no_pg
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


@no_pg
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


@no_pg
@pytest.mark.parametrize("hub_url", ["https://hub.example:port", "https://hub.example:84x3", "https://[::1"])
def test_hub_url_that_is_not_a_valid_url_is_refused(hub_url: str) -> None:
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.hub_origin(hub_url)
    assert str(caught.value).startswith("the configured hub URL is invalid: ")


@no_pg
def test_hub_url_without_a_host_is_refused() -> None:
    with pytest.raises(keys.KeyProtocolError) as caught:
        keys.hub_origin("https://")
    assert str(caught.value) == "the configured hub URL has no host"


@no_pg
def test_whoami_signs_the_normalised_host_of_the_configured_hub(agent_home: Path, hub: FakeHub) -> None:
    assert call(agent_home, hub, NOW, "https://HUB.Example:443/") == WHOAMI
    assert hub.requests[0].url.host == HUB_HOST


@no_pg
def test_whoami_refuses_a_hub_url_with_a_path_before_sending(agent_home: Path, hub: FakeHub) -> None:
    with pytest.raises(keys.KeyProtocolError):
        call(agent_home, hub, NOW, "https://hub.example/prefix")
    assert hub.requests == []


# Encodings


@no_pg
def test_request_and_response_texts_have_no_trailing_line_feed() -> None:
    request = keys.request_text(CLIENT_ID, "GET", HUB_HOST, TARGET, NOW, "n", EMPTY_SHA)
    response = keys.response_text(CLIENT_ID, "n", NOW, 200, NOW, EMPTY_SHA)
    assert request == f"agent-request-v1\n{CLIENT_ID}\nGET\n{HUB_HOST}\n{TARGET}\n{NOW}\nn\n{EMPTY_SHA}"
    assert response == f"agent-response-v1\n{CLIENT_ID}\nn\n{NOW}\n200\n{NOW}\n{EMPTY_SHA}"
    assert keys.sha256_hex(b"") == EMPTY_SHA


@no_pg
@pytest.mark.parametrize("bad_key", BAD_PUBLIC_KEYS)
def test_decode_public_key_refuses_bad_values(bad_key: str) -> None:
    with pytest.raises(ValueError):
        keys.decode_public_key(bad_key)


@no_pg
def test_decode_public_key_accepts_a_real_key() -> None:
    assert public_text_of(keys.decode_public_key(HUB_PUBLIC)) == HUB_PUBLIC


@no_pg
@pytest.mark.parametrize("raw", [b"", bytes(31), bytes(33), bytes(64)])
def test_ed25519_public_key_refuses_a_value_of_another_length(raw: bytes) -> None:
    with pytest.raises(ValueError) as caught:
        keys.ed25519_public_key(raw)
    assert str(caught.value) == "not 32 bytes"


@no_pg
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("AAAAA", "not base64url without padding"),
        ("AA==", "not base64url without padding"),
        ("AA+A", "not base64url without padding"),
        ("AB", "not canonical base64url"),
        ("AAAA", "not 2 bytes"),
    ],
)
def test_b64url_decode_refuses_other_forms(text: str, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        keys.b64url_decode(text, 2)
    assert str(caught.value) == message


@no_pg
@pytest.mark.parametrize("length", [0, 1, 2, 3, 16, 32, 64])
def test_b64url_decode_reads_every_unpadded_length(length: int) -> None:
    raw = bytes(range(length))
    assert keys.b64url_decode(b64(raw), length) == raw


# Command-line arguments


@no_pg
def test_cli_refuses_a_repeated_flag_with_the_usage_line(agent_home: Path) -> None:
    message = cli_refused("create", "--id", CLIENT_ID, "--id", CLIENT_ID)
    assert message == f"agent: {agent_main.KEY_USAGE}"
    assert not (agent_home / "keys").exists()


@no_pg
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ((), f"agent: {agent_main.KEY_USAGE}"),
        (("rotate",), "agent: unknown key command: rotate"),
        (("--id", CLIENT_ID), "agent: unknown key command: --id"),
        (("create", "--id"), "agent: --id needs a value"),
        (("whoami", "--id"), "agent: --id needs a value"),
        (("pin-hub", "--id", HUB_ID, "--public-key"), "agent: --public-key needs a value"),
        (("list", "--all"), f"agent: {agent_main.KEY_USAGE}"),
        (("list", CLIENT_ID), f"agent: {agent_main.KEY_USAGE}"),
        (("create", "--id", CLIENT_ID, "--replace"), f"agent: {agent_main.KEY_USAGE}"),
        (("whoami", "--id", CLIENT_ID, "--verbose"), f"agent: {agent_main.KEY_USAGE}"),
        (
            ("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC, "--replace", "--replace"),
            f"agent: {agent_main.KEY_USAGE}",
        ),
        (
            ("pin-hub", "--id", HUB_ID, "--public-key", HUB_PUBLIC, "--force"),
            f"agent: {agent_main.KEY_USAGE}",
        ),
        (("create",), "agent: --id is required"),
        (("whoami",), "agent: --id is required"),
        (("pin-hub", "--id", HUB_ID), "agent: --public-key is required"),
    ],
)
def test_cli_refuses_malformed_key_arguments(agent_home: Path, argv: tuple[str, ...], expected: str) -> None:
    assert cli_refused(*argv) == expected
    assert not agent_home.exists()
