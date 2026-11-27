"""The CI controller `agent-ci` (decision D-3 (b), spec 16 section 17.5): a service of its own, under its own account,
next to `agent-runner` on a pool device.

Per repository of the configuration one listener (`bridge.Bridge` over the Go sidecar) holds the session of the
repository's scale set. For each message of the service (`on_batch`):

1. **Offered jobs** (`available`): each job is attributed (`attribution.attribute`, with the runner's records for
   pull requests and, with `push_attribution`, the app's reads for pushes). A refused job is not acquired; it is
   audited once per request (`ci.job` `refused` under the repository's fixed task, result `denied`) and counted. An
   attributed job is acquired only while a CI VM of the repository's size would start now (`capacity`, the device's
   admission): the controller takes a job only for capacity it has (spec 16 section 6.6); otherwise the job stays at
   GitHub for a later message or another device (`agent_ci_jobs_deferred_total`).
2. **Acquired jobs** (`on_acquired`): for each one a runner is started in its own task - a fresh CI VM whose row
   names the job's task and bearing session (`vm`), a JIT configuration for the runner `agent-ci-<workvm id>`, the
   runner process in the VM (`run`), then the audit row `accepted`. A failed step ends the VM (`end`) and is audited
   `ended` with the reason (`vm_refused`, `jit_failed`, `run_failed`).
3. **Started jobs** (`started`): GitHub gives a job to any idle runner of the scale set (spec 16 section 3.2), so the
   runner's job may be another accepted job than the one it was started for: the VM's row is then re-bound to that
   job's task (`rebind`), so its runtime is billed along the right chain. A job the controller never accepted on one
   of its runners is ended at once (`end`, audit `ended` reason `job_not_accepted`).
4. **Completed jobs** (`completed`): audited `ended` with GitHub's result and the duration; the VM goes when the
   runner process exits (the runner area's watch).

Every `SWEEP_S` seconds a runner that got no job within `IDLE_S` seconds is ended (`runner_idle`); the scale set's
capacity is refreshed (`capacity`: 1 while a CI VM fits, else 0 - the service then holds new jobs back, M-8).

Metrics: `<textfile dir>/agent_ci.prom` every sweep (`Metrics.render`), labels are closed codes only - no repository,
branch or person names: `agent_ci_jobs_offered_total`, `agent_ci_jobs_accepted_total{basis}`,
`agent_ci_jobs_refused_total{reason}`, `agent_ci_jobs_deferred_total`, `agent_ci_jobs_ended_total{reason}`,
`agent_ci_runner_failures_total{stage}`, `agent_ci_jobs_running`, `agent_ci_runners_waiting`,
`agent_ci_oldest_waiting_seconds`, `agent_ci_listeners_up`, `agent_ci_pickup_seconds_{sum,count}` (acquired ->
started), `agent_ci_vm_ready_seconds_{sum,count}` and `agent_ci_job_seconds_{sum,count}`.

The controller never stores a JIT configuration, a token or the app key; its log lines carry IDs and codes only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_runner.ci.attribution import Attribution, Owner, Pulls, Refusal, Runs, attribute
from agent_runner.ci.jobs import Job, job_of
from agent_runner.ci.link import LinkDown, LinkRefused
from agent_runner.ci.repos import CiConfig, Repository
from agent_runner.metrics.textfile import Metric, render

IDLE_S = 600.0
SWEEP_S = 30.0
RUNNER_PREFIX = "agent-ci-"
METRICS_FILE = "agent_ci.prom"


class Link(Protocol):
    async def call(self, op: str, **body: Any) -> dict[str, Any]: ...


class Listener(Protocol):
    async def jit(self, name: str) -> str: ...

    async def capacity(self, most: int) -> None: ...


@dataclass
class Runner:
    """A runner the controller started for an accepted job."""

    request_id: int
    job: Job
    repository: Repository
    attribution: Attribution
    acquired_at: float
    workvm_id: str | None = None
    started_at: float | None = None
    name: str | None = None


@dataclass
class Metrics:
    offered: int = 0
    deferred: int = 0
    accepted: Counter[str] = field(default_factory=Counter)
    refused: Counter[str] = field(default_factory=Counter)
    ended: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    pickup: list[float] = field(default_factory=lambda: [0.0, 0.0])
    ready: list[float] = field(default_factory=lambda: [0.0, 0.0])
    duration: list[float] = field(default_factory=lambda: [0.0, 0.0])

    @staticmethod
    def observe(pair: list[float], seconds: float) -> None:
        pair[0] += max(0.0, seconds)
        pair[1] += 1

    def render(self, runners: Sequence[Runner], listeners: int, now: float) -> str:
        def counter(name: str, text: str, label: str | None, values: Mapping[str, int] | int) -> Metric:
            metric = Metric(name, "counter", text)
            if isinstance(values, int):
                metric.add((), values)
            else:
                for key, value in sorted(values.items()):
                    metric.add(((str(label), key),), value)
            return metric

        def gauge(name: str, text: str, value: float) -> Metric:
            metric = Metric(name, "gauge", text)
            metric.add((), value)
            return metric

        waiting = [runner for runner in runners if runner.started_at is None]
        oldest = max((now - runner.acquired_at for runner in waiting), default=0.0)
        metrics = [
            counter("agent_ci_jobs_offered_total", "Jobs the scale sets offered.", None, self.offered),
            counter("agent_ci_jobs_accepted_total", "Jobs taken, by the basis of their task.", "basis", self.accepted),
            counter("agent_ci_jobs_refused_total", "Jobs not taken, by reason.", "reason", self.refused),
            counter(
                "agent_ci_jobs_deferred_total",
                "Attributed jobs left at GitHub for lack of capacity.",
                None,
                self.deferred,
            ),
            counter("agent_ci_jobs_ended_total", "Jobs and runners that ended, by reason.", "reason", self.ended),
            counter("agent_ci_runner_failures_total", "Runner starts that failed, by stage.", "stage", self.failures),
            gauge("agent_ci_jobs_running", "Runners with a job.", len(runners) - len(waiting)),
            gauge("agent_ci_runners_waiting", "Runners started that have no job yet.", len(waiting)),
            gauge("agent_ci_oldest_waiting_seconds", "Age of the oldest runner without a job.", round(oldest, 3)),
            gauge("agent_ci_listeners_up", "Listeners with an open scale-set session.", listeners),
        ]
        for name, text, pair in (
            ("agent_ci_pickup_seconds", "From acquiring a job to its start on a runner.", self.pickup),
            ("agent_ci_vm_ready_seconds", "From asking for a CI VM to the VM running.", self.ready),
            ("agent_ci_job_seconds", "From a job's start to its end.", self.duration),
        ):
            metrics.append(counter(f"{name}_sum", text, None, 0))
            metrics[-1].samples = [((), round(pair[0], 3))]
            metrics.append(counter(f"{name}_count", text, None, int(pair[1])))
        return render(metrics)


def target_of(job: Job) -> str:
    """The audit target of a job: GitHub's run ID and the scale set's request ID (IDs, no names)."""
    return f"run:{job.run_id}:{job.request_id}"


def _owner(value: object) -> Owner | None:
    if not isinstance(value, dict):
        return None
    ids = [value.get(key) for key in ("org_id", "wg_id", "task_id", "session_id")]
    texts = [ident for ident in ids if isinstance(ident, str)]
    return Owner(*texts) if len(texts) == len(ids) else None


@dataclass
class RunnerPulls:
    """The `Pulls` port over the runner's `pull` (only pull requests a session of the device created)."""

    link: Link

    async def find(self, repo: str, number: int) -> Owner | None:
        answer = await self.link.call("pull", repo=repo, number=number)
        return _owner(answer.get("owner"))


@dataclass
class Controller:
    config: CiConfig
    link: Link
    runs: Runs
    log: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    idle_s: float = IDLE_S
    runners: dict[int, Runner] = field(default_factory=dict)
    decided: dict[int, Attribution | Refusal] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    listeners: dict[str, Listener] = field(default_factory=dict)

    @property
    def pulls(self) -> Pulls:
        return RunnerPulls(self.link)

    # -- audit -----------------------------------------------------------------------------------------------------

    async def _audit(
        self,
        task_id: str,
        job: Job,
        stage: str,
        *,
        result: str = "ok",
        basis: str | None = None,
        reason: str | None = None,
        github: str | None = None,
        size: str | None = None,
        duration_ms: int | None = None,
        workvm_id: str | None = None,
    ) -> None:
        detail = {
            "stage": stage,
            "event": job.event if job.event.replace("_", "").isalpha() and job.event.islower() else "other",
            "basis": basis,
            "reason": reason,
            "result": github,
            "size": size,
            "duration_ms": duration_ms,
            "workvm_id": workvm_id,
        }
        try:
            await self.link.call("audit", task_id=task_id, target_id=target_of(job), result=result, detail=detail)
        except (LinkDown, LinkRefused) as error:
            self.log(f"ci.audit_failed request_id={job.request_id} stage={stage} error={type(error).__name__}")

    # -- messages --------------------------------------------------------------------------------------------------

    async def on_batch(self, repository: Repository, batch: Mapping[str, Any]) -> list[int]:
        """The request IDs to acquire of one message (module docstring steps 1, 3 and 4)."""
        for message in _items(batch, "started"):
            await self.on_started(message)
        for message in _items(batch, "completed"):
            await self.on_completed(message)
        chosen: list[int] = []
        fits: bool | None = None
        for message in _items(batch, "available"):
            job = job_of(message)
            self.metrics.offered += 1
            if job is None:
                self.metrics.refused["message_invalid"] += 1
                continue
            if job.request_id in self.runners or job.request_id in chosen:
                continue
            decision = self.decided.get(job.request_id)
            if decision is None:
                decision = await attribute(job, self.config, self.pulls, self.runs)
                self.decided[job.request_id] = decision
                if isinstance(decision, Refusal):
                    self.metrics.refused[decision.reason] += 1
                    self.log(f"ci.job_refused request_id={job.request_id} reason={decision.reason}")
                    await self._audit(repository.task_id, job, "refused", result="denied", reason=decision.reason)
            if isinstance(decision, Refusal):
                continue
            if fits is None:
                fits = await self._fits(repository)
            if not fits:
                self.metrics.deferred += 1
                continue
            chosen.append(job.request_id)
            self.runners[job.request_id] = Runner(job.request_id, job, repository, decision, self.clock())
            fits = None  # the next job looks again: this one takes the room
        return chosen

    async def _fits(self, repository: Repository) -> bool:
        try:
            return (await self.link.call("capacity", size=repository.size)).get("fits") is True
        except (LinkDown, LinkRefused):
            return False

    async def on_acquired(self, listener: Listener, request_ids: Sequence[int], offered: Sequence[int]) -> None:
        """Start a runner for each acquired job; offered jobs the service did not give are forgotten."""
        for request_id in offered:
            if request_id not in request_ids:
                self.runners.pop(request_id, None)
        for request_id in request_ids:
            runner = self.runners.get(request_id)
            if runner is not None and runner.workvm_id is None and runner.name is None:
                runner.name = "starting"
                task = asyncio.ensure_future(self.start(listener, runner))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def start(self, listener: Listener, runner: Runner) -> None:
        """A fresh CI VM, a JIT configuration and the runner process (module docstring step 2)."""
        owner, size = runner.attribution.owner, runner.repository.size
        begun = self.clock()
        stage = "vm_refused"
        try:
            answer = await self.link.call("vm", size=size, owner=_owner_body(owner))
            runner.workvm_id = str(answer["workvm_id"])
            self.metrics.observe(self.metrics.ready, self.clock() - begun)
            runner.name = RUNNER_PREFIX + runner.workvm_id
            stage = "jit_failed"
            jit = await listener.jit(runner.name)
            stage = "run_failed"
            await self.link.call("run", workvm_id=runner.workvm_id, jit=jit)
        except Exception as error:
            self.metrics.failures[stage] += 1
            self.log(f"ci.runner_failed request_id={runner.request_id} stage={stage} error={type(error).__name__}")
            await self._finish(runner, stage, end_vm=True)
            return
        self.metrics.accepted[runner.attribution.basis] += 1
        self.log(f"ci.job_accepted request_id={runner.request_id} workvm_id={runner.workvm_id}")
        await self._audit(
            owner.task_id,
            runner.job,
            "accepted",
            basis=runner.attribution.basis,
            size=size,
            workvm_id=runner.workvm_id,
        )

    async def _finish(
        self, runner: Runner, reason: str, *, end_vm: bool, github: str | None = None, result: str = "failed"
    ) -> None:
        """Forget the runner, end its VM when asked and audit the end."""
        self.runners.pop(runner.request_id, None)
        self.decided.pop(runner.request_id, None)
        self.metrics.ended[reason] += 1
        if end_vm and runner.workvm_id is not None:
            with contextlib.suppress(LinkDown, LinkRefused):
                await self.link.call("end", workvm_id=runner.workvm_id)
        duration = None if runner.started_at is None else int((self.clock() - runner.started_at) * 1000)
        await self._audit(
            runner.attribution.owner.task_id,
            runner.job,
            "ended",
            result=result,
            basis=runner.attribution.basis,
            reason=reason,
            github=github,
            size=runner.repository.size,
            duration_ms=duration,
            workvm_id=runner.workvm_id,
        )

    def _by_name(self, message: Mapping[str, Any]) -> Runner | None:
        name = message.get("runner_name")
        return next((runner for runner in self.runners.values() if runner.name == name), None)

    async def on_started(self, message: Mapping[str, Any]) -> None:
        """Module docstring step 3."""
        runner, job = self._by_name(message), job_of(message)
        if runner is None or job is None:
            return
        if job.request_id != runner.request_id:
            other = self.runners.get(job.request_id)
            if other is None or other.started_at is not None:
                self.log(f"ci.job_not_accepted request_id={job.request_id} workvm_id={runner.workvm_id}")
                await self._finish(runner, "job_not_accepted", end_vm=True)
                return
            # the two accepted jobs swap runners: each VM's row follows the job it will carry
            runner.workvm_id, runner.name, other.workvm_id, other.name = (
                other.workvm_id,
                other.name,
                runner.workvm_id,
                runner.name,
            )
            if other.attribution.owner != runner.attribution.owner:
                for moved in (other, runner):
                    if moved.workvm_id is not None:
                        with contextlib.suppress(LinkDown, LinkRefused):
                            await self.link.call(
                                "rebind", workvm_id=moved.workvm_id, owner=_owner_body(moved.attribution.owner)
                            )
            runner = other
        runner.started_at = self.clock()
        self.metrics.observe(self.metrics.pickup, runner.started_at - runner.acquired_at)

    async def on_completed(self, message: Mapping[str, Any]) -> None:
        """Module docstring step 4: a job that ran on one of the runners ended."""
        runner = self._by_name(message)
        if runner is None or runner.started_at is None:
            return  # a job GitHub took back before it started: its runner waits for another one
        result = message.get("result")
        github = result.lower() if isinstance(result, str) and result.isalpha() and len(result) <= 32 else None
        self.metrics.observe(self.metrics.duration, self.clock() - runner.started_at)
        await self._finish(runner, "completed", end_vm=False, github=github, result="ok")

    async def sweep(self) -> None:
        """End runners without a job for `idle_s` seconds and refresh the capacity of every scale set."""
        now = self.clock()
        for runner in list(self.runners.values()):
            if runner.started_at is None and runner.workvm_id is not None and now - runner.acquired_at >= self.idle_s:
                self.log(f"ci.runner_idle request_id={runner.request_id} workvm_id={runner.workvm_id}")
                await self._finish(runner, "runner_idle", end_vm=True)
        for repository in self.config.repositories:
            listener = self.listeners.get(repository.scale_set)
            if listener is not None:
                with contextlib.suppress(Exception):
                    await listener.capacity(1 if await self._fits(repository) else 0)


def _owner_body(owner: Owner) -> dict[str, str]:
    return {"org_id": owner.org_id, "wg_id": owner.wg_id, "task_id": owner.task_id, "session_id": owner.session_id}


def _items(batch: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = batch.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def write_metrics(directory: Path, text: str) -> None:
    """`<directory>/agent_ci.prom`, written atomically like the runner's textfile."""
    descriptor, temporary = tempfile.mkstemp(prefix=".agent_ci.prom.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, directory / METRICS_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# -- the service -------------------------------------------------------------------------------------------------


Spawn = Callable[[Repository], Any]
"""Builds the bridge of a repository's listener (production: `listener_bridge`)."""


async def listen(controller: Controller, repository: Repository, bridge: Any) -> int:
    """Serve one run of a repository's listener; returns its exit code."""
    controller.listeners[repository.scale_set] = bridge
    offered: list[int] = []

    async def on_batch(batch: Mapping[str, Any]) -> list[int]:
        chosen = await controller.on_batch(repository, batch)
        offered[:] = chosen
        return chosen

    async def on_acquired(ids: Sequence[int]) -> None:
        await controller.on_acquired(bridge, ids, list(offered))

    return int(await bridge.run(on_batch, on_acquired))


async def serve(
    controller: Controller,
    spawn: Spawn,
    *,
    stopping: asyncio.Event,
    textfile_dir: Path | None,
    sweep_s: float = SWEEP_S,
    restart_s: float = 10.0,
) -> None:
    """Run one listener per repository (restarted `restart_s` after it exits) and the sweep until `stopping`."""

    async def keep(repository: Repository) -> None:
        while not stopping.is_set():
            code = await listen(controller, repository, spawn(repository))
            controller.listeners.pop(repository.scale_set, None)
            controller.log(f"ci.listener_exited scale_set={repository.scale_set} code={code}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), restart_s)

    keepers = [asyncio.ensure_future(keep(repository)) for repository in controller.config.repositories]
    await asyncio.sleep(0)  # the listeners start before the first sweep
    try:
        while not stopping.is_set():
            await controller.sweep()
            if textfile_dir is not None:
                text = controller.metrics.render(
                    list(controller.runners.values()), len(controller.listeners), controller.clock()
                )
                with contextlib.suppress(OSError):
                    write_metrics(textfile_dir, text)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), sweep_s)
    finally:
        for keeper in keepers:
            keeper.cancel()
        for task in [*keepers, *controller.tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def listener_argv(config: CiConfig, repository: Repository) -> list[str]:
    """The sidecar's command line: no secret in it (the key file is named, never passed)."""
    return [
        str(config.listener),
        "--url",
        f"https://github.com/{repository.owner}",
        "--client-id",
        str(config.client_id),
        "--installation-id",
        str(repository.installation_id),
        "--key-file",
        str(config.key_file),
        "--scale-set",
        repository.scale_set,
    ]


def _owner_of(repository: Repository) -> dict[str, str]:
    return {
        "org_id": repository.org_id,
        "wg_id": repository.wg_id,
        "task_id": repository.task_id,
        "session_id": repository.session_id,
    }


async def setup(config: CiConfig, config_path: Path, link: Link, line: Callable[[str], None], api: Any = None) -> int:
    """`agent-ci setup` (`setup.py`): create the app in the first repository's organisation, install it in every
    organisation of the configuration and write client ID and installation IDs into the configuration."""
    from agent_runner.ci.setup import AppApi, AppSetup, RunnerBrowser, SetupFailed, update_config  # noqa: PLC0415

    flow = AppSetup(
        RunnerBrowser(link, _owner_of(config.repositories[0])),
        api or AppApi(config.api_url),
        config.key_file,
        line,
    )
    organisations = config.organisations
    try:
        app = await flow.create(organisations[0], public=len(organisations) > 1)
        installations = await flow.install(app, organisations)
    except (SetupFailed, LinkDown, LinkRefused) as error:
        line(f"ci.setup failed code={getattr(error, 'code', type(error).__name__)}")
        return 1
    update_config(config_path, app, installations)
    line("ci.setup done")
    return 0


def main(argv: Sequence[str] | None = None, *, run: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run) -> int:
    """`agent-ci --config <file> [--textfile-dir <dir>]`: the controller service (launchd starts it);
    `agent-ci --config <file> setup`: create and install the GitHub App (`setup`)."""
    from agent_runner.ci.bridge import Bridge  # noqa: PLC0415 - the service only
    from agent_runner.ci.github import AppRuns  # noqa: PLC0415
    from agent_runner.ci.link import RunnerLink  # noqa: PLC0415
    from agent_runner.ci.repos import ConfigError, load  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="agent-ci")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--textfile-dir", type=Path, default=None)
    parser.add_argument("command", nargs="?", choices=("serve", "setup"), default="serve")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except ConfigError as error:
        print(f"agent-ci: {error}", flush=True)
        return 2

    def line(text: str) -> None:
        print(text, flush=True)

    if args.command == "setup":
        return int(run(setup(config, args.config, RunnerLink(config.socket), line)))
    if not config.ready:
        line("agent-ci: the GitHub App is not set up; run `agent-ci --config <file> setup` first")
        return 2

    controller = Controller(config, RunnerLink(config.socket), AppRuns(config, config.key_file.read_bytes), line)

    def spawn(repository: Repository) -> Bridge:
        return Bridge(listener_argv(config, repository), log=line)

    async def service() -> None:
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            import signal  # noqa: PLC0415

            loop.add_signal_handler(getattr(signal, name), stopping.set)
        await serve(controller, spawn, stopping=stopping, textfile_dir=args.textfile_dir)

    run(service())
    return 0
