"""Fakes for the unit tests of the ATK-33 … ATK-40 plans (``agent_doubles.atk.extended``) and the verdict assertion
of the real runs (``tests/platform/atk/test_atk_*``)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_doubles.atk import catalog, evidence, extended

SID = "01a20253-25bb-76fc-8ab6-83805076021e"
VICTIM = "01a20252-d744-7068-a101-e7f864df3bf8"
UID, VICTIM_UID = 20001, 20000


def selected_modes() -> list[str]:
    """``AGENT_ATK_MODES`` in catalog order (default: both modes; a production golden runs ``hooks`` only)."""
    chosen = [item for item in os.environ.get("AGENT_ATK_MODES", "").split(",") if item]
    unknown = set(chosen) - set(catalog.MODES)
    if unknown:
        pytest.fail(f"unknown AGENT_ATK_MODES {sorted(unknown)}")
    return [mode for mode in catalog.MODES if not chosen or mode in chosen]


def skip_unselected(attack_id: str | None, mode: str | None) -> None:
    """Skip a real-run case whose attack (``AGENT_ATK_ONLY``) or mode (``AGENT_ATK_MODES``) did not run."""
    only = [item for item in os.environ.get("AGENT_ATK_ONLY", "").split(",") if item]
    if attack_id is not None and only and attack_id not in only:
        pytest.skip(f"{attack_id} is not selected by AGENT_ATK_ONLY")
    # the run `outage` belongs to mode `fault` (driver.run_outage, ADR-064 section 3)
    selected = "fault" if mode == "outage" else mode
    if selected is not None and selected not in selected_modes():
        pytest.skip(f"mode {selected} is not selected by AGENT_ATK_MODES")


def assert_attack(results: Mapping[str, Any], attack_id: str, mode: str) -> None:
    """The verdict of one real run (``atk_results`` of conftest): every criterion holds, ``specific`` included."""
    skip_unselected(attack_id, mode)
    found = [v for v in results["verdicts"] if v["attack"] == attack_id and v["mode"] == mode]
    if len(found) != 1:
        pytest.fail(f"{attack_id} ({mode}) did not run")
    failed = {name: c["details"] for name, c in found[0]["criteria"].items() if not c["ok"]}
    assert not failed, failed
    assert evidence.SPECIFIC in found[0]["criteria"], "the attack-specific checks did not run"
    assert found[0]["passed"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@dataclass
class FakeGuest:
    """The guest control link of a plan: imports and exports with scripted answers."""

    imports: list[tuple[str, str, bytes, bool]] = field(default_factory=list)
    import_answers: dict[str, Any] = field(default_factory=dict)
    exports: dict[str, Any] = field(default_factory=dict)
    exported: list[tuple[str, str, Sequence[str] | None]] = field(default_factory=list)

    async def import_files(self, session_id: str, directory: str, data: bytes, *, replace: bool) -> dict[str, Any]:
        self.imports.append((session_id, directory, data, replace))
        answer = self.import_answers.get(directory, {"files": 1, "bytes": 8})
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def export_files(
        self, session_id: str, directory: str, write: Callable[[bytes], object], *, paths: Sequence[str] | None = None
    ) -> str:
        self.exported.append((session_id, directory, paths))
        answer = self.exports.get(directory if paths is None else f"{directory}:{','.join(paths)}", b"")
        if isinstance(answer, Exception):
            raise answer
        write(answer)
        return "0" * 64


class Refused(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass
class FakeRunner:
    checks: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    archives: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    repacked: dict[bytes, tuple[bytes | None, str | None]] = field(default_factory=dict)

    def check(self, op_type: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.checks.get(op_type, {"code": "params_invalid", "message": "m"})

    def tarball(self, repo: str) -> tuple[str, bytes]:
        return self.archives[repo]

    def repack(self, data: bytes) -> tuple[bytes | None, str | None]:
        return self.repacked[data]

    def raw(self, repo: str) -> bytes:
        return b"raw:" + repo.encode()


@dataclass
class FakeHost:
    """``extended.Host``: probe commands answered by ``answers`` (a value, an exception, or a callable)."""

    mode: str = "hooks"
    markers: dict[str, str] = field(
        default_factory=lambda: {"platform_canary": "ATK-CANARY-ETC", "upstream_token": "u" * 32}
    )
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    probes: list[tuple[str, ...]] = field(default_factory=list)
    guest: FakeGuest = field(default_factory=FakeGuest)
    stand_in: FakeRunner = field(default_factory=FakeRunner)
    ticks: Clock = field(default_factory=Clock)

    @property
    def clock(self) -> Callable[[], float]:
        return self.ticks

    @property
    def sleep(self) -> Callable[[float], Any]:
        return self.ticks.sleep

    @property
    def link(self) -> FakeGuest:
        return self.guest

    @property
    def runner(self) -> FakeRunner:
        return self.stand_in

    async def probe(self, *args: str) -> Any:
        self.probes.append(args)
        answer = self.answers.get(args[0], {})
        if callable(answer):
            answer = answer(*args[1:])
        if isinstance(answer, Exception):
            raise answer
        return answer


def context(attack_id: str, host: FakeHost | None = None) -> extended.RunContext:
    return extended.RunContext(host or FakeHost(), catalog.BY_ID[attack_id], SID, UID, VICTIM, VICTIM_UID)


def conversation(*attempts: tuple[str, str, bool | None]) -> list[dict[str, Any]]:
    """Double exchanges of one conversation: tool name, result text, is_error."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Work."}]
    for index, (tool, result, is_error) in enumerate(attempts):
        use = f"toolu_{index}"
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": use, "name": tool}]})
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use, "content": result}
        if is_error is not None:
            block["is_error"] = is_error
        messages.append({"role": "user", "content": [block]})
    return [{"body": {"messages": messages}}]


def record(kind: str, **body: Any) -> dict[str, Any]:
    return {"kind": kind, "body": body}


def run_of(
    attack_id: str,
    *,
    mode: str = "hooks",
    exchanges: Sequence[Mapping[str, Any]] = (),
    journal: Sequence[Mapping[str, Any]] = (),
) -> evidence.RunEvidence:
    return evidence.RunEvidence(
        attack_id=attack_id,
        mode=mode,
        session_id=SID,
        session_dir=f"/srv/sessions/{SID}",
        victim_dir=f"/srv/sessions/{VICTIM}",
        scripted_tools=[],
        double_state={},
        exchanges=exchanges,
        journal=journal,
        transcript=[],
        fs_changes=[],
        targets_before={},
        targets_after={},
        counters={},
        processes=[],
        forbidden={},
    )


def payload(value: Any) -> str:
    return json.dumps(value)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)
