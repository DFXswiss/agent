"""The browser sandbox of one Claude login (KOR-73, ADR-099 section 3; 05-runner.md section 11.3).

Every automatic login drives its own browser, nothing is shared between logins:

- `HelperSandbox` (`LOGIN_SANDBOX=vm`, the default): a login VM of the work VMs (`WorkVms.obtain_login`, scope
  `login`) driven through the platform browser helper of ADR-085 (`sudo -n workvm-browser call`, one JSON request on
  stdin, one untrusted answer checked by `browser_helper.parse_answer`). `close` deletes the VM (`login_ended`,
  shielded from cancellation by `WorkVms.delete`).
- `ProcessSandbox` (`LOGIN_SANDBOX=process`): a headless Chromium of the device (Playwright for Python, extra `login`)
  with its own profile directory below the login's working directory, removed with it. When `SSL_CERT_FILE` names a
  private CA (the hermetic doubles), Chromium accepts exactly the key of the authorize origin verified against it.

Both answer the same calls: `goto`, `evaluate` (the JSON text of a script's value), `type` and `click` at a CSS
selector, `click_at` a viewport point, and `picture` (the viewport as JPEG, at most `IMAGE_BYTES`, or None). Every
failure is `SandboxError` with a short reason (never a page text or secret); the automation treats it as
`browser_failed`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_runner.workvm.browser_helper import ANSWER_LIMIT, Answer, call_argv, parse_answer
from agent_runner.workvm.jobs import ShellResult, WorkVmError

IMAGE_BYTES = 100 * 1024
QUALITIES = (60, 45, 30)
CALL_TIMEOUT = 27.0
TLS_TIMEOUT = 10.0
BROWSERS_DIR = "ms-playwright"
"""Chromium of the platform install profile, below the runner home (`profile.py`)."""


class SandboxError(Exception):
    """The sandbox could not run a call (`reason`, a short word for the log)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Picture:
    width: int
    height: int
    jpeg: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.jpeg).hexdigest()


class Sandbox(Protocol):
    async def goto(self, url: str) -> None: ...

    async def evaluate(self, script: str) -> str: ...

    async def type(self, target: str, text: str) -> None: ...

    async def click(self, target: str) -> None: ...

    async def click_at(self, x: float, y: float) -> None: ...

    async def picture(self) -> Picture | None: ...

    async def close(self) -> None: ...


# --- the login VM -------------------------------------------------------------------------------------------------

Run = Callable[[bytes], Awaitable[ShellResult]]


class HelperSandbox:
    """A sandbox behind the platform browser helper; `run` sends one `call` request, `release` ends the sandbox."""

    def __init__(self, run: Run, release: Callable[[], Awaitable[None]]) -> None:
        self.run = run
        self.release = release
        self.last: Answer | None = None

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Answer:
        payload = json.dumps({"tool": tool, "arguments": {**arguments, "snapshot": False}}).encode()
        try:
            answer = parse_answer(await self.run(payload))
        except WorkVmError:
            raise SandboxError("helper_invalid") from None
        self.last = answer
        if not answer.ok:
            raise SandboxError("action_failed")
        return answer

    async def goto(self, url: str) -> None:
        await self._call("browser_navigate", {"url": url})

    async def evaluate(self, script: str) -> str:
        return (await self._call("browser_evaluate", {"expression": script})).result or "null"

    async def type(self, target: str, text: str) -> None:
        await self._call("browser_type", {"target": target, "text": text})

    async def click(self, target: str) -> None:
        await self._call("browser_click", {"target": target})

    async def click_at(self, x: float, y: float) -> None:
        await self._call("browser_click", {"x": x, "y": y})

    async def picture(self) -> Picture | None:
        answer = await self._call("browser_evaluate", {"expression": "0"})
        if answer.image is None:
            return None
        return Picture(answer.image.width, answer.image.height, answer.image.jpeg)

    async def close(self) -> None:
        await self.release()


async def vm_sandbox(workvms: Any, request_id: str, log: Callable[[str], None]) -> HelperSandbox:
    """A login VM for the request (`SandboxError("no_sandbox")` when the device offers none now)."""
    from agent_runner.operations.extensions import OperationFailed  # noqa: PLC0415 - the operations import the login

    try:
        vm = await workvms.obtain_login(request_id)
    except OperationFailed as failed:
        log(f"sandbox_unavailable error={failed.code}")
        raise SandboxError("no_sandbox") from None
    shell = workvms.deps.shell_for(vm)

    async def run(payload: bytes) -> ShellResult:
        result: ShellResult = await shell(call_argv(), stdin=payload, timeout=CALL_TIMEOUT, limit=ANSWER_LIMIT + 1)
        return result

    async def release() -> None:
        await workvms.delete(vm, reason="login_ended")

    return HelperSandbox(run, release)


# --- a browser process of the device ------------------------------------------------------------------------------


def server_pin(url: str) -> str | None:
    """`base64(sha256(SPKI))` of the origin's certificate verified against `SSL_CERT_FILE`; None without that variable
    or for a plain-HTTP origin. Raises `ssl.SSLError`/`OSError` when the certificate does not verify."""
    parts = urlsplit(url)
    if not os.environ.get("SSL_CERT_FILE") or parts.scheme != "https":
        return None
    host = parts.hostname or ""
    context = ssl.create_default_context()
    with (
        socket.create_connection((host, parts.port or 443), timeout=TLS_TIMEOUT) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert der is not None
    key = x509.load_der_x509_certificate(der).public_key()
    spki = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def use_browsers(home: Path) -> None:
    """Point Playwright at the Chromium the platform profile installed below the runner home, unless the environment
    names a location already (the daemon runs as the runner account, not as the installing administrator)."""
    installed = home / BROWSERS_DIR
    if installed.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(installed))


def load_playwright(factory: Callable[[], Callable[[], Any]] | None) -> Callable[[], Any]:
    """`async_playwright` of the `login` extra; `SandboxError("extra_missing")` when it is not installed."""
    try:
        if factory is not None:
            return factory()
        starter: Callable[[], Any] = importlib.import_module("playwright.async_api").async_playwright
    except ImportError:
        raise SandboxError("extra_missing") from None
    return starter


class ProcessSandbox:
    """One headless Chromium process with its own profile (module docstring)."""

    def __init__(self, manager: Any, context: Any, page: Any, timeout: float) -> None:
        self.manager = manager
        self.context = context
        self.page = page
        self.timeout_ms = timeout * 1000

    @classmethod
    async def start(cls, starter: Callable[[], Any], profile_dir: Path, url: str, timeout: float) -> ProcessSandbox:
        manager = starter()
        try:
            pin = await asyncio.to_thread(server_pin, url)
            args = [] if pin is None else [f"--ignore-certificate-errors-spki-list={pin}"]
            playwright = await manager.__aenter__()
            context = await playwright.chromium.launch_persistent_context(str(profile_dir), headless=True, args=args)
            page = await context.new_page()
        except Exception as exc:
            await manager.__aexit__(None, None, None)
            raise SandboxError(type(exc).__name__) from None
        return cls(manager, context, page, timeout)

    async def _do(self, work: Awaitable[Any]) -> Any:
        try:
            return await work
        except Exception as exc:
            raise SandboxError(type(exc).__name__) from None

    async def goto(self, url: str) -> None:
        await self._do(self.page.goto(url, timeout=self.timeout_ms))

    async def evaluate(self, script: str) -> str:
        value = await self._do(self.page.evaluate(script))
        return json.dumps(value)

    async def type(self, target: str, text: str) -> None:
        await self._do(self.page.fill(target, text, timeout=self.timeout_ms))

    async def click(self, target: str) -> None:
        await self._do(self.page.click(target, timeout=self.timeout_ms))

    async def click_at(self, x: float, y: float) -> None:
        await self._do(self.page.mouse.click(x, y))

    async def picture(self) -> Picture | None:
        size = self.page.viewport_size or {"width": 0, "height": 0}
        for quality in QUALITIES:
            data: bytes = await self._do(self.page.screenshot(type="jpeg", quality=quality))
            if len(data) <= IMAGE_BYTES:
                return Picture(int(size["width"]), int(size["height"]), data)
        return None

    async def close(self) -> None:
        try:
            await self.context.close()
        finally:
            await self.manager.__aexit__(None, None, None)
