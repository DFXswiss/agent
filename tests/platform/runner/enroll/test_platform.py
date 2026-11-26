"""U-RUN-identity / enroll: platform enrollment (`agent-runner enroll --platform`), success and every failure path
without a half identity left behind."""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from agent_runner.cli import run_cli
from agent_runner.config import Config, load_config
from agent_runner.crypto.enroll import verify_enrollment
from agent_runner.crypto.invite import format_code, invite_secrets
from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.enroll import cli, platform
from agent_runner.errors import RunnerError
from agent_runner.identity import key_path as identity_key_path
from agent_runner.identity import load_identity, write_keys
from agent_runner.vm.capacity import host_reserve_mib

from ..reconcile.rig import ROOT, device_keys, pid
from ..sync.fakehub import FakeHub, Received, utc_ms

CODE = bytes(range(16))
CODE_TEXT = format_code(CODE)
SECRETS = invite_secrets(CODE)
WALL = 1_795_000_000.0
DEVICE_ID = pid("enrolled")
HOST = "127.0.0.1:9"
URL = f"http://{HOST}"


class EnrollHub(FakeHub):
    """`GET /v1/config` (anonymous, unsigned) and `POST /v1/device/enroll` (signed with the key in the body)."""

    config_status: int = 200
    config_body: object
    enroll_answer: tuple[int, dict[str, Any]] = (200, {"state": "pending_certification"})
    enrolled: list[dict[str, Any]]

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["raw_path"] == b"/v1/config":
            self.requests.append(Received("GET", "/v1/config", None))
            raw = self.config_body if isinstance(self.config_body, bytes) else json.dumps(self.config_body).encode()
            await send({"type": "http.response.start", "status": self.config_status, "headers": []})
            await send({"type": "http.response.body", "body": raw})
            return
        await super().__call__(scope, receive, send)

    def verify(self, method: str, target: str, headers: dict[str, str], body: bytes) -> str | None:
        data = json.loads(body) if body else {}
        self.device_id = data.get("device_id", "")
        self.device_public = b64u_decode(data.get("pub", {}).get("sig", b64u_encode(bytes(32))))
        return super().verify(method, target, headers, body)

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        if (method, target) == ("POST", "/v1/device/enroll"):
            self.check("api/device-enroll.request.json", body)
            verify_enrollment(body, b64u_decode(SECRETS.pk_inv))
            self.enrolled.append(body)
            status, answer = self.enroll_answer
            if status == 200:
                self.check("api/device-enroll.response.json", answer)
            return status, answer
        return super().route(method, target, body)


def make_hub(**config: object) -> EnrollHub:
    hub = EnrollHub(device_id="", device_public=bytes(32), host=HOST)
    hub.wall = lambda: WALL
    hub.enrolled = []
    body = {
        "rp_id": "localhost",
        "origin": "http://localhost",
        "platform_root_key": ROOT,
        "hub_key": {"id": "hub-test", "public_key": b64u_encode(hub.public)},
        "version": "0.1.0",
        "limits": {},
        "models": {"platform": [], "api_key": [], "subscription": []},
        "defaults": {"platform": "deepseek/deepseek-v4.1-flash-1017"},
    }
    body.update(cast(dict[str, Any], config))
    hub.config_body = body
    return hub


def options(hub: EnrollHub, **extra: Any) -> platform.EnrollOptions:
    keys = device_keys("enrolled")
    values: dict[str, Any] = {
        "transport": httpx.ASGITransport(app=hub),  # type: ignore[arg-type]
        "wall": lambda: WALL,
        "generate": lambda: keys,
        "new_id": lambda: DEVICE_ID,
        "arch": "arm64",
        "physical_mib": lambda: 65536,
    }
    values.update(extra)
    return platform.EnrollOptions(**values)


@pytest.fixture
def config(runner_home: Path, env_for: Callable[..., dict[str, str]]) -> Config:
    env = env_for(runner_home, AGENT_RUNNER_HUB_URL=URL, AGENT_RUNNER_PLATFORM_ROOT_KEY=ROOT)
    return load_config(env, platform="darwin")


def enroll(config: Config, hub: EnrollHub, code: str = CODE_TEXT, **extra: Any) -> Any:
    return asyncio.run(platform.enroll_platform(config, code, options(hub, **extra)))


def nothing_written(home: Path) -> bool:
    return load_identity(home) is None and not identity_key_path(home).exists()


def test_successful_enrollment_writes_the_identity_last(config: Config) -> None:
    hub = make_hub()
    device = enroll(config, hub)
    assert (device.device_id, device.kind, device.hub_url) == (DEVICE_ID, "platform", URL)
    assert device.hub_key == b64u_encode(hub.public)
    assert device.enrolled_at == utc_ms(WALL)
    identity = load_identity(config.home)
    assert identity is not None
    assert identity.device == device
    assert identity.keys == device_keys("enrolled")
    [body] = hub.enrolled
    assert body["invite_id"] == SECRETS.invite_id
    assert body["kind"] == "platform"
    assert body["pub"] == {"kem": identity.keys.kem_public, "sig": identity.keys.sig_public}
    assert (body["os"], body["arch"], body["agent_version"]) == ("darwin", "arm64", "0.1.0")
    assert body["capacity"]["vm_memory_budget_mib"] == 65536 - host_reserve_mib(config.memory_reserve_mib)
    assert [r.target for r in hub.requests] == ["/v1/config", "/v1/device/enroll"]
    assert hub.violations == []
    with pytest.raises(RunnerError, match="already_enrolled"):
        enroll(config, hub)


def test_a_half_identity_is_refused(config: Config) -> None:
    write_keys(config.home, device_keys("enrolled"))
    with pytest.raises(RunnerError, match="identity_incomplete"):
        enroll(config, make_hub())


@pytest.mark.parametrize(("code", "error"), [("ABCDE", "invite_code_length"), ("!!!!!-" * 5, "invite_code_invalid")])
def test_bad_codes_are_refused_before_any_request(config: Config, code: str, error: str) -> None:
    hub = make_hub()
    with pytest.raises(RunnerError, match=error):
        enroll(config, hub, code)
    assert hub.requests == []
    assert nothing_written(config.home)


def test_a_different_platform_root_is_fatal(config: Config) -> None:
    hub = make_hub(platform_root_key=b64u_encode(bytes(32)))
    with pytest.raises(RunnerError, match="platform_root_mismatch"):
        enroll(config, hub)
    assert nothing_written(config.home)
    assert [r.target for r in hub.requests] == ["/v1/config"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"config_status": 503}, "response_invalid"),
        ({"config_body": b"not json"}, "response_invalid"),
        ({"config_body": []}, "response_invalid"),
        ({"config_body": {"hub_key": {"id": "Hub Key", "public_key": ROOT}}}, "response_invalid"),
        (
            {"config_body": {"hub_key": {"id": "hub", "public_key": "short"}, "platform_root_key": ROOT}},
            "response_invalid",
        ),
        ({"config_body": {"hub_key": {"id": "hub", "public_key": 5}, "platform_root_key": ROOT}}, "response_invalid"),
        (
            {"config_body": {"hub_key": {"id": "hub", "public_key": "a="}, "platform_root_key": ROOT}},
            "response_invalid",
        ),
        ({"config_body": b" " * (platform.MAX_CONFIG_BYTES + 1)}, "response_invalid"),
    ],
)
def test_bad_config_answers_are_refused(config: Config, change: dict[str, object], error: str) -> None:
    hub = make_hub()
    for name, value in change.items():
        setattr(hub, name, value)
    with pytest.raises(RunnerError, match=error):
        enroll(config, hub)
    assert nothing_written(config.home)


def test_an_unreachable_hub_is_refused(config: Config) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(RunnerError, match="hub_unreachable"):
        enroll(config, make_hub(), transport=httpx.MockTransport(refuse))
    assert nothing_written(config.home)


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda hub: setattr(hub, "enroll_answer", (404, {"error": "invite_unknown"})), "invite_unknown"),
        (lambda hub: hub.tamper.append("unsigned"), "response_unsigned"),
        (lambda hub: hub.tamper.append("other_key"), "response_signature_invalid"),
        (
            lambda hub: setattr(hub, "enroll_answer", (200, {"state": "active", "org_id": pid("org")})),
            "response_invalid",
        ),
    ],
)
def test_enrollment_failures_leave_no_identity(config: Config, setup: Callable[[EnrollHub], None], error: str) -> None:
    hub = make_hub()
    setup(hub)
    with pytest.raises(RunnerError, match=error):
        enroll(config, hub)
    assert nothing_written(config.home)


def test_a_failed_device_write_removes_the_keys(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(platform, "write_device", broken)
    with pytest.raises(OSError, match="disk full"):
        enroll(config, make_hub())
    assert nothing_written(config.home)


def test_configuration_and_host_facts(
    config: Config, runner_home: Path, env_for: Callable[..., dict[str, str]]
) -> None:
    hub = make_hub()
    no_root = load_config(
        env_for(runner_home, AGENT_RUNNER_HUB_URL=URL, AGENT_RUNNER_PLATFORM_ROOT_KEY=""),
        platform="darwin",
        require=frozenset(),
    )
    with pytest.raises(RunnerError, match="config_invalid PLATFORM_ROOT_KEY"):
        enroll(no_root, hub)
    no_hub = load_config(env_for(runner_home, AGENT_RUNNER_HUB_URL=""), platform="darwin", require=frozenset())
    with pytest.raises(RunnerError, match="config_invalid HUB_URL"):
        enroll(no_hub, hub)
    with_path = load_config(env_for(runner_home, AGENT_RUNNER_HUB_URL=URL + "/base"), platform="darwin")
    with pytest.raises(RunnerError, match="config_invalid HUB_URL"):
        enroll(with_path, hub)
    body = platform.enrollment_body(
        device_id=DEVICE_ID,
        invite_id=SECRETS.invite_id,
        keys=device_keys("enrolled"),
        config=config,
        options=options(hub, arch="x86-64"),
    )
    assert body["arch"] == "unknown"
    defaults = platform.default_options()
    assert defaults.transport is None
    assert defaults.os_name == "darwin"


def test_the_enroll_command(
    config: Config,
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = make_hub()
    monkeypatch.setattr(platform, "default_options", lambda: options(hub))
    env = env_for(runner_home, AGENT_RUNNER_PLATFORM_ROOT_KEY=ROOT)
    captured = make_host(env)
    code = run_cli(["enroll", "--platform", "--hub", URL, "--code", CODE_TEXT], captured.host)
    assert code == 0
    assert (
        captured.stdout.getvalue() == f"runner.enroll state=pending_certification kind=platform device_id={DEVICE_ID}\n"
    )
    assert CODE_TEXT not in captured.stdout.getvalue() + captured.stderr.getvalue()
    again = make_host(env)
    assert run_cli(["--json", "enroll", "--platform", "--hub", URL, "--code", CODE_TEXT], again.host) == 1
    assert json.loads(again.stdout.getvalue())["error"] == {"code": "already_enrolled", "name": None}
    bad = make_host(env)
    assert run_cli(["enroll", "--platform", "--hub", "http://hub.example", "--code", CODE_TEXT], bad.host) == 1
    assert bad.stderr.getvalue() == "runner.error code=config_invalid name=HUB_URL\n"


def test_org_enrollment_is_delegated(
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = env_for(runner_home)
    calls: list[tuple[Config, str]] = []

    def org_enroll(config: Config, code: str, out: object) -> int:
        calls.append((config, code))
        return 0

    fake = types.ModuleType(cli.ORG_MODULE)
    fake.__spec__ = importlib.machinery.ModuleSpec(cli.ORG_MODULE, None)
    fake.enroll = org_enroll  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, cli.ORG_MODULE, fake)
    assert run_cli(["enroll", "--hub", URL, "--code", CODE_TEXT], make_host(env).host) == 0
    assert calls[0][0].hub_url == URL
    assert calls[0][1] == CODE_TEXT


def test_a_transport_failure_during_the_enrollment_removes_the_keys(config: Config) -> None:
    hub = make_hub()
    asgi = httpx.ASGITransport(app=hub)  # type: ignore[arg-type]

    class Failing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == platform.ENROLL_TARGET:
                raise httpx.ReadTimeout("slow", request=request)
            return await asgi.handle_async_request(request)

    with pytest.raises(RunnerError, match="hub_unreachable"):
        enroll(config, hub, transport=Failing())
    assert nothing_written(config.home)
