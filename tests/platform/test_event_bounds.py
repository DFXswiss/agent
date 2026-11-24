"""Every kind of record, event and outbox item a device produces has a bound (ADR-086 section 3, KOR-35).

The kinds are discovered - the guest journal kinds (`agent_guest.journal.KINDS`, `contracts/guest/journal/INDEX.json`),
the device-authored event kinds (`contracts/events/kinds.json`) and the outbox item kinds (`agent_runner.sync.codes`)
- and each needs an entry here (guest kinds in `agent_guest.limits.KIND_BOUNDS`) that names what bounds its count. A
new kind without an entry fails this suite, so no source can flood a view unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_guest import journal, limits, recorder
from agent_runner.model import merge
from agent_runner.sync.codes import ITEM_KINDS

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"

EVENT_BOUNDS = {
    "task.meta": "one per task.set_title operation",
    "session.output": "the guest's session.output records (one per assistant message); clipped at 256 KiB",
    "session.tool": "the guest's session.tool records (one per tool call); clipped field by field at 256 KiB",
    "session.hook": "the guest's session.hook records: routine calls as one summary per 10 s",
    "session.file": "the guest's session.file records (FileLimits)",
    "session.terminal": "the guest's session.terminal records (TerminalLimits); split at 64 KiB",
    "session.status": "one per failed session or failed prompt delivery",
    "operation.params": "one per request_operation call (the guest's operation.request)",
    "operation.result": "one per finished operation",
    "platform.read": "one per answered MCP read (guest bucket: burst 120, 2/s; at most 4 in flight per session)",
    "session.error": "one per guest.error record of a session with a code of the closed list (GUEST_ERROR_CODES)",
    "workvm.state": "one per state change of a work VM (WorkVmManager)",
    "workvm.run": "at most two per work-VM job: running at its start and its final state",
    "test.run": "at most two per test run (a test.run operation): running at its start and its final state",
    "browser.action": "one per browser tool call of a session (13-work-vm.md 13.5); a watched kind, read on demand",
    "browser.state": "one per state change of a platform browser session (starting, open, restarted, closed)",
}
ITEM_BOUNDS = {
    "event": "EVENT_BOUNDS",
    "session_transition": "one per session state change",
    "vm_state": "one per VM state change",
    "operation_transition": "one per operation state change (about four per operation)",
    "usage": "one per session and model.merge.WINDOW_S (10 s), plus one when the session ends",
    "quota": "one per owner poll (at most every 900 s)",
    "metrics": "one sample round every 15 s, backlog bounded to 1000",
    "audit": "per denied hook, refused prompt, delivered ping, read or provider outside the allow-list",
    "wrap": "one per key wrap the device writes (key rotations)",
    "runtime": "one per session and VM per billing.meter.TICK_S (5 s) on a platform device while the hub bills",
    "session_wait": "one per change of a session's model wait (until, choice); unchanged states are not sent again",
    "operator_quota": "one per changed operator quota snapshot, at most every quota.operator.REPORT_S",
    "dfx_key_state": "one per change of the device's issuer role (management key present or not)",
    "dfx_key_issued": "one per DFX key the issuer answers (one per org device request)",
    "dfx_key_usage": "one per issued key and spend report round of the issuer",
}


def device_event_kinds() -> set[str]:
    index = json.loads((CONTRACTS / "events" / "kinds.json").read_text(encoding="utf-8"))
    return {row["kind"] for row in index["kinds"] if "device" in row["authors"]}


def test_every_guest_journal_kind_has_a_bound() -> None:
    index = json.loads((CONTRACTS / "guest" / "journal" / "INDEX.json").read_text(encoding="utf-8"))
    assert {row["kind"] for row in index["kinds"]} == set(journal.KINDS)
    assert set(limits.KIND_BOUNDS) == set(journal.KINDS)
    assert all(text.strip() for text in limits.KIND_BOUNDS.values())


def test_every_device_event_kind_and_outbox_item_kind_has_a_bound() -> None:
    assert set(EVENT_BOUNDS) == device_event_kinds()
    assert set(ITEM_BOUNDS) == set(ITEM_KINDS)


def test_the_bounds_are_the_documented_settings() -> None:
    assert limits.TerminalLimits(quiet_ms=100, max_age_ms=1000, record_json=60 * 1024) == limits.TERMINAL
    assert limits.TERMINAL.record_json <= recorder.MAX_BODY_JSON  # below the 64 KiB ciphertext bound of E8
    assert limits.HOOKS.summary_seconds == 10.0
    assert limits.FileLimits(100, 600.0, 1000, 8 * 1024 * 1024, 20) == limits.FILES
    assert merge.WINDOW_S == 10.0
