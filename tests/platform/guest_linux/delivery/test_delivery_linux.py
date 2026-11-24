"""U-GST-delivery on Linux (marker ``linux``): real tmux, a TUI dummy and real Claude Code (GST-06).

* TUI dummy (``tui_dummy.py``): a root tmux server on a test socket, one session ``s-<id>`` of 160x48 running the
  dummy; ``delivery.Delivery`` with ``TmuxPane`` types and pastes into it. Short, long and multi-line prompts, a
  trailing ``;`` at a chunk end, a mode character, Unicode and tabs must arrive exactly; ``Escape`` is the interrupt;
  the input box of the dummy is the TUI-ready signal.
* Real agent: Claude Code 2.1.330 in a real session unit (GST-02's rig) against the model double, started without an
  initial prompt: once Claude Code listed the tools of the rig's MCP stand-in and the TUI is up, the pane watcher
  moves the session to ``prompt_delivering`` (HOT-09), then a typed and a pasted multi-line
  prompt are delivered and answered; the transcript holds the pasted text exactly.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.model import ModelDouble
from agent_doubles.model.scenario import load_scenario
from agent_guest import delivery, prompts, sessions, tmux, toolsready
from agent_guest.clock import uuid7
from agent_guest.config import load_config
from agent_guest.control import ControlContext, ControlService
from agent_guest.control_routes import control
from agent_guest.prompts import PromptRegistry
from tests.platform.guest.conftest import make_session
from tests.platform.guest.sessions.conftest import request
from tests.platform.guest_linux.conftest import Guest, ldd_paths
from tests.platform.guest_linux.test_sessions_linux import (
    REPO,
    Rig,
    _Forward,
    _release_platform_port,
    _restore_platform_listener,
    claude_binary,
    make_rig,
    new_id,
    states,
    unit_property,
    wait_for,
)

# real Claude Code in the VM (download on a cold cache, model turns): longer than the 180 s default limit (FIX-01)
pytestmark = [pytest.mark.linux, pytest.mark.timeout(900)]

__all__ = ["claude_binary", "make_rig"]  # fixtures of GST-02's Linux suite, used here

DUMMY = Path(__file__).with_name("tui_dummy.py")
SOCKET = "/run/agent-guest/tmux-gst06.sock"
TOKEN = "Z3N0MDYtbGludXgtY29udHJvbC10b2tlbi0xMjM0NTY3OA"


# -------------------------------------------------------------------------------------------- TUI dummy rig


class Acceptor:
    """Stands in for the UserPromptSubmit and Stop hooks (no hook server in these rigs): every new text that the TUI
    took (``source()``) goes through ``PromptRegistry.check`` like the hook's body, then the turn ends (``idle``) -
    unless ``turns`` is set: then an accepted turn runs until it is interrupted. With ``verdicts`` every result is
    appended there as a JSON line (the TUI dummy puts a blocked submission back into its input like Claude Code)."""

    def __init__(
        self,
        context: ControlContext,
        session_id: Callable[[], str | None],
        source: Callable[[], list[str]],
        verdicts: Path | None = None,
    ):
        self.context = context
        self.session_id = session_id
        self.source = source
        self.verdicts = verdicts
        self.turns = False
        self.results: list[tuple[str, bool]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        seen = 0
        while not self.stop.wait(0.05):
            sid, texts = self.session_id(), self.source()
            for text in texts[seen:]:
                assert sid is not None
                accepted = PromptRegistry(self.context.journal, self.context.state).check(sid, text)
                self.results.append((text, accepted))
                if self.verdicts is not None:
                    with self.verdicts.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"text": text, "accepted": accepted}, ensure_ascii=False) + "\n")
                if not (accepted and self.turns):
                    control.mark_idle(self.context, sid, None)
            seen = len(texts)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)


class DummyRig:
    def __init__(self, context: ControlContext, server: tmux.Tmux, output: Path) -> None:
        self.context = context
        self.server = server
        self.output = output
        self.engine = delivery.install(context, delivery.Delivery(context, delivery.TmuxPane(server)))
        self.current: str | None = None
        self.restore_delay = 0.0
        self.acceptor = Acceptor(context, lambda: self.current, self.submitted, Path(f"{output}.verdicts"))

    def submitted(self) -> list[str]:
        return [event["submit"] for event in self.events() if "submit" in event]

    def start(self, session_id: str) -> None:
        self.current = session_id
        command = [sys.executable, "-I", str(DUMMY), str(self.output), str(self.restore_delay)]
        self.server.new_session(tmux.session_name(session_id), command)
        wait_for(lambda: self.box(session_id) is not None, 10, lambda: self.screen(session_id))

    def screen(self, session_id: str) -> str:
        try:
            return self.server.capture(tmux.session_name(session_id))
        except tmux.TmuxError:
            return ""

    def box(self, session_id: str) -> str | None:
        return delivery.input_box(self.screen(session_id))

    def events(self) -> list[dict[str, Any]]:
        if not self.output.exists():
            return []
        return [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def dummy(tmp_path: Path) -> Iterator[DummyRig]:
    token = tmp_path / "control-token"
    token.write_text(TOKEN + "\n")
    config = load_config(
        {
            "AGENT_GUEST_CONTROL_SOCKET": str(tmp_path / "control.sock"),
            "AGENT_GUEST_API_SOCKET": str(tmp_path / "api.sock"),
            "AGENT_GUEST_CONTROL_GROUP": "root",
            "AGENT_GUEST_API_GROUP": "root",
            "AGENT_GUEST_CONTROL_TOKEN_FILE": str(token),
            "AGENT_GUEST_JOURNAL": str(tmp_path / "state" / "journal.db"),
            "AGENT_GUEST_BUILD_FILE": str(tmp_path / "missing-build.json"),
            "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(tmp_path / "missing-install.json"),
            "AGENT_GUEST_CONTRACTS": str(REPO / "contracts"),
        }
    )
    service = ControlService(config)
    context = service.start()
    DUMMY.chmod(0o644)
    rig = DummyRig(context, tmux.Tmux(socket=SOCKET, scope=False), tmp_path / "submitted.jsonl")
    try:
        yield rig
    finally:
        rig.engine.close()
        rig.acceptor.close()
        service.stop()
        subprocess.run(["tmux", "-S", SOCKET, "kill-server"], capture_output=True, check=False)


def ready_session(rig: DummyRig, state: str = "idle") -> str:
    session_id = new_id()
    rig.context.state.save_session(replace(make_session(state=state), id=session_id))
    rig.start(session_id)
    return session_id


def deliver(rig: DummyRig, session_id: str, text: str) -> bool:
    prompt_id = uuid7(time.time())
    PromptRegistry(rig.context.journal, rig.context.state).register(session_id, prompt_id, text)
    return rig.engine.deliver(session_id, prompt_id, text)


SHORT = "Summarise the repository; list each module with one line. Unicode: äöü ✓ 中文;"
LONG_LINE = "x" * 39 + ";" + "Read the README and the CONTRIBUTING file, then explain the build. " * 20
MULTI = "First line of the task.\n\n\tIndented with a tab; and a semicolon;\n" + "\n".join(
    f"Line {n}: {'detail ' * 12}" for n in range(60)
)


@pytest.mark.parametrize(
    "text",
    [SHORT, "x" * 39 + ";" + "y" * 39 + "\\;" + "z" * 20, LONG_LINE, MULTI, "?typed this would open the shortcuts"],
    ids=["short", "semicolons", "long-line", "multi-line", "mode-character"],
)
def test_prompts_arrive_exactly(dummy: DummyRig, text: str) -> None:
    session_id = ready_session(dummy)
    started = time.monotonic()
    assert deliver(dummy, session_id, text)
    elapsed = time.monotonic() - started
    assert dummy.events() == [{"submit": text}]
    assert dummy.box(session_id) == ""
    print(f"delivered {len(text)} characters ({'typed' if delivery.typed(text) else 'pasted'}) in {elapsed:.2f} s")


def test_two_prompts_in_a_row_arrive_in_order(dummy: DummyRig) -> None:
    session_id = ready_session(dummy)
    assert deliver(dummy, session_id, "First prompt.")
    assert deliver(dummy, session_id, MULTI)
    assert dummy.events() == [{"submit": "First prompt."}, {"submit": MULTI}]


def test_leftover_input_is_cleared_before_typing(dummy: DummyRig) -> None:
    session_id = ready_session(dummy)
    dummy.server.send_keys(tmux.session_name(session_id), "typed by somebody", literal=True)
    wait_for(lambda: dummy.box(session_id) == "typed by somebody", 5)
    assert deliver(dummy, session_id, "Only this arrives.")
    assert dummy.events() == [{"submit": "Only this arrives."}]


# Prompts of LVT-01's interrupt step (agent-core e2e/platform/live/live-01-openrouter.spec.ts): a plain multi-line
# prompt that Claude Code shows uncollapsed (two line breaks, 750 characters: six visual rows at the pane width) is
# interrupted, then a prompt with a leading ``?`` (pasted) follows.
INTERRUPTED = "\n".join(
    [
        "LIVE01-LONG: Write a numbered list of 300 different animals, one per line. " + "Explain each one. " * 12,
        "Use one sentence about each animal and keep the order of the alphabet. " + "Be precise. " * 14,
        "Do not use any tool. " * 7,
    ]
)
FOLLOW_UP = "? LIVE01-PREFIX: reply with the single word QMARK-OK and nothing else."


def interrupted_turn(rig: DummyRig) -> str:
    """A session whose long turn the user interrupted: the dummy puts the turn's text back into its input box."""
    session_id = ready_session(rig)
    rig.acceptor.turns = True
    assert deliver(rig, session_id, INTERRUPTED)
    rig.acceptor.turns = False
    control.interrupt(rig.context, session_id)
    return session_id


def test_a_prompt_after_an_interrupt_with_the_text_put_back_arrives(dummy: DummyRig) -> None:
    """NA-01 (AUD-02, LVT-01): the interrupted prompt is back in the input box, six rows; three ``C-e C-u`` left part
    of it there and the next prompt was submitted mixed with it (``unregistered``, then ``delivery_failed``)."""
    session_id = interrupted_turn(dummy)
    wait_for(lambda: dummy.box(session_id) not in (None, ""), 5, lambda: dummy.screen(session_id))
    assert deliver(dummy, session_id, FOLLOW_UP)
    assert dummy.submitted() == [INTERRUPTED, FOLLOW_UP]
    assert dummy.acceptor.results[-1] == (FOLLOW_UP, True)
    assert "prompt_refused" not in json.dumps(dummy.context.journal.read(after=0, limit=1000)[0])


@pytest.mark.parametrize("delay", [0.05, 0.15, 0.3])
def test_text_put_back_late_after_an_interrupt_is_never_submitted_with_the_prompt(
    dummy: DummyRig, delay: float
) -> None:
    """The interrupt marks the session ``idle`` when it sent ``Escape``; Claude Code may put the interrupted text back
    only while the next prompt is typed. Whether it arrives before the input was checked or after ``Enter`` (blocked by
    the hook and put back as well), the prompt is delivered alone."""
    dummy.restore_delay = delay
    session_id = interrupted_turn(dummy)
    assert deliver(dummy, session_id, FOLLOW_UP)
    assert dummy.submitted()[-1] == FOLLOW_UP
    assert dummy.acceptor.results[-1] == (FOLLOW_UP, True)
    refusals = [
        record["body"]
        for record in dummy.context.journal.read(after=0, limit=1000)[0]
        if record["body"].get("state") == "prompt_refused"
    ]
    assert all(body["reason"] == "unregistered" for body in refusals), refusals
    assert len(refusals) <= delivery.RETRIES, refusals


def test_interrupt_is_one_escape(dummy: DummyRig) -> None:
    session_id = ready_session(dummy, "working")
    control.interrupt(dummy.context, session_id)
    wait_for(lambda: dummy.events() == [{"escape": True}], 5, lambda: str(dummy.events()))
    session = dummy.context.state.session(session_id)
    assert session is not None
    assert session.state == "idle"


def test_the_ready_watcher_sees_the_input_box(dummy: DummyRig) -> None:
    session_id = ready_session(dummy, "agent_starting")
    toolsready.mark(dummy.context, session_id, 0)  # HOT-09: the agent listed the platform tools
    assert dummy.engine.watch_ready(session_id)
    assert states(dummy.context, session_id) == ["prompt_delivering"]


def test_a_command_like_prompt_never_reaches_the_pane(dummy: DummyRig) -> None:
    session_id = ready_session(dummy)
    assert not deliver(dummy, session_id, "!touch /tmp/pwned")
    assert not deliver(dummy, session_id, "/exit")
    assert dummy.events() == []
    assert dummy.box(session_id) == ""


def test_a_pane_that_is_gone_refuses_the_prompt(dummy: DummyRig) -> None:
    session_id = ready_session(dummy)
    dummy.engine.wait_seconds = 0.5
    dummy.server.kill_session(tmux.session_name(session_id))
    assert not deliver(dummy, session_id, "Nobody listens.")
    records, _ = dummy.context.journal.read(after=0, limit=100)
    assert records[-1]["body"]["state"] == "prompt_refused"
    assert records[-1]["body"]["reason"] == "delivery_failed"


# -------------------------------------------------------------------------------------------- real Claude Code

SCENARIO = {
    "name": "gst-06-delivery",
    "description": "A typed prompt and a pasted multi-line prompt, each answered with text.",
    "steps": [
        {
            "id": "typed",
            "expect": {"user_text_contains": "2+2", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "2+2 is 4."}]},
        },
        {
            "id": "pasted",
            "expect": {"user_text_contains": "ZEBRA-7781", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "Pasted prompt received."}]},
        },
    ],
    "side": [{"id": "title", "expect": {"has_tools": False}, "reply": {"content": [{"type": "text", "text": "T"}]}}],
}
PASTED = "Please read the following notes (ZEBRA-7781).\n" + "\n".join(
    f"- note {n}: {'keep this line exactly; ' * 4}" for n in range(30)
)


INTERRUPT_SCENARIO = {
    "name": "kor-09-interrupt",
    "description": "A long turn that is interrupted before any answer, then a prompt with a leading '?'.",
    "steps": [
        {
            "id": "long",
            "expect": {"user_text_contains": "LIVE01-LONG", "has_tools": True, "stream": True},
            "delay_ms": 60_000,
            "reply": {"content": [{"type": "text", "text": "Never seen: the turn is interrupted."}]},
        },
        {
            "id": "follow-up",
            "expect": {"user_text_contains": "LIVE01-PREFIX", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "Follow-up answered: ZEBRA-4410."}]},
        },
    ],
    "side": SCENARIO["side"],
}


@pytest.fixture
def scenario() -> dict[str, Any]:
    return SCENARIO


@pytest.fixture
def delivery_gateway(tmp_path: Path, scenario: dict[str, Any]) -> Iterator[ModelDouble]:
    path = tmp_path / "delivery-scenario.json"
    path.write_text(json.dumps(scenario))
    double = ModelDouble(load_scenario(path))
    double.start()
    _release_platform_port()
    _Forward.upstream = ("127.0.0.1", int(double.url.rsplit(":", 1)[1]))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 7700), _Forward)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True)
    thread.start()
    try:
        yield double
    finally:
        server.shutdown()
        server.server_close()
        double.stop()
        _restore_platform_listener()


def user_texts(transcript: Path) -> list[str]:
    found = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content")
        if isinstance(content, str):
            found.append(content)
        elif isinstance(content, list):
            found.extend(part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text")
    return found


def answered(rig: Rig, sid: str, answer: str) -> Callable[[], bool]:
    return lambda: answer in rig.pane(sid)


def test_real_claude_code_takes_typed_and_pasted_prompts(
    make_rig: Callable[[str, tuple[str, ...]], Rig],
    guest: Guest,
    claude_binary: str,
    delivery_gateway: ModelDouble,
) -> None:
    ctypes_so = next(Path(guest.lib_dynload).glob("_ctypes.cpython-*.so"))
    rig = make_rig(claude_binary, tuple(ldd_paths(claude_binary, guest.python, str(ctypes_so))))
    sid = new_id()
    body = request(
        session_id=sid,
        model={"access": "platform", "model_id": "double-model", "env": {"ANTHROPIC_MODEL": "double-model"}},
    )
    del body["initial_prompt"]
    reply = rig.client.request("POST", "/v1/sessions", body)
    assert reply.status == 200, reply.body

    def describe() -> str:
        state = json.dumps(delivery_gateway.state(), indent=1)[:4000]
        return f"pane:\n{rig.pane(sid)}\nstates: {states(rig.context, sid)}\ndouble:\n{state}"

    started = time.monotonic()
    assert sessions.credential_provisioned(rig.context, sid)
    wait_for(lambda: "prompt_delivering" in states(rig.context, sid), 30, describe)
    ready = time.monotonic() - started
    transcripts = Path(f"/srv/sessions/{sid}/home/.claude/projects")

    def taken() -> list[str]:
        return [text for path in sorted(transcripts.rglob("*.jsonl")) for text in user_texts(path)]

    acceptor = Acceptor(rig.context, lambda: sid, taken)
    for text, answer in (("What is 2+2?", "2+2 is 4."), (PASTED, "Pasted prompt received.")):
        reply = rig.client.request(
            "POST", f"/v1/sessions/{sid}/prompts", {"prompt_id": uuid7(time.time()), "text": text}
        )
        assert (reply.status, reply.json()) == (200, {"state": "registered"})
        wait_for(answered(rig, sid, answer), 90, describe)
    print(f"TUI ready (prompt_delivering) {ready:.2f} s after the start; pane:\n{rig.pane(sid)}")
    assert delivery_gateway.state()["ok"], describe()
    wait_for(lambda: len(acceptor.results) == 2, 20, lambda: str(acceptor.results))
    acceptor.close()
    # The texts Claude Code took match their registered hashes (what the UserPromptSubmit hook checks), the pasted one
    # included; nothing was refused.
    assert [(prompts.normalise(text), ok) for text, ok in acceptor.results] == [
        ("What is 2+2?", True),
        (prompts.normalise(PASTED), True),
    ]
    assert "prompt_refused" not in json.dumps(rig.context.journal.read(after=0, limit=1000)[0])
    assert unit_property(sid, "ActiveState") == "active"
    delivery.manager(rig.context).close()


@pytest.mark.parametrize("scenario", [INTERRUPT_SCENARIO], ids=["interrupt"])
def test_real_claude_code_takes_the_prompt_after_an_interrupt(
    make_rig: Callable[[str, tuple[str, ...]], Rig],
    guest: Guest,
    claude_binary: str,
    delivery_gateway: ModelDouble,
) -> None:
    """NA-01 against the real TUI: Claude Code 2.1.330 puts an interrupted prompt back into its input box (here the
    plain three-line prompt, shown on several rows); the next prompt arrives alone and is answered."""
    ctypes_so = next(Path(guest.lib_dynload).glob("_ctypes.cpython-*.so"))
    rig = make_rig(claude_binary, tuple(ldd_paths(claude_binary, guest.python, str(ctypes_so))))
    sid = new_id()
    body = request(
        session_id=sid,
        model={"access": "platform", "model_id": "double-model", "env": {"ANTHROPIC_MODEL": "double-model"}},
    )
    del body["initial_prompt"]
    reply = rig.client.request("POST", "/v1/sessions", body)
    assert reply.status == 200, reply.body

    def describe() -> str:
        return f"pane:\n{rig.pane(sid)}\nstates: {states(rig.context, sid)}"

    assert sessions.credential_provisioned(rig.context, sid)
    wait_for(lambda: "prompt_delivering" in states(rig.context, sid), 30, describe)
    transcripts = Path(f"/srv/sessions/{sid}/home/.claude/projects")

    def taken() -> list[str]:
        texts = [text for path in sorted(transcripts.rglob("*.jsonl")) for text in user_texts(path)]
        return [text for text in texts if not text.startswith("[Request interrupted")]

    acceptor = Acceptor(rig.context, lambda: sid, taken)
    acceptor.turns = True

    def send(text: str) -> None:
        reply = rig.client.request(
            "POST", f"/v1/sessions/{sid}/prompts", {"prompt_id": uuid7(time.time()), "text": text}
        )
        assert (reply.status, reply.json()) == (200, {"state": "registered"})

    send(INTERRUPTED)
    wait_for(lambda: len(acceptor.results) == 1, 30, lambda: f"{acceptor.results}\n{describe()}")
    # The turn waits for the model double's slow step (LVT-01 stopped it three seconds after `working`).
    wait_for(lambda: "long" in delivery_gateway.state()["consumed"], 30, describe)
    acceptor.turns = False
    control.interrupt(rig.context, sid)
    assert states(rig.context, sid)[-1] == "idle"
    # Claude Code puts the interrupted prompt back into its input box: the case under test.
    wait_for(lambda: "LIVE01-LONG" in (delivery.input_box(rig.pane(sid)) or ""), 10, describe)
    send(FOLLOW_UP)
    wait_for(answered(rig, sid, "Follow-up answered: ZEBRA-4410."), 90, describe)
    wait_for(lambda: len(acceptor.results) == 2, 20, lambda: str(acceptor.results))
    acceptor.close()
    print(f"pane after the follow-up:\n{rig.pane(sid)}")
    assert [(prompts.normalise(text), ok) for text, ok in acceptor.results] == [
        (prompts.normalise(INTERRUPTED), True),
        (FOLLOW_UP, True),
    ]
    refused = [
        record["body"]
        for record in rig.context.journal.read(after=0, limit=1000)[0]
        if record["body"].get("state") == "prompt_refused"
    ]
    assert refused == [], refused
    delivery.manager(rig.context).close()
