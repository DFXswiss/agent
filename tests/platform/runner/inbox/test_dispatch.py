"""U-RUN-inbox: discovery of event consumers (`<area>/consumers.py:CONSUMERS`, ADR-039)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_runner.errors import RunnerError
from agent_runner.inbox.dispatch import EVENT_KINDS, Consumer, consumers

HEADER = "from agent_runner.inbox.dispatch import Consumer\nasync def handle(context, conn, event):\n    pass\n"


def test_consumers_are_discovered_in_area_order(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package(
        {
            "b/consumers.py": HEADER + 'CONSUMERS = [Consumer("b.prompts", frozenset({"prompt"}), handle)]\n',
            "a/consumers.py": HEADER
            + 'CONSUMERS = (Consumer("a.settings", frozenset({"settings.workgroup", "wg.profile"}), handle),)\n',
            "c/other.py": "X = 1\n",
        }
    )
    found = consumers(package)
    assert [c.name for c in found] == ["a.settings", "b.prompts"]
    assert all(isinstance(c, Consumer) for c in found)
    assert {"prompt", "settings.workgroup", "session.output"} <= EVENT_KINDS


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ('CONSUMERS = "prompt"\n', "registry_invalid"),
        ("CONSUMERS = 3\n", "registry_invalid"),
        ("CONSUMERS = [handle]\n", "registry_invalid"),
        ('CONSUMERS = [Consumer("Bad Name", frozenset({"prompt"}), handle)]\n', "registry_invalid"),
        ('CONSUMERS = [Consumer("x", frozenset(), handle)]\n', "registry_invalid"),
        ('CONSUMERS = [Consumer("x", frozenset({"mail"}), handle)]\n', "registry_invalid"),
        ('X = Consumer("x", frozenset({"prompt"}), handle)\nCONSUMERS = [X, X]\n', "registry_duplicate"),
        ("OTHER = []\n", "registry_invalid"),
    ],
)
def test_invalid_registrations_stop_the_start(
    area_package: Callable[[dict[str, str]], str], source: str, code: str
) -> None:
    package = area_package({"a/consumers.py": HEADER + source})
    with pytest.raises(RunnerError) as raised:
        consumers(package)
    assert raised.value.code == code


def test_the_real_package_registers_valid_consumers() -> None:
    names = [c.name for c in consumers()]
    assert len(names) == len(set(names))
