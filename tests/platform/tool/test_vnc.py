"""`agent-platform vnc` and `subscriptions add|login --vnc` against the hub double (ADR-103): only the user's own
identity takes a screen over, the tunnel is listened for on the loopback only, the viewer is started by the tool (Screen
Sharing on macOS, `vncviewer`, else port and password on stderr), the device's records are opened with the key of the
`vnc` step, `needs_human` opens the connection by itself and the login is followed again afterwards; the responses of
`vnc.open` and the new login step are checked like every response."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_platform import devicereq, subscriptions, vnc
from agent_platform.errors import PlatformError
from agent_platform.hub import AgentClient
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.vnc.tunnel import DEVICE, Tunnel, new_key

from .conftest import Tool
from .fakes.relay import opened, relay, respond
from .fakes.subscriptions import state_of
from .hubfake import HubFake, User
from .seeding import assign_device

PASSWORD = "Ab3dEf7h"
KEY = new_key()
OFFER = {"step": "vnc", "key": b64u_encode(KEY), "password": PASSWORD, "idle_seconds": 120}
NEEDS_HUMAN = {"step": "needs_human", "vnc_available": True}
VIEWS: list[asyncio.Future[None]] = []
"""The viewers started by the tests (kept, so none is collected while it runs)."""


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch, hub: HubFake) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)
    state_of(hub).announce = True


def screen_device(tool: Tool, hub: HubFake, account: User) -> str:
    _org, _wg, device = assign_device(tool, hub, account)
    state_of(hub).parallel.add(device)
    state_of(hub).screens.add(device)
    return device


class Device:
    """The device's end of the tunnel in the test: says `paired`, sends the server's version and closes; records
    what the client sent; then answers the `vnc.open` (and, when given, the login) with their final responses."""

    def __init__(self, hub: HubFake, *, login_done: str | None = None, paired: str = vnc.PAIRED) -> None:
        self.hub = hub
        self.login_done = login_done
        self.paired = paired
        self.received: list[bytes] = []
        self.urls: list[tuple[str, dict[str, str]]] = []

    def connect(self, url: str, headers: Mapping[str, str]) -> Any:
        device = self
        request = url.rsplit("/", 1)[1]
        tunnel = Tunnel(KEY, request, DEVICE)
        self.urls.append((url, dict(headers)))

        class Socket:
            def __init__(self) -> None:
                self.outgoing = [device.paired, tunnel.seal(b"RFB 003.008\n"), tunnel.seal(b"")]

            async def recv(self) -> Any:
                if not self.outgoing:
                    await asyncio.sleep(3600)
                return self.outgoing.pop(0)

            async def send(self, record: bytes) -> None:
                device.received.append(tunnel.open(record))

        @contextlib.asynccontextmanager
        async def opened() -> AsyncIterator[Any]:
            yield Socket()
            respond(device.hub, request, {"step": "done", "reason": "ended"}, final=True)
            if device.login_done is not None:
                respond(device.hub, device.login_done, {"step": "done"}, final=True)

        return opened()


def viewer(seen: list[bytes], sends: bytes = b"RFB 003.008\n") -> vnc.Opener:
    """The person's viewer: connects to the tool's port, answers the version and reads until the end."""

    def opener(port: int, password: str) -> str:
        async def view() -> None:
            reader, writer = await asyncio.open_connection(vnc.HOST, port)
            writer.write(sends)
            try:
                seen.append(await reader.read(12))
            except ConnectionResetError:
                seen.append(b"reset")  # the tool ended the connection before anything came
            writer.close()

        VIEWS.append(asyncio.ensure_future(view()))
        return "viewer-double"

    return opener


def test_vnc_takes_the_login_screen_over_through_the_tunnel(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    screen_device(tool, hub, account)
    relay(hub).script["claude.login"] = [[(NEEDS_HUMAN, False)]]
    code, login = tool.run("subscriptions", "add", "--name", "Ada", "--automatic", "--wait", "1")
    assert code == 0, login
    assert login["step"] == "needs_human"
    assert login["next"] == f"a person takes the screen over with `agent-platform vnc --login {login['request_id']}`"
    relay(hub).script["vnc.open"] = [[(OFFER, False)]]
    device = Device(hub)
    seen: list[bytes] = []
    monkeypatch.setattr(vnc, "websocket_connect", device.connect)
    monkeypatch.setattr(vnc, "open_viewer", viewer(seen))
    code, taken = tool.run("vnc", "--login", login["request_id"], "--wait", "5")
    assert code == 0, taken
    assert (taken["step"], taken["reason"], taken["kind"]) == ("done", "ended", "login")
    assert taken["id"] == login["request_id"]
    assert taken["local"] == "peer_closed"
    assert seen == [b"RFB 003.008\n"]
    assert device.received == [b"RFB 003.008\n"]
    request_id = taken["request_id"]
    stored = relay(hub).requests[request_id]
    assert stored["slot"] == login["request_id"]
    url, headers = device.urls[0]
    assert url.endswith(f"/v1/vnc/{request_id}")
    assert url.startswith("ws://")
    assert headers["Agent-Key"].startswith("agent.")
    assert PASSWORD not in repr(taken)
    assert f"one-time password {PASSWORD}" in capsys.readouterr().err  # a viewer that does not take it: the person


def test_subscriptions_add_with_vnc_opens_the_screen_by_itself_and_follows_the_login(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen_device(tool, hub, account)
    verify = {"step": "verify", "frame": 1, "image": {"width": 960, "height": 600, "data": "AAAA"}}
    relay(hub).script["claude.login"] = [[(NEEDS_HUMAN, False), (verify, False)]]
    relay(hub).script["vnc.open"] = [[(OFFER, False)]]
    seen: list[bytes] = []
    devices: list[Device] = []

    def connect(url: str, headers: Mapping[str, str]) -> Any:
        login = next(r for r, s in relay(hub).requests.items() if s["kind"] == "claude.login")
        devices.append(Device(hub, login_done=login))
        return devices[-1].connect(url, headers)

    monkeypatch.setattr(vnc, "websocket_connect", connect)
    monkeypatch.setattr(vnc, "open_viewer", viewer(seen))
    code, added = tool.run("subscriptions", "add", "--name", "Ada", "--automatic", "--vnc", "--wait", "5")
    assert code == 0, added
    assert added["step"] == "done"
    assert added["vnc"]["reason"] == "ended"
    assert seen == [b"RFB 003.008\n"]
    body = opened(hub, added["request_id"])
    assert isinstance(body, dict)
    assert body["vnc"] is True


def test_without_a_device_that_announced_vnc_there_is_none(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    state_of(hub).parallel.add(device)
    code, refused = tool.run("subscriptions", "add", "--name", "Ada", "--automatic", "--vnc", "--wait", "1")
    assert (code, refused["error"], refused["reason"]) == (1, "no_device", "device_outdated")


def test_a_screen_the_device_does_not_have_is_its_failure(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen_device(tool, hub, account)
    relay(hub).script["claude.login"] = [[(NEEDS_HUMAN, False)]]
    _code, login = tool.run("subscriptions", "add", "--name", "Ada", "--automatic", "--wait", "1")
    relay(hub).script["vnc.open"] = [[({"step": "failed", "code": "no_screen"}, True)]]
    code, failed = tool.run("vnc", "--login", login["request_id"], "--wait", "5")
    assert (code, failed["error"], failed["failure"]) == (1, "device_request_failed", "no_screen")


def test_only_the_users_own_identity_and_one_target(tool: Tool, hub: HubFake, account: User) -> None:
    agent = SimpleNamespace(identity=None)
    with pytest.raises(PlatformError) as refused:
        vnc.command(cast(Any, agent), SimpleNamespace(), cast(Any, None))
    assert refused.value.fields["reason"] == "human_only"
    with pytest.raises(PlatformError) as refused:
        subscriptions._run_login(cast(Any, agent), SimpleNamespace(vnc=True), cast(Any, None), uuid7(), {})
    assert refused.value.fields["reason"] == "human_only"
    for argv in (("vnc",), ("vnc", "--login", uuid7(), "--task", uuid7())):
        code, wrong = tool.run(*argv)
        assert (code, wrong["error"], wrong["reason"]) == (1, "invalid_argument", "vnc_target")
    code, wrong = tool.run("vnc", "--login", uuid7(), "--wait", "0")
    assert (code, wrong["error"]) == (1, "invalid_argument")


def test_the_session_and_task_targets_name_the_sessions_device(monkeypatch: pytest.MonkeyPatch) -> None:
    session, device = uuid7(), uuid7()
    rows: dict[str, Any] = {session: {"session": {"device_id": device}}}
    monkeypatch.setattr(vnc, "session_row", lambda account, found: rows[found])
    listed = {"browsers": [{"session_id": uuid7(), "state": "closed"}, {"session_id": session, "state": "open"}]}
    monkeypatch.setattr("agent_platform.browser.browsers", lambda account, s, task: listed)
    args = SimpleNamespace(login=None, session=session, task=None)
    assert vnc.target_of(cast(Any, None), args) == ("session", session, device)
    args = SimpleNamespace(login=None, session=None, task=uuid7())
    assert vnc.target_of(cast(Any, None), args) == ("session", session, device)
    rows[session] = {"session": {"device_id": None}}
    with pytest.raises(PlatformError) as stopped:
        vnc.target_of(cast(Any, None), args)
    assert stopped.value.fields["reason"] == "session_not_running"
    listed["browsers"] = []
    with pytest.raises(PlatformError) as none:
        vnc.target_of(cast(Any, None), args)
    assert none.value.fields["failure"] == "no_screen"


def test_a_device_the_user_cannot_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnc, "target_of", lambda account, args: ("session", uuid7(), uuid7()))
    monkeypatch.setattr(vnc, "device_keys", lambda account, device: None)
    account = SimpleNamespace(identity=object())
    with pytest.raises(PlatformError) as refused:
        vnc.command(cast(Any, account), SimpleNamespace(wait=5), cast(Any, None))
    assert (refused.value.code, refused.value.fields["reason"]) == ("no_device", "device_not_assigned")


def test_the_viewer_is_started_by_the_tool(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv: started.append(list(argv)))
    assert vnc.open_viewer(5901, PASSWORD, platform="darwin") == "screen_sharing"
    assert started == [["open", f"vnc://:{PASSWORD}@127.0.0.1:5901"]]
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/vncviewer")
    assert vnc.open_viewer(5901, PASSWORD, platform="linux") == "vncviewer"
    assert started[-1] == ["/usr/bin/vncviewer", "127.0.0.1::5901"]
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert vnc.open_viewer(5901, PASSWORD, platform="linux") is None
    vnc._tell(5901, PASSWORD, "screen_sharing")
    err = capsys.readouterr().err
    assert "Screen Sharing opens the screen on 127.0.0.1:5901" in err
    assert PASSWORD not in err


def _bridge(connect: Any, opener: vnc.Opener, **options: Any) -> str:
    request = uuid7()
    url = "ws://hub.test/v1/vnc/" + request

    async def main() -> str:
        VIEWS.clear()
        result = await vnc.bridge(url, {}, KEY, request, PASSWORD, opener=opener, connect=connect, **options)
        await asyncio.gather(*VIEWS)  # the viewers saw the end too
        return result

    return asyncio.run(main())


def test_the_bridge_listens_on_the_loopback_for_one_viewer_and_ends_without_a_partner() -> None:
    ports: list[int] = []

    def nobody(port: int, password: str) -> None:
        ports.append(port)

    assert _bridge(None, nobody, accept_seconds=0.05) == "not_opened"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", ports[0]), timeout=1)  # the port is closed again

    @contextlib.asynccontextmanager
    async def silent(url: str, headers: Mapping[str, str]) -> AsyncIterator[Any]:
        class Socket:
            async def recv(self) -> str:
                await asyncio.sleep(3600)
                return ""

        yield Socket()

    @contextlib.asynccontextmanager
    async def other(url: str, headers: Mapping[str, str]) -> AsyncIterator[Any]:
        class Socket:
            async def recv(self) -> str:
                return '{"type":"other"}'

        yield Socket()

    refused: list[bytes] = []

    def two_viewers(port: int, password: str) -> str:
        async def view() -> None:
            # two viewers at the same moment: one is joined, the other closed at once; then the port closes
            both = await asyncio.gather(*(asyncio.open_connection(vnc.HOST, port) for _ in range(2)))
            reads = await asyncio.gather(*(reader.read(1) for reader, _writer in both))
            refused.extend(reads)
            with pytest.raises(OSError, match="Connect call failed"):
                await asyncio.open_connection(vnc.HOST, port)
            for _reader, writer in both:
                writer.close()

        VIEWS.append(asyncio.ensure_future(view()))
        return "viewer-double"

    assert _bridge(silent, two_viewers, pair_seconds=0.3) == "unpaired"
    assert refused == [b"", b""]  # the joined one ends with the bridge, the other was closed at once
    assert _bridge(other, viewer([])) == "unpaired"


def test_the_client_upgrade_is_signed_and_needs_a_direct_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AgentClient.__new__(AgentClient)

    def sign(method: str, target: str, body: bytes) -> Any:
        return SimpleNamespace(headers={"Agent-Key": f"{method} {target}"})

    signer = SimpleNamespace(sign=sign)
    client._client = cast(Any, SimpleNamespace(signer=signer, origin=SimpleNamespace(ws_base_url="wss://hub.example")))
    monkeypatch.setattr("agent_platform.hub.relay.relay_path", lambda: None)
    assert client.upgrade("/v1/vnc/x") == ("wss://hub.example/v1/vnc/x", {"Agent-Key": "GET /v1/vnc/x"})
    monkeypatch.setattr("agent_platform.hub.relay.relay_path", lambda: "/run/relay.sock")
    with pytest.raises(PlatformError) as refused:
        client.upgrade("/v1/vnc/x")
    assert refused.value.code == "not_in_session"


@pytest.mark.parametrize(
    ("body", "valid"),
    [
        (OFFER, True),
        ({"step": "done", "reason": "idle"}, True),
        ({**OFFER, "port": 5900}, False),
        ({**OFFER, "password": "short"}, False),
        ({**OFFER, "key": "x"}, False),
        ({**OFFER, "idle_seconds": 0}, False),
        ({**OFFER, "idle_seconds": True}, False),
        ({**OFFER, "idle_seconds": "120"}, False),
        ({"step": "done", "reason": "solved"}, False),
        ({"step": "failed", "code": "no_screen"}, True),
    ],
)
def test_the_responses_of_vnc_open_are_checked(body: dict[str, Any], valid: bool) -> None:
    assert (devicereq.response_body("vnc.open", body) is not None) == valid


@pytest.mark.parametrize(
    ("body", "valid"),
    [(NEEDS_HUMAN, True), ({"step": "needs_human", "vnc_available": 1}, False), ({"step": "needs_human"}, False)],
)
def test_needs_human_is_a_login_step(body: dict[str, Any], valid: bool) -> None:
    assert (devicereq.response_body("claude.login", body) is not None) == valid
