"""U-GST-delivery: prompt delivery into the TUI, verification, refusal and the TUI-ready watcher (GST-06).

The control process of the guest fixtures is real (journal, session cache, prompt registry, schemas); the pane is a
small TUI simulation (``FakeTui``) and time is simulated (``sleep`` advances a counter), so the tests never wait.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from agent_guest import delivery, sessions, tmux, toolsready
from agent_guest.control import ControlContext
from agent_guest.control_routes import prompts as control_prompts
from agent_guest.driver import ClaudeCodeDriver
from agent_guest.journal import JournalError
from agent_guest.prompts import PromptRegistry
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, ControlClient, make_session
from tests.platform.guest.sessions.conftest import FakeSystem, TmuxRunner

PROMPT_ID = "01a1ee56-5e00-7007-8f1b-340691d99f07"
SECOND_ID = "01a1ee56-5e00-7007-8f1b-340691d99f08"
NAME = f"s-{SESSION_ID}"
CLAUDE_ID = "5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69"
RULE = "─" * 40
MARK = "\u276f"


def screen(box: list[str], above: str = "Welcome to Claude Code") -> str:
    return "\n".join([above, RULE, f"{MARK} {box[0]}", *box[1:], RULE, "  ? for shortcuts"])


# -------------------------------------------------------------------------------------------- fake TUI


@dataclass
class FakeTui:
    """A pane with Claude Code's input box: keys edit it, ``Enter`` submits (and runs the UserPromptSubmit check).

    Like Claude Code 2.1.330 (KOR-09): ``C-u`` removes the last line of shown text (a collapsed paste whole), and with
    ``put_back_refused`` a submission the hook refused goes back in front of the input; ``put_back(text)`` is a text
    the TUI puts back (an interrupted prompt)."""

    context: ControlContext
    hook: bool = True
    ignore_enters: int = 0
    box: bool = True
    fail: dict[str, BaseException] = field(default_factory=dict)
    on_capture: Callable[[int], None] | None = None
    on_enter: Callable[[], None] | None = None
    put_back_refused: bool = False
    text: str = ""
    pasted: bool = False
    submitted: list[str] = field(default_factory=list)
    keys: list[tuple[str, ...]] = field(default_factory=list)
    captures: int = 0

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            raise self.fail[what]

    def send_keys(self, name: str, *keys: str, literal: bool = False) -> None:
        assert name == NAME
        self._maybe_fail("Enter" if keys == ("Enter",) else "send_keys")
        self.keys.append(("literal", *keys) if literal else keys)
        if literal:
            (chunk,) = keys
            self.text += chunk[:-2] + ";" if chunk.endswith("\\;") else chunk
            return
        for key in keys:
            if key == "C-u":
                if self.pasted or "\n" not in self.text:
                    self.text, self.pasted = "", False
                else:
                    self.text = self.text[: self.text.rindex("\n")]
            elif key == "Enter":
                self._enter()

    def _enter(self) -> None:
        if self.on_enter is not None:
            self.on_enter()
        if self.ignore_enters:
            self.ignore_enters -= 1
            return
        submitted = self.text
        self.submitted.append(submitted)
        self.text, self.pasted = "", False
        if self.hook:
            accepted = PromptRegistry(self.context.journal, self.context.state).check(SESSION_ID, submitted)
            if not accepted and self.put_back_refused:
                self.put_back(submitted)

    def put_back(self, text: str) -> None:
        self.text, self.pasted = text + self.text, False

    def paste(self, name: str, text: str) -> None:
        assert name == NAME
        self._maybe_fail("paste")
        self.keys.append(("paste", text))
        self.text += text
        self.pasted = True

    def capture(self, name: str) -> str:
        assert name == NAME
        self.captures += 1
        if self.on_capture is not None:
            self.on_capture(self.captures)
        self._maybe_fail("capture")
        if not self.box:
            return "Starting...\n"
        if self.pasted and "\n" in self.text:
            return screen([f"[Pasted text #1 +{self.text.count(chr(10))} lines]"])
        return screen(self.text.split("\n"))


@dataclass
class Clock:
    """Simulated time of ``Delivery.sleep``; ``on_sleep`` runs after every pause."""

    slept: list[float] = field(default_factory=list)
    on_sleep: Callable[[float], None] | None = None

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(sum(self.slept))


def inline(work: Callable[[], object]) -> None:
    work()


@pytest.fixture
def tui(context: ControlContext) -> FakeTui:
    return FakeTui(context)


@pytest.fixture
def clock_() -> Clock:
    return Clock()


@pytest.fixture
def make(context: ControlContext, tui: FakeTui, clock_: Clock) -> Callable[..., delivery.Delivery]:
    def build(**changes: Any) -> delivery.Delivery:
        options: dict[str, Any] = {"sleep": clock_, "spawn": inline, **changes}
        return delivery.install(context, delivery.Delivery(context, tui, **options))

    return build


@pytest.fixture
def engine(make: Callable[..., delivery.Delivery]) -> delivery.Delivery:
    return make()


def put(context: ControlContext, state: str, claude_session_id: str | None = None) -> Session:
    session = replace(make_session(state=state), claude_session_id=claude_session_id)
    context.state.save_session(session)
    return session


def set_state(context: ControlContext, state: str) -> None:
    current = context.state.session(SESSION_ID)
    assert current is not None
    context.state.save_session(replace(current, state=state))


def register(context: ControlContext, text: str, prompt_id: str = PROMPT_ID) -> None:
    PromptRegistry(context.journal, context.state).register(SESSION_ID, prompt_id, text)


def tools_listed(context: ControlContext) -> None:
    """The session's agent listed the platform tools (what ``/mcp`` reports, HOT-09)."""
    session = context.state.session(SESSION_ID)
    assert session is not None
    toolsready.mark(context, SESSION_ID, 17, session.token_sha256)


def kinds(context: ControlContext) -> list[str]:
    records, _ = context.journal.read(after=0, limit=1000)
    return [record["kind"] for record in records]


def journal(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(after=0, limit=1000)
    return [record["body"] for record in records if record["kind"] == "session.state"]


def refused(prompt_id: str = PROMPT_ID) -> dict[str, Any]:
    return {"state": "prompt_refused", "prompt_id": prompt_id, "reason": "delivery_failed"}


def without_at(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in body.items() if key != "at"} for body in bodies]


def registered(context: ControlContext, prompt_id: str = PROMPT_ID) -> bool:
    with context.journal.transaction() as db:
        row = db.execute("SELECT 1 FROM prompt_registry WHERE prompt_id = ?", (prompt_id,)).fetchone()
    return row is not None


# -------------------------------------------------------------------------------------------- pure helpers


def test_input_box_is_the_last_marker_line_below_a_rule() -> None:
    assert delivery.input_box(screen(["hello"])) == "hello"
    assert delivery.input_box(screen([""])) == ""
    # A marker in the conversation above is not the input box; continuation lines belong to it up to the next rule.
    text = "\n".join([f"{MARK} old prompt", "answer", RULE, f"{MARK} first line", "  second   line", RULE, "x"])
    assert delivery.input_box(text) == "first line second   line"
    # Without the closing rule the box reaches to the end of the screen.
    assert delivery.input_box(f"{RULE}\n  {MARK} typed\n  more") == "typed more"


@pytest.mark.parametrize("text", ["", "Starting...", f"{MARK} no rule above", f"{MARK} x\n{RULE}"])
def test_no_input_box(text: str) -> None:
    assert delivery.input_box(text) is None


def test_typed_or_pasted() -> None:
    assert delivery.typed("Summarise the repository.")
    assert delivery.typed("x" * 200)
    assert not delivery.typed("x" * 201)
    assert not delivery.typed("two\nlines")
    assert not delivery.typed("tab\there")
    for char in "!#/?&":
        assert not delivery.typed(f"{char}rm -rf /")
    assert delivery.typed("a!b")


@pytest.mark.parametrize("char", ["\x00", "\x1b", "\r", "\x7f", "\x9b", "\x08", "\x0b"])
def test_control_characters_are_not_deliverable(char: str) -> None:
    assert not delivery.deliverable(f"text{char}more")


def test_lone_surrogates_are_not_deliverable() -> None:
    # subprocess would encode them with surrogateescape: U+DCC2 U+DC9B would reach tmux as C2 9B, a C1 CSI.
    assert not delivery.deliverable("a\udcc2\udc9bb")


def test_newline_tab_and_unicode_are_deliverable() -> None:
    assert delivery.deliverable("Zeile 1\n\tZeile 2 \u2013 \u2713 \u4e2d\u6587 \U0001f642")


def test_literal_chunks_split_and_escape_a_trailing_semicolon() -> None:
    assert delivery.literal_chunks("abcdefgh", 3) == ["abc", "def", "gh"]
    assert delivery.literal_chunks("ab;cd;", 3) == ["ab\\;", "cd\\;"]
    assert delivery.literal_chunks("a\\;", 3) == ["a\\\\;"]
    assert delivery.literal_chunks("", 3) == []


def test_marker_is_the_collapsed_start_of_the_first_line() -> None:
    assert (
        delivery.marker("  Fix   the\tbug in parser.py please, and then also\nsecond") == "Fix the bug in parser.py ple"
    )
    assert delivery.marker("short\nsecond") == "short"


# -------------------------------------------------------------------------------------------- delivery


def test_a_short_prompt_is_typed_in_chunks_and_accepted(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    text = "Summarise the repository and list every module; then propose three improvements now."
    register(context, text)
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    # The input box reads empty: nothing to clear.
    chunks = [("literal", text[0:40]), ("literal", text[40:80]), ("literal", text[80:])]
    assert tui.keys == [*chunks, ("Enter",)]
    assert tui.submitted == [text]
    assert clock_.slept[:4] == [0.04, 0.04, 0.04, 0.3]
    assert without_at(journal(context)) == [{"state": "prompt_accepted", "prompt_id": PROMPT_ID}]
    session = context.state.session(SESSION_ID)
    assert session is not None
    assert session.state == "working"


def test_a_long_multi_line_prompt_is_pasted(context: ControlContext, engine: delivery.Delivery, tui: FakeTui) -> None:
    put(context, "prompt_delivering")
    text = "Line one of a long prompt\n" + "more text " * 50
    register(context, text)
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.keys == [("paste", text), ("Enter",)]
    assert tui.submitted == [text]


@pytest.mark.parametrize("text", ["?what does this do", "#remember this", "/src/main.py: explain it", "see @README"])
def test_a_prompt_with_a_mode_or_completion_character_is_pasted(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, text: str
) -> None:
    put(context, "idle")
    register(context, text)
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert ("paste", text) in tui.keys
    assert not any(key[0] == "literal" for key in tui.keys)


@pytest.mark.parametrize("text", ["!cat /etc/shadow", "  !ls", "/exit", "\n/config set x", "/mcp__platform__x arg"])
def test_a_prompt_that_looks_like_a_command_is_refused(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, text: str
) -> None:
    put(context, "idle")
    register(context, text)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.keys == []
    assert without_at(journal(context)) == [refused()]


def test_a_semicolon_at_a_chunk_end_arrives_literally(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    text = "x" * 39 + ";" + "y" * 39 + "\\;"
    register(context, text)
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == [text]
    assert tui.keys[0] == ("literal", "x" * 39 + "\\;")


def test_control_characters_are_refused_without_touching_the_pane(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    text = "harmless\x1b[201~\rinjected"
    register(context, text)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.keys == []
    assert without_at(journal(context)) == [refused()]
    assert not registered(context)


def test_verification_by_the_pane_retries_enter(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    tui.ignore_enters = 2
    register(context, "Explain the build.")
    assert engine.deliver(SESSION_ID, PROMPT_ID, "Explain the build.")
    assert tui.keys.count(("Enter",)) == 3
    assert tui.submitted == ["Explain the build."]
    assert [body["state"] for body in journal(context)] == ["prompt_accepted"]


def test_a_prompt_the_hook_never_accepts_is_refused(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    tui.hook = False
    register(context, "Explain the build.")
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Explain the build.")
    # The text left the input (verified by the pane), but nobody accepted it within ten seconds.
    assert tui.submitted == ["Explain the build."]
    assert clock_.slept.count(0.05) >= 200
    assert without_at(journal(context)) == [refused()]
    assert not registered(context)


def test_a_paste_that_stays_as_placeholder_is_refused_after_three_retries(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    tui.ignore_enters = 10
    text = "first\nsecond"
    register(context, text)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.keys.count(("Enter",)) == 4
    # The input is cleared afterwards, the registration withdrawn: the text can never be accepted later.
    assert tui.keys[-3:] == [("C-e", "C-u")] * 3
    assert tui.text == ""
    assert without_at(journal(context)) == [refused()]
    assert not registered(context)
    # Each check polls for one second.
    assert clock_.slept.count(0.05) == 80


def test_a_typed_prompt_that_stays_in_the_input_is_refused(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    tui.ignore_enters = 10
    register(context, "Stays in the input line.")
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Stays in the input line.")
    assert without_at(journal(context)) == [refused()]


def test_an_input_box_that_disappears_is_not_verified(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    tui.hook = False

    def hide() -> None:
        tui.box = False

    tui.on_enter = hide
    register(context, "Hello.")
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert without_at(journal(context)) == [refused()]


@pytest.mark.parametrize(
    ("what", "error"),
    [
        ("send_keys", tmux.TmuxError("gone")),
        ("Enter", OSError("x")),
        ("paste", subprocess.TimeoutExpired("tmux", 10)),
    ],
)
def test_a_tmux_failure_refuses_the_prompt(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, what: str, error: BaseException
) -> None:
    put(context, "idle")
    text = "two\nlines" if what == "paste" else "Hello."
    register(context, text)
    tui.fail[what] = error
    assert not engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert without_at(journal(context)) == [refused()]


def test_an_accepted_or_unknown_prompt_is_not_delivered_again(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "working")
    register(context, "Done before.")
    PromptRegistry(context.journal, context.state).check(SESSION_ID, "Done before.")
    assert engine.deliver(SESSION_ID, PROMPT_ID, "Done before.")
    assert engine.deliver(SESSION_ID, SECOND_ID, "Never registered.")
    assert tui.keys == []


def test_a_prompt_waits_while_the_session_works(
    context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui, clock_: Clock
) -> None:
    put(context, "working")
    register(context, "Next step.")
    engine = make(wait_seconds=1.0)

    def finish(elapsed: float) -> None:
        if elapsed >= 30.0:
            set_state(context, "idle")

    clock_.on_sleep = finish
    # Working time does not count against the wait limit (1 s here).
    assert engine.deliver(SESSION_ID, PROMPT_ID, "Next step.")
    assert sum(clock_.slept) >= 30.0
    assert tui.submitted == ["Next step."]


@pytest.mark.parametrize("state", ["sandbox_ready", "agent_starting", "suspended"])
def test_a_session_that_does_not_become_ready_refuses_after_the_limit(
    context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui, clock_: Clock, state: str
) -> None:
    put(context, state)
    register(context, "Hello.")
    engine = make(wait_seconds=0.5)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert sum(clock_.slept) == pytest.approx(0.55)
    assert tui.keys == []
    assert without_at(journal(context)) == [refused()]


def test_a_ready_state_without_an_input_box_waits(
    context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    tui.fail["capture"] = tmux.TmuxError("no pane")
    engine = make(wait_seconds=0.2)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert without_at(journal(context)) == [refused()]
    tui.fail.clear()
    tui.box = False
    assert engine._box(SESSION_ID) is None


@pytest.mark.parametrize("state", ["stopping", "stopped", "failed"])
def test_an_ended_session_refuses(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock, state: str
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    set_state(context, state)
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert clock_.slept == []
    assert without_at(journal(context)) == [refused()]


def test_a_session_suspended_between_check_and_lock_is_waited_for_again(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    register(context, "After the resume.")

    def suspend_once(count: int) -> None:
        if count == 1:
            set_state(context, "suspended")

    tui.on_capture = suspend_once

    def resume(elapsed: float) -> None:
        set_state(context, "idle")

    clock_.on_sleep = resume
    assert engine.deliver(SESSION_ID, PROMPT_ID, "After the resume.")
    assert tui.submitted == ["After the resume."]


def test_a_refusal_never_overrides_an_acceptance(context: ControlContext, engine: delivery.Delivery) -> None:
    put(context, "idle")
    register(context, "Accepted.")
    PromptRegistry(context.journal, context.state).check(SESSION_ID, "Accepted.")
    engine._refuse(SESSION_ID, PROMPT_ID)
    assert [body["state"] for body in journal(context)] == ["prompt_accepted"]
    # Refusing a prompt that is not registered (any more) still reports it.
    engine._refuse(SESSION_ID, SECOND_ID)
    assert without_at(journal(context))[-1] == refused(SECOND_ID)


def test_close_stops_waiting_without_refusing(
    context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui
) -> None:
    put(context, "sandbox_ready")
    register(context, "Hello.")
    engine = make(sleep=None)
    engine.close()
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert journal(context) == []


def test_close_during_the_verification_ends_it(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    tui.ignore_enters = 10
    tui.on_enter = engine.close
    register(context, "Hello.")
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert tui.keys.count(("Enter",)) == 1


def test_close_while_waiting_for_the_acceptance(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    tui.hook = False
    register(context, "Hello.")

    def close_late(elapsed: float) -> None:
        if tui.submitted:
            engine.close()

    clock_.on_sleep = close_late
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert journal(context) == []


# -------------------------------------------------------------------------------------------- text put back (NA-01)

INTERRUPTED = "Write a numbered list of 300 animals.\nOne sentence about each.\nNo tools.\nKeep the order.\nEnd."


def test_shows_is_the_text_alone() -> None:
    text = "Read the README and explain the build in two paragraphs, then list the open questions."
    assert delivery.shows(text, text, False)
    # The TUI wraps long lines, also inside words: white space is ignored.
    wrapped = "Read the README and explain the bu ild in two  paragraphs, then list the open questions."
    assert delivery.shows(wrapped, text, False)
    assert delivery.shows("[Pasted text #3 +12 lines]", "x\n" * 12, True)
    assert delivery.shows("[Pasted text #2]", "y" * 1000, True)
    # A placeholder is the prompt only when the prompt was pasted, and only one placeholder alone.
    assert not delivery.shows("[Pasted text #2]", "typed", False)
    assert not delivery.shows("[Pasted text #1 +3 lines][Pasted text #2]", "a\nb\nc\nd", True)
    # The pane may render fewer characters (zero-width ones), never more, and nothing before the text.
    assert delivery.shows(text[:-1], text, False)
    assert not delivery.shows(text + " more", text, False)
    assert not delivery.shows("No tools. " + text, text, False)
    assert not delivery.shows("End.[Pasted text #1 +3 lines]", "a\nb\nc\nd", True)
    assert not delivery.shows("", text, False)


def test_text_put_back_after_an_interrupt_is_cleared_until_the_box_is_empty(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    """Five lines put back: three ``C-e C-u`` (one line each) are not enough; the box is read again until empty."""
    put(context, "idle")
    tui.put_back(INTERRUPTED)
    text = "? reply with the single word QMARK-OK and nothing else."
    register(context, text)
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == [text]
    assert tui.keys[:6] == [("C-e", "C-u")] * 6
    assert tui.keys[6:] == [("paste", text), ("Enter",)]


def test_text_put_back_while_typing_starts_the_attempt_again(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    text = "Explain the build."
    register(context, text)

    def late(elapsed: float) -> None:
        # Right after the text was typed (the ENTER_PAUSE): the interrupted prompt comes back in front of it.
        if clock_.slept[-1] == delivery.ENTER_PAUSE and len(clock_.slept) < 3:
            tui.put_back(INTERRUPTED + "\n")

    clock_.on_sleep = late
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == [text]
    assert tui.keys.count(("literal", text)) == 2
    assert [body["state"] for body in journal(context)] == ["prompt_accepted"]


def test_a_submission_the_hook_refused_is_not_final(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    """Text put back after the input was checked is submitted with the prompt: the hook refuses it (``unregistered``)
    and the TUI puts the mixed text back; the delivery empties the input and types the prompt again."""
    put(context, "idle")
    tui.put_back_refused = True
    text = "Explain the build."
    register(context, text)
    entered: list[int] = []

    def mix_once() -> None:
        entered.append(1)
        if len(entered) == 1:
            tui.put_back("No tools. ")

    tui.on_enter = mix_once
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == ["No tools. " + text, text]
    assert [body.get("reason") for body in journal(context)] == ["unregistered", None]
    assert [body["state"] for body in journal(context)] == ["prompt_refused", "prompt_accepted"]


def test_a_refusal_while_waiting_for_the_acceptance_is_not_final(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    """The text left the input (verified by the pane) and only then the hook refused it: delivered again."""
    put(context, "idle")
    text = "Explain the build."
    register(context, text)
    tui.hook = False

    def hook_late(elapsed: float) -> None:
        registry = PromptRegistry(context.journal, context.state)
        if len(tui.submitted) == 1 and not any(body["state"] == "prompt_refused" for body in journal(context)):
            registry.check(SESSION_ID, "mixed " + text)
        elif len(tui.submitted) == 2 and not any(body["state"] == "prompt_accepted" for body in journal(context)):
            registry.check(SESSION_ID, text)

    engine.sleep = hook_late
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == [text, text]
    assert [body["state"] for body in journal(context)] == ["prompt_refused", "prompt_accepted"]


def test_refused_submissions_end_after_three_more_attempts(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    tui.put_back_refused = True
    text = "Explain the build."
    register(context, text)

    def mix() -> None:
        tui.text = "x" + tui.text

    tui.on_enter = mix
    assert not engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert len(tui.submitted) == delivery.RETRIES + 1
    # The last mixed text the TUI put back is cleared; the prompt is refused and withdrawn.
    assert tui.text == ""
    assert [body.get("reason") for body in journal(context)] == ["unregistered"] * 4 + ["delivery_failed"]
    assert not registered(context)


def test_an_input_that_does_not_become_empty_refuses_before_writing(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    original = tui.send_keys
    rounds: list[int] = []

    def changing(name: str, *keys: str, literal: bool = False) -> None:
        original(name, *keys, literal=literal)
        if keys == ("C-e", "C-u"):
            rounds.append(1)
            tui.text = f"still changing {len(rounds)}"

    tui.send_keys = changing  # type: ignore[method-assign]
    tui.text = "left over"
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert tui.submitted == []
    assert not any(key[0] in ("literal", "paste") or key == ("Enter",) for key in tui.keys)
    # The attempt and the clearing before the refusal: CLEAR_ROUNDS rounds each.
    assert tui.keys.count(("C-e", "C-u")) == delivery.CLEARS * delivery.CLEAR_ROUNDS * 2
    assert without_at(journal(context)) == [refused()]


def test_a_hint_that_clearing_does_not_change_is_no_input(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    """A text the TUI shows in an empty input box (a hint) does not react to ``C-u``: after two rounds the prompt is
    typed; typing replaces the hint."""
    put(context, "idle")
    text = "Explain the build."
    register(context, text)
    original = tui.send_keys
    hint = ['Try "refactor parser.py"']

    def with_hint(name: str, *keys: str, literal: bool = False) -> None:
        if literal and hint:
            hint.clear()
            tui.text = ""
        original(name, *keys, literal=literal)
        if keys == ("C-e", "C-u") and hint:
            tui.text = hint[0]

    tui.send_keys = with_hint  # type: ignore[method-assign]
    tui.text = hint[0]
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.submitted == [text]
    assert tui.keys.count(("C-e", "C-u")) == delivery.CLEARS * 2


def test_a_prompt_accepted_after_a_refusal_is_not_typed_again(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    """The hook refused one submission, then accepted the prompt (a late hook answer): no second attempt types it."""
    put(context, "idle")
    text = "Explain the build."
    register(context, text)
    tui.hook = False

    def refuse_then_accept() -> None:
        registry = PromptRegistry(context.journal, context.state)
        registry.check(SESSION_ID, "mixed " + text)
        registry.check(SESSION_ID, text)

    tui.on_enter = refuse_then_accept
    original = engine._check
    engine._check = lambda *args: None if original(*args) is not False else False  # type: ignore[method-assign]
    assert engine.deliver(SESSION_ID, PROMPT_ID, text)
    assert tui.keys.count(("Enter",)) == 1
    assert [body["state"] for body in journal(context)] == ["prompt_refused", "prompt_accepted"]


def test_an_input_box_that_vanishes_before_writing_refuses(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")

    def hide(count: int) -> None:
        if count >= 2:
            tui.box = False

    tui.on_capture = hide
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert tui.keys == []
    assert without_at(journal(context)) == [refused()]


def test_close_while_clearing_or_checking_the_input_ends_it(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    tui.put_back("left over")
    original = tui.send_keys

    def close_on_clear(name: str, *keys: str, literal: bool = False) -> None:
        original(name, *keys, literal=literal)
        engine.close()

    tui.send_keys = close_on_clear  # type: ignore[method-assign]
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert tui.submitted == []
    assert journal(context) == []


def test_close_while_the_input_shows_other_text_ends_it(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "?Hello.")
    original = tui.paste

    def paste_and_close(name: str, text: str) -> None:
        original(name, text)
        tui.text = "other " + tui.text
        engine.close()

    tui.paste = paste_and_close  # type: ignore[method-assign]
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "?Hello.")
    assert tui.submitted == []
    assert journal(context) == []


def test_the_default_pause_waits_on_the_close_event(context: ControlContext, tui: FakeTui) -> None:
    engine = delivery.Delivery(context, tui)
    assert engine._pause(0.001)
    engine.close()
    assert not engine._pause(10)


# -------------------------------------------------------------------------------------------- queue


def test_prompts_are_delivered_in_order_once(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, clock_: Clock
) -> None:
    put(context, "idle")
    register(context, "First.")
    register(context, "Second.", SECOND_ID)

    # The first delivery makes the session working; the second one waits for idle (the Stop hook).
    def stop_hook(elapsed: float) -> None:
        session = context.state.session(SESSION_ID)
        assert session is not None
        if session.state == "working":
            set_state(context, "idle")

    clock_.on_sleep = stop_hook
    started: list[Callable[[], object]] = []
    engine.spawn = started.append
    engine.submit(SESSION_ID, PROMPT_ID, "First.")
    engine.submit(SESSION_ID, PROMPT_ID, "First.")
    engine.submit(SESSION_ID, SECOND_ID, "Second.")
    assert len(started) == 1
    started[0]()
    assert tui.submitted == ["First.", "Second."]
    assert [body["prompt_id"] for body in journal(context)] == [PROMPT_ID, SECOND_ID]
    # The worker ended with the queue: a later prompt starts a new one.
    engine.submit(SESSION_ID, PROMPT_ID, "First.")
    assert len(started) == 2


def test_an_unexpected_error_refuses_the_prompt_and_the_worker_goes_on(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, capsys: pytest.CaptureFixture[str]
) -> None:
    put(context, "idle")
    register(context, "Breaks.")
    tui.fail["send_keys"] = RuntimeError("bug")
    engine.submit(SESSION_ID, PROMPT_ID, "Breaks.")
    assert without_at(journal(context)) == [refused()]
    assert "prompt delivery failed" in capsys.readouterr().err


def test_a_closed_journal_ends_the_delivery_quietly(
    context: ControlContext,
    engine: delivery.Delivery,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def closed(*args: object) -> bool:
        raise JournalError("unavailable")

    monkeypatch.setattr(engine, "deliver", closed)
    engine.submit(SESSION_ID, PROMPT_ID, "Hello.")
    assert engine._queues == {}
    assert capsys.readouterr().err == ""


def test_a_failing_refusal_does_not_kill_the_worker(
    context: ControlContext,
    engine: delivery.Delivery,
    tui: FakeTui,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    put(context, "idle")
    register(context, "Breaks.")
    tui.fail["send_keys"] = RuntimeError("bug")

    def broken(*args: object) -> None:
        raise RuntimeError("refusal failed")

    monkeypatch.setattr(engine, "_refuse", broken)
    engine.submit(SESSION_ID, PROMPT_ID, "Breaks.")
    assert engine._queues == {}
    assert "refusal failed" in capsys.readouterr().err


def test_a_prompt_registered_again_after_its_refusal_is_queued_again(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Retry me.")
    calls: list[str] = []
    original = engine.deliver

    def deliver(session_id: str, prompt_id: str, text: str) -> bool:
        calls.append(prompt_id)
        if len(calls) == 1:
            engine._refuse(session_id, prompt_id)
            register(context, text)  # the runner's retry, while the worker still holds the first entry
            engine.submit(session_id, prompt_id, text)
            return False
        return original(session_id, prompt_id, text)

    engine.deliver = deliver  # type: ignore[method-assign]
    engine.submit(SESSION_ID, PROMPT_ID, "Retry me.")
    assert calls == [PROMPT_ID, PROMPT_ID]
    assert tui.submitted == ["Retry me."]


def test_a_closed_delivery_leaves_the_queue(context: ControlContext, engine: delivery.Delivery, tui: FakeTui) -> None:
    put(context, "idle")
    register(context, "Hello.")
    engine.close()
    engine.submit(SESSION_ID, PROMPT_ID, "Hello.")
    assert tui.keys == []
    assert engine._queues == {}


def test_workers_run_in_threads_by_default(context: ControlContext, tui: FakeTui) -> None:
    put(context, "idle")
    register(context, "Threaded.")
    engine = delivery.Delivery(context, tui, poll=0.001)
    engine.submit(SESSION_ID, PROMPT_ID, "Threaded.")
    done = threading.Event()

    def finished() -> None:
        while engine._queues:
            done.wait(0.01)
        done.set()

    threading.Thread(target=finished, daemon=True).start()
    assert done.wait(10)
    assert tui.submitted == ["Threaded."]


# -------------------------------------------------------------------------------------------- interrupt


def test_interrupt_sends_escape_under_the_pane_lock(engine: delivery.Delivery, tui: FakeTui) -> None:
    engine.interrupt(SESSION_ID)
    assert tui.keys == [("Escape",)]
    assert engine.pane_lock(SESSION_ID) is engine.pane_lock(SESSION_ID)


# -------------------------------------------------------------------------------------------- TUI ready


def test_the_tui_coming_up_moves_the_session_to_prompt_delivering(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "agent_starting", CLAUDE_ID)
    tools_listed(context)
    tui.box = False

    def paint(count: int) -> None:
        if count == 3:
            tui.box = True

    tui.on_capture = paint
    assert engine.watch_ready(SESSION_ID)
    assert without_at(journal(context)) == [{"state": "prompt_delivering", "claude_session_id": CLAUDE_ID}]
    session = context.state.session(SESSION_ID)
    assert session is not None
    assert session.state == "prompt_delivering"


def test_no_transition_when_the_prompt_was_taken_first(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "working")
    assert not engine.watch_ready(SESSION_ID)
    engine._delivering(SESSION_ID)
    engine._delivering("01a1ee56-5e00-7003-8f1b-340691d99fff")
    assert journal(context) == []
    assert tui.captures == 0


def test_a_slow_tui_is_watched_until_the_session_ends(
    context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui, clock_: Clock
) -> None:
    put(context, "agent_starting")
    tui.box = False
    engine = make(ready_seconds=0.2)

    def fail_late(elapsed: float) -> None:
        if elapsed > 3:
            set_state(context, "failed")

    clock_.on_sleep = fail_late
    assert not engine.watch_ready(SESSION_ID)
    # Every 50 ms at first, then once a second.
    assert clock_.slept[:4] == [0.05] * 4
    assert clock_.slept[-1] == 1.0
    assert journal(context) == []


def test_the_tui_alone_is_not_ready_the_tools_must_have_arrived(
    context: ControlContext,
    engine: delivery.Delivery,
    tui: FakeTui,
    clock_: Clock,
    session_manager: sessions.SessionManager,
) -> None:
    """HOT-09: the input box is visible at once, the tool server answers late; the session moves on only after the
    agent listed the tools, and then the initial prompt is the first thing typed."""
    put(context, "agent_starting")
    register(context, "Read the platform at once.")
    session_manager._deferred[SESSION_ID] = (PROMPT_ID, "Read the platform at once.")
    tui.box = True
    started: list[Callable[[], object]] = []
    engine.spawn = started.append

    def tools_late(elapsed: float) -> None:
        if elapsed >= 1.5 and not toolsready.ready(context, SESSION_ID):
            assert journal(context) == []  # nothing typed, no transition while the tools are missing
            assert tui.submitted == []
            tools_listed(context)
        session = context.state.session(SESSION_ID)
        if session is not None and session.state == "working":
            set_state(context, "idle")

    clock_.on_sleep = tools_late
    assert engine.watch_ready(SESSION_ID)
    assert kinds(context)[:2] == ["session.tools_ready", "session.state"]
    assert sum(clock_.slept) >= 1.5
    (worker,) = started
    worker()
    assert tui.submitted == ["Read the platform at once."]


def test_missing_tools_restart_the_agent_twice_then_fail_the_start(
    context: ControlContext,
    make: Callable[..., delivery.Delivery],
    tui: FakeTui,
    *,
    clock_: Clock,
    session_manager: sessions.SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(context, "agent_starting")
    register(context, "Needs the platform.")
    session_manager._deferred[SESSION_ID] = (PROMPT_ID, "Needs the platform.")
    tui.box = True
    restarts: list[str] = []

    def restart(ctx: ControlContext, session_id: str) -> bool:
        restarts.append(session_id)
        return True

    monkeypatch.setattr(sessions, "restart_session", restart)
    engine = make(tools_seconds=2.0)
    for _ in range(2):
        clock_.slept.clear()
        assert not engine.watch_ready(SESSION_ID)
        assert sum(clock_.slept) == pytest.approx(2.0)
    assert restarts == [SESSION_ID, SESSION_ID]
    assert session_manager._deferred[SESSION_ID] == (PROMPT_ID, "Needs the platform.")  # still waiting
    assert journal(context) == []
    # the third time the start ends: failed, and the waiting prompt is refused, never typed
    assert not engine.watch_ready(SESSION_ID)
    assert restarts == [SESSION_ID, SESSION_ID]
    assert [(body["state"], body.get("reason")) for body in journal(context)] == [
        ("failed", "agent_start_failed"),
        ("prompt_refused", "delivery_failed"),
    ]
    assert tui.submitted == []
    records, _ = context.journal.read(after=0, limit=1000)
    errors = [record for record in records if record["kind"] == "guest.error"]
    assert [(record["session_id"], record["body"]) for record in errors] == [
        (SESSION_ID, {"code": "tools_unavailable"})
    ] * 3


def test_a_run_that_cannot_be_restarted_fails_at_once(
    context: ControlContext,
    make: Callable[..., delivery.Delivery],
    tui: FakeTui,
    session_manager: sessions.SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(context, "agent_starting")
    monkeypatch.setattr(sessions, "restart_session", lambda ctx, session_id: False)
    assert not make(tools_seconds=1.0).watch_ready(SESSION_ID)
    assert [body["state"] for body in journal(context)] == ["failed"]


def test_the_restarts_count_again_after_a_start_that_got_its_tools(
    context: ControlContext,
    make: Callable[..., delivery.Delivery],
    tui: FakeTui,
    session_manager: sessions.SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(context, "agent_starting")
    monkeypatch.setattr(sessions, "restart_session", lambda ctx, session_id: True)
    engine = make(tools_seconds=1.0)
    assert not engine.watch_ready(SESSION_ID)
    assert engine._restarts == {SESSION_ID: 1}
    tools_listed(context)
    assert engine.watch_ready(SESSION_ID)
    assert engine._restarts == {}


def test_a_journal_that_cannot_take_the_error_still_restarts(
    context: ControlContext,
    make: Callable[..., delivery.Delivery],
    tui: FakeTui,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(context, "agent_starting")
    monkeypatch.setattr(sessions, "restart_session", lambda ctx, session_id: True)

    def unavailable(*_args: object) -> int:
        raise JournalError("unavailable")

    monkeypatch.setattr(context.journal, "append", unavailable)
    assert not make(tools_seconds=1.0).watch_ready(SESSION_ID)


def test_a_closing_journal_ends_the_watch_without_an_error(
    context: ControlContext,
    make: Callable[..., delivery.Delivery],
    tui: FakeTui,
    session_manager: sessions.SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(context, "agent_starting")

    def closing(ctx: ControlContext, session_id: str) -> bool:
        raise JournalError("unavailable")

    monkeypatch.setattr(sessions, "restart_session", closing)
    assert not make(tools_seconds=1.0).watch_ready(SESSION_ID)
    assert journal(context) == []


def test_a_session_taken_over_between_the_checks_is_not_moved(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(context, "agent_starting")
    tools_listed(context)
    monkeypatch.setattr(engine, "_delivering", lambda session_id: False)
    monkeypatch.setattr(sessions, "deferred_prompt", lambda ctx, session_id: None)
    assert not engine.watch_ready(SESSION_ID)


def test_close_ends_the_watch(context: ControlContext, make: Callable[..., delivery.Delivery], tui: FakeTui) -> None:
    put(context, "agent_starting")
    tui.box = False
    engine = make(sleep=None)
    engine.close()
    assert not engine.watch_ready(SESSION_ID)


# -------------------------------------------------------------------------------------------- with the session manager


@pytest.fixture
def session_manager(context: ControlContext, tmp_path: Any) -> Iterator[sessions.SessionManager]:
    runner = TmuxRunner()
    created = sessions.SessionManager(
        context=context,
        system=FakeSystem(),
        tmux=tmux.Tmux(socket=tmux.SOCKET, run=runner, scope=False),
        driver=ClaudeCodeDriver("2.1.330"),
        paths=sessions.Paths(meta_dir=str(tmp_path / "meta")),
    )
    sessions.install(context, created)
    try:
        yield created
    finally:
        created.close()


def test_the_deferred_initial_prompt_is_delivered_when_the_tui_is_up(
    context: ControlContext,
    engine: delivery.Delivery,
    tui: FakeTui,
    clock_: Clock,
    session_manager: sessions.SessionManager,
) -> None:
    put(context, "agent_starting")
    tools_listed(context)
    text = "A long initial prompt " * 10
    register(context, text)
    session_manager._deferred[SESSION_ID] = (PROMPT_ID, text)
    started: list[Callable[[], object]] = []
    engine.spawn = started.append
    engine.agent_starting(SESSION_ID)
    # The deferred prompt is registered and waits for the transition.
    started.pop(0)()
    assert [body["state"] for body in journal(context)] == ["prompt_delivering"]
    register(context, "Follow-up.", SECOND_ID)
    engine.submit(SESSION_ID, SECOND_ID, "Follow-up.")

    def stop_hook(elapsed: float) -> None:
        session = context.state.session(SESSION_ID)
        assert session is not None
        if session.state == "working":
            set_state(context, "idle")

    clock_.on_sleep = stop_hook
    (worker,) = started
    worker()
    assert [body["state"] for body in journal(context)] == ["prompt_delivering", "prompt_accepted", "prompt_accepted"]
    assert tui.submitted == [text, "Follow-up."]


def test_the_process_wide_delivery_uses_the_session_managers_tmux(
    context: ControlContext, session_manager: sessions.SessionManager
) -> None:
    created = delivery.manager(context)
    assert isinstance(created.pane, delivery.TmuxPane)
    assert created.pane.server is session_manager.tmux
    assert delivery.manager(context) is created


def test_the_prompts_route_registers_and_queues(
    context: ControlContext, control_client: ControlClient, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    reply = control_client.request(
        "POST", f"/v1/sessions/{SESSION_ID}/prompts", {"prompt_id": PROMPT_ID, "text": "Over the socket."}
    )
    assert (reply.status, reply.json()) == (200, {"state": "registered"})
    assert tui.submitted == ["Over the socket."]
    assert [body["state"] for body in journal(context)] == ["prompt_accepted"]


def test_the_prompts_module_watches_every_start(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "agent_starting")
    tools_listed(context)
    watcher = control_prompts.SESSION_EVENTS["agent_starting"]
    assert watcher in sessions.session_events()["agent_starting"]
    tui.box = True
    watcher(context, SESSION_ID)
    assert [body["state"] for body in journal(context)] == ["prompt_delivering"]


# -------------------------------------------------------------------------------------------- tmux pane


@dataclass
class Recorder:
    codes: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        sub = argv[3]
        return subprocess.CompletedProcess(argv, self.codes.get(sub, 0), "screen\n", "")


def test_tmux_pane_types_captures_and_pastes_through_the_root_socket() -> None:
    recorder = Recorder()
    pane = delivery.TmuxPane(tmux.Tmux(run=recorder, scope=False), run=recorder)
    pane.send_keys(NAME, "abc;", literal=True)
    pane.send_keys(NAME, "Enter")
    assert pane.capture(NAME) == "screen\n"
    pane.paste(NAME, "multi\nline")
    argvs = [argv for argv, _ in recorder.calls]
    socket = ["tmux", "-S", "/run/agent-guest/tmux.sock"]
    assert argvs == [
        [*socket, "send-keys", "-t", f"={NAME}:", "-l", "--", "abc;"],
        [*socket, "send-keys", "-t", f"={NAME}:", "--", "Enter"],
        [*socket, "capture-pane", "-p", "-t", f"={NAME}:"],
        [*socket, "load-buffer", "-b", f"p-{SESSION_ID}", "-"],
        [*socket, "paste-buffer", "-p", "-d", "-b", f"p-{SESSION_ID}", "-t", f"={NAME}:"],
    ]
    load, paste = recorder.calls[3][1], recorder.calls[4][1]
    assert load["input"] == "multi\nline"
    assert load["env"] == paste["env"] == tmux.CLIENT_ENV
    assert paste["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("failing", ["load-buffer", "paste-buffer"])
def test_tmux_pane_paste_failures_raise(failing: str) -> None:
    recorder = Recorder(codes={failing: 1})
    pane = delivery.TmuxPane(tmux.Tmux(run=recorder, scope=False), run=recorder)
    with pytest.raises(tmux.TmuxError, match=failing):
        pane.paste(NAME, "text")
    subcommands = [argv[3] for argv, _ in recorder.calls]
    # A loaded buffer is deleted when the paste fails: the text does not stay in the server's memory.
    expected = ["load-buffer"] if failing == "load-buffer" else ["load-buffer", "paste-buffer", "delete-buffer"]
    assert subcommands == expected


def test_a_pane_that_fails_while_the_input_is_emptied_still_refuses(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    tui.put_back("left over")
    tui.fail["send_keys"] = tmux.TmuxError("gone")
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert without_at(journal(context)) == [refused()]


def test_a_prompt_accepted_after_the_acceptance_wait_is_neither_cleared_nor_refused(
    context: ControlContext, engine: delivery.Delivery, tui: FakeTui
) -> None:
    put(context, "idle")
    register(context, "Hello.")
    tui.hook = False

    def late(session_id: str, prompt_id: str, mark: int) -> bool:
        PromptRegistry(context.journal, context.state).check(SESSION_ID, "Hello.")
        return False

    engine._await_acceptance = late  # type: ignore[method-assign]
    assert not engine.deliver(SESSION_ID, PROMPT_ID, "Hello.")
    assert [body["state"] for body in journal(context)] == ["prompt_accepted"]
    assert tui.keys.count(("C-e", "C-u")) == 0
