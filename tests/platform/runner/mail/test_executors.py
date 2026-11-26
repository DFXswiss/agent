"""U-RUN-mail (ADR-099 sections 3 to 7): `mail.find` on the device - the mailbox named by its name or ID among those
released to the session's workgroup (sealed release and the owner's explicit access count, the hub's mirror only
finds candidates), the answer, the built-in wait for a new mail, several sessions reading different mailboxes at once,
the audit row `mail.read` with mailbox, time and number only, and no password in answer, audit or log."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_doubles.mail.double import MailDouble
from agent_runner.mail import executors, mailboxes, reader
from agent_runner.mail.executors import find
from agent_runner.operations.extensions import OperationFailed
from tests.platform.runner.credentials.kit import ALICE, BOB, Env, kit_env, pid

from .kit import OTHER_WG, PASSWORD, WG, MailboxHub, OpEnv, mailbox_body, operation_context, session

Body = Callable[[Env, MailboxHub, OpEnv], Awaitable[None]]
SENDER = "Sign-in <no-reply@mail.example.com>"


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            await env.add_ck()
            await env.add_ck(BOB)
            await body(env, MailboxHub(env.hub).install(), OpEnv(env))

    asyncio.run(main())


def _double(mail: Callable[[], MailDouble], *users: str) -> MailDouble:
    double = mail()
    for user in users:
        double.add_account(user, PASSWORD)
    return double


def _send(double: MailDouble, to: str, **body: Any) -> None:
    double.deliver({"to": to, "from": SENDER, "subject": "Your sign-in link", **body})


async def _find(env: Env, op_env: OpEnv, params: dict[str, Any], *, wg: str = WG) -> dict[str, Any]:
    return await find(operation_context(env, op_env, params, wg=wg))


async def _fails(env: Env, op_env: OpEnv, params: dict[str, Any]) -> OperationFailed:
    with pytest.raises(OperationFailed) as failed:
        await _find(env, op_env, params)
    return failed.value


def test_a_released_mailbox_is_found_by_name_or_id_and_answers_text_and_links(
    store_dsn: str, mail: Callable[[], MailDouble]
) -> None:
    double = _double(mail, "signup-1@example.org")
    _send(double, "signup-1@example.org", text="Sign in: https://example.com/magic-link#abc")

    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        record_id = hub.add(mailbox_body(double, "signup-1@example.org", name="signup-1", wgs=[WG]))
        found = await _find(env, op_env, {"mailbox": "signup-1", "body": True, "limit": 1})
        assert found["mailbox"] == {"id": record_id, "name": "signup-1", "email": "signup-1@example.org"}
        (message,) = found["messages"]
        assert message["links"] == ["https://example.com/magic-link#abc"]
        assert (message["id"], message["folder"], message["from"]) == ("inbox:1", "inbox", SENDER)
        assert (found["waited_seconds"], found["timed_out"]) == (0, False)
        env.hub.check("ops/mail.find.result.json", found)
        by_id = await _find(env, op_env, {"mailbox": record_id, "id": "inbox:1"})
        assert by_id["messages"][0]["text"] == "Sign in: https://example.com/magic-link#abc"
        gone = await _fails(env, op_env, {"mailbox": record_id, "id": "inbox:9"})
        assert gone.code == "not_found"
        audits = op_env.audits()
        assert [
            (row["action"], row["target_id"], row["detail"]["count"], row["detail"]["result"]) for row in audits
        ] == [
            ("mail.read", record_id, 1, "ok"),
            ("mail.read", record_id, 1, "ok"),
            ("mail.read", record_id, 0, "not_found"),
        ]
        assert audits[0]["detail"]["owner_id"] == ALICE
        for row in audits:
            env.hub.check("audit/mail.read.json", row["detail"])
        text = json.dumps([found, by_id, audits]) + env.log()
        assert PASSWORD not in text
        assert "magic-link" not in json.dumps(audits)

    run(store_dsn, body)


def test_only_the_sealed_release_of_an_owner_with_access_counts(store_dsn: str, mail: Callable[[], MailDouble]) -> None:
    double = _double(mail, "a@example.org")

    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        hub.add(mailbox_body(double, "a@example.org", name="other-wg", wgs=[OTHER_WG]), mirror=[WG])  # mirror lies
        hub.add(
            mailbox_body(double, "a@example.org", name="zeds", wgs=[WG]), owner=pid("zed"), author="zed"
        )  # no member
        hub.add(
            mailbox_body(double, "a@example.org", name="forged", wgs=[WG]), owner=BOB, author="alice"
        )  # wrong author
        hub.add({"imap_host": "x"}, mirror=[WG])  # opens, but is no mailbox
        hub.add(mailbox_body(double, "a@example.org", name="nameless", wgs=[WG]) | {"imap_port": "x"})
        failed = await _fails(env, op_env, {"mailbox": "other-wg"})
        assert failed.code == "not_found"
        assert failed.message is not None
        assert "No mailbox named other-wg is released to this workgroup. No mailbox is released here." in failed.message
        assert "agent-platform mailbox release" in failed.message
        good = hub.add(mailbox_body(double, "a@example.org", wgs=[WG]))
        failed = await _fails(env, op_env, {"mailbox": "missing"})
        assert f"Released here: {good}." in (failed.message or "")
        assert op_env.audits() == []  # nothing was read
        # a record the hub serves under another ID, or that does not verify, is left out
        hub.served[0] = ("01930000-0000-7000-8000-000000000001", *hub.served[0][1:])
        assert await mailboxes.released(env.context, session()) == []

    run(store_dsn, body)


def test_a_hub_that_cannot_list_fails_retryably(store_dsn: str) -> None:
    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        hub.refusal = (503, {"error": "unavailable"})
        failed = await _fails(env, op_env, {"mailbox": "a"})
        assert (failed.code, failed.retryable) == ("hub_unreachable", True)
        hub.refusal, hub.raw = None, {"mailboxes": [7, {"id": 1}]}
        assert await mailboxes.released(env.context, session()) == []

    run(store_dsn, body)


def test_the_wait_ends_with_the_first_new_mail_or_times_out(
    store_dsn: str, mail: Callable[[], MailDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    double = _double(mail, "w@example.org")
    _send(double, "w@example.org", subject="Old mail")  # arrived before the wait: counts (two minutes back)
    monkeypatch.setattr(executors, "POLL_SECONDS", 0.2)

    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        op_env.now = time.time()
        hub.add(mailbox_body(double, "w@example.org", name="w", wgs=[WG]))
        quick = await _find(env, op_env, {"mailbox": "w", "subject": "Old mail", "wait_seconds": 5})
        assert [item["subject"] for item in quick["messages"]] == ["Old mail"]
        timer = threading.Timer(1.0, lambda: _send(double, "w@example.org", subject="Code 123456"))
        timer.start()
        waited = await _find(env, op_env, {"mailbox": "w", "subject": "Code", "wait_seconds": 30, "body": True})
        timer.join()
        assert [item["subject"] for item in waited["messages"]] == ["Code 123456"]
        assert waited["timed_out"] is False
        assert 1 <= waited["waited_seconds"] < 30
        window = {"since": "2020-01-01T00:00:00.000Z", "until": "2099-01-01T00:00:00.000Z"}
        assert len((await _find(env, op_env, {"mailbox": "w", **window}))["messages"]) == 2
        nothing = await _find(env, op_env, {"mailbox": "w", "from": "nobody.example", "wait_seconds": 1})
        assert (nothing["messages"], nothing["timed_out"]) == ([], True)
        assert op_env.audits()[-1]["detail"]["timed_out"] is True

    run(store_dsn, body)


def test_sessions_read_different_mailboxes_at_once(store_dsn: str, mail: Callable[[], MailDouble]) -> None:
    double = _double(mail, *(f"s{index}@example.org" for index in range(5)))
    for index in range(5):
        _send(double, f"s{index}@example.org", subject=f"For session {index}")

    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        for index in range(5):
            hub.add(mailbox_body(double, f"s{index}@example.org", name=f"s{index}", wgs=[WG]))
        found = await asyncio.gather(*(_find(env, OpEnv(env), {"mailbox": f"s{index}"}) for index in range(5)))
        assert [[item["subject"] for item in answer["messages"]] for answer in found] == [
            [f"For session {index}"] for index in range(5)
        ]

    run(store_dsn, body)


def test_refusals_and_slow_servers_of_the_mailbox(
    store_dsn: str, mail: Callable[[], MailDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    double = _double(mail, "r@example.org")

    async def body(env: Env, hub: MailboxHub, op_env: OpEnv) -> None:
        hub.add(mailbox_body(double, "r@example.org", name="r", wgs=[WG]) | {"imap_password": "wrong-one"})
        refused = await _fails(env, op_env, {"mailbox": "r"})
        assert (refused.code, refused.retryable) == ("upstream_error", False)
        assert "refused the sign-in" in (refused.message or "")

        def slow(*_args: Any, **_kwargs: Any) -> Any:
            raise TimeoutError

        monkeypatch.setattr(executors, "imap_factory", slow)
        timeout = await _fails(env, op_env, {"mailbox": "r"})
        assert (timeout.code, timeout.retryable) == ("timeout", True)

        class Refusing:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def login(self, *_args: Any) -> None:
                pass

            def select(self, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
                return "NO", []

            def logout(self) -> None:
                pass

        monkeypatch.setattr(executors, "imap_factory", Refusing)
        command = await _fails(env, op_env, {"mailbox": "r", "folder": "inbox"})
        assert command.message == executors.COMMAND
        assert [row["detail"]["result"] for row in op_env.audits()] == ["upstream_error", "timeout", "upstream_error"]
        assert "wrong-one" not in json.dumps(op_env.audits()) + env.log()

    run(store_dsn, body)


def test_the_answer_stays_within_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:

    box = mailboxes.Mailbox(
        "01930000-0000-7000-8000-000000000001", ALICE, None, "a@example.org", reader.Server("h", 1, "u", "p")
    )
    messages = [
        reader.Message("inbox", index, "a", "b", "c", None, 1, text="x" * 100, links=["https://example.com/"])
        for index in range(1, 4)
    ]
    messages[0].text_truncated = True
    monkeypatch.setattr(executors, "MAX_ANSWER_BYTES", 900)
    answer = executors.answer(box, [*messages, reader.Message("inbox", 9, "a", "b", "c", None, 1)], 0, False)
    assert [item.get("text") for item in answer["messages"]] == ["x" * 100, "", "", None]
    assert answer["messages"][0]["text_truncated"] is True
    assert answer["messages"][2]["text_truncated"] is True
    assert answer["messages"][0]["date"] is None
    assert executors.query({"wait_seconds": 0}, datetime.now(UTC)).since is None
