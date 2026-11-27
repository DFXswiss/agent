"""The CI controller (`agent_runner.ci.controller`): which job it takes, the runner it starts, the chain to the task and
its workgroup for billing and audit, metrics, the service loop (spec 16 section 17.5). The runner's socket and the
listener are in-memory doubles here; `test_area.py` and `test_link.py` cover the real ones."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runner.ci.attribution import Attribution, RunsUnavailable
from agent_runner.ci.bridge import BridgeError
from agent_runner.ci.controller import Controller, Metrics, Runner, listener_argv, main, serve, target_of, write_metrics
from agent_runner.ci.jobs import job_of
from agent_runner.ci.link import LinkDown, LinkRefused

from .kit import FIXED_TASK, OTHER_OWNER, PR_OWNER, PR_TASK, SHA, config, job_message, write_config


class FakeLink:
    """The runner's CI area in memory."""

    def __init__(self) -> None:
        self.pulls: dict[int, Any] = {7: PR_OWNER, 8: OTHER_OWNER}
        self.fits = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[dict[str, Any]] = []
        self.fail: dict[str, Exception] = {}
        self.next_vm = 0

    def _owner(self, owner: Any) -> dict[str, str] | None:
        if owner is None:
            return None
        return {"org_id": owner.org_id, "wg_id": owner.wg_id, "task_id": owner.task_id, "session_id": owner.session_id}

    async def call(self, op: str, **body: Any) -> dict[str, Any]:
        self.calls.append((op, body))
        if op in self.fail:
            raise self.fail[op]
        if op == "pull":
            return {"owner": self._owner(self.pulls.get(body["number"]))}
        if op == "capacity":
            return {"fits": self.fits}
        if op == "vm":
            self.next_vm += 1
            return {"workvm_id": f"wvm-{self.next_vm:08x}", "ready_ms": 5}
        if op == "audit":
            self.audits.append(body)
        return {}

    def ops(self, name: str) -> list[dict[str, Any]]:
        return [body for op, body in self.calls if op == name]


class FakeListener:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.names: list[str] = []
        self.capacities: list[int] = []

    async def jit(self, name: str) -> str:
        if self.fail:
            raise BridgeError("github_error")
        self.names.append(name)
        return f"jit-{name}" * 4

    async def capacity(self, most: int) -> None:
        if self.fail:
            raise BridgeError("listener_down")
        self.capacities.append(most)


class Runs:
    def __init__(self, numbers: Sequence[int] = (8,), fail: bool = False) -> None:
        self.numbers, self.fail = list(numbers), fail

    async def head_sha(self, repo: str, run_id: int) -> str:
        if self.fail:
            raise RunsUnavailable("x")
        return SHA

    async def pulls_of(self, repo: str, sha: str) -> Sequence[int]:
        return self.numbers


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _controller(
    link: FakeLink | None = None, runs: Runs | None = None, **changes: Any
) -> tuple[Controller, FakeLink, Clock, list[str]]:
    fake, clock = link or FakeLink(), Clock()
    logs: list[str] = []
    built = Controller(config(**changes), fake, runs or Runs(), logs.append, clock=clock)
    return built, fake, clock, logs


def _started(request_id: int, runner_name: str, **changes: Any) -> dict[str, Any]:
    return {**job_message(request_id, **changes), "runner_name": runner_name, "runner_id": 3}


async def _accept(control: Controller, listener: FakeListener, *messages: dict[str, Any]) -> list[int]:
    repository = control.config.repositories[0]
    chosen = await control.on_batch(repository, {"available": list(messages)})
    await control.on_acquired(listener, chosen, chosen)
    await asyncio.gather(*control.tasks)
    return chosen


def test_an_agent_pull_request_job_is_taken_and_its_runner_started() -> None:
    control, link, clock, logs = _controller()
    listener = FakeListener()

    async def main_() -> None:
        assert await _accept(control, listener, job_message(42)) == [42]
        runner = control.runners[42]
        assert runner.workvm_id == "wvm-00000001"
        assert runner.name == "agent-ci-wvm-00000001"
        assert listener.names == ["agent-ci-wvm-00000001"]
        assert link.ops("vm") == [{"size": "standard", "owner": link._owner(PR_OWNER)}]
        assert link.ops("run")[0]["jit"].startswith("jit-agent-ci-wvm-00000001")
        assert link.audits[-1] == {
            "task_id": PR_TASK,
            "target_id": "run:942:42",
            "result": "ok",
            "detail": {
                "stage": "accepted",
                "event": "pull_request",
                "basis": "pull_request",
                "reason": None,
                "result": None,
                "size": "standard",
                "duration_ms": None,
                "workvm_id": "wvm-00000001",
            },
        }
        # the same job offered again is not taken twice
        assert await control.on_batch(control.config.repositories[0], {"available": [job_message(42)]}) == []
        clock.now += 30
        await control.on_started(_started(42, "agent-ci-wvm-00000001"))
        clock.now += 600
        await control.on_completed({**_started(42, "agent-ci-wvm-00000001"), "result": "Succeeded"})
        ended = link.audits[-1]
        assert ended["detail"]["stage"] == "ended"
        assert ended["detail"]["result"] == "succeeded"
        assert ended["detail"]["duration_ms"] == 600_000
        assert ended["result"] == "ok"
        assert control.runners == {}
        assert link.ops("end") == []

    asyncio.run(main_())
    assert control.metrics.accepted == {"pull_request": 1}
    assert control.metrics.pickup == [30.0, 1]
    assert "ci.job_accepted request_id=42 workvm_id=wvm-00000001" in logs


def test_jobs_without_an_agent_pull_request_are_refused_once_under_the_fixed_task() -> None:
    control, link, _clock, logs = _controller()
    link.pulls = {}
    repository = control.config.repositories[0]

    async def main_() -> None:
        for _ in range(2):
            assert await control.on_batch(repository, {"available": [job_message(5), {"request_id": "bad"}]}) == []

    asyncio.run(main_())
    assert len(link.audits) == 1
    refused = link.audits[0]
    assert refused["task_id"] == FIXED_TASK
    assert refused["result"] == "denied"
    assert refused["detail"]["reason"] == "pull_request_not_from_agent"
    assert control.metrics.refused == {"pull_request_not_from_agent": 1, "message_invalid": 2}
    assert control.metrics.offered == 4
    assert "ci.job_refused request_id=5 reason=pull_request_not_from_agent" in logs


def test_without_capacity_jobs_stay_at_github() -> None:
    control, link, _clock, _logs = _controller()
    link.fits = False

    async def main_() -> list[int]:
        return await control.on_batch(control.config.repositories[0], {"available": [job_message(1), job_message(2)]})

    assert asyncio.run(main_()) == []
    assert control.metrics.deferred == 2
    link.fail["capacity"] = LinkDown("x")
    assert asyncio.run(main_()) == []


def test_the_service_drops_jobs_it_did_not_give_and_scheduled_runs_use_the_fixed_task() -> None:
    control, link, _clock, _logs = _controller()
    listener = FakeListener()

    async def main_() -> None:
        repository = control.config.repositories[0]
        chosen = await control.on_batch(
            repository, {"available": [job_message(1, event="schedule", ref="refs/heads/main"), job_message(2)]}
        )
        assert chosen == [1, 2]
        await control.on_acquired(listener, [1], chosen)
        await asyncio.gather(*control.tasks)
        assert sorted(control.runners) == [1]
        await control.on_acquired(listener, [1], [1])  # already starting: no second runner

    asyncio.run(main_())
    assert link.ops("vm") == [
        {
            "size": "standard",
            "owner": {
                "org_id": control.config.repositories[0].org_id,
                "wg_id": control.config.repositories[0].wg_id,
                "task_id": FIXED_TASK,
                "session_id": control.config.repositories[0].session_id,
            },
        }
    ]


@pytest.mark.parametrize(("failing", "stage"), [("vm", "vm_refused"), ("jit", "jit_failed"), ("run", "run_failed")])
def test_a_runner_that_cannot_start_is_ended_and_audited(failing: str, stage: str) -> None:
    control, link, _clock, logs = _controller()
    listener = FakeListener(fail=failing == "jit")
    if failing != "jit":
        link.fail[failing] = LinkRefused("start_failed")

    asyncio.run(_accept(control, listener, job_message(42)))
    assert control.runners == {}
    assert control.metrics.failures == {stage: 1}
    ended = link.audits[-1]
    assert ended["detail"]["stage"] == "ended"
    assert ended["detail"]["reason"] == stage
    assert ended["result"] == "failed"
    assert (link.ops("end") != []) == (failing != "vm")
    assert any(line.startswith(f"ci.runner_failed request_id=42 stage={stage}") for line in logs)


def test_a_runner_that_got_another_accepted_job_is_rebound_to_that_job_s_task() -> None:
    control, link, clock, _logs = _controller()
    listener = FakeListener()

    async def main_() -> None:
        await _accept(control, listener, job_message(1), job_message(2, ref="refs/pull/8/merge"))
        first = control.runners[1]
        clock.now += 5
        # GitHub gives job 2 (task of pull request 8) to the runner started for job 1
        await control.on_started(_started(2, str(first.name), ref="refs/pull/8/merge"))
        assert control.runners[2].workvm_id == "wvm-00000001"
        assert control.runners[2].started_at == clock.now
        assert control.runners[1].workvm_id == "wvm-00000002"
        assert link.ops("rebind") == [
            {"workvm_id": "wvm-00000001", "owner": link._owner(OTHER_OWNER)},
            {"workvm_id": "wvm-00000002", "owner": link._owner(PR_OWNER)},
        ]
        # job 1 then starts on the VM it took over: no further rebind
        await control.on_started(_started(1, "agent-ci-wvm-00000002"))
        # two jobs of the same task swap without a rebind
        await _accept(control, listener, job_message(3), job_message(4))
        await control.on_started(_started(4, str(control.runners[3].name)))
        # a runner whose partner has no VM yet is moved alone
        control.runners[5] = Runner(
            5,
            control.runners[3].job,
            control.runners[3].repository,
            Attribution(OTHER_OWNER, "pull_request", 8),
            clock.now,
        )
        control.runners[5].job = job_of(job_message(5, ref="refs/pull/8/merge")) or control.runners[5].job
        await control.on_started(_started(5, str(control.runners[3].name), ref="refs/pull/8/merge"))

    asyncio.run(main_())
    assert len(link.ops("rebind")) == 3


def test_a_job_the_controller_did_not_take_is_ended_on_its_runner() -> None:
    control, link, _clock, logs = _controller()
    listener = FakeListener()

    async def main_() -> None:
        await _accept(control, listener, job_message(1))
        await control.on_started(_started(99, "agent-ci-wvm-00000001"))
        await control.on_started(_started(1, "unknown-runner"))
        await control.on_started({"runner_name": "agent-ci-wvm-00000001"})
        await control.on_completed({"runner_name": "unknown"})

    asyncio.run(main_())
    assert link.ops("end") == [{"workvm_id": "wvm-00000001"}]
    assert link.audits[-1]["detail"]["reason"] == "job_not_accepted"
    assert "ci.job_not_accepted request_id=99 workvm_id=wvm-00000001" in logs


def test_started_and_completed_jobs_arrive_in_batches() -> None:
    control, link, _clock, _logs = _controller()

    async def main_() -> None:
        await _accept(control, FakeListener(), job_message(1))
        name = "agent-ci-wvm-00000001"
        repository = control.config.repositories[0]
        await control.on_batch(repository, {"started": [_started(1, name), "noise"]})
        await control.on_batch(repository, {"completed": [{**_started(1, name), "result": 5}], "available": "x"})

    asyncio.run(main_())
    assert control.runners == {}
    assert link.audits[-1]["detail"]["result"] is None


def test_a_service_stopped_before_it_started() -> None:
    control, _link, _clock, _logs = _controller()

    async def main_() -> None:
        stopping = asyncio.Event()
        stopping.set()
        await serve(control, lambda _repository: FakeBridge([]), stopping=stopping, textfile_dir=None)

    asyncio.run(main_())


def test_a_job_github_took_back_leaves_its_runner_waiting() -> None:
    control, _link, _clock, _logs = _controller()

    async def main_() -> None:
        await _accept(control, FakeListener(), job_message(1))
        await control.on_completed({**_started(1, "agent-ci-wvm-00000001"), "result": "canceled"})

    asyncio.run(main_())
    assert 1 in control.runners


def test_the_sweep_ends_idle_runners_and_offers_capacity() -> None:
    control, link, clock, logs = _controller()
    listener = FakeListener()
    control.listeners["agent-ci-example-repo"] = listener

    async def main_() -> None:
        await _accept(control, listener, job_message(1))
        await control.sweep()
        assert 1 in control.runners
        assert listener.capacities == [1]
        clock.now += 600
        link.fits = False
        await control.sweep()
        control.listeners["agent-ci-example-repo"] = FakeListener(fail=True)
        await control.sweep()

    asyncio.run(main_())
    assert control.runners == {}
    assert listener.capacities == [1, 0]
    assert link.audits[-1]["detail"]["reason"] == "runner_idle"
    assert "ci.runner_idle request_id=1 workvm_id=wvm-00000001" in logs


def test_a_push_job_and_an_unreachable_runner() -> None:
    control, link, _clock, logs = _controller()

    async def main_() -> None:
        await _accept(control, FakeListener(), job_message(3, event="push", ref="refs/heads/develop"))
        assert control.runners[3].attribution == Attribution(OTHER_OWNER, "commit_pull_request", 8)
        link.fail["audit"] = LinkDown("gone")
        link.pulls = {}
        await control.on_batch(control.config.repositories[0], {"available": [job_message(4)]})

    asyncio.run(main_())
    assert "ci.audit_failed request_id=4 stage=refused error=LinkDown" in logs


def test_event_names_in_audit_rows_are_closed_codes() -> None:
    control, link, _clock, _logs = _controller()
    link.pulls = {}

    async def main_() -> None:
        await control.on_batch(control.config.repositories[0], {"available": [job_message(6, event="Odd-Event")]})

    asyncio.run(main_())
    assert link.audits[0]["detail"]["event"] == "other"


def test_metrics_carry_codes_only() -> None:
    metrics = Metrics()
    metrics.offered, metrics.deferred = 3, 1
    metrics.accepted["pull_request"] += 2
    metrics.refused["pull_request_not_from_agent"] += 1
    Metrics.observe(metrics.pickup, 2.5)
    Metrics.observe(metrics.duration, -1)
    job = job_of(job_message(1))
    assert job is not None
    repository = config().repositories[0]
    attribution = Attribution(PR_OWNER, "pull_request", 7)
    runners = [
        Runner(1, job, repository, attribution, 10.0),
        Runner(2, job, repository, attribution, 5.0, started_at=6.0),
    ]
    text = metrics.render(runners, 1, 70.0)
    assert 'agent_ci_jobs_accepted_total{basis="pull_request"} 2' in text
    assert "agent_ci_runners_waiting 1" in text
    assert "agent_ci_jobs_running 1" in text
    assert "agent_ci_oldest_waiting_seconds 60" in text
    assert "agent_ci_pickup_seconds_sum 2.5" in text
    assert "agent_ci_job_seconds_count 1" in text
    assert "example" not in text
    assert target_of(job) == "run:901:1"


def test_metrics_are_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_metrics(tmp_path, "x 1\n")
    assert (tmp_path / "agent_ci.prom").read_text(encoding="utf-8") == "x 1\n"

    def refuse(_source: Any, _target: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="read-only"):
        write_metrics(tmp_path, "y 1\n")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["agent_ci.prom"]


class FakeBridge:
    """A listener run: one batch, then it exits."""

    def __init__(self, batches: list[dict[str, Any]], code: int = 0) -> None:
        self.batches, self.code = batches, code
        self.jits: list[str] = []

    async def run(self, on_batch: Any, on_acquired: Any) -> int:
        for batch in self.batches:
            chosen = await on_batch(batch)
            await on_acquired(chosen)
        await asyncio.sleep(0)
        return self.code

    async def jit(self, name: str) -> str:
        self.jits.append(name)
        return "jit-" * 8

    async def capacity(self, most: int) -> None:
        return None


def test_the_service_runs_listeners_and_writes_metrics(tmp_path: Path) -> None:
    control, link, _clock, logs = _controller()
    spawned: list[FakeBridge] = []

    def spawn(repository: Any) -> FakeBridge:
        bridge = FakeBridge([{"available": [job_message(len(spawned) + 1)]}], code=len(spawned))
        spawned.append(bridge)
        return bridge

    async def main_() -> None:
        stopping = asyncio.Event()

        async def stop_later() -> None:
            while len(spawned) < 2:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            stopping.set()

        stopper = asyncio.ensure_future(stop_later())
        await serve(control, spawn, stopping=stopping, textfile_dir=tmp_path, sweep_s=0.01, restart_s=0.01)
        await stopper

    asyncio.run(main_())
    assert spawned[0].jits == ["agent-ci-wvm-00000001"]
    assert "ci.listener_exited scale_set=agent-ci-example-repo code=0" in logs
    assert "agent_ci_listeners_up" in (tmp_path / "agent_ci.prom").read_text(encoding="utf-8")
    assert link.ops("run")


def test_the_service_without_a_textfile_and_with_an_unwritable_one(tmp_path: Path) -> None:
    control, _link, _clock, _logs = _controller()

    async def run_once(directory: Path | None) -> None:
        stopping = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stopping.set)

        def spawn(_repository: Any) -> FakeBridge:
            return FakeBridge([])

        await serve(control, spawn, stopping=stopping, textfile_dir=directory, sweep_s=0.01, restart_s=0.01)

    asyncio.run(run_once(None))
    asyncio.run(run_once(tmp_path / "missing"))


def test_the_listener_command_line_names_the_key_file_but_holds_no_secret() -> None:
    settings = config()
    argv = listener_argv(settings, settings.repositories[0])
    assert argv[0] == "/nonexistent/agent-ci-listener"
    assert argv[argv.index("--key-file") + 1] == "/nonexistent/app.pem"
    assert "https://github.com/example-org" in argv  # the organisation's scale set (spec 16 section 17.9)


def test_main_refuses_a_bad_configuration_and_starts_the_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    assert main(["--config", str(broken)]) == 2
    assert "ci configuration invalid" in capsys.readouterr().out
    key = tmp_path / "app.pem"
    key.write_text("not used before the first push job", encoding="utf-8")
    good = write_config(
        tmp_path / "ci.json",
        app={"client_id": "Iv23-test", "key_file": str(key)},
        socket=str(tmp_path / "missing.sock"),
        listener="/bin/false",
    )
    ran: list[Any] = []

    def run(coroutine: Any) -> None:
        async def stopped() -> None:
            task = asyncio.ensure_future(coroutine)
            await asyncio.sleep(0.05)
            import os  # noqa: PLC0415
            import signal  # noqa: PLC0415

            os.kill(os.getpid(), signal.SIGTERM)
            await task

        asyncio.run(stopped())
        ran.append(True)

    assert main(["--config", str(good), "--textfile-dir", str(tmp_path)], run=run) == 0
    assert ran == [True]
    assert json.loads(good.read_text(encoding="utf-8"))["repositories"]
