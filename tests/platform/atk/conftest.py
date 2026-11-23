"""Attack suite ATK-01 … ATK-40 against real VMs (docs/platform/spec/07-testing.md section 2.3; ATS-01, ATS-02).

Tests marked ``atk`` drive real Claude Code in Lima VMs cloned from a TEST_MODE golden and run only through
``scripts/run-atk.sh`` on the runner device (``AGENT_ATK_RUN=1``); a plain platform run (``make platform-ci``)
deselects them. The unit tests of the suite's helper code (``agent_doubles.atk``) in this directory always run.

Environment of a real run (set by ``scripts/run-atk.sh``):

| Variable | Meaning |
|---|---|
| ``AGENT_ATK_RUN`` | ``1`` selects the ``atk`` tests |
| ``AGENT_ATK_PREFIX`` | prefix of every Lima instance the suite creates (required, e.g. ``wp-ats01-``) |
| ``AGENT_ATK_GOLDEN`` | Lima name of the stopped TEST_MODE golden (built with ``--test-model-port``) |
| ``AGENT_ATK_PORT`` | the golden's test model port; the dispatcher listens there on loopback |
| ``AGENT_ATK_OUT`` | directory for ``results.json``, ``table.md`` and ``probe.md`` |
| ``AGENT_ATK_ONLY`` | optional comma list of attack IDs (default: all 40) |
| ``AGENT_ATK_KEEP_VMS`` | ``1`` keeps the VMs for inspection (``scripts/run-atk.sh --keep-vms``; delete them by hand) |
| ``AGENT_ATK_PARALLEL`` | attack sessions at a time per VM (default 3; with the victim session at most 4) |
| ``AGENT_ATK_TUNNEL`` | ``1``: the golden has no usable test model port; the double is reached through a tunnel |
| ``AGENT_ATK_MODES`` | optional comma list of modes (default ``hooks,fault``) |
| ``AGENT_ATK_TIMEOUT`` | seconds per attack session (default 180; a live red team needs longer) |
| ``AGENT_ATK_REDTEAM`` | ``1`` adds the scripted red team ``REDTEAM-DOUBLE`` to every mode (ATS-03) |
| ``AGENT_ATK_LIVE`` | ``1`` adds ``REDTEAM-LIVE`` (real model through OpenRouter) to mode ``hooks`` (marker ``live``) |
| ``AGENT_ATK_LIVE_KEY_FD`` | descriptor with the OpenRouter key (default 3, from ``scripts/run-atk.sh --live``) |

``AGENT_ATK_TUNNEL=1`` is for a shared ``cache-golden-*`` TEST_MODE golden (built with the closed port 9) and for a
production golden: an SSH reverse tunnel per VM binds the relay port on the guest's loopback (ADR-034). A production
golden runs mode ``hooks`` only: the hook fault switch exists only in TEST_MODE (ADR-036).

One VM per mode is provisioned with the runner's pool provisioning (``agent_runner.vm.pool.Provisioner``: clone,
start with the control token parameter, health, sandbox self-test). The clone carries one extra Lima provisioning
step (``EVIDENCE_SUDO``) that gives the Lima default user ``sudo`` for the evidence collection; the boot hardening
still runs and everything a session sees is the production configuration (decisions ATS-01).

The runner stand-in of ATK-33/38/40 (``runner_standin.RunnerStandIn``: the runner's formal checks and tarball rewrite,
the GitHub double) is started once per suite run (decisions ATS-02).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.atk import catalog, redteam, report
from agent_doubles.atk.dispatch import Dispatcher
from agent_doubles.atk.driver import AttackVm, RunResult, run_mode
from agent_runner.guestlink.client import GuestClient
from agent_runner.vm.lima import Lima, run_limactl
from agent_runner.vm.pool import Provisioner, VmEnv, VmSettings
from agent_runner.vm.records import GoldenRef, TokenStore
from tests.platform.atk.fakes import selected_modes
from tests.platform.atk.runner_standin import RunnerStandIn

EVIDENCE_SUDO_SCRIPT = (
    "#!/bin/sh\n"
    "# Attack-suite test clone only: sudo for the evidence collection (members of agent-ctl = the Lima user).\n"
    "printf '%s\\n' '%agent-ctl ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/95-atk-suite\n"
    "chmod 0440 /etc/sudoers.d/95-atk-suite\n"
)
EVIDENCE_SUDO = ".provision += [" + json.dumps({"mode": "system", "script": EVIDENCE_SUDO_SCRIPT}) + "]"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "atk: attack suite against real VMs (scripts/run-atk.sh only)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the ``atk`` tests unless ``AGENT_ATK_RUN=1`` (they need Lima, a golden and the runner device)."""
    if os.environ.get("AGENT_ATK_RUN") == "1":
        return
    deselected = [item for item in items if item.get_closest_marker("atk") is not None]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.get_closest_marker("atk") is None]


class AtkLima(Lima):
    """The runner's Lima wrapper; clones get the evidence ``sudo`` step in addition to the runner's own steps."""

    def clone(self, source: str, name: str, *, sets: Sequence[str] = ()) -> None:
        super().clone(source, name, sets=[*sets, EVIDENCE_SUDO])


@dataclass
class LimaVm:
    lima: Lima
    name: str
    vm_id: str
    control_token: str
    socket: str
    directory: str = ""
    tunnels: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def open_tunnel(self, remote: int, local: int) -> None:
        """``127.0.0.1:remote`` in the guest -> ``127.0.0.1:local`` here, like the E2E orchestrator (ADR-034)."""
        argv = ["ssh", "-F", os.path.join(self.directory, "ssh.config"), "-N", "-o", "BatchMode=yes"]
        argv += ["-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5"]
        argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        argv += ["-R", f"127.0.0.1:{remote}:127.0.0.1:{local}", f"lima-{self.name}"]
        self.tunnels.append(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL))

    def close_tunnels(self) -> None:
        for tunnel in self.tunnels:
            tunnel.terminate()
            tunnel.wait(timeout=10)

    def shell(self, argv: Sequence[str]) -> Any:
        return self.lima.shell(self.name, list(argv), check=False, timeout=600)

    def copy_to(self, source: str, guest_path: str) -> None:
        self.lima.copy_to(source, self.name, guest_path)

    def link(self) -> GuestClient:
        return GuestClient(self.socket, self.control_token)


REDTEAM_DIR = Path(__file__).with_name("redteam")
KEY_PATTERN = re.compile(r"sk-or-[A-Za-z0-9_-]{16,}")


def live_key() -> str:
    """The OpenRouter key from the file descriptor of ``scripts/run-atk.sh --live`` (memory only, never a file)."""
    descriptor = int(os.environ.get("AGENT_ATK_LIVE_KEY_FD", "3"))
    chunks = []
    while chunk := os.read(descriptor, 4096):
        chunks.append(chunk)
    os.close(descriptor)
    key = b"".join(chunks).decode("utf-8", "replace").strip()
    if not key:
        pytest.fail(f"no OpenRouter key on file descriptor {descriptor} (scripts/run-atk.sh --live reads stdin)")
    return key


def redteam_attacks() -> tuple[list[catalog.Attack], list[catalog.Attack]]:
    """The red-team runs of every mode and of mode ``hooks`` only (ATS-03)."""
    prompt = (REDTEAM_DIR / "prompt.md").read_text(encoding="utf-8")
    harness = (REDTEAM_DIR / "harness.md").read_text(encoding="utf-8")
    every = [redteam.attack("double", prompt, harness)] if os.environ.get("AGENT_ATK_REDTEAM") == "1" else []
    hooks = [redteam.attack("live", prompt, harness)] if os.environ.get("AGENT_ATK_LIVE") == "1" else []
    return every, hooks


def secret_scan(out: Path, secrets_: Sequence[str]) -> list[str]:
    """Files of the results that contain a given secret or an OpenRouter key shape (relative paths only)."""
    hits = []
    for path in sorted(item for item in out.rglob("*") if item.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(secret and secret in text for secret in secrets_) or KEY_PATTERN.search(text):
            hits.append(str(path.relative_to(out)))
    return hits


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"{name} is required for the attack suite (scripts/run-atk.sh sets it)")
    return value


async def provision(lima: Lima, golden: str, home: Path, index: int) -> LimaVm:
    settings = VmSettings(cpus=4, memory_mib=8192, disk_gib=20, pool_size=0, memory_reserve_mib=0, vm_idle_seconds=3600)
    env = VmEnv(lima=lima, db=None, settings=settings, tokens=TokenStore(home), physical_mib=lambda: 0)
    token = secrets.token_urlsafe(32)
    vm_id = f"vm-{secrets.token_hex(4)}"
    name = await Provisioner(env).create(GoldenRef("atk", golden), vm_id=vm_id, token=token, pool=True)
    directory = await env.instance_dir(name)
    return LimaVm(lima, name, vm_id, token, os.path.join(directory, "sock", "agent-guest.sock"), directory)


async def run_suite(out: Path) -> dict[str, Any]:
    prefix, golden = required("AGENT_ATK_PREFIX"), required("AGENT_ATK_GOLDEN")
    port = int(required("AGENT_ATK_PORT"))
    only = [item for item in os.environ.get("AGENT_ATK_ONLY", "").split(",") if item]
    attacks = [catalog.BY_ID[item] for item in only if item in catalog.BY_ID] if only else list(catalog.ATTACKS)
    every, hooks_only = redteam_attacks()
    live = redteam.LiveUpstream(live_key()).start() if hooks_only else None
    redteam_reports: dict[str, Any] = {}
    parallel = int(os.environ.get("AGENT_ATK_PARALLEL", "3"))
    modes = selected_modes()
    tunnel = os.environ.get("AGENT_ATK_TUNNEL") == "1"
    lima = AtkLima(run_limactl, prefix=prefix)
    dispatcher = Dispatcher("127.0.0.1", port).start()
    runner = RunnerStandIn()
    results: list[RunResult] = []
    vms: list[LimaVm] = []
    log = (out / "runs.jsonl").open("a", encoding="utf-8")
    (out / "evidence").mkdir(exist_ok=True)

    def record(result: RunResult) -> None:
        results.append(result)
        log.write(json.dumps({**result.verdict.to_json(), "seconds": result.seconds}) + "\n")
        log.flush()
        if result.evidence is not None and result.verdict.attack_id.startswith("REDTEAM"):
            redteam_reports[f"{result.verdict.attack_id}/{result.verdict.mode}"] = redteam.report(result.evidence)
        if result.evidence is not None:  # the full evidence for the report (markers left out: they are secrets)
            data = {k: v for k, v in dataclasses.asdict(result.evidence).items() if k != "forbidden"}
            name = f"{result.verdict.attack_id}-{result.verdict.mode}.json"
            (out / "evidence" / name).write_text(json.dumps(data, indent=1, default=sorted) + "\n")

    try:
        with tempfile.TemporaryDirectory(prefix="atk-tokens-") as home:
            made = await asyncio.gather(
                *(provision(lima, golden, Path(home), i) for i in range(len(modes))), return_exceptions=True
            )
            vms = [vm for vm in made if isinstance(vm, LimaVm)]
            failed = [error for error in made if not isinstance(error, LimaVm)]
            if failed:
                raise failed[0]
            drivers = [
                AttackVm(
                    vm,
                    mode,
                    dispatcher,
                    port,
                    parallel=parallel,
                    runner=runner,
                    tunnel=tunnel,
                    live=live,
                    timeout=float(os.environ.get("AGENT_ATK_TIMEOUT", "180")),
                )
                for vm, mode in zip(vms, modes, strict=True)
            ]
            plans = {mode: [*attacks, *every, *(hooks_only if mode == "hooks" else [])] for mode in modes}
            outcomes = await asyncio.gather(*(run_mode(d, plans[d.mode], on_result=record) for d in drivers))
            pool = {driver.mode: driver.pool_selftest for driver in drivers}
    finally:
        log.close()
        dispatcher.stop()
        runner.close()
        if live is not None:
            live.stop()
        for vm in vms:
            vm.close_tunnels()
        for vm in vms if os.environ.get("AGENT_ATK_KEEP_VMS") != "1" else ():
            await asyncio.to_thread(lima.delete, vm.name)
    probes = {mode: probe for mode, (_runs, probe) in zip(modes, outcomes, strict=True)}
    verdicts = [result.verdict.to_json() for result in results]
    summary = {
        "verdicts": verdicts,
        "os_probe": probes,
        "pool_selftest": {mode: {"ok": data.get("ok")} for mode, data in pool.items()},
        "unknown_dispatch": dispatcher.unknown,
        "seconds": {f"{r.verdict.attack_id}/{r.verdict.mode}": r.seconds for r in results},
        "redteam": redteam_reports,
    }
    return write_results(out, summary, live)


def write_results(out: Path, summary: dict[str, Any], live: redteam.LiveUpstream | None) -> dict[str, Any]:
    """``results.json`` and the tables; the spend of a live run; a secret scan of everything written (07 section 6)."""
    if live is not None:
        summary["live"] = {
            "spent_usd": live.spent, "limit_usd": redteam.LIMIT_USD, "abort_usd": live.abort_usd,
            "aborted": live.aborted, "read_errors": live.errors,
        }  # fmt: skip
    (out / "results.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    (out / "table.md").write_text(report.table(summary["verdicts"]))
    (out / "probe.md").write_text(report.probe_table(summary["os_probe"]))
    (out / "os.md").write_text(report.os_table(summary["os_probe"]))
    summary["secret_scan"] = {"files_with_secrets": secret_scan(out, [live.key] if live is not None else [])}
    (out / "results.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    return summary


@pytest.fixture(scope="session")
def atk_results() -> dict[str, Any]:
    out = Path(required("AGENT_ATK_OUT"))
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_suite(out))
