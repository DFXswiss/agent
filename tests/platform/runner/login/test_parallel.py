"""U-RUN-login (KOR-73, ADR-098 sections 1-3, 8): five subscriptions logged in at once in one run, each with its own
record, its own mailbox and its own browser sandbox - the real platform browser helper of the login VM
(`workvm-browser call`, one state directory and socket per login, as one VM per login), driven through
`HelperSandbox`; then a new login of one of them reads its own mailbox again.

The helper runs with Node and the Playwright of the web client (`web/node_modules`) as in the helper suite of the
work image; without them the test is skipped, unless `WORKVM_BROWSER_TEST_REQUIRED=1` says the machine must run it.
The auth-page double serves plain HTTP on loopback here (the sandbox's Chromium trusts no private CA): the legacy
link poll takes only `https://` links, so these logins sign in with the code of the mail."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.login_cli.authpage import AuthPageDouble, Scenario
from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA
from agent_runner.crypto.ids import uuid7
from agent_runner.login.sandbox import HelperSandbox
from agent_runner.workvm.jobs import ShellResult

from ..credentials.kit import ALICE, seal_request
from ..credentials.subscription_kit import SubscriptionHub, subscription_body
from .conftest import FAST, MAIL_FROM, PASSWORD, Run, run_request

REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = REPO_ROOT / "images" / "workvm" / "files" / "usr" / "local" / "bin" / "workvm-browser"
NODE_MODULES = REPO_ROOT / "web" / "node_modules"
REQUIRED_ENV = "WORKVM_BROWSER_TEST_REQUIRED"
COUNT = 5


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(f"{REQUIRED_ENV}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def node_env() -> dict[str, str]:
    if shutil.which("node") is None:
        _unavailable("Node is not installed")
    if not (NODE_MODULES / "playwright" / "package.json").is_file():
        _unavailable("web/node_modules has no Playwright (npm ci in web/)")
    return dict(os.environ, NODE_PATH=str(NODE_MODULES))


class Vms:
    """One helper instance per login (the login VMs), counting how many are open at once."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.base = Path(tempfile.mkdtemp(prefix="wl-", dir="/tmp"))  # macOS: socket paths of at most 104 bytes
        self.open = 0
        self.most = 0
        self.started: list[str] = []

    async def __call__(self, flow: Any, url: str, workdir: Path) -> HelperSandbox:
        state = self.base / f"vm{len(self.started)}"
        self.started.append(flow.id)
        env = dict(
            self.env,
            WORKVM_BROWSER_DIR=str(state),
            WORKVM_BROWSER_SOCKET=str(state) + ".sock",
            WORKVM_BROWSER_START="direct",
        )

        async def run(payload: bytes) -> ShellResult:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(HELPER),
                "call",
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            out, _err = await process.communicate(payload)
            return ShellResult(process.returncode or 0, out, False)

        async def release() -> None:
            pid_file = state / "serve.pid"
            if pid_file.is_file():
                with contextlib.suppress(ProcessLookupError, ValueError):
                    os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
            self.open -= 1

        self.open += 1
        self.most = max(self.most, self.open)
        return HelperSandbox(run, release)

    def remove(self) -> None:
        for pid_file in self.base.glob("vm*/serve.pid"):
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
        shutil.rmtree(self.base, ignore_errors=True)


@pytest.fixture
def vms(node_env: dict[str, str]) -> Iterator[Vms]:
    found = Vms(node_env)
    try:
        yield found
    finally:
        found.remove()


@dataclasses.dataclass
class World:
    mail: MailDouble
    auth: AuthPageDouble
    addresses: list[str]


@pytest.fixture
def world(login_ca: DoubleCA, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setenv("SSL_CERT_FILE", str(login_ca.ca_file))
    mail = MailDouble(ca=login_ca).start()
    addresses = [f"person{n}@example.test" for n in range(COUNT)]
    for address in addresses:
        mail.add_account(address, PASSWORD)
    assert mail.smtp is not None
    auth = AuthPageDouble(smtp=(mail.smtp.host, mail.smtp.port), mail_from=MAIL_FROM, scenario=Scenario()).start()
    try:
        yield World(mail, auth, addresses)
    finally:
        auth.stop()
        mail.stop()


def full_argv(world: World) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "agent_doubles.login_cli",
        "auth",
        "login",
        "--claudeai",
        "--auth-url",
        world.auth.page_url,
    )


def own_mailbox(world: World, address: str) -> dict[str, Any]:
    assert world.mail.imaps is not None
    port = world.mail.imaps.port
    return {
        "imap_host": "127.0.0.1",
        "imap_port": port,
        "imap_user": address,
        "imap_password": PASSWORD,
        "email": address,
    }


def test_five_subscriptions_log_in_at_once_each_with_its_own_mailbox_and_sandbox(
    store_dsn: str, tmp_path: Path, world: World, vms: Vms
) -> None:
    records = [uuid7() for _ in range(COUNT)]

    async def login(run: Run, record_id: str) -> str:
        body = {"mode": "automatic", "extended": True, "subscription_id": record_id}
        request_id: str = run.env.hub.add_request(seal_request("claude.login", body))
        await run.service.process(request_id)
        return request_id

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        for record_id, address in zip(records, world.addresses, strict=True):
            body = subscription_body(None, kind="oauth_login", email=address, mailbox=own_mailbox(world, address))
            hub.add(body, owner=ALICE, record_id=record_id)
        first = await login(run, records[0])
        requests = [first, *[await login(run, record_id) for record_id in records[1:]]]
        assert len([flow for flow in run.service.flows.values() if not flow.finished]) == COUNT
        await run.settle()
        for request_id, address in zip(requests, world.addresses, strict=True):
            assert run.bodies(request_id)[-1] == {"step": "done", "email": address}, run.bodies(request_id)
        assert vms.most >= 2, "the logins ran one after the other"
        assert sorted(vms.started) == sorted(requests)
        assert vms.open == 0
        for record_id, address in zip(records, world.addresses, strict=True):
            stored = hub.opened(record_id)
            assert (stored["email"], stored["kind"], stored["mailbox"]["email"]) == (address, "oauth_login", address)
            assert stored["token"]
        counts = world.mail.store.counts()
        assert all(counts[address]["INBOX"] == 1 for address in world.addresses)
        # a new login of one subscription reads its own mailbox again (ADR-098 section 1)
        again = await login(run, records[3])
        await run.settle()
        assert run.bodies(again)[-1] == {"step": "done", "email": world.addresses[3]}
        assert world.mail.store.counts()[world.addresses[3]]["INBOX"] == 2
        assert world.mail.store.counts()[world.addresses[0]]["INBOX"] == 1
        assert json.dumps(run.env.log()).count("runner.login done") == COUNT + 1

    opts = dataclasses.replace(FAST, mail_wait=60.0, page_timeout=20.0)
    # the harness's own first request is a harmless `mailbox.check` (no single mailbox: `credential_missing`); the five
    # logins start in `check`, after the records exist
    run_request(
        store_dsn,
        tmp_path,
        "mailbox.check",
        {},
        check,
        automation=True,
        opts=dataclasses.replace(opts, sandbox=vms),
        config={"subscription_login_argv": full_argv(world)},
    )
