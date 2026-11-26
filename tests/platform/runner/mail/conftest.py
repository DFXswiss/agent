"""Fixtures of the KOR-74 runner suites (`mail.find`): a migrated store and mail doubles over TLS whose runtime CA the
device trusts through `SSL_CERT_FILE` (the IMAP reader uses the default trust store)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA
from agent_runner.store.migrate import migrate


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture(scope="session")
def mail_ca(tmp_path_factory: pytest.TempPathFactory) -> DoubleCA:
    return DoubleCA.create(tmp_path_factory.mktemp("kor74-ca"))


@pytest.fixture
def mail(mail_ca: DoubleCA, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], MailDouble]]:
    """Start mail doubles (IMAPS with the runtime CA, a spam folder flagged `\\Junk`)."""
    monkeypatch.setenv("SSL_CERT_FILE", str(mail_ca.ca_file))
    started: list[MailDouble] = []

    def start() -> MailDouble:
        double = MailDouble(ca=mail_ca).start()
        started.append(double)
        return double

    yield start
    for double in started:
        double.stop()
