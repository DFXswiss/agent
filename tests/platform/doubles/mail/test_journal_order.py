"""U-DBL-mail: a client that has read a reply finds the command in the journal (record first, then reply).

Under load a reply written before its record lets a test read the journal too early (INF-01); a slow recorder makes
that order visible: with the record after the reply the last entry would still be missing here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from .conftest import Mail, Wire

DELAY_SECONDS = 0.2


@pytest.fixture
def slow_recorder(mail: Mail, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = mail.double.recorder
    record = recorder.record

    def delayed(*args: Any, **kwargs: Any) -> Any:
        time.sleep(DELAY_SECONDS)
        return record(*args, **kwargs)

    monkeypatch.setattr(recorder, "record", delayed)


def last_path(mail: Mail) -> str:
    return str(mail.double.recorder.journal.records()[-1]["path"])


@pytest.mark.usefixtures("slow_recorder")
def test_an_imap_reply_follows_its_record(mail: Mail, imap_session: Callable[..., Wire]) -> None:
    client = imap_session()
    assert client.imap("n NOOP")[-1].startswith("n OK")
    assert last_path(mail) == "NOOP"


@pytest.mark.usefixtures("slow_recorder")
def test_an_smtp_reply_follows_its_record(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    assert client.line().startswith("220 ")
    assert client.command("HELO client.test") == "250 mail.double.test\r\n"
    assert last_path(mail) == "HELO"
