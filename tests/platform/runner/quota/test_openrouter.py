"""U-RUN-quota: spend of the platform key (`GET <openrouter>/api/v1/key`) and its interface to the device metrics of
RUN-15 (05-runner.md section 10.6, 08-monitoring.md section 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.metrics import textfile
from agent_runner.quota import openrouter
from agent_runner.quota.openrouter import SpendPoller, spend_metrics, spend_poller

from .double import KEY_PATH, UsageDouble

KEY = "test-router-key-SPENDKEYMARKER-0123456789abcdef"
NOW = 1_795_000_000.0


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        return

    def text(self) -> str:
        return "".join(self.lines)


def make(double: UsageDouble, key: str | None = KEY) -> tuple[SpendPoller, Log]:
    log = Log()
    context = SimpleNamespace(log=log)
    return SpendPoller(context=context, upstream=double.url + "/api/", key=lambda: key, wall=lambda: NOW), log


def run(body: Any, offset: int = 1) -> None:
    async def main() -> None:
        with UsageDouble(KEY, offset=offset) as double:
            await body(double)

    asyncio.run(main())


def test_the_spend_is_read_with_the_key_and_offered_as_metric() -> None:
    async def body(double: UsageDouble) -> None:
        poller, log = make(double)
        assert poller.metrics(NOW) is None  # nothing read yet
        await poller.tick(NOW)
        [(path, headers)] = double.requests
        assert path == KEY_PATH
        assert headers["authorization"] == f"Bearer {KEY}"
        assert poller.metrics(NOW) == {"daily": 0.75, "weekly": 3.25, "monthly": 9.0, "total": 12.5}
        await poller.tick(NOW + 899)  # not due
        assert len(double.requests) == 1
        assert poller.metrics(NOW + openrouter.STALE_S + 1) is None  # stale
        assert KEY not in log.text()

    run(body)


@pytest.mark.parametrize(
    ("status", "answer", "headers", "line", "next_s"),
    [
        (429, {}, {"Retry-After": "900"}, "spend_rate_limited", 900),
        (429, {}, {}, "spend_rate_limited", 300),
        (500, {}, {}, "spend_failed status=500", 900),
        (200, b"{", {}, "spend_invalid field=json", 900),
        (200, {"data": {}}, {}, "spend_invalid field=usage", 900),
    ],
)
def test_failures_keep_no_value_and_wait(
    status: int, answer: object, headers: dict[str, str], line: str, next_s: int
) -> None:
    async def body(double: UsageDouble) -> None:
        poller, log = make(double)
        double.queue(KEY_PATH, status, answer, headers)
        await poller.tick(NOW)
        assert line in log.text()
        assert poller.metrics(NOW) is None
        assert poller.next_at == NOW + next_s

    run(body)


def test_a_wrong_key_a_missing_key_and_no_connection() -> None:
    async def body(double: UsageDouble) -> None:
        poller, log = make(double, key="wrong")
        await poller.tick(NOW)
        assert "spend_failed status=401" in log.text()
        missing, _ = make(double, key=None)
        await missing.tick(NOW)
        assert len(double.requests) == 1
        offline = SpendPoller(context=SimpleNamespace(log=log), upstream="http://127.0.0.1:9/api", key=lambda: KEY)
        await offline.tick(NOW)
        assert "spend_failed error=ConnectError" in log.text()

    run(body)


def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "openrouter.key"
    path.write_text(KEY + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def context_of(kind: str, key_path: Path | None, upstream: str = "https://openrouter.ai/api") -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(device=SimpleNamespace(kind=kind)),
        config=SimpleNamespace(openrouter_key_file=key_path, model_upstream_openrouter=upstream),
        log=Log(),
    )


def test_the_poller_exists_only_on_platform_devices_with_a_key_file(tmp_path: Path) -> None:
    path = key_file(tmp_path)
    assert spend_poller(context_of("org", path)) is None
    assert spend_poller(context_of("platform", None)) is None
    assert spend_poller(SimpleNamespace(identity=None, config=SimpleNamespace())) is None
    context = context_of("platform", path)
    found = spend_poller(context, wall=lambda: NOW)
    assert found is not None
    assert spend_poller(context) is found
    assert found.key() == KEY
    assert found.url == "https://openrouter.ai/api/v1/key"
    assert spend_metrics(context) is None
    assert spend_metrics(SimpleNamespace()) is None
    found.spend = {"daily": 1, "weekly": 2, "monthly": 3, "total": 4}
    found.fetched_at = NOW
    metrics = spend_metrics(context)
    assert metrics == {"daily": 1.0, "weekly": 2.0, "monthly": 3.0, "total": 4.0}
    # what the sampler of RUN-15 passes on: the gauge per window in the textfile
    from ..metrics.test_sinks import snapshot  # noqa: PLC0415

    rendered = textfile.render(textfile.metrics_of(snapshot(), spend=metrics))
    assert 'agent_runner_device_model_spend_usd{window="daily"} 1' in rendered
    assert 'agent_runner_device_model_spend_usd{window="total"} 4' in rendered
    assert KEY not in rendered
