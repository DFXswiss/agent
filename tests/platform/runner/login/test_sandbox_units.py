"""U-RUN-login (KOR-73, ADR-099): units of the login sandbox, the page recognition, the automation driver, the check
for humans and the code of the sign-in mail - with scripted sandboxes, follow-ups and IMAP connections."""

from __future__ import annotations

import asyncio
import base64
import builtins
import dataclasses
import json
import os
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli.account_login import MAX_MESSAGE_BYTES, Mailbox
from agent_cli.store import StoreError
from agent_runner.login import automation, mailcode, pages, requests, sandbox
from agent_runner.login.automation import AutomationFailed, Driver, Plan
from agent_runner.login.pages import Page
from agent_runner.login.relay import LoginOptions
from agent_runner.login.sandbox import HelperSandbox, Picture, ProcessSandbox, SandboxError
from agent_runner.workvm.jobs import ShellResult

BOX = Mailbox(host="h", user="u", password="p", port=993)
FAST = LoginOptions(page_timeout=0.3, page_poll=0.01, mail_poll=0.01, mail_wait=1.0, verify_tick=0.01)
FAST = dataclasses.replace(FAST, verify_wait=0.3, verify_settle=0.0)


def page(kind: str, **extra: Any) -> str:
    return json.dumps(
        {
            "kind": kind,
            "url": "https://x.example/",
            "view": [1280, 800],
            "code": None,
            "boxes": [],
            "marks": [],
            **extra,
        }
    )


# --- pages ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"kind": "nope"}),
        page("email", url=5),
        page("email", view=[1]),
        page("email", view=[0, 800]),
        page("email", view=[True, 800]),
        page("email", boxes="x"),
        page("email", boxes=[{"mark": "box-0", "kind": "terms", "required": True}]),
        page("email", boxes=[1]),
        page("email", marks=["Bad Mark"]),
        page("email", marks="email"),
    ],
)
def test_a_page_answer_out_of_form_is_unknown(text: str) -> None:
    assert pages.parse(text) == pages.UNKNOWN


def test_a_page_answer_is_read_with_its_boxes_marks_and_code() -> None:
    box = {"mark": "box-0", "kind": "age", "required": True, "checked": False}
    found = pages.parse(page("code", code="AB#CD", boxes=[box], marks=["submit"]))
    assert found == Page(
        "code",
        "https://x.example/",
        1280.0,
        800.0,
        "AB#CD",
        (pages.Box("box-0", "age", True, False),),
        frozenset({"submit"}),
    )
    assert pages.parse(page("code", code="a b")).code is None


# --- the helper sandbox (login VM) ---------------------------------------------------------------------------------


def answer(**changes: Any) -> bytes:
    body: dict[str, Any] = {
        "ok": True,
        "error": None,
        "notice": None,
        "url": "https://x.example/",
        "title": "t",
        "tab": 0,
        "tabs": [{"index": 0, "url": "https://x.example/", "title": "t", "active": True}],
        "snapshot": "",
        "masked": False,
        "result": None,
        "image": None,
        "file": None,
    }
    body.update(changes)
    return json.dumps(body).encode()


def test_the_helper_sandbox_sends_each_call_without_snapshot_and_reads_the_picture() -> None:
    sent: list[dict[str, Any]] = []
    jpeg = base64.b64encode(b"\xff\xd8jpeg").decode()
    replies = [
        answer(),
        answer(result='{"a":1}'),
        answer(),
        answer(),
        answer(),
        answer(image={"width": 960, "height": 600, "jpeg": jpeg}),
        answer(),
        answer(ok=False, error="no such element"),
        b"garbage",
    ]
    released: list[bool] = []

    async def run(payload: bytes) -> ShellResult:
        sent.append(json.loads(payload))
        return ShellResult(0, replies.pop(0), False)

    async def release() -> None:
        released.append(True)

    async def main() -> None:
        box = HelperSandbox(run, release)
        await box.goto("https://x.example/")
        assert await box.evaluate("1") == '{"a":1}'
        assert await box.evaluate("2") == "null"
        await box.type("#a", "text")
        await box.click("#b")
        assert await box.picture() == Picture(960, 600, b"\xff\xd8jpeg")
        assert await box.picture() is None
        with pytest.raises(SandboxError, match="action_failed"):
            await box.click_at(1.0, 2.0)
        with pytest.raises(SandboxError, match="helper_invalid"):
            await box.goto("https://x.example/")
        await box.close()

    asyncio.run(main())
    assert [item["tool"] for item in sent] == [
        "browser_navigate",
        "browser_evaluate",
        "browser_evaluate",
        "browser_type",
        "browser_click",
        "browser_evaluate",
        "browser_evaluate",
        "browser_click",
        "browser_navigate",
    ]
    assert all(item["arguments"]["snapshot"] is False for item in sent)
    assert sent[7]["arguments"] == {"x": 1.0, "y": 2.0, "snapshot": False}
    assert released == [True]


class FakeWorkVms:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.deleted: list[tuple[str, str]] = []
        self.calls: list[tuple[list[str], bytes]] = []
        self.deps = SimpleNamespace(shell_for=self.shell_for)

    async def obtain_login(self, request_id: str) -> Any:
        from agent_runner.operations.extensions import OperationFailed  # noqa: PLC0415

        if self.failure is not None:
            raise OperationFailed(self.failure, message="none")
        return SimpleNamespace(id="wvm-0000000a", request=request_id)

    def shell_for(self, vm: Any) -> Callable[..., Any]:
        async def call(argv: list[str], *, stdin: bytes, timeout: float, limit: int) -> ShellResult:
            self.calls.append((argv, stdin))
            return ShellResult(0, answer(), False)

        return call

    async def delete(self, vm: Any, *, reason: str) -> None:
        self.deleted.append((vm.id, reason))


def test_a_login_vm_is_obtained_driven_and_deleted_with_the_login() -> None:
    workvms = FakeWorkVms()
    lines: list[str] = []

    async def main() -> None:
        box = await sandbox.vm_sandbox(workvms, "req-1", lines.append)
        await box.goto("https://x.example/")
        await box.close()
        with pytest.raises(SandboxError, match="no_sandbox"):
            await sandbox.vm_sandbox(FakeWorkVms("workvm_unavailable"), "req-2", lines.append)

    asyncio.run(main())
    assert workvms.calls[0][0] == ["sudo", "-n", "/usr/local/bin/workvm-browser", "call"]
    assert workvms.deleted == [("wvm-0000000a", "login_ended")]
    assert lines == ["sandbox_unavailable error=workvm_unavailable"]


# --- the process sandbox (fakes of Playwright) ---------------------------------------------------------------------


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class FakePage:
    def __init__(self, shots: list[bytes]) -> None:
        self.shots = shots
        self.mouse = FakeMouse()
        self.viewport_size = {"width": 1280, "height": 720}
        self.done: list[str] = []

    async def goto(self, url: str, timeout: float) -> None:
        self.done.append(f"goto {url}")

    async def evaluate(self, script: str) -> Any:
        return {"script": script}

    async def fill(self, target: str, text: str, timeout: float) -> None:
        raise RuntimeError("no such element")

    async def click(self, target: str, timeout: float) -> None:
        self.done.append(f"click {target}")

    async def screenshot(self, *, type: str, quality: int) -> bytes:
        return self.shots.pop(0)


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, page: FakePage, *, fail: bool = False) -> None:
        self.context = FakeContext(page)
        self.chromium = self
        self.fail = fail
        self.exited = 0
        self.args: list[str] = []

    async def launch_persistent_context(self, profile: str, *, headless: bool, args: list[str]) -> FakeContext:
        if self.fail:
            raise RuntimeError("no chromium")
        self.args = args
        return self.context

    async def __aenter__(self) -> FakePlaywright:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.exited += 1


def test_the_process_sandbox_drives_one_page_and_maps_every_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    big = b"x" * (sandbox.IMAGE_BYTES + 1)
    fake_page = FakePage([big, b"small", big, big, big])
    fake = FakePlaywright(fake_page)

    async def main() -> None:
        box = await ProcessSandbox.start(lambda: fake, tmp_path / "browser", "https://x.example/", 1.0)
        await box.goto("https://x.example/")
        assert json.loads(await box.evaluate("1")) == {"script": "1"}
        await box.click("#a")
        await box.click_at(3.0, 4.0)
        with pytest.raises(SandboxError, match="RuntimeError"):
            await box.type("#a", "x")
        assert await box.picture() == Picture(1280, 720, b"small")
        assert await box.picture() is None
        await box.close()
        failing = FakePlaywright(FakePage([]), fail=True)
        with pytest.raises(SandboxError, match="RuntimeError"):
            await ProcessSandbox.start(lambda: failing, tmp_path / "b2", "https://x.example/", 1.0)
        assert failing.exited == 1

    asyncio.run(main())
    assert fake.context.closed
    assert fake.exited == 1
    assert fake_page.mouse.clicks == [(3.0, 4.0)]
    assert fake.args == []


def test_a_missing_login_extra_is_a_sandbox_error() -> None:
    def missing() -> Callable[[], Any]:
        raise ImportError("playwright")

    with pytest.raises(SandboxError, match="extra_missing"):
        sandbox.load_playwright(missing)
    assert callable(sandbox.load_playwright(None))


# --- the driver ----------------------------------------------------------------------------------------------------


class Scripted:
    """A sandbox whose pages follow a script: each action moves to the next page."""

    def __init__(self, pages_: list[str], *, picture: Picture | None = None) -> None:
        self.pages = pages_
        self.index = 0
        self.actions: list[str] = []
        self.picture_value = picture
        self.broken = False

    def _next(self, action: str) -> None:
        self.actions.append(action)
        self.index = min(self.index + 1, len(self.pages) - 1)

    async def goto(self, url: str) -> None:
        self.actions.append(f"goto {url}")
        if url.startswith("https://x.example/magic"):
            self.index = min(self.index + 1, len(self.pages) - 1)

    async def evaluate(self, script: str) -> str:
        if self.broken:
            self.broken = False
            raise SandboxError("navigating")
        return self.pages[self.index]

    async def type(self, target: str, text: str) -> None:
        self.actions.append(f"type {target} {text}")

    async def click(self, target: str) -> None:
        if "box-" in target:
            self.actions.append(f"click {target}")
        else:
            self._next(f"click {target}")

    async def click_at(self, x: float, y: float) -> None:
        self._next(f"click_at {x} {y}")

    async def picture(self) -> Picture | None:
        return self.picture_value

    async def close(self) -> None:
        return None


def drive(
    box: Scripted, plan: Plan, *, human: Any = None, opts: LoginOptions = FAST, mail: Any = None
) -> tuple[str, list[str]]:
    stages: list[str] = []

    async def progress(stage: str) -> None:
        stages.append(stage)

    async def no_human(_box: Any, _page: Page) -> None:
        box._next("human")

    async def main() -> str:
        loop = asyncio.get_running_loop()
        driver = Driver(
            box,
            plan,
            url="https://x.example/oauth/authorize",
            opts=opts,
            progress=progress,
            human=human or no_human,
            end=loop.time() + 1.0,
            clock=lambda: 100.0,
        )
        if mail is not None:
            driver._mail = mail  # type: ignore[method-assign]
        return await driver.run()

    return asyncio.run(main()), stages


PLAN = Plan(BOX, "a@x.example", {"name": "Ada", "birth_date": "1990-04-01", "consents": ["terms", "age"]})


async def link_mail(_with_code: bool) -> tuple[str, str]:
    return "link", "https://x.example/magic-link#t"


def test_the_driver_registers_with_parts_of_the_birth_date_and_only_the_given_consents() -> None:
    boxes = [
        {"mark": "box-0", "kind": "terms", "required": True, "checked": False},
        {"mark": "box-1", "kind": "age", "required": False, "checked": False},
        {"mark": "box-2", "kind": "marketing", "required": False, "checked": False},
        {"mark": "box-3", "kind": "terms", "required": True, "checked": True},
    ]
    script = [
        page("challenge"),
        page("register", boxes=boxes, marks=["name", "birth-day", "birth-month", "birth-year", "submit"]),
        page("authorize", marks=["authorize"]),
        page("code", code="CODE#STATE"),
    ]
    box = Scripted(script)
    code, stages = drive(box, PLAN)
    assert code == "CODE#STATE"
    assert stages == ["browser_started", "registered", "authorized"]
    assert box.actions == [
        "goto https://x.example/oauth/authorize",
        "human",
        "type [data-agent-login='name'] Ada",
        "type [data-agent-login='birth-day'] 01",
        "type [data-agent-login='birth-month'] 04",
        "type [data-agent-login='birth-year'] 1990",
        "click [data-agent-login='box-0']",
        "click [data-agent-login='box-1']",
        "click [data-agent-login='submit']",
        "click [data-agent-login='authorize']",
    ]


def test_the_driver_registers_without_a_name_input_and_with_one_date_input() -> None:
    script = [page("register", marks=["birth", "submit"]), page("code", code="C#S")]
    box = Scripted(script)
    assert drive(box, PLAN)[0] == "C#S"
    assert "type [data-agent-login='birth'] 1990-04-01" in box.actions


@pytest.mark.parametrize(
    ("script", "plan", "reason"),
    [
        ([page("register", marks=["name"])], Plan(BOX, "a@x.example"), "account_missing"),
        ([page("register", marks=["birth"])], Plan(BOX, "a@x.example", {"name": "Ada"}), "account_missing"),
        (
            [page("register", boxes=[{"mark": "box-0", "kind": "other", "required": True, "checked": False}])],
            PLAN,
            "consent_missing",
        ),
        ([page("code")], PLAN, "code_form"),
        ([page("error")], PLAN, "unknown_page"),
        ([page("unknown")], PLAN, "unknown_page"),
        ([page("email", marks=["email", "continue"])], PLAN, "unknown_page"),
    ],
)
def test_the_driver_gives_up_with_its_reason(script: list[str], plan: Plan, reason: str) -> None:
    with pytest.raises(AutomationFailed) as failed:
        drive(Scripted(script), plan)
    assert failed.value.reason == reason


def test_the_driver_gives_up_after_too_many_steps() -> None:
    script = [page("authorize" if n % 2 else "challenge", marks=["authorize"]) for n in range(40)]
    with pytest.raises(AutomationFailed, match="unknown_page"):
        drive(Scripted(script), PLAN)


def test_the_driver_reads_the_code_of_the_mail_and_waits_out_a_navigation() -> None:
    script = [
        page("email", marks=["email", "continue"]),
        page("check_email", marks=["mail-code", "submit"]),
        page("code", code="C#S"),
    ]
    box = Scripted(script)
    tries: list[bool] = []

    async def mail(with_code: bool) -> tuple[str, str] | None:
        tries.append(with_code)
        return None if len(tries) < 2 else ("code", "123456")

    box.broken = True
    code, stages = drive(box, PLAN, mail=mail)
    assert code == "C#S"
    assert stages == ["browser_started", "email_entered", "mail_code_received", "authorized"]
    assert "type [data-agent-login='mail-code'] 123456" in box.actions
    assert "click [data-agent-login='submit']" in box.actions


def test_the_driver_opens_the_link_of_the_mail_and_a_code_page_without_submit() -> None:
    box = Scripted([page("check_email"), page("code", code="C#S")])
    assert drive(box, PLAN, mail=link_mail)[1] == ["browser_started", "magic_link_received", "authorized"]


def test_no_mail_in_time_ends_mail_timeout() -> None:
    async def none(_with_code: bool) -> None:
        return None

    with pytest.raises(AutomationFailed, match="mail_timeout"):
        drive(Scripted([page("check_email")]), PLAN, mail=none)


def test_the_mail_is_read_link_first_then_the_code_and_imap_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    links: list[str | None] = [None, None, "https://x.example/magic-link#t", None]
    codes: list[Any] = ["654321", OSError("down")]

    def link(*_a: Any, **_k: Any) -> str | None:
        return links.pop(0)

    def code(*_a: Any, **_k: Any) -> str | None:
        value = codes.pop(0)
        if isinstance(value, Exception):
            raise value
        return str(value)

    monkeypatch.setattr(automation, "newest_magic_link", link)
    monkeypatch.setattr(mailcode, "newest_code", code)

    async def main() -> list[Any]:
        loop = asyncio.get_running_loop()
        driver = Driver(
            Scripted([page("unknown")]),
            PLAN,
            url="https://x.example/oauth/authorize",
            opts=FAST,
            progress=lambda _s: asyncio.sleep(0),
            human=lambda _b, _p: asyncio.sleep(0),
            end=loop.time() + 1,
            clock=lambda: 5.0,
        )
        return [
            await driver._mail(True),
            await driver._mail(True),
            await driver._mail(False),
            await driver._mail(False),
        ]

    assert asyncio.run(main()) == [("code", "654321"), None, ("link", "https://x.example/magic-link#t"), None]


def test_a_sandbox_failure_ends_the_automation_with_its_reason() -> None:
    class Broken(Scripted):
        async def goto(self, url: str) -> None:
            raise SandboxError("helper_invalid")

    async def main() -> None:
        await automation.authorize(
            "https://x.example/",
            PLAN,
            sandbox=Broken([page("unknown")]),
            opts=FAST,
            progress=lambda _s: asyncio.sleep(0),
            human=lambda _b, _p: asyncio.sleep(0),
            budget=1.0,
            clock=lambda: 0.0,
        )

    with pytest.raises(AutomationFailed) as failed:
        asyncio.run(main())
    assert (failed.value.reason, failed.value.answer) == ("helper_invalid", "browser_failed")
    assert AutomationFailed("human_check").answer == "human_check"


# --- the check for humans ------------------------------------------------------------------------------------------


class Flow:
    def __init__(self, follow_ups: list[Any]) -> None:
        self.follow_ups = follow_ups
        self.sent: list[dict[str, Any]] = []
        self.id = "req-1"
        self.context = SimpleNamespace(log=SimpleNamespace(lines=[], write=lambda _t: None, flush=lambda: None))

    async def respond(self, body: dict[str, Any], **_kw: Any) -> None:
        self.sent.append(body)

    async def wait_follow_up(self, seconds: float) -> Any:
        if self.follow_ups:
            return self.follow_ups.pop(0)
        await asyncio.sleep(min(seconds, 0.01))
        return None


def follow(body: dict[str, Any], *, agent: bool = False) -> Any:
    return SimpleNamespace(request=SimpleNamespace(body=body, user="agent" if agent else "alice", owner="alice"))


def check_human(flow: Flow, box: Scripted, *, extended: bool = True, opts: LoginOptions = FAST) -> list[str]:
    stages: list[str] = []

    async def progress(stage: str) -> None:
        stages.append(stage)

    human = requests.HumanCheck(flow, opts, extended=extended, progress=progress)  # type: ignore[arg-type]
    asyncio.run(human(box, pages.parse(page("challenge"))))
    return stages


SHOT = Picture(960, 600, b"\xff\xd8one")


def test_a_click_of_the_owner_passes_the_check_and_an_agent_click_or_a_bad_follow_up_does_not() -> None:
    flow = Flow(
        [
            follow({"click": {"x": 0.5, "y": 0.5}}, agent=True),
            follow({"click": {"x": 2, "y": 0.5}}),
            follow({"code": "x"}),
            follow({"click": {"x": 0.1, "y": 0.25}}),
        ]
    )
    box = Scripted([page("challenge"), page("email")], picture=SHOT)
    assert check_human(flow, box) == ["verified"]
    assert box.actions == ["click_at 128.0 200.0"]
    assert flow.sent == [
        {
            "step": "verify",
            "frame": 1,
            "image": {"width": 960, "height": 600, "data": base64.b64encode(SHOT.jpeg).decode()},
        }
    ]


@pytest.mark.parametrize(
    ("follow_ups", "extended", "picture"),
    [
        ([follow({"relay": True})], True, SHOT),
        ([], True, None),
        ([], False, SHOT),
    ],
)
def test_the_check_is_given_up(follow_ups: list[Any], extended: bool, picture: Picture | None) -> None:
    flow = Flow(follow_ups)
    with pytest.raises(AutomationFailed, match="human_check"):
        check_human(flow, Scripted([page("challenge")], picture=picture), extended=extended)


def test_a_changing_picture_is_sent_until_the_frames_are_used_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "VERIFY_FRAMES", 3)

    class Changing(Scripted):
        async def picture(self) -> Picture | None:
            self.index += 0
            self.actions.append("look")
            return Picture(10, 10, str(len(self.actions)).encode())

    flow = Flow([])
    with pytest.raises(AutomationFailed, match="human_check"):
        check_human(flow, Changing([page("challenge")]), opts=dataclasses.replace(FAST, verify_wait=5.0))
    assert [body["frame"] for body in flow.sent] == [1, 2, 3]


# --- the code of the mail ------------------------------------------------------------------------------------------


def mail(text: str, *, html: bool = False, date: str = "Thu, 26 Nov 2026 12:00:00 +0000") -> bytes:
    message = EmailMessage()
    message["From"] = "Claude <no-reply@mail.anthropic.com>"
    message["Date"] = date
    message.set_content(text, subtype="html" if html else "plain")
    return message.as_bytes()


class Imap:
    def __init__(self, folders: dict[str, list[bytes]], *, junk: bool = True, failing: str | None = None) -> None:
        self.folders = folders
        self.junk = junk
        self.failing = failing
        self.selected = ""
        self.logged_out = False

    def login(self, user: str, password: str) -> None:
        return None

    def select(self, folder: str, readonly: bool) -> tuple[str, list[bytes]]:
        folder = folder.strip('"')
        self.selected = folder
        return ("OK" if folder in self.folders and self.failing != "select" else "NO"), [b""]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren \\Junk) "/" "Junk"'] if self.junk else []

    def search(self, *_a: Any) -> tuple[str, builtins.list[bytes]]:
        if self.failing == "search":
            return "NO", []
        count = len(self.folders[self.selected])
        return "OK", [" ".join(str(n + 1) for n in range(count)).encode()]

    def fetch(self, number: bytes, _what: str) -> tuple[str, builtins.list[Any]]:
        if self.failing == "fetch":
            return "NO", []
        return "OK", [(b"1 (BODY[] {1}", self.folders[self.selected][int(number) - 1]), b")"]

    def logout(self) -> None:
        self.logged_out = True


def newest(imap: Imap, since: float = 0.0) -> str | None:
    return mailcode.newest_code(BOX, since, mail_from="anthropic.com", imap_factory=lambda *_a, **_k: imap)


def test_the_code_of_the_newest_sign_in_mail_is_found_in_inbox_or_junk() -> None:
    old = mail("Your login code is 111111.", date="Thu, 26 Nov 2026 11:00:00 +0000")
    new = mail("<p>Your login code is <b>222222</b>.</p>", html=True)
    assert newest(Imap({"INBOX": [old], "Junk": [new]})) == "222222"
    assert newest(Imap({"INBOX": [new], "Junk": [old]})) == "222222"
    assert newest(Imap({"INBOX": [mail("Hello, no number here.")], "Junk": []})) is None
    assert newest(Imap({"INBOX": [old]}, junk=False)) == "111111"
    assert newest(Imap({"INBOX": [old], "Junk": [old]})) == "111111"
    assert newest(Imap({"INBOX": [b"x" * (MAX_MESSAGE_BYTES + 1)]})) is None
    assert newest(Imap({"INBOX": [old]}), since=2e9) is None
    message = EmailMessage()
    message["From"] = "Claude <no-reply@mail.anthropic.com>"
    message.set_content("Your code: 333333")
    assert newest(Imap({"INBOX": [message.as_bytes()], "Junk": [message.as_bytes()]})) == "333333"


@pytest.mark.parametrize("failing", ["select", "search", "fetch"])
def test_a_failing_mailbox_raises_and_logs_out(failing: str) -> None:
    imap = Imap({"INBOX": [mail("code 123456")]}, failing=failing)
    with pytest.raises(StoreError):
        newest(imap)
    assert imap.logged_out


def test_a_message_without_text_part_has_no_code() -> None:
    message = EmailMessage()
    message.add_attachment(b"code 123456", maintype="application", subtype="octet-stream")
    assert mailcode.code_in(message) is None


def test_a_code_without_a_button_and_a_registration_without_a_birth_date() -> None:
    async def code_mail(_with_code: bool) -> tuple[str, str]:
        return "code", "123456"

    class Typed(Scripted):
        async def type(self, target: str, text: str) -> None:
            self._next(f"type {target} {text}")

    typed = Typed([page("check_email", marks=["mail-code"]), page("code", code="C#S")])
    assert drive(typed, PLAN, mail=code_mail)[0] == "C#S"
    assert "click [data-agent-login='submit']" not in typed.actions
    register = Scripted([page("register", marks=["name", "submit"]), page("code", code="C#S")])
    assert drive(register, Plan(BOX, "a@x.example", {"name": "Ada"}))[0] == "C#S"
    assert register.actions[1:] == ["type [data-agent-login='name'] Ada", "click [data-agent-login='submit']"]


def test_the_platform_chromium_below_the_runner_home_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "unset")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    sandbox.use_browsers(tmp_path)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
    (tmp_path / "ms-playwright").mkdir()
    sandbox.use_browsers(tmp_path)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "ms-playwright")


def test_a_pre_ticked_box_without_consent_is_unticked() -> None:
    boxes = [
        {"mark": "box-0", "kind": "terms", "required": True, "checked": True},
        {"mark": "box-1", "kind": "marketing", "required": False, "checked": True},
    ]
    box = Scripted([page("register", boxes=boxes, marks=["submit"]), page("code", code="C#S")])
    assert drive(box, Plan(BOX, "a@x.example", {"name": "Ada", "consents": ["terms"]}))[0] == "C#S"
    assert box.actions[1:] == ["click [data-agent-login='box-1']", "click [data-agent-login='submit']"]


def _sharing(name: str, order: list[str], end: float, wait: float = 0.0) -> Driver:
    script = Scripted([page("email", marks=["email", "continue"]), page("check_email"), page("code", code="C#S")])

    async def mail(_with_code: bool) -> tuple[str, str]:
        order.append(f"{name} reads")
        await asyncio.sleep(wait)
        return "link", "https://x.example/magic-link#" + name

    async def quiet(_stage: str) -> None:
        return None

    async def no_human(_box: Any, _page: Page) -> None:
        return None

    made = Driver(
        script,
        PLAN,
        url="https://x.example/oauth/authorize",
        opts=FAST,
        progress=quiet,
        human=no_human,
        end=end,
        clock=lambda: 1.0,
    )
    made._mail = mail  # type: ignore[method-assign,assignment]
    return made


def test_logins_sharing_a_mailbox_take_turns_from_the_address_to_the_mail() -> None:
    order: list[str] = []

    async def main() -> list[str]:
        end = asyncio.get_running_loop().time() + 5
        first, second = _sharing("first", order, end, 0.1), _sharing("second", order, end)
        codes = list(await asyncio.gather(first.run(), second.run()))
        assert not first.lock.locked()
        await first.lock.acquire()  # a login that holds the mailbox past the other's time
        late = _sharing("late", order, asyncio.get_running_loop().time() + 0.05)
        with pytest.raises(AutomationFailed, match="mail_timeout"):
            await late.run()
        first.lock.release()
        return codes

    assert asyncio.run(main()) == ["C#S", "C#S"]
    assert order == ["first reads", "second reads"]


def test_an_unexpected_error_in_the_automation_is_a_browser_failure() -> None:
    class Broken(Scripted):
        async def goto(self, url: str) -> None:
            raise RuntimeError("boom")

    async def main() -> None:
        await automation.authorize(
            "https://x.example/",
            PLAN,
            sandbox=Broken([page("unknown")]),
            opts=FAST,
            progress=lambda _s: asyncio.sleep(0),
            human=lambda _b, _p: asyncio.sleep(0),
            budget=1.0,
            clock=lambda: 0.0,
        )

    with pytest.raises(AutomationFailed) as failed:
        asyncio.run(main())
    assert (failed.value.reason, failed.value.answer) == ("RuntimeError", "browser_failed")


def test_only_the_owners_click_gives_the_check_more_time() -> None:
    flow = Flow([follow({"click": {"x": 0.5, "y": 0.5}}, agent=True) for _ in range(200)])
    box = Scripted([page("challenge")], picture=SHOT)
    with pytest.raises(AutomationFailed, match="human_check"):
        check_human(flow, box, opts=dataclasses.replace(FAST, verify_wait=0.2))
    assert box.actions == []


def test_the_page_recognition_never_reads_the_markers_of_the_double() -> None:
    assert "data-page" not in pages.CLASSIFY


def test_consents_sent_by_the_users_agent_are_ignored() -> None:
    lines: list[str] = []
    log = SimpleNamespace(write=lines.append, flush=lambda: None)
    config = SimpleNamespace(login_automation=True)
    stored = requests.Stored("a@x.example", (BOX, "a@x.example"))
    account = {"name": "Ada", "consents": ["terms", "age"]}

    def flow(user: str) -> Any:
        return SimpleNamespace(
            id="req-1",
            user="alice",
            request=SimpleNamespace(user=user),
            context=SimpleNamespace(config=config, log=log),
        )

    async def main() -> tuple[Any, Any]:
        wanted = {"account": account}
        return await requests._plan(flow("agent"), wanted, stored), await requests._plan(flow("alice"), wanted, stored)

    by_agent, by_user = asyncio.run(main())
    assert by_agent.account == {"name": "Ada"}
    assert by_user.account == account
    assert lines == ["runner.login consents_ignored request_id=req-1\n"]


def test_an_e_mail_page_seen_again_keeps_the_mailbox() -> None:
    script = [
        page("email", marks=["email", "continue"]),
        page("challenge"),
        page("email", marks=["email", "continue"]),
        page("check_email"),
        page("code", code="C#S"),
    ]
    box = Scripted(script)
    assert drive(box, PLAN, mail=link_mail)[0] == "C#S"
    assert box.actions.count("type [data-agent-login='email'] a@x.example") == 2
