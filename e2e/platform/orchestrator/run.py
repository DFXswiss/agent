"""One E2E run: pre-checks once, then `repeat` iterations of setup → Playwright → final checks → teardown.

Topology T1: one device runs everything. T2: this device (second device) runs PostgreSQL, hub, browser relay,
control API and Playwright; the runner device (`--runner <ssh target>`, LAN only) runs the doubles, the runners, the
runner relay to the hub and the VMs, driven over one SSH connection per iteration (`node.py`).

Each iteration writes `<artifacts>/iter-<n>/`: `hub-device/` (logs, egress log, hub DB dump), `runner-device/` (T2),
`playwright/` (results, traces, videos), `web-coverage/` (browser coverage, informative), `summary.json`. The run
fails when Playwright fails, a final check fails (doubles, egress, audit chain, runners), VMs are left over or the
artefact scan finds a secret.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO, Any

from orchestrator import artifacts, checkout, prechecks, vms
from orchestrator.config import RunConfig, clean_env, home_relative
from orchestrator.context import Context
from orchestrator.control import ControlServer
from orchestrator.device import Device
from orchestrator.egress import violations
from orchestrator.golden import BUILT_MARK
from orchestrator.live_bridge import peers
from orchestrator.node import LocalNode, Node, NodeError, RemoteNode, ssh_argv
from orchestrator.relay import BROWSER_TAP, RUNNER_TAP
from orchestrator.services import (
    GITHUB_HTML_URL,
    Cluster,
    Double,
    Hub,
    Runner,
    Secrets,
    ServiceError,
    build_web,
    find_pg_bin,
    hub_env,
    runner_env,
    start_double,
    test_root_key,
    write_platform_key,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PRECHECK = 3
PLAYWRIGHT_TIMEOUT_S = 3600.0
SAMPLE_INTERVAL_S = 0.5
IDLE_SCENARIO = Path(__file__).with_name("idle-scenario.json")
IDLE_NAME = "idle"
ERRORS = (ServiceError, NodeError, vms.VmError, OSError)
# Warm pool per runner when a selected spec needs VMs and E2E_POOL_SIZE is not set (07-testing.md section 5.2).
VM_POOL_SIZE = 2
# Live runs (07-testing.md section 6): the operator's secrets arrive in `E2E_LIVE_*` variables (Playwright gets them,
# the artefact scan looks for them), the operator's non-secret live settings in `AGENT_LIVE_*` variables (Playwright
# only, e.g. the GitHub test repository: its name appears in results and is no secret), and HTTPS to the live
# providers is the only egress beside the doubles (`orchestrator/live_bridge.py`).
LIVE_PREFIX = "E2E_LIVE_"
LIVE_SETTINGS_PREFIX = "AGENT_LIVE_"
LIVE_HOSTS = ("openrouter.ai", "api.anthropic.com", "api.github.com", "codeload.github.com")
# The live bridge's journal on the runner device (`routes/quota.py`: `<run dir>/usage-journal.jsonl` + `-bridge`).
LIVE_BRIDGE_JOURNAL = "usage-journal-bridge.jsonl"
# The model double gets the run's platform key so its key document answers only the runners' spend poll.
MODEL_KEY_ENV = "MODEL_DOUBLE_KEY_TOKEN"
HTTPS_PORT = 443
# The process of the live bridge (`routes/quota.py` starts the usage double, which bridges in a live run).
LIVE_BRIDGE = "double-usage"


def live_addresses(hosts: tuple[str, ...] = LIVE_HOSTS) -> set[str]:
    """The current addresses of the live providers (resolved when the egress is checked)."""
    found: set[str] = set()
    for host in hosts:
        with contextlib.suppress(OSError):
            found.update(str(info[4][0]) for info in socket.getaddrinfo(host, HTTPS_PORT, proto=socket.IPPROTO_TCP))
    return found


def egress_patterns(config: RunConfig) -> list[str]:
    """Command-line patterns of the run's detached processes for the egress sampler: the Lima host agents of the
    run's VMs leave the runner's process tree and are matched by the run prefix in their instance names. Shared golden
    caches (`cache-golden-<sha12>`) are not matched: on a shared runner device another work package may be building
    one (with its downloads) during this run, and the run's own cache is only cloned (clones carry the run prefix);
    a run that had to build its cache fails (`Run.ensure_golden`).
    """
    return [config.run_prefix]


def say(message: str) -> None:
    with contextlib.suppress(OSError):  # a closed console (SSH gone) must not stop the teardown
        print(f"e2e: {message}", flush=True)


def lan_address(target_host: str) -> str:
    """This device's address on the route to the runner device (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((socket.gethostbyname(target_host), 22))
        return str(sock.getsockname()[0])


@dataclass
class Tools:
    """Everything the run starts or asks the system for; replaced by fakes in the unit tests."""

    hub_python: str = sys.executable
    pg_bin: str | None = None
    device: Callable[[Path], Device] = Device
    remote: Callable[[str, list[str], IO[bytes] | None], Node] = RemoteNode
    ssh: Callable[[list[str]], subprocess.CompletedProcess[str]] = lambda argv: subprocess.run(
        argv, capture_output=True, text=True, check=False
    )
    lan_address: Callable[[str], str] = lan_address
    playwright: str | None = None
    playwright_timeout: float = PLAYWRIGHT_TIMEOUT_S
    live_addresses: Callable[[], set[str]] = live_addresses


@dataclass
class Outcome:
    iteration: int
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    prechecks_only: bool = False

    def redact(self, text: str) -> str:
        for value in self.secrets:
            text = text.replace(value, "[run secret]")
        return text

    def fail(self, message: str) -> None:
        self.ok = False
        self.failures.append(self.redact(message))
        say(f"FAILED: {self.failures[-1]}")


def needs_tag(suite: Path, only: tuple[str, ...], tag: str) -> bool:
    """True when a selected spec title carries `tag`."""
    for path in sorted(p for sub in ("specs", "perf", "live") for p in (suite / sub).rglob("*.spec.ts")):
        for literal in re.findall(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", path.read_text(encoding="utf-8")):
            if tag in literal and (not only or any(f"@{test_id}" in literal for test_id in only)):
                return True
    return False


def needs_vm(suite: Path, only: tuple[str, ...]) -> bool:
    """True when a selected spec title carries `@needs-vm` (the run then ensures the golden image)."""
    return needs_tag(suite, only, "@needs-vm")


def needs_workvm(suite: Path, only: tuple[str, ...]) -> bool:
    """True when a selected spec title carries `@needs-workvm` (the run then ensures the work image cache)."""
    return needs_tag(suite, only, "@needs-workvm")


def download_area(ctx: Context) -> str:
    """The hub's download area (ADR-074 section 7) on the hub device, with its `runner/` directory: E2E-56 publishes a
    test release there (routes/own_device.py)."""
    downloads = f"{ctx.scratch}/downloads"
    ctx.hub_node.run(["mkdir", "-p", f"{downloads}/runner"])
    return downloads


def grep_args(config: RunConfig) -> list[str]:
    args = ["--project", config.project]
    if config.only:
        args += ["--grep", "|".join(f"@{test_id}(?![0-9])" for test_id in config.only)]
    if not config.live:
        args += ["--grep-invert", "@live"]
    return args


class Run:
    def __init__(self, config: RunConfig, environ: Mapping[str, str], tools: Tools | None = None) -> None:
        self.config = config
        self.environ = dict(environ)
        self.tools = tools or Tools()
        self.secrets = Secrets()
        if config.live:
            self.secrets.values += [value for name, value in environ.items() if name.startswith(LIVE_PREFIX) and value]
        self.root_key = ""

    # ------------------------------------------------------------------ nodes

    @property
    def remote_agent(self) -> str:
        return self.config.remote_agent_src or str(self.config.agent_src)

    @property
    def remote_core(self) -> str:
        return self.config.remote_core_src or str(self.config.core_src)

    def _remote_dir(self, local: Path) -> str:
        return home_relative(local, Path(self.environ.get("HOME", str(Path.home()))))

    def ensure_remote_checkout(self) -> None:
        assert self.config.runner_target is not None
        command = f"cd {self.remote_agent} && /bin/sh -c {_quote(checkout.ENSURE_SH)}"
        done = self.tools.ssh(
            ssh_argv(self.config.runner_target, command, self._known_hosts(), self.config.path_prefix)
        )
        if done.returncode != 0:
            raise ServiceError(f"agent checkout on the runner device not ready: {done.stderr.strip()[-300:]}")
        say(f"runner device: {done.stdout.strip() or 'venv ready'}")

    def _known_hosts(self) -> str:
        # Next to (not inside) the artefacts, so the runner device's host key is kept from run to run.
        path = self.config.artifacts.with_name(self.config.artifacts.name + "-known_hosts")
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def open_remote(self, iteration_dir: Path) -> Node:
        assert self.config.runner_target is not None
        run_dir = self._remote_dir(iteration_dir) + "-runner-device"
        python = checkout.python_of(self.remote_agent)
        command = f"cd {self.remote_core}/e2e/platform && exec {python} -m orchestrator.node --run-dir {run_dir}"
        argv = ssh_argv(self.config.runner_target, command, self._known_hosts(), self.config.path_prefix)
        with (iteration_dir / "runner-device-ssh.log").open("ab") as stderr:
            return self.tools.remote("runner-device", argv, stderr)

    # ------------------------------------------------------------------ phases

    def prepare(self) -> list[prechecks.Problem]:
        """Once per run: checkouts, web build, root key, pre-checks."""
        if "E2E_POOL_SIZE" not in self.environ and needs_vm(self.config.suite, self.config.only):
            self.config = dataclasses.replace(self.config, pool_size=VM_POOL_SIZE)
        config = self.config
        self.config.artifacts.mkdir(parents=True, exist_ok=True)
        _link_node_modules(config.suite)
        if config.topology == "t2":
            self.ensure_remote_checkout()
        else:
            local = LocalNode("device", self.tools.device(config.artifacts / "prepare"))
            # Preparation, not part of the hermetic run: uv may need the package index (same as in T2 over SSH).
            # Never the operator's live values (`--live-stdin`): they go to Playwright only.
            env = {n: v for n, v in self.environ.items() if not n.startswith((LIVE_PREFIX, LIVE_SETTINGS_PREFIX))}
            done = local.run(["/bin/sh", "-c", checkout.ENSURE_SH], env=env, cwd=str(config.agent_src))
            if done["rc"] != 0:
                raise ServiceError(f"agent checkout not ready: {done['stderr'].strip()[-300:]}")
        self.root_key = test_root_key(config.core_src)
        build_node = LocalNode("device", self.tools.device(config.artifacts / "prepare"))
        build_web(build_node, config, self.environ, self.root_key)
        say(f"web build ready ({'minified' if config.web_minify else 'unminified'}, test root key)")
        problems: list[prechecks.Problem] = []
        trace = prechecks.trace(config)
        if trace:
            problems.append(trace)
        return problems

    def iteration(self, number: int) -> Outcome:
        config = self.config
        outcome = Outcome(number, secrets=self.secrets.values)
        root = config.artifacts / f"iter-{number}"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        scratch = config.artifacts.with_name(config.artifacts.name + "-scratch") / f"iter-{number}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        hub_node: Node = LocalNode("hub-device", self.tools.device(root / "hub-device"))
        ctx: Context | None = None
        control: ControlServer | None = None
        try:
            runner_node = self.open_remote(root) if config.topology == "t2" else hub_node
            ctx = self.context(hub_node, runner_node, root, scratch)
            problems = self.node_prechecks(ctx)
            for problem in problems:
                outcome.fail(str(problem))
            outcome.prechecks_only = bool(problems)
            if not problems:
                self.cleanup_vms(runner_node)  # leftovers of an earlier, broken run of this prefix
                if runner_node.remote:
                    runner_node.run(["rm", "-rf", ctx.runner_scratch])
                    runner_node.abandon_cleanup(config.run_prefix, [ctx.runner_scratch])
                self.start_services(ctx)
                token = self.secrets.new()
                control = ControlServer(ctx, config.port("control"), token)
                control.start()
                ctx.control_url = control.url
                say(f"iteration {number}: services up; running Playwright")
                rc = self.playwright(ctx, root, token)
                if rc != 0:
                    outcome.fail(f"Playwright exited with {rc}")
                self.final_checks(ctx, outcome)
        except Exception as exc:
            outcome.fail(str(exc) if isinstance(exc, ERRORS) else f"{type(exc).__name__}: {exc}")
        finally:
            if control is not None:
                control.stop()
            if ctx is not None:
                self.teardown(ctx, root, outcome)
        self.write_summary(root, outcome, ctx)
        findings = artifacts.scan(root, self.secrets.values)
        for finding in findings:
            outcome.fail(f"secret in artefacts: {finding}")
        if findings:
            self.write_summary(root, outcome, ctx)
        return outcome

    def context(self, hub_node: Node, runner_node: Node, root: Path, scratch: Path) -> Context:
        config = self.config
        remote = runner_node.remote
        agent_src = self.remote_agent if remote else str(config.agent_src)
        ctx = Context(config, hub_node, runner_node, checkout.python_of(agent_src), agent_src=agent_src)
        ctx.core_on_runner = self.remote_core if remote else str(config.core_src)
        ctx.runner_run_dir = self._remote_dir(root) + "-runner-device" if remote else str(root / "hub-device")
        ctx.scratch = str(scratch)
        ctx.runner_scratch = self._remote_dir(scratch) + "-runner-device" if remote else str(scratch)
        if remote:
            assert config.runner_target is not None
            runner_host = config.runner_target.rsplit("@", 1)[-1]
            ctx.bind = self.tools.lan_address(runner_host)
            ctx.peer = runner_host if _is_ip(runner_host) else socket.gethostbyname(runner_host)
        return ctx

    def write_summary(self, root: Path, outcome: Outcome, ctx: Context | None) -> None:
        notes = [outcome.redact(note) for note in outcome.notes + (ctx.notes if ctx else [])]
        data = {"iteration": outcome.iteration, "ok": outcome.ok, "failures": outcome.failures, "notes": notes}
        (root / "summary.json").write_text(json.dumps(data, indent=1))

    def node_prechecks(self, ctx: Context) -> list[prechecks.Problem]:
        config = self.config
        found = [
            prechecks.contracts_version(ctx.hub_node, str(config.core_src), ctx.runner_node, ctx.agent_src),
            prechecks.budget(ctx.runner_node, config),
        ]
        hub_ports = [config.port(name) for name in ("control", "postgres", "hub", "browser")]
        if ctx.runner_node is not ctx.hub_node:
            hub_ports.append(config.port("public"))  # the operator relay (T2)
        runner_ports = [config.port(name) for name in ("public", "model", "model-relay", "github", "github-relay")]
        if ctx.runner_node is ctx.hub_node:
            found.append(prechecks.ports(ctx.hub_node, hub_ports + runner_ports))
        else:
            found += [prechecks.ports(ctx.hub_node, hub_ports), prechecks.ports(ctx.runner_node, runner_ports)]
            found.append(prechecks.ports(ctx.hub_node, [config.port("hub")], ctx.bind))
        return [problem for problem in found if problem is not None]

    def start_services(self, ctx: Context) -> None:
        config = self.config
        hub_node, runner_node = ctx.hub_node, ctx.runner_node
        bind = ctx.bind
        match = egress_patterns(config)
        if runner_node.remote:
            # Each device's own LAN address (the hub listens on it) and the other device are not egress.
            hub_node.egress_start(SAMPLE_INTERVAL_S, [ctx.peer, bind])
            runner_node.egress_start(SAMPLE_INTERVAL_S, [bind, ctx.peer], match)
        else:
            hub_node.egress_start(SAMPLE_INTERVAL_S, [], match)
        # E2E_PG_BIN: another PostgreSQL bin directory for a local run on a computer without PostgreSQL 16 (KOR-24)
        pg_bin = self.tools.pg_bin or self.environ.get("E2E_PG_BIN") or find_pg_bin(hub_node)
        base_env = clean_env(self.environ, {})
        cluster = Cluster(hub_node, pg_bin, f"{ctx.scratch}/pg", config.port("postgres"), base_env)
        cluster.password, cluster.reader_password = self.secrets.new(), self.secrets.new()
        cluster.start(f"{ctx.scratch}/pwfile")
        ctx.cluster = cluster
        owner_url, writer_url = cluster.create_hub_database(self.secrets.new(), self.secrets.new())
        env = hub_env(
            self.environ,
            config,
            writer_url=writer_url,
            signing_key=self.secrets.new(),
            root_key=self.root_key,
            bind=bind,
            downloads=download_area(ctx),
        )
        hub = Hub(hub_node, self.tools.hub_python, env, bind, config.port("hub"))
        say(f"hub migrate: {hub.migrate(owner_url)}")
        cluster.grant_reader()
        hub.start()
        ctx.hub = hub
        browser = ("127.0.0.1", config.port("browser"))
        hub_node.relay_start("browser-hub", browser, (bind, config.port("hub")), tap=f"{ctx.scratch}/{BROWSER_TAP}")
        public = ("127.0.0.1", config.port("public"))
        # Both links to the hub are tapped: E2E-09 scans everything the hub received and sent (routes/canary.py).
        runner_tap = f"{ctx.runner_scratch}/{RUNNER_TAP}"
        runner_node.relay_start("runner-hub", public, (bind, config.port("hub")), tap=runner_tap)
        ctx.links.update({"browser-hub": hub_node, "runner-hub": runner_node})
        # The runner device reaches the hub under the public origin (T2: across the LAN) before any runner starts.
        probe = runner_node.http("GET", f"http://127.0.0.1:{config.port('public')}/v1/health")
        if probe["status"] != 200:
            raise ServiceError(f"runner device cannot reach the hub through the runner relay ({probe['status']})")
        model, github, platform_key = self.start_doubles(ctx)
        golden = None
        if needs_vm(config.suite, config.only):
            golden = self.ensure_images(ctx)
            tunnels = [ctx.agent_python, "-m", "orchestrator.tunnels", "--prefix", config.run_prefix]
            tunnels += ["--port", str(config.port("model-relay"))]
            env = self.runner_side(ctx, clean_env(self.environ, {}))
            runner_node.spawn("tunnels", tunnels, env, cwd=f"{ctx.core_on_runner}/e2e/platform")
        key_file = f"{ctx.runner_scratch}/platform-openrouter.key"
        write_platform_key(runner_node, key_file, platform_key)
        for index in range(1, config.runners + 1):
            name = f"runner-{index}"
            env = runner_env(
                self.environ,
                config,
                index=index,
                home=f"{ctx.runner_scratch}/{name}",
                root_key=self.root_key,
                model_url=model.url,
                github_url=github.url,
                key_file=key_file,
            )
            env = self.runner_side(ctx, env)
            runner = Runner(runner_node, name, ctx.agent_python, env)
            if golden is not None:
                runner.adopt(golden)
            runner.start()
            ctx.runners[name] = runner

    def start_doubles(self, ctx: Context) -> tuple[Double, Double, str]:
        """The model double (idle scenario until a spec loads its own) and the GitHub double (empty world until a spec
        seeds it; https web links, served through its relay) on the runner device."""
        config, runner_node = self.config, ctx.runner_node
        platform_key = self.secrets.new()  # the runners' key file gets the same run secret
        model = start_double(
            runner_node,
            name="model",
            python=ctx.agent_python,
            env=self.runner_side(ctx, clean_env(self.environ, {MODEL_KEY_ENV: platform_key})),
            run_dir=ctx.runner_run_dir,
            raw_port=config.port("model"),
            relay_port=config.port("model-relay"),
            args=[
                "--scenario",
                f"{ctx.core_on_runner}/e2e/platform/orchestrator/idle-scenario.json",
                "--key-token-env",
                MODEL_KEY_ENV,
            ],
        )
        ctx.doubles["model"] = model
        ctx.links["model"] = runner_node
        relay = f"http://127.0.0.1:{config.port('github-relay')}"
        github = start_double(
            runner_node,
            name="github",
            python=ctx.agent_python,
            env=self.runner_side(ctx, clean_env(self.environ, {})),
            run_dir=ctx.runner_run_dir,
            raw_port=config.port("github"),
            relay_port=config.port("github-relay"),
            args=["--public-url", relay, "--html-url", GITHUB_HTML_URL],
        )
        ctx.doubles["github"] = github
        ctx.links["github"] = runner_node
        return model, github, platform_key

    @staticmethod
    def runner_side(ctx: Context, env: dict[str, str]) -> dict[str, str]:
        """The environment of a process on the runner device. In T2 the hub device's `HOME`, `TMPDIR`, `USER` and
        `LOGNAME` do not exist there (Lima keeps its instances below the home): `HOME` becomes `~`, which the runner
        device's node expands to its own home, and the others are left to that device."""
        if not ctx.runner_node.remote:
            return env
        local = {key: value for key, value in env.items() if key not in ("TMPDIR", "USER", "LOGNAME")}
        return {**local, "HOME": "~"}

    def ensure_golden(self, ctx: Context) -> str:
        argv = [ctx.agent_python, "-m", "orchestrator.golden", "--agent", ctx.agent_src, "--python", ctx.agent_python]
        argv += ["--home", f"{ctx.runner_scratch}/golden-cache-home"]
        env = self.runner_side(ctx, clean_env(self.environ, {}))
        done = ctx.runner_node.run(argv, env=env, cwd=f"{ctx.core_on_runner}/e2e/platform", timeout=1800)
        if done["rc"] != 0:
            raise ServiceError(f"golden image: {done['stderr'].strip()[-300:]}")
        name = str(done["stdout"]).strip()
        if BUILT_MARK in str(done["stderr"]):
            # A build downloads packages; its detached Lima host agent is not sampled (`egress_patterns`), so a run
            # never counts as hermetic when it built its golden image: build it first (tools/wp/golden-cache.sh).
            raise ServiceError(f"golden image {name} was built inside the run; build the cache before the run")
        return name

    def ensure_images(self, ctx: Context) -> str:
        """The golden image and, when a selected spec needs work VMs, the work image cache (both noted)."""
        golden = self.ensure_golden(ctx)
        ctx.notes.append(f"golden image {golden}")
        if needs_workvm(self.config.suite, self.config.only):
            ctx.workvm_image = self.ensure_workvm_image(ctx)
            ctx.notes.append(f"work image {ctx.workvm_image}")
        return golden

    def ensure_workvm_image(self, ctx: Context) -> str:
        argv = [ctx.agent_python, "-m", "orchestrator.workvm_image", "--agent", ctx.agent_src]
        argv += ["--python", ctx.agent_python, "--home", f"{ctx.runner_scratch}/workvm-image-home"]
        env = self.runner_side(ctx, clean_env(self.environ, {}))
        done = ctx.runner_node.run(argv, env=env, cwd=f"{ctx.core_on_runner}/e2e/platform", timeout=3600)
        if done["rc"] != 0:
            raise ServiceError(f"work image: {done['stderr'].strip()[-300:]}")
        name = str(done["stdout"]).strip()
        if BUILT_MARK in str(done["stderr"]):
            # same rule as the golden cache: a build downloads packages outside the egress sampler
            raise ServiceError(f"work image {name} was built inside the run; build the cache before the run")
        return name

    def playwright(self, ctx: Context, root: Path, token: str) -> int:
        config = self.config
        cwd = root / "web-coverage"
        cwd.mkdir(parents=True, exist_ok=True)
        binary = self.tools.playwright or str(config.core_src / "web" / "node_modules" / ".bin" / "playwright")
        argv = [binary, "test", "--config", str(config.suite / "playwright.config.ts"), *grep_args(config)]
        env = clean_env(
            self.environ,
            {
                "E2E_CONTROL_URL": ctx.control_url,
                "E2E_CONTROL_TOKEN": token,
                "E2E_HUB_ORIGIN": config.public_origin,
                "E2E_BROWSER_HUB": ctx.browser_address,
                "E2E_ITERATION_DIR": str(root),
                "E2E_TOPOLOGY": config.topology,
                "CI": "1",
                **{name: value for name, value in self.environ.items() if name.startswith("E2E_PERF_")},
                **{
                    n: v
                    for n, v in self.environ.items()
                    if config.live and n.startswith((LIVE_PREFIX, LIVE_SETTINGS_PREFIX))
                },
            },
        )
        ctx.hub_node.spawn("playwright", argv, env, cwd=str(cwd))
        return ctx.hub_node.wait_exit("playwright", self.tools.playwright_timeout)

    def final_checks(self, ctx: Context, outcome: Outcome) -> None:
        for name, double in ctx.doubles.items():
            state = double.state()
            if state.get("scenario") == IDLE_NAME:
                if state.get("failures") or state.get("requests"):
                    outcome.fail(f"{name} double: unexpected requests while idle ({state.get('failures')})")
            elif not state.get("ok"):
                detail = f"remaining {state.get('remaining')}, failures {state.get('failures')}"
                outcome.fail(f"{name} double: state not ok ({detail})")
        for name, runner in ctx.runners.items():
            if runner.state() == "stopped" and not runner.stopped_on_request:
                outcome.fail(f"{name} is not running any more:\n{runner.node.log_tail(name)}")
        hub = ctx.hub
        if hub is not None and hub.has_command("audit", "verify"):
            done = hub.node.run([hub.python, "-m", "agent_hub", "audit", "verify"], env=hub.env)
            if done["rc"] != 0:
                outcome.fail(f"hub audit chain does not verify: {done['stdout'].strip()[-300:]}")
        else:
            outcome.notes.append("audit chain: agent-hub audit verify not available yet (HUB-16)")

    def egress_check(self, ctx: Context, root: Path, outcome: Outcome) -> None:
        records: list[dict[str, Any]] = []
        for node in dict.fromkeys((ctx.hub_node, ctx.runner_node)):
            records += [dict(record, device=node.name) for record in node.egress_records()]
        links: dict[str, int] = {}
        for record in records:
            if record.get("event") == "open":
                links[record["link"]] = links.get(record["link"], 0) + 1
        bad = violations(records)
        if self.config.live and bad:
            bad = self.outside_live_bridge(ctx, bad)
        summary = {"connections_per_link": links, "violations": bad, "records": len(records)}
        (root / "egress-summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
        for record in bad:
            if "error" in record:
                outcome.fail(f"egress sampler on {record['device']}: {record['error']}")
            else:
                outcome.fail(f"egress to {record['remote']} by {record['command']} on {record['device']}")

    def outside_live_bridge(self, ctx: Context, bad: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Live runs: HTTPS from the live bridge's own process to a current address of a live provider - or to an
        address the bridge journaled for its upstream connections (host name checked by TLS; providers such as GitHub
        answer from changing addresses) - is allowed; every other connection (also a VM's own, through its Lima host
        agent) stays a violation."""
        addresses = self.tools.live_addresses() | self.bridge_peers(ctx)
        bridges = {
            (node.name, info.get("pid"))
            for node in dict.fromkeys((ctx.hub_node, ctx.runner_node))
            for name, info in node.processes().items()
            if name == LIVE_BRIDGE
        }
        return [
            record
            for record in bad
            if record.get("port") != HTTPS_PORT
            or record.get("host") not in addresses
            or (record.get("device"), record.get("pid")) not in bridges
        ]

    @staticmethod
    def bridge_peers(ctx: Context) -> set[str]:
        """The upstream addresses in the live bridge's journal (none when the bridge never ran)."""
        try:
            text = ctx.runner_node.read_text(f"{ctx.runner_run_dir}/{LIVE_BRIDGE_JOURNAL}")
        except ERRORS:
            return set()
        return peers(text)

    def teardown(self, ctx: Context, root: Path, outcome: Outcome) -> None:
        """Every step runs even when an earlier one fails; failures fail the iteration."""
        runner, hub_node = ctx.runner_node, ctx.hub_node
        steps: list[tuple[str, Callable[[], object]]] = [("egress", lambda: self.egress_check(ctx, root, outcome))]
        cluster = ctx.cluster
        if cluster is not None:
            steps.append(("hub database dump", lambda: cluster.dump(str(root / "hub-device" / "hub-db.sql"))))
        steps.append(("runner device processes", runner.shutdown))
        steps.append(("hub device processes", hub_node.shutdown))
        steps.append(("VM cleanup", lambda: outcome.notes.append(f"VMs deleted: {self.cleanup_vms(runner)}")))
        if runner.remote:
            steps.append(("runner device artefacts", lambda: _extract(runner.archive(), root / "runner-device")))
            steps.append(("runner device scratch", lambda: runner.run(["rm", "-rf", ctx.runner_scratch])))
        steps.append(("scratch", lambda: shutil.rmtree(ctx.scratch, ignore_errors=True)))
        for name, step in steps:
            try:
                step()
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ERRORS) else f"{type(exc).__name__}: {exc}"
                outcome.fail(f"teardown ({name}): {detail}")
        if isinstance(runner, RemoteNode):
            runner.close()

    def cleanup_vms(self, node: Node) -> list[str]:
        return vms.cleanup(node, self.config.run_prefix)

    # ------------------------------------------------------------------ run

    def execute(self) -> int:
        try:
            problems = self.prepare()
        except (ServiceError, NodeError, OSError) as exc:
            say(f"FAILED before the run: {exc}")
            return EXIT_FAILED
        if problems:
            for problem in problems:
                say(str(problem))
            return EXIT_PRECHECK
        for number in range(1, self.config.repeat + 1):
            outcome = self.iteration(number)
            say(f"iteration {number}/{self.config.repeat}: {'green' if outcome.ok else 'RED'}")
            for note in outcome.notes:
                say(f"note: {outcome.redact(note)}")
            if not outcome.ok:
                return EXIT_PRECHECK if outcome.prechecks_only and len(outcome.failures) == 1 else EXIT_FAILED
        say(f"all {self.config.repeat} iteration(s) green")
        return EXIT_OK


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _is_ip(host: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host))


def _link_node_modules(suite: Path) -> None:
    """`e2e/platform/node_modules` → `web/node_modules` (one set of web dependencies, WEB-01)."""
    link = suite / "node_modules"
    if not link.exists():
        if link.is_symlink():
            link.unlink()
        link.symlink_to(Path("..") / ".." / "web" / "node_modules")


def _extract(archive: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(archive)), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")


def install_sigterm() -> None:
    def on_term(_signum: int, _frame: FrameType | None) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_term)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
