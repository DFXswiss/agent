"""U-RUN-prompts: the first prompt of a task reaches its session in every order in which the prompt, the device's
assignment to the workgroup and the membership log arrive (decisions/HOT-03.md).

The scene follows E2E-26 with the research task sent right after a member joined: the org's log ends with the
member's `accept` (entry n), the admin's browser then stores the device's `device_assign` to a workgroup (entry n+1)
together with the device's key wrap, and the task's first prompt names `lh = n`. Everything runs through the real
store and the real reconcile steps (state, logs, wraps, prompts), inbox, session start and delivery against the hub
double; only the orders are chosen by the test. Each case checks that the prompt is delivered exactly once - with the
start or afterwards - and that no check is weakened (an unverifiable log or a missing key still holds the prompt).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runner.crypto.log.entries import entry_hash
from agent_runner.inbox.pull import ATTRIBUTE as INBOX_ATTRIBUTE
from agent_runner.orgs.book import ATTRIBUTE as BOOK_ATTRIBUTE
from agent_runner.orgs.book import book_for
from agent_runner.prompts.deliver import Deliverer, install
from agent_runner.prompts.reconcile import STEPS as PROMPT_STEPS
from agent_runner.reconcile.engine import Engine
from agent_runner.reconcile.reconcile import STEPS as LOG_STEPS
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS
from agent_runner.sessions.start import Starter
from agent_runner.sync.http import HubTransportError

from ..reconcile.rig import CERT_SEQ, DEVICE, Rig, pid, seal, wrap_for
from ..sessions.kit import ORG, WG_OTHER, desired, session_id
from ..sync.fakehub import FakeHub
from .kit import PKit, PromptGuest, run, setup

WG2 = WG_OTHER
TASK2 = "01930004-0000-7000-8000-0000000000b2"
AT = "2026-11-21T12:00:02.000Z"


@dataclass
class Scene:
    """The E2E-26 order on the rig: the device verified the log up to the member's `accept` (entry `n`)."""

    env: Any
    pk: PKit
    engine: Engine
    n: int
    sid: str = field(default_factory=lambda: session_id(1))
    routed: list[dict[str, Any]] = field(default_factory=list)
    journal_seq: int = 0

    @property
    def spec(self) -> Any:
        return self.env.world.orgs[ORG]

    def assign_with_wrap(self) -> None:
        """The browser's one request: `device_assign` of the device to the workgroup (n+1) and the device's wrap."""
        self.spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=WG2, platform_seq=CERT_SEQ)
        self.env.world.wraps.append(wrap_for("alice", "wg", WG2, 1))

    def prompt(self, seq: int, label: str) -> str:
        """A prompt of the task at `seq` of the workgroup, written at `lh = n`, routed to the session."""
        sealed = seal(self.spec, "alice", "prompt", scope_id=WG2, lh=self.n, label=label, task=TASK2)
        self.env.world.add_event(WG2, seq, sealed)
        event_id = str(sealed["header"]["id"])
        self.routed.append(
            {"task_id": TASK2, "session_id": self.sid, "scope_id": WG2, "scope_seq": seq, "event_id": event_id}
        )
        return event_id

    def state(self) -> dict[str, Any]:
        return {**FakeHub.active_state(self.env.hub), "pending_prompts": list(self.routed)}

    def offer_stale_head(self) -> None:
        """The next state answer offers the head of entry n (taken before the browser's request was stored)."""
        self.env.world.offered[ORG] = {"seq": self.n, "hash": entry_hash(self.spec.log.entries[self.n])}

    def offer_current_head(self) -> None:
        self.env.world.offered.pop(ORG, None)

    async def reconcile(self) -> None:
        run_ = await self.engine.run_once()
        assert run_ is not None
        assert run_.halted is None, self.env.log()

    async def start(self) -> dict[str, Any]:
        """The session service starts the session of the desired state; returns the guest's create request."""
        self.pk.kit.session_env.desired = self.state()
        report = await Starter(self.pk.kit.session_env).start(desired(1, wg_id=WG2, task_id=TASK2))
        assert report.started, report
        await self.journal("agent_starting")
        await self.journal("prompt_delivering")
        return next(value for name, value in self.pk.guest.calls if name == "create_session")

    async def journal(self, state: str, **body: Any) -> None:
        from agent_runner.guestlink.client import JournalRecord  # noqa: PLC0415
        from agent_runner.prompts import session_state  # noqa: PLC0415

        self.journal_seq += 1
        row = await self.pk.kit.vms.ensure_vm(ORG, WG2)
        reader = VmJournal(self.pk.kit.session_env, row, handlers=HANDLERS, observers=session_state.OBSERVERS)
        record = JournalRecord(
            seq=self.journal_seq,
            ts=AT,
            session_id=self.sid,
            kind="session.state",
            body={"state": state, "at": AT, **body},
        )
        await reader.handle([record])

    async def rounds(self, count: int = 3) -> None:
        """What the daemon does on its own: reconcile runs and delivery rounds."""
        for _ in range(count):
            await self.reconcile()
            await self.pk.deliverer.run_once()

    def delivered(self, request: dict[str, Any]) -> list[str]:
        """Every hand-over to the guest: the initial prompt of the start, then `POST .../prompts`."""
        initial = request.get("initial_prompt")
        return ([initial["prompt_id"]] if initial is not None else []) + self.pk.guest.sent()

    async def session_state(self) -> str:
        return (await self.pk.session(self.sid)).state


async def scene(env: Any) -> Scene:
    pk = await setup(env, guest=PromptGuest(wg_id=WG2))
    engine = Engine(env.context, [*LOG_STEPS, *PROMPT_STEPS])
    env.context.reconcile_engine = engine
    found = Scene(env=env, pk=pk, engine=engine, n=0)
    env.hub.active_state = found.state
    # carol joins (entries 5, 6); the device verifies it
    found.spec.log.invite_user("alice", "carol", wgs={WG2: "write"})
    found.spec.log.accept("carol")
    found.n = len(found.spec.log.entries) - 1
    await found.reconcile()
    assert pk.kit.session_env.book.head(ORG).seq == found.n
    return found


def test_the_wrap_fetched_before_its_log_entry_still_brings_the_first_prompt(rig: Rig) -> None:
    """The SCN-06 order: a run takes the state before the browser's request (head n) and the wraps after it, so the
    device's wrap is refused `wrap_unauthorized` against entry n - held back, not audited (decisions/SCN-08.md) -;
    the next run verifies n+1 and checks it again."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        first = sc.prompt(1, "first")
        sc.offer_stale_head()
        await sc.reconcile()
        assert "code=wrap_unauthorized" not in env.log()  # a wrap ahead of its log entry is no attack
        sc.offer_current_head()
        await sc.reconcile()
        request = await sc.start()
        await sc.rounds()
        assert sc.delivered(request) == [first]
        await sc.journal("prompt_accepted", prompt_id=first)
        await sc.rounds()
        assert sc.delivered(request) == [first]
        assert await sc.pk.rows() == {first: "accepted"}

    run(rig, body)


def test_a_prompt_that_reaches_the_device_after_the_start_is_delivered_once(rig: Rig) -> None:
    """The session starts before the device can read the prompt (the hub stored it after the start's pull): the
    start goes without initial prompt, the session waits in `prompt_delivering` and the next round delivers it."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        await sc.reconcile()
        request = await sc.start()
        assert "initial_prompt" not in request
        assert await sc.session_state() == "prompt_delivering"
        first = sc.prompt(1, "first")
        await sc.rounds()
        assert sc.delivered(request) == [first]
        assert (await sc.pk.row(first)).channel == "prompt"
        await sc.rounds()
        assert sc.delivered(request) == [first]

    run(rig, body)


def test_a_prompt_routed_before_the_assignment_waits_for_it(rig: Rig) -> None:
    """Prompt first, assignment later: the start cannot happen before the assignment is verified, and the prompt is
    not read before the device holds the key."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        first = sc.prompt(1, "first")
        await sc.rounds()
        assert sc.pk.guest.sent() == []
        assert await env.rows("SELECT event_id FROM runner.prompt_event") == []
        sc.assign_with_wrap()
        await sc.reconcile()
        request = await sc.start()
        await sc.rounds()
        assert sc.delivered(request) == [first]
        assert request["initial_prompt"]["prompt_id"] == first

    run(rig, body)


def test_a_prompt_hinted_during_the_start_is_handed_over_once(rig: Rig) -> None:
    """The prompt's hint arrives while the start runs (after the start asked for its first prompt): it is routed to
    the starting session and sent once the session is ready, never twice."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        await sc.reconcile()
        first: list[str] = []
        guest = sc.pk.guest
        original = guest.create_session

        async def create_session(*args: Any, **kwargs: Any) -> Any:
            first.append(sc.prompt(1, "first"))
            await sc.pk.deliverer.hinted(sc.sid, WG2, 1)
            # the session is still starting: nothing is sent through the prompt endpoint
            assert guest.sent() == []
            return await original(*args, **kwargs)

        guest.create_session = create_session  # type: ignore[method-assign]
        request = await sc.start()
        assert "initial_prompt" not in request
        await sc.rounds()
        assert sc.delivered(request) == first
        await sc.rounds()
        assert sc.delivered(request) == first

    run(rig, body)


def test_after_a_runner_restart_the_waiting_session_gets_its_prompt(rig: Rig) -> None:
    """The session started without prompt; the daemon restarts before the prompt is routed - a new deliverer, inbox,
    book (loaded again from the store) and reconcile engine, nothing kept in memory: it delivers the prompt once."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        await sc.reconcile()
        request = await sc.start()
        assert "initial_prompt" not in request
        for attribute in (BOOK_ATTRIBUTE, INBOX_ATTRIBUTE, "reconcile_engine"):
            delattr(env.context, attribute)
        sc.pk.kit.session_env.book = book_for(env.context)
        sc.engine = Engine(env.context, [*LOG_STEPS, *PROMPT_STEPS])
        env.context.reconcile_engine = sc.engine
        restarted = Deliverer(sc.pk.kit.session_env, idle_seconds=60.0, stop=sc.pk.stops.append)
        sc.pk.deliverer = install(env.context, restarted)
        first = sc.prompt(1, "first")
        await sc.rounds()
        assert sc.delivered(request) == [first]

    run(rig, body)


def test_two_prompts_right_after_each_other_keep_their_order_and_are_delivered_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        first = sc.prompt(1, "first")
        second = sc.prompt(2, "second")
        sc.offer_stale_head()
        await sc.reconcile()
        sc.offer_current_head()
        await sc.reconcile()
        request = await sc.start()
        await sc.rounds()
        assert sc.delivered(request) == [first]
        await sc.journal("prompt_accepted", prompt_id=first)
        await sc.rounds()
        assert sc.delivered(request) == [first]
        await sc.journal("idle")
        await sc.rounds()
        assert sc.delivered(request) == [first, second]
        await sc.journal("prompt_accepted", prompt_id=second)
        await sc.journal("idle")
        await sc.rounds()
        assert sc.delivered(request) == [first, second]
        assert await sc.pk.rows() == {first: "accepted", second: "accepted"}

    run(rig, body)


def test_an_inbox_refresh_racing_the_state_does_not_stop_the_org(rig: Rig) -> None:
    """The inbox verifies entry n+1 (L10 refresh for an event naming it) between a run's state request and its log
    step: the run's offered head n is older than the copy, not a rollback, and the prompt is delivered."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        first = sc.prompt(1, "first")
        sc.offer_stale_head()
        steps = sc.engine.steps
        refresh_between = steps[0].__class__(15, "inbox-race", lambda _run: sc.pk.deliverer.refresh())
        # an event of the default workgroup names entry n+1: the inbox pulls it and refreshes the log (L10)
        env.world.add_event(sc.spec.default_wg, 1, seal(sc.spec, "alice", "prompt", label="other", task=pid("t/o")))
        sc.engine.steps = [steps[0], refresh_between, *steps[1:]]
        await sc.reconcile()
        sc.engine.steps = steps
        sc.offer_current_head()
        assert sc.pk.kit.session_env.book.stopped == {}
        assert "log_rollback" not in env.log()
        request = await sc.start()
        await sc.rounds()
        assert sc.delivered(request) == [first]

    run(rig, body)


def test_an_unreachable_hub_holds_the_prompt_until_it_answers(rig: Rig) -> None:
    """The pull of the start cannot reach the hub: the session waits in `prompt_delivering` (its true state) and the
    prompt is delivered once the hub answers again."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        await sc.reconcile()
        first = sc.prompt(1, "first")
        original = sc.env.hub.route

        def down(method: str, target: str, payload: Any) -> Any:
            if target.startswith("/v1/device/pull"):
                raise HubTransportError("connect")
            return original(method, target, payload)

        env.hub.route = down
        request = await sc.start()
        assert "initial_prompt" not in request
        await sc.pk.deliverer.run_once()
        assert await sc.session_state() == "prompt_delivering"
        assert sc.pk.guest.sent() == []
        env.hub.route = original
        await sc.rounds()
        assert sc.delivered(request) == [first]

    run(rig, body)


def test_a_prompt_beyond_the_verified_head_waits_for_a_successful_refresh(rig: Rig) -> None:
    """The prompt names an `lh` beyond the verified head and the log cannot be fetched: it is not accepted unverified,
    not lost, and the session waits in `prompt_delivering`; once the log is fetched and verified, it is delivered."""

    async def body(env: Any) -> None:
        sc = await scene(env)
        sc.assign_with_wrap()
        await sc.reconcile()
        # a member joins after the device's last run; the prompt is written at that newest entry
        sc.spec.log.invite_user("alice", "dave", wgs={WG2: "write"})
        sc.n = len(sc.spec.log.entries) - 1
        first = sc.prompt(1, "first")
        original = env.hub.route

        def logs_down(method: str, target: str, payload: Any) -> Any:
            if target.startswith("/v1/device/logs"):
                raise HubTransportError("connect")
            return original(method, target, payload)

        env.hub.route = logs_down
        request = await sc.start()
        assert "initial_prompt" not in request
        await sc.pk.deliverer.run_once()
        await sc.pk.deliverer.apply_desired(sc.state())
        assert await env.rows("SELECT event_id FROM runner.prompt_event") == []
        assert await sc.pk.rows() == {}
        assert await sc.session_state() == "prompt_delivering"
        assert sc.pk.kit.session_env.book.head(ORG).seq == sc.n - 1
        env.hub.route = original
        await sc.rounds()
        assert sc.delivered(request) == [first]

    run(rig, body)
