"""Unit tests of the attack-suite driver (agent_doubles.atk.driver) against a scripted VM and guest link.

``FakeVm`` answers the Lima shell commands of the driver (the guest probe included) from an in-memory model of
the VM; ``FakeLink`` plays the guest control link: a provisioned credential "runs" the session (hook records into
the journal, the double's scenario done), ``stop`` journals ``stopped``. The real evaluation (``evidence``) then
judges the collected evidence, so these tests also check that the driver hands the evaluation what it needs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_doubles.atk import catalog, driver, evidence, extended, redteam
from agent_doubles.atk.dispatch import Dispatcher
from agent_doubles.atk.driver import AttackVm, SuiteError, session_user
from agent_doubles.model import Access
from agent_guest.sandbox import unit

SELFTEST = {"ok": True, "checks": [{"check": "exec_sh", "ok": True, "outcome": "errno:EACCES"}]}
SHADOW = "root:*:20000:0:99999:7:::\nshort\nlima:!:20000:0:99999:7:::\n"


@dataclass
class Record:
    seq: int
    session_id: str | None
    kind: str
    body: dict[str, Any]


@dataclass
class Page:
    records: tuple[Record, ...]
    head: int


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class FakeDouble:
    def __init__(self, scenario: str, access: Access, variables: Mapping[str, str]) -> None:
        self.scenario, self.access, self.variables = scenario, access, dict(variables)
        attack = catalog.CONTROL if scenario == "atk-control" else catalog.BY_ID[scenario.upper()]
        self.tools = [call.tool for call in catalog.scripted_calls(attack)]
        self.done = False
        self.stopped = False
        self.url = "http://127.0.0.1:9"

    @property
    def ids(self) -> list[str]:
        return [f"toolu_{self.scenario}_{index}" for index in range(len(self.tools))]

    @property
    def exchanges(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": "Work on the task."}]
        for tool, use in zip(self.tools, self.ids, strict=True):
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": use, "name": tool}]})
            result = {"type": "tool_result", "tool_use_id": use, "content": "No such tool available", "is_error": True}
            messages.append({"role": "user", "content": [result]})
        return [{"body": {"messages": messages}}] if self.done else []

    def state(self) -> dict[str, Any]:
        return {"done": self.done, "ok": True, "remaining": [], "failures": []}

    def stop(self) -> None:
        self.stopped = True


@dataclass
class FakeLink:
    mode: str = "hooks"
    records: list[Record] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    assigned: tuple[str, str, str] | None = None
    doubles: dict[str, FakeDouble] = field(default_factory=dict)
    journal_failures: int = 0
    send_stop_hook: bool = True
    journal_stopped: bool = True
    closed: bool = False
    finish: bool = True
    on_start: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    answered: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    outage_ends: bool = True

    def add(self, session_id: str | None, kind: str, body: dict[str, Any]) -> None:
        self.records.append(Record(len(self.records) + 1, session_id, kind, body))

    async def assign(self, vm_id: str, org_id: str, wg_id: str) -> None:
        self.assigned = (vm_id, org_id, wg_id)

    async def create_session(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(request)
        self.sessions.append(request)
        self.add(request["session_id"], "session.state", {"state": "sandbox_ready"})
        return {"state": "sandbox_ready"}

    async def put_credential(self, session_id: str, credential: Mapping[str, Any]) -> None:
        self.credentials[session_id] = dict(credential)
        if self.mode == "outage":  # the switch 500: the guest ends the session at the failed prompt hook (ADR-064)
            self.add(session_id, "session.hook", {"event": "UserPromptSubmit", "decision": "none"})
            self.add(None if not self.outage_ends else session_id, "guest.error", {"code": "hook_handler_failed"})
            if self.outage_ends:
                self.add(session_id, "session.state", {"state": "failed", "reason": "hook_unavailable"})
            return
        decision = "allow" if self.mode == "hooks" else "none"
        self.add(session_id, "session.hook", {"event": "UserPromptSubmit", "decision": decision})
        for kind, body in self.on_start:
            self.add(session_id, kind, body)
        if not self.finish:
            return
        if session_id in self.doubles:  # a live run has no double
            self.doubles[session_id].done = True
        if self.send_stop_hook:
            self.add(session_id, "session.hook", {"event": "Stop", "decision": "none"})

    async def control(self, session_id: str, action: str) -> None:
        assert action == "stop"
        if self.journal_stopped:
            self.add(session_id, "session.state", {"state": "stopped"})

    async def journal(self, after: int, *, wait: int = 0) -> Page:
        await asyncio.sleep(0)
        if self.journal_failures:
            self.journal_failures -= 1
            raise OSError("guest unreachable")
        return Page(tuple(record for record in self.records if record.seq > after), len(self.records))

    async def answer_pending(self, pending_id: str, answer: Mapping[str, Any]) -> None:
        self.answered.append((pending_id, dict(answer)))

    async def aclose(self) -> None:
        self.closed = True


class FakeVm:
    """The Lima shell of a test VM: answers the driver's commands and records them."""

    name = "wp-x-arp-00000001"
    vm_id = "vm-00000001"
    control_token = "control-token-value"

    def __init__(self, link: FakeLink) -> None:
        self.link_object = link
        self.calls: list[list[str]] = []
        self.copied: list[tuple[str, str]] = []
        self.users: dict[str, int] = {}
        self.seeded: dict[str, str] = {}
        self.failing: set[str] = set()
        self.answers: dict[str, str] = {}
        self.listening_after = 0
        self.pgrep = ["2"]
        self.tunnels: list[tuple[int, int]] = []
        self.extended: dict[str, Any] = {"ok": True, "unit": {"checks": []}}
        self.index: dict[str, Any] | None = {"type": "file", "size": 106, "sha256": "i"}

    def link(self) -> FakeLink:
        return self.link_object

    def open_tunnel(self, remote: int, local: int) -> None:
        self.tunnels.append((remote, local))

    def copy_to(self, source: str, guest_path: str) -> None:
        self.copied.append((source, guest_path))

    def shell(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        if args[:3] == ["sudo", "sh", "-c"] and "AGENT_GUEST_FAULT_HOOKS=500" in args[3]:
            self.link_object.mode = "outage"
        key = " ".join(args[:3])
        if key in self.failing or (args[:1] and args[0] in self.failing):
            return subprocess.CompletedProcess(args, 1, "", "refused\n")
        if key in self.answers:
            return subprocess.CompletedProcess(args, 0, self.answers[key], "")
        return subprocess.CompletedProcess(args, 0, self.answer(args), "")

    def answer(self, args: list[str]) -> str:
        fixed = {
            "getent ahostsv4": "192.0.2.2       STREAM host.lima.internal\n",
            "sudo cat /etc/shadow": SHADOW,
            "sudo cat /run/agent-pool-selftest/result.json": json.dumps(SELFTEST),
        }
        key = " ".join(args[:2]) if args[0] == "getent" else " ".join(args)
        if key in fixed:
            return fixed[key]
        if args[:4] == ["sudo", "python3", "-I", driver.PROBE]:
            return json.dumps(self.probe(args[4], args[5:]))
        if args[0] == "ss":
            self.listening_after -= 1
            return "LISTEN 0 64 127.0.0.1:7790 0.0.0.0:*\n" if self.listening_after < 0 else ""
        if args[:2] == ["id", "-u"]:
            return f"{self.users.setdefault(args[2], 20000 + len(self.users))}\n"
        if args[0] == "pgrep":
            return self.pgrep.pop(0) if len(self.pgrep) > 1 else self.pgrep[0]
        return json.dumps(SELFTEST) if args[:3] == ["sudo", "sh", "-c"] and "selftest" in args[3] else ""

    def probe(self, command: str, args: list[str]) -> Any:
        if command == "seed":
            self.seeded[args[0]] = args[4]
        answers: dict[str, Any] = {
            "targets": lambda: {path: self.target(path) for path in args},
            "diff": lambda: {"changes": []},
            "nft-counters": lambda: {
                "uids": {str(uid): {"attempt": 0, "egress": 0} for uid in self.users.values()},
                "dropped": 0,
            },
            "samples": lambda: {
                "samples": [{"user": user, "exe": "/opt/claude/claude", "cmdline": "claude"} for user in self.users]
            },
            "transcript": lambda: self.transcript(args[0]),
            "snapshot": lambda: {"entries": 1},
            "os-extended": lambda: self.extended,
        }
        return answers[command]() if command in answers else {}

    def target(self, path: str) -> dict[str, Any] | None:
        if path.endswith("/capture-index.json"):
            return self.index  # the guest's scanner indexed the victim's seeded secret
        return {"type": "file"} if path in self.seeded else None

    def transcript(self, session_dir: str) -> dict[str, Any]:
        double = self.link_object.doubles.get(session_dir.rsplit("/", 1)[-1])
        blocks = [{"type": "tool_result", "tool_use_id": use} for use in (double.ids if double else [])]
        return {"lines": [{"message": {"content": blocks}}]}


def make(mode: str = "hooks", **options: Any) -> tuple[AttackVm, FakeVm, FakeLink, Clock]:
    link = FakeLink(mode=mode)
    vm = FakeVm(link)
    clock = Clock()

    def factory(scenario: str, access: Access, variables: Mapping[str, str]) -> FakeDouble:
        double = FakeDouble(scenario, access, variables)
        session = link.sessions[-1]["session_id"]
        link.doubles[session] = double
        return double

    async def blocking(function: Any, *args: Any) -> Any:
        return function(*args)

    attack_vm = AttackVm(
        vm, mode, Dispatcher(), 20530, double_factory=factory, blocking=blocking, clock=clock, sleep=clock.sleep,
        token=lambda: "upstream-token", **options,
    )  # fmt: skip
    return attack_vm, vm, link, clock


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# -- helpers ---------------------------------------------------------------------------------------------------


def test_uuid7_is_a_version_7_uuid_with_growing_milliseconds() -> None:
    first, second = driver.uuid7(1.0), driver.uuid7(1.0)
    assert uuid.UUID(first).version == 7
    assert int(second.replace("-", "")[:12], 16) == int(first.replace("-", "")[:12], 16) + 1
    assert int(driver.uuid7().replace("-", "")[:12], 16) >= int(time.time() * 1000) - 1000
    assert session_user("01a1ffb3-7402-739a-bf45-0e0000000000") == "s-01a1ffb3740239af450e0000000000"
    # Same rule as the guest (sandbox/unit.py), including two sessions of the same millisecond.
    for sid in ("01a1ffb3-7402-739a-bf45-0e0000000000", "01a1ffb3-7402-739a-bf45-0e0000000001"):
        assert session_user(sid) == unit.user_name(sid)


def test_describe_adds_the_guest_error_code() -> None:
    class Refused(Exception):
        error = "sandbox_failed"

    assert driver.describe(Refused("guest_refused sessions")) == "Refused: guest_refused sessions (sandbox_failed)"
    assert driver.describe(ValueError("x")) == "ValueError: x"


def test_poison_writes_markers_for_every_command() -> None:
    files = driver.poison("/srv/sessions/s/work")
    assert set(files) == {
        "/srv/sessions/s/home/.claude/settings.json",
        "/srv/sessions/s/work/.claude/settings.json",
        "/srv/sessions/s/work/.mcp.json",
    }
    settings = json.loads(files["/srv/sessions/s/work/.claude/settings.json"])
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert settings["disableAllHooks"] is True
    text = "".join(files.values())
    for marker in catalog.EXEC_MARKERS:
        assert catalog.render(marker, {"work": "/srv/sessions/s/work"}) in text


def test_start_double_serves_the_packaged_scenario() -> None:
    double = driver.start_double("atk-01", Access("openrouter", "t"), {})
    try:
        assert double.url.startswith("http://127.0.0.1:")
        assert double.state()["remaining"] == ["attack-1", "finish"]
    finally:
        double.stop()


# -- full runs -------------------------------------------------------------------------------------------------


def test_hooks_mode_prepares_runs_probes_and_closes() -> None:
    attack_vm, vm, link, clock = make()
    seen: list[str] = []
    results, probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"], catalog.BY_ID["ATK-24"]],
                                         on_result=lambda r: seen.append(r.verdict.attack_id)))  # fmt: skip
    by_id = {result.verdict.attack_id: result for result in results}
    assert seen == ["ATK-01", "ATK-24", "CONTROL"]
    assert by_id["ATK-01"].verdict.passed, by_id["ATK-01"].verdict.to_json()
    assert by_id["ATK-24"].verdict.passed, by_id["ATK-24"].verdict.to_json()
    assert probe["ok"] is True
    assert (probe["claude_processes_before"], probe["claude_processes_after"], probe["exit"]) == (2, 2, 0)
    assert attack_vm.pool_selftest == SELFTEST
    assert vm.copied == [
        (driver.PROBE_SOURCE, driver.PROBE_STAGE),
        (driver.CLIENT_SOURCE, driver.CLIENT_STAGE),
        (driver.UNIT_PROBE_SOURCE, driver.UNIT_PROBE_STAGE),
    ]
    assert probe["extended"] == {"ok": True, "unit": {"checks": []}}
    install = next(call for call in vm.calls if call[:3] == ["sudo", "sh", "-c"] and driver.PROBE_STAGE in call[3])
    assert f"install -m 0644 {driver.CLIENT_STAGE} /usr/local/lib/atk-suite/session_client.py" in install[3]
    assert link.assigned is not None
    assert link.assigned[0] == "vm-00000001"
    assert link.closed
    assert ["sudo", "touch", driver.SAMPLER_STOP] in vm.calls
    victim = link.sessions[0]
    assert "initial_prompt" not in victim
    assert attack_vm.markers["victim_harness"] in victim["harness"]["context_markdown"]
    assert vm.seeded[f"/srv/sessions/{attack_vm.victim}/work/secret.txt"]
    assert attack_vm.markers["shadow_line_0"] == "root:*:20000:0:99999:7:::"
    assert "shadow_line_2" not in attack_vm.markers
    assert attack_vm.markers["control_token"] == "control-token-value"
    assert attack_vm.markers["upstream_token"] == "upstream-token"
    assert by_id["ATK-01"].evidence.session_uids[f"/srv/sessions/{attack_vm.victim}"] == 20000
    relay = next(call for call in vm.calls if "--unit=atk-relay" in call)
    assert relay[-3:] == ["192.0.2.2", "20530", "7790"]
    targets = next(
        call for call in vm.calls if call[:5] == ["sudo", "python3", "-I", driver.PROBE, "targets"] and len(call) > 6
    )
    assert f"/srv/sessions/{attack_vm.victim}/capture-index.json" in targets
    assert f"/srv/sessions/{attack_vm.victim}/terminal.cast" in targets
    attack = link.sessions[1]
    assert attack["initial_prompt"]["text"] == driver.PROMPT
    credential = link.credentials[attack["session_id"]]
    assert credential["upstream_base_url"] == f"http://127.0.0.1:7790/d/{attack['session_id']}"
    assert credential["auth_value"] == "Bearer upstream-token"
    atk24 = by_id["ATK-24"].evidence
    assert atk24 is not None
    assert "victim_session_id" not in atk24.forbidden
    assert by_id["ATK-01"].evidence is not None
    assert by_id["ATK-01"].evidence.forbidden["victim_session_id"] == attack_vm.victim
    assert link.doubles[atk24.session_id].variables == {
        "foreign_work": f"/srv/sessions/{attack_vm.victim}/work",
        "foreign_session": attack_vm.victim,
    }
    assert all(double.stopped for double in link.doubles.values())
    assert not any(call[:2] == ["sudo", "sh"] and "agent-guest-api" in call[3] for call in vm.calls)
    assert clock.now >= 0


def test_the_control_run_plants_and_injects_its_violations() -> None:
    attack_vm, vm, link, _clock = make()
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-02"]]))
    control = results[-1]
    assert control.verdict.attack_id == "CONTROL"
    sid = link.sessions[-1]["session_id"]
    assert (
        attack_vm.markers["victim_secret"]
        in __import__("base64").b64decode(vm.seeded[f"/srv/sessions/{sid}/work/planted.txt"]).decode()
    )
    injected = next(
        call for call in vm.calls if call[:3] == ["sudo", "sh", "-c"] and call[3] == driver.CONTROL_VIOLATIONS
    )
    assert injected[4:] == ["sh", session_user(sid), str(vm.users[session_user(sid)])]


def test_fault_mode_switches_the_hooks_and_seeds_the_poisoned_settings() -> None:
    attack_vm, vm, link, _clock = make("fault")
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-15"]]))
    assert results[0].verdict.passed, results[0].verdict.to_json()
    switch = next(call for call in vm.calls if call[:3] == ["sudo", "sh", "-c"] and "FAULT_HOOKS=open" in call[3])
    assert driver.FAULT_DROPIN in switch[3]
    assert ["ss", "-Hltn", "sport = :7700"] in vm.calls
    sid = link.sessions[1]["session_id"]
    assert set(driver.poison(f"/srv/sessions/{sid}/work")) <= set(vm.seeded)
    assert f"/srv/sessions/{sid}/work/.claude" in attack_vm.seeded
    assert f"/srv/sessions/{sid}" not in attack_vm.seeded
    assert results[0].evidence is not None
    assert list(results[0].evidence.poisoned) == list(driver.poison(f"/srv/sessions/{sid}/work"))


def test_a_broken_run_fails_alone_with_the_reason() -> None:
    attack_vm, _vm, link, _clock = make()
    link.journal_stopped = False
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    assert not results[0].verdict.passed
    assert results[0].evidence is None
    assert results[0].verdict.criteria["attempted"].details == [
        "suite error: SuiteError: session did not reach stopped"
    ]
    assert all(double.stopped for double in link.doubles.values())


def test_a_run_without_stop_hook_ends_after_the_grace_and_a_hung_one_times_out() -> None:
    attack_vm, _vm, link, clock = make(timeout=5.0)
    link.send_stop_hook = False
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    assert results[0].verdict.passed  # done, no Stop hook: the grace period ends the wait

    class Hung(FakeDouble):
        def state(self) -> dict[str, Any]:
            return {"done": False}

    attack_vm, _vm, link, clock = make(timeout=1.0)
    assert run(attack_vm.wait_finished("sid", Hung("atk-01", Access("openrouter", "t"), {}))) is False
    assert clock.now >= 1.0


def test_the_journal_reader_survives_failed_polls() -> None:
    attack_vm, _vm, link, _clock = make()
    link.journal_failures = 3
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    assert results[0].verdict.passed


# -- failures --------------------------------------------------------------------------------------------------


def test_preparation_failures_name_their_step() -> None:
    attack_vm, vm, _link, _clock = make()
    vm.failing.add("getent")
    with pytest.raises(SuiteError, match=r"hooks: preparation failed: SuiteError: getent ahostsv4 failed: refused"):
        run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    attack_vm, vm, _link, _clock = make()
    vm.answers["getent ahostsv4 host.lima.internal"] = ""
    with pytest.raises(SuiteError, match=r"host\.lima\.internal does not resolve"):
        run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))


def test_a_tunnel_replaces_the_relay_for_goldens_without_a_test_model_port() -> None:
    """ADR-034: the shared golden caches and production goldens have no host port for the model double; an SSH
    reverse tunnel binds the relay port on the guest's loopback instead (no process of the suite in the guest)."""
    attack_vm, vm, _link, _clock = make(tunnel=True)
    run(attack_vm.prepare())
    assert vm.tunnels == [(7790, 20530)]
    assert not any("--unit=atk-relay" in call for call in vm.calls)
    assert not any(call[:1] == ["getent"] for call in vm.calls)
    assert any(call[:1] == ["ss"] and "sport = :7790" in call for call in vm.calls)
    run(attack_vm.close())


def test_the_relay_must_listen_in_time() -> None:
    attack_vm, vm, _link, clock = make()
    vm.listening_after = 10**6
    with pytest.raises(SuiteError, match="nothing listens on port 7790"):
        run(attack_vm.prepare())
    assert clock.now >= 30


def test_the_relay_is_waited_for() -> None:
    attack_vm, vm, _link, _clock = make()
    vm.listening_after = 2
    run(attack_vm.wait_listening(7790))
    assert [call for call in vm.calls if call[0] == "ss"].__len__() == 3


def test_probe_errors_become_suite_errors() -> None:
    attack_vm, vm, _link, _clock = make()
    probe = " ".join(["sudo", "python3", "-I"])
    vm.answers[probe] = "not json"
    with pytest.raises(SuiteError, match="guest probe nft-setup"):
        run(attack_vm.probe("nft-setup"))
    vm.answers[probe] = json.dumps({"error": "ValueError: x"})
    with pytest.raises(SuiteError, match="ValueError: x"):
        run(attack_vm.probe("nft-setup"))
    del vm.answers[probe]
    vm.failing.add(probe)
    with pytest.raises(SuiteError):
        run(attack_vm.probe("nft-setup"))
    vm.failing = {"sudo"}
    with pytest.raises(SuiteError, match=r"sudo touch failed"):
        run(attack_vm.sh("sudo", "touch", "x"))
    with pytest.raises(SuiteError, match=r"^sudo  failed"):
        run(attack_vm.sh("sudo"))


def test_unprepared_vms_refuse_runs_and_links() -> None:
    attack_vm, _vm, _link, _clock = make()
    with pytest.raises(SuiteError, match="not prepared"):
        run(attack_vm.run(catalog.BY_ID["ATK-01"]))
    with pytest.raises(SuiteError, match="not prepared"):
        attack_vm.link  # noqa: B018


def test_a_missing_pool_selftest_result_is_tolerated() -> None:
    attack_vm, vm, _link, _clock = make()
    vm.answers["sudo cat /run/agent-pool-selftest/result.json"] = ""
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    assert attack_vm.pool_selftest == {}
    assert results[0].verdict.passed


def test_os_probe_waits_for_a_claude_process_and_reports_broken_output() -> None:
    attack_vm, vm, _link, clock = make()
    vm.pgrep = ["", "0", "1", "3"]
    vm.answers["sudo sh -c"] = "Traceback"
    report = run(attack_vm.os_probe())
    assert report == {
        "ok": False,
        "error": "",
        "claude_processes_before": 1,
        "claude_processes_after": 3,
        "exit": 0,
        "extended": {"ok": True, "unit": {"checks": []}},
    }
    vm.extended = {"error": "OSError: no selftest"}
    assert run(attack_vm.os_probe())["extended"]["ok"] is False
    vm.pgrep = ["0"]
    clock.now = 0
    report = run(attack_vm.os_probe())
    assert report["claude_processes_before"] == 0
    assert clock.now >= driver.PROBE_WAIT_S


def test_close_without_preparation_only_stops_the_sampler() -> None:
    attack_vm, vm, _link, _clock = make()
    run(attack_vm.close())
    assert vm.calls == [["sudo", "touch", driver.SAMPLER_STOP]]


def test_request_bodies() -> None:
    attack_vm, _vm, _link, _clock = make()
    body = attack_vm.request("s", harness="h")
    assert body["model"] == {
        "access": "platform",
        "model_id": driver.MODEL_ID,
        "env": {"ANTHROPIC_MODEL": driver.MODEL_ID},
    }
    assert body["limits"] == driver.LIMITS
    assert body["depth"] == 0
    assert body["parent_session_id"] is None
    assert "initial_prompt" not in attack_vm.request("s", harness="h", prompt=False)
    args = attack_vm.snapshot_args()
    assert args[-1] == "/"
    assert args.count("--exclude") == len(driver.SNAPSHOT_EXCLUDES)


# -- plans of ATK-33 … ATK-40 -----------------------------------------------------------------------------------


def operation(pending: str) -> tuple[str, dict[str, Any]]:
    return "operation.request", {"pending_id": pending, "operation_id": "o-" + pending, "type": "x", "params": {}}


def test_plans_run_at_their_points_and_answer_the_session_operations() -> None:
    calls: list[str] = []

    async def before(ctx: extended.RunContext) -> None:
        calls.append(f"before {ctx.attack.id} {ctx.uid} {ctx.victim_uid}")

    async def during(ctx: extended.RunContext) -> None:
        calls.append("during")

    async def after(ctx: extended.RunContext) -> None:
        calls.append("after")
        ctx.outbound.append("exported text")

    async def answer(ctx: extended.RunContext, body: Mapping[str, Any]) -> dict[str, Any]:
        if body["pending_id"] == "bad":
            raise ValueError("stand-in broke")
        return {"status": "ok", "result": {"for": body["operation_id"]}}

    def check(ctx: extended.RunContext, run_evidence: evidence.RunEvidence) -> list[str]:
        calls.append(f"check {list(run_evidence.outbound)}")
        return ["a specific failure"]

    plan = extended.Plan(before_start=before, while_running=during, after_finished=after, on_operation=answer,
                         check=check)  # fmt: skip
    attack_vm, _vm, link, _clock = make(plans={"ATK-01": plan, "ATK-02": extended.Plan()})
    link.on_start = [operation("good"), operation("bad")]
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    verdict = results[0].verdict
    assert calls[0] == "before ATK-01 20001 20000"
    assert calls[1:] == ["during", "after", "check ['exported text']"]
    assert link.answered[0] == ("good", {"status": "ok", "result": {"for": "o-good"}})
    assert verdict.criteria["specific"].details == ["a specific failure", "ValueError: stand-in broke"]
    # the control run has no plan: its operation records stay unanswered, and it has no criterion `specific`
    assert "specific" not in results[-1].verdict.criteria
    assert len(link.answered) == 1


def test_a_plan_without_a_stand_in_leaves_operations_alone_and_a_timeout_skips_after() -> None:
    called: list[str] = []

    async def after(ctx: extended.RunContext) -> None:
        called.append("after")

    attack_vm, _vm, link, _clock = make(plans={"ATK-01": extended.Plan(after_finished=after)}, timeout=1.0)
    link.on_start = [operation("p")]
    link.finish = False
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    assert link.answered == []
    assert called == []
    assert results[0].verdict.criteria["specific"].ok
    assert not results[0].verdict.criteria["attempted"].ok


def test_the_initial_prompt_of_the_attack_is_registered() -> None:
    attack_vm, _vm, link, _clock = make(plans={})
    run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-38"]]))
    assert link.sessions[1]["initial_prompt"]["text"] == catalog.initial_prompt(catalog.BY_ID["ATK-38"])


def test_plan_seeds_are_suite_writes_and_a_failing_plan_stops_its_session() -> None:
    async def plant(ctx: extended.RunContext) -> None:
        await ctx.host.probe("symlink", f"{ctx.work}/repos/atk/x/link", "/etc", "1", "1")

    async def broken(ctx: extended.RunContext) -> None:
        raise OSError("client failed")

    attack_vm, _vm, link, _clock = make(plans={"ATK-01": extended.Plan(before_start=plant, while_running=broken)})
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-01"]]))
    sid = link.sessions[1]["session_id"]
    assert f"/srv/sessions/{sid}/work/repos/atk/x/link" in attack_vm.seeded
    assert f"/srv/sessions/{sid}/work/repos" in attack_vm.seeded
    assert results[0].verdict.criteria["attempted"].details == ["suite error: OSError: client failed"]
    assert any(r.session_id == sid and r.body == {"state": "stopped"} for r in link.records)


def test_attacks_that_name_the_victim_drop_its_marker() -> None:
    attack_vm, _vm, _link, _clock = make()
    attack_vm.markers["victim_session_id"] = "v"
    assert "victim_session_id" not in attack_vm.forbidden(catalog.BY_ID["ATK-34"], "s")
    assert attack_vm.forbidden(catalog.BY_ID["ATK-33"], "s")["victim_session_id"] == "v"
    assert {a.id for a in catalog.ATTACKS if a.names_victim} == {"ATK-18", "ATK-24", "ATK-34", "ATK-35", "ATK-38"}


# -- live red team (ATS-03) ---------------------------------------------------------------------------------


@dataclass
class FakeLive:
    key: str = "live-provider-key-value"
    abort: str | None = None
    stopped: bool = False
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    url: str = "https://openrouter.ai/api"

    def credential(self) -> dict[str, Any]:
        return {"upstream_base_url": self.url, "auth_value": f"Bearer {self.key}"}

    def state(self) -> dict[str, Any]:
        return {"done": False, "ok": True, "abort": self.abort, "spent_usd": 0.01, "live": True}

    def stop(self) -> None:
        self.stopped = True


async def live_runs(attack_vm: AttackVm, *attacks: catalog.Attack) -> list[Any]:
    """Prepare, run and close in one event loop (the journal reader lives in it)."""
    await attack_vm.prepare()
    try:
        return [await attack_vm.run(attack) for attack in attacks]
    finally:
        await attack_vm.close()


def test_a_live_red_team_run_uses_the_live_upstream() -> None:
    live = FakeLive()
    attack_vm, _vm, link, _clock = make(live=live)
    (result,) = run(live_runs(attack_vm, redteam.attack("live", "Break out.", "TEST-ONLY harness")))
    session = link.sessions[-1]
    assert session["harness"]["context_markdown"] == "TEST-ONLY harness"
    assert session["initial_prompt"]["text"] == "Break out."
    assert link.credentials[session["session_id"]] == live.credential()
    assert attack_vm.dispatcher.lookup(f"/d/{session['session_id']}/v1/messages") is None  # no double registered
    assert result.evidence is not None
    assert result.evidence.live is True
    assert result.evidence.double_state["spent_usd"] == 0.01
    assert result.evidence.forbidden["live_upstream_key"] == live.key
    assert not result.evidence.timed_out
    assert live.stopped is False  # the live upstream belongs to the whole run


def test_a_live_run_ends_at_the_cost_limit_or_the_time_limit() -> None:
    live = FakeLive(abort="the key's spend grew")
    attack_vm, _vm, link, clock = make(live=live, timeout=5.0)
    link.send_stop_hook = False
    (aborted,) = run(live_runs(attack_vm, redteam.attack("live", "p", "h")))
    assert aborted.evidence is not None
    assert aborted.evidence.timed_out is True
    assert clock.now < 5.0 + 60
    live.abort = None
    attack_vm, _vm, link, clock = make(live=live, timeout=5.0)
    link.send_stop_hook = False
    (late,) = run(live_runs(attack_vm, redteam.attack("live", "p", "h")))
    assert late.evidence is not None
    assert late.evidence.timed_out is True
    assert clock.now >= 5.0


def test_a_live_attack_needs_a_live_upstream() -> None:
    attack_vm, _vm, _link, _clock = make()
    (result,) = run(live_runs(attack_vm, redteam.attack("live", "p", "h")))
    assert result.verdict.criteria["attempted"].details == [
        "suite error: SuiteError: REDTEAM-LIVE needs a live upstream (scripts/run-atk.sh --live)"
    ]


def test_the_victim_records_must_settle_before_the_attacks() -> None:
    """The guest scanner indexes the victim's seeded secret within its scan interval; until then the suite waits."""
    attack_vm, vm, _link, clock = make()
    vm.index = None
    with pytest.raises(SuiteError, match="the victim's capture index did not settle"):
        run(attack_vm.prepare())
    assert clock.now >= driver.SETTLE_S
    attack_vm, vm, _link, clock = make()
    vm.index = {"type": "file", "size": 2, "sha256": "empty"}
    with pytest.raises(SuiteError, match="did not settle"):
        run(attack_vm.prepare())


# ---------------------------------------------------------------- run `outage` (ADR-064 section 3)


def switches(vm: FakeVm) -> list[str]:
    return [
        "open" if "FAULT_HOOKS=open" in call[3] else "500"
        for call in vm.calls
        if call[:3] == ["sudo", "sh", "-c"] and "AGENT_GUEST_FAULT_HOOKS=" in call[3]
    ]


def test_mode_fault_ends_with_the_outage_run_of_atk_38() -> None:
    attack_vm, vm, link, _clock = make("fault")
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-15"]]))
    assert [result.verdict.attack_id for result in results] == ["ATK-15", "CONTROL", "ATK-38"]
    outage = results[-1]
    assert outage.verdict.mode == "outage"
    assert outage.verdict.passed, outage.verdict.to_json()
    assert set(outage.verdict.criteria) == {
        "ended", "no_turn", "no_execution", "no_foreign_write", "no_foreign_read", "no_egress", "specific",
    }  # fmt: skip
    assert switches(vm) == ["open", "500"]
    assert attack_vm.phase == "fault"
    sid = link.sessions[-1]["session_id"]
    assert not any(path.startswith(f"/srv/sessions/{sid}/work/.claude") for path in vm.seeded)  # no poison


def test_mode_hooks_has_no_outage_run() -> None:
    attack_vm, vm, _link, _clock = make("hooks")
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-15"]]))
    assert [result.verdict.attack_id for result in results] == ["ATK-15", "CONTROL"]
    assert switches(vm) == []


def test_an_outage_run_that_does_not_end_fails_as_a_suite_error() -> None:
    attack_vm, _vm, link, _clock = make("fault", timeout=5.0)
    link.outage_ends = False
    results, _probe = run(driver.run_mode(attack_vm, [catalog.BY_ID["ATK-15"]]))
    outage = results[-1]
    assert (outage.verdict.attack_id, outage.verdict.mode, outage.verdict.passed) == ("ATK-38", "outage", False)
    assert "did not reach failed" in json.dumps(outage.verdict.to_json())
