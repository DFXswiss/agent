"""U-RUN-sessions (KOR-46, ADR-083 section 1): the first prompt of a start carries its sender line; an older guest that
refuses the unknown field (`400 request_invalid`) gets the same request once more without it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.start import Starter, without_speaker

from ..reconcile.rig import Rig, standard_world
from .kit import ORG, WG, FakeGuest, desired, kit, session_id

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")

LINE = "agent.dfx.swiss instruction from: an unnamed member (member x)."
PROMPT = InitialPrompt(prompt_id="01930006-0000-7000-8000-000000000001", text="What is 2+2?", speaker=LINE)


class OldGuest(FakeGuest):
    """Refuses `speaker` like a golden without the field (the request schema is closed)."""

    async def create_session(self, request: Any) -> dict[str, Any]:
        self._call("create_session", dict(request))
        if "speaker" in request.get("initial_prompt", {}):
            raise GuestRefused("sessions", 400, "request_invalid")
        return dict(self.create_answer)


def run(rig: Rig, body: Any) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(env)

    asyncio.run(main())


def test_the_initial_prompt_carries_the_line() -> None:
    assert PROMPT.body() == {"prompt_id": PROMPT.prompt_id, "text": PROMPT.text, "speaker": LINE}
    assert InitialPrompt("p", "t").body() == {"prompt_id": "p", "text": "t"}


def test_an_older_guest_gets_the_start_again_without_the_line(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env, guest=OldGuest(), prompt=PROMPT)
        report = await Starter(found.session_env).start(desired())
        assert report.started
        first, second = [value for name, value in found.guest.calls if name == "create_session"]
        assert first["initial_prompt"]["speaker"] == LINE
        assert second["initial_prompt"] == {"prompt_id": PROMPT.prompt_id, "text": PROMPT.text}
        assert {key: value for key, value in second.items() if key != "initial_prompt"} == {
            key: value for key, value in first.items() if key != "initial_prompt"
        }
        vm = (await found.vms.ensure_vm(ORG, WG)).id
        assert f"runner.prompts speaker_unsupported vm_id={vm} session_id={session_id()} channel=start" in found.lines
        alerts = [item for item in await found.of_kind("audit") if item["action"] == "runner.alert"]
        assert alerts == [
            {
                "action": "runner.alert",
                "result": "failed",
                "target_kind": "vm",
                "target_id": vm,
                "detail": {"alert": "speaker_unsupported", "value": None},
            }
        ]
        assert SCHEMAS.errors("outbox/objects.json#/$defs/body_audit", alerts[0]) == []

    run(rig, body)


def test_the_line_is_dropped_once_only(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env, guest=OldGuest(), prompt=PROMPT)
        found.guest.fail["create_session"] = GuestRefused("sessions", 400, "request_invalid")
        report = await Starter(found.session_env).start(desired())
        assert (report.started, report.reason) == (False, "sandbox_failed")
        assert found.guest.names().count("create_session") == 2
        assert sum("speaker_unsupported" in line for line in found.lines) == 1

    run(rig, body)


@pytest.mark.parametrize(
    ("request_body", "refused"),
    [
        ({"session_id": "s"}, GuestRefused("sessions", 400, "request_invalid")),
        ({"initial_prompt": {"prompt_id": "p", "text": "t"}}, GuestRefused("sessions", 400, "request_invalid")),
        ({"initial_prompt": {"prompt_id": "p", "text": "t", "speaker": LINE}}, GuestRefused("sessions", 409, "x")),
        (
            {"initial_prompt": {"prompt_id": "p", "text": "t", "speaker": LINE}},
            GuestRefused("sessions", 400, "sandbox_failed"),
        ),
    ],
)
def test_other_refusals_are_not_taken_for_an_older_guest(request_body: Any, refused: GuestRefused) -> None:
    assert without_speaker(request_body, refused) is None


def test_the_line_is_the_only_field_dropped() -> None:
    prompt = {"prompt_id": "p", "text": "t", "speaker": LINE}
    request = {"session_id": "s", "initial_prompt": prompt}
    assert without_speaker(request, GuestRefused("sessions", 400, "request_invalid")) == {
        "session_id": "s",
        "initial_prompt": {"prompt_id": "p", "text": "t"},
    }
    assert prompt["speaker"] == LINE  # the original stays as it was
