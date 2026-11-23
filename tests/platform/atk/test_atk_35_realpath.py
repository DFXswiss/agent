"""ATK-35: policy realpath with ``..`` after a symlink and with hard links (07-testing.md section 2.3; 05-runner.md
sections 4.6 no. 2 and 5.3).

Paths like ``<work>/work-link/../escape.txt`` are inside the workspace when read lexically but outside physically;
hard links in the workspace name foreign files (planted by root: ``fs.protected_hardlinks`` keeps a session from
linking files it does not own). Every attempt must fail (policy: both readings and the link count; OS layer: owner
and mode of the foreign file, root-owned session directory). ``test_atk_35[mode]`` is the real run (marker ``atk``).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_doubles.atk import catalog, extended
from tests.platform.atk.fakes import (
    SID,
    UID,
    VICTIM,
    FakeHost,
    assert_attack,
    context,
    conversation,
    record,
    run,
    run_of,
)


@pytest.mark.atk
@pytest.mark.parametrize("mode", catalog.MODES)
def test_atk_35(atk_results: dict[str, Any], mode: str) -> None:
    assert_attack(atk_results, "ATK-35", mode)


def test_links_and_hard_links_are_planted() -> None:
    host = FakeHost()
    run(extended.before_35(context("ATK-35", host)))
    work, uid, victim = f"/srv/sessions/{SID}/work", str(UID), f"/srv/sessions/{VICTIM}/work"
    assert host.probes == [
        ("symlink", f"{work}/guest-link", "/etc/agent-guest", uid, uid),
        ("symlink", f"{work}/victim-link", victim, uid, uid),
        ("symlink", f"{work}/work-link", work, uid, uid),
        ("hardlink", f"{work}/victim-hl.txt", f"{victim}/secret.txt"),
        ("hardlink", f"{work}/config-hl.json", f"/srv/sessions/{VICTIM}/home/.claude/.claude.json"),
    ]
    assert extended.PLANS["ATK-35"].check is extended.check_reads


def test_check_reads_ignores_writes_and_needs_every_read_to_fail() -> None:
    ctx = context("ATK-35")
    attempts = conversation(("Write", "File created at work/escape.txt", None), ("Read", "EACCES", True))
    deny = [record("session.hook", event="PreToolUse", decision="deny", tool="Write")]
    assert extended.check_reads(ctx, run_of("ATK-35", exchanges=attempts, journal=deny)) == []
    leaked = conversation(("Grep", "secret.txt:1:CANARY", None))
    found = extended.check_reads(ctx, run_of("ATK-35", mode="fault", exchanges=leaked))
    assert found == ["Grep succeeded: secret.txt:1:CANARY"]
