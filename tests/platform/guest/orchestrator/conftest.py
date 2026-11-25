"""Fixtures of the main orchestrator's guest suite (05-runner.md section 17): a fake agent tool (``orchestrator.Tool``)
and the orchestrator of the control fixture with that tool, a main-session depth and a synchronous background."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_guest import orchestrator
from agent_guest.control import ControlContext
from agent_guest.orchestrator import Orchestrator, ToolFailed
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, make_session

OTHER_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
OTHER_TOKEN = "b3RoZXItc2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtMDAx"
PAIRING_ID = "0193000a-0000-7000-8000-00000000000b"
USER_ID = "0193000a-0000-7000-8000-00000000000c"
K32 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
K32B = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"
KEYS_INPUT = {
    "hub_url": "https://hub.example.org",
    "hub_key": {"id": "hub-1", "public_key": K32},
    "platform_root_key": K32B,
}
KEYS = {"pairing_id": PAIRING_ID, "pub": {"kem": K32, "sig": K32B}, "pop": "A" * 86, "fp": "AAECAwQFBgcICQoLDA0ODw"}
HANDOVER = {"pairing_id": PAIRING_ID, "ct": "AAECAwQF", "user_id": USER_ID, "user_pub": {"kem": K32, "sig": K32B}}


@dataclass
class FakeTool:
    """``orchestrator.Tool`` in memory: records every call; ``fail`` maps a command to the code it fails with."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)
    keys: dict[str, Any] = field(default_factory=lambda: {"ok": True, "state": "waiting", **KEYS})
    serving: bool = False
    stops: int = 0
    on_claim: Callable[[], None] | None = None

    def run(self, args: list[str], stdin: Mapping[str, Any]) -> dict[str, Any]:
        name = " ".join(args)
        self.calls.append(tuple(args))
        self.inputs.append(dict(stdin))
        if args != ["pair", "--session"] and self.on_claim is not None:
            self.on_claim()
        if name in self.fail:
            raise ToolFailed(self.fail[name])
        if args == ["pair", "--session"]:
            return dict(self.keys)
        return {"ok": True, "state": "connected", "agent_id": PAIRING_ID, "user_id": USER_ID}

    def serve(self) -> None:
        if "serve" in self.fail:
            raise ToolFailed(self.fail["serve"])
        self.serving = True

    def stop(self) -> None:
        self.serving = False
        self.stops += 1


@dataclass
class Reloads:
    """The reload of the session's agent (``sessions.reload_session``) as a recorder with planned answers."""

    answers: list[bool] = field(default_factory=lambda: [True])
    calls: list[str] = field(default_factory=list)

    def __call__(self, session_id: str) -> bool:
        self.calls.append(session_id)
        return self.answers.pop(0) if self.answers else True


@pytest.fixture
def tool() -> FakeTool:
    return FakeTool()


@pytest.fixture
def reloads() -> Reloads:
    return Reloads()


@pytest.fixture
def depths() -> dict[str, int | None]:
    return {SESSION_ID: 0, OTHER_ID: 0}


@dataclass
class Ticks:
    """Monotonic time that only ``sleep`` moves; ``on_sleep`` runs at every sleep (a session turning idle)."""

    now: float = 0.0
    slept: list[float] = field(default_factory=list)
    on_sleep: Callable[[], None] | None = None

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def orch(
    context: ControlContext, tool: FakeTool, reloads: Reloads, depths: dict[str, int | None], ticks: Ticks
) -> Orchestrator:
    created = Orchestrator(
        context,
        tool,
        spawn=lambda target: target(),
        reload=reloads,
        sleep=ticks.sleep,
        monotonic=lambda: ticks.now,
        depth=depths.get,
    )
    return orchestrator.install(context, created)


def set_state(context: ControlContext, state: str, session_id: str = SESSION_ID) -> None:
    current = context.state.session(session_id)
    assert current is not None
    context.state.save_session(dataclasses.replace(current, state=state))


def add_session(context: ControlContext, session_id: str = OTHER_ID, state: str = "working") -> Session:
    created = make_session(session_id, OTHER_TOKEN, state)
    context.state.save_session(dataclasses.replace(created, uid=20001))
    return created
