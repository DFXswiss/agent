"""Unit tests of the in-VM evidence collector (agent_doubles.atk.guest_probe) on a temporary directory tree.

The commands that need root or a Linux kernel (``nft``, ``/proc`` of session users) run against injected stand-ins:
a recording ``run`` for ``nft`` and a fake ``/proc`` tree whose directories carry the UIDs to sample.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pwd
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_doubles.atk import guest_probe

UID, GID = os.getuid(), os.getgid()


class Runs:
    def __init__(self, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.stdout = stdout

    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append((list(argv), stdin))
        return self.stdout


def probe(argv: list[str], state: Path, run: Runs | None = None) -> tuple[int, Any]:
    out = io.StringIO()
    code = guest_probe.main(argv, out=out, run=run or Runs(), state_dir=str(state))
    return code, json.loads(out.getvalue())


# -- file system -----------------------------------------------------------------------------------------------


def test_entry_describes_files_directories_links_and_others(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("abc")
    (tmp_path / "d").mkdir(mode=0o750)
    (tmp_path / "l").symlink_to("f")
    os.mkfifo(tmp_path / "p")
    info = os.lstat(tmp_path / "f")
    assert guest_probe.entry(str(tmp_path / "f")) == ["f", 0o644 & ~0o022 | (info.st_mode & 0o777), UID, GID, 3,
                                                        info.st_mtime_ns, info.st_ino]  # fmt: skip
    assert guest_probe.entry(str(tmp_path / "d")) == ["d", os.lstat(tmp_path / "d").st_mode & 0o777, UID, GID]
    link, other = guest_probe.entry(str(tmp_path / "l")), guest_probe.entry(str(tmp_path / "p"))
    assert link is not None
    assert other is not None
    assert (link[0], link[4], other[0]) == ("l", "f", "o")
    assert guest_probe.entry(str(tmp_path / "missing")) is None


def test_walk_skips_excluded_prefixes_follows_no_links_and_survives_unreadable_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "keep" / "sub").mkdir(parents=True)
    (tmp_path / "keep" / "sub" / "x").write_text("x")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "y").write_text("y")
    (tmp_path / "skipped-not").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "keep")
    (tmp_path / "closed").mkdir()
    real_listdir = os.listdir

    def listdir(path: str) -> list[str]:
        if path.endswith("closed"):
            raise PermissionError(path)
        names = real_listdir(path)
        return [*names, "vanished"] if path == str(tmp_path / "keep") else names

    monkeypatch.setattr(os, "listdir", listdir)
    (tmp_path / "tmp-a").mkdir()
    found = guest_probe.walk(
        [str(tmp_path), str(tmp_path / "skip")], [str(tmp_path / "skip") + "/", f"{tmp_path}/tmp-*"]
    )
    names = {os.path.relpath(path, tmp_path) for path in found}
    assert names == {".", "keep", "keep/sub", "keep/sub/x", "skipped-not", "link", "closed"}
    assert found[str(tmp_path / "link")][0] == "l"


def test_diff_names_changes_and_the_lines_of_tracked_files() -> None:
    old = {
        "entries": {"/a": ["f", 1], "/b": ["f", 1], "/etc/passwd": ["f", 1]},
        "texts": {"/etc/passwd": ["root:x", "s-old:x"]},
    }
    new = {"entries": {"/a": ["f", 1], "/c": ["f", 1], "/etc/passwd": ["f", 2]},
           "texts": {"/etc/passwd": ["root:x", "s-new:x"]}}  # fmt: skip
    assert guest_probe.diff(old, new) == [
        {"path": "/b", "before": ["f", 1], "after": None},
        {"path": "/c", "before": None, "after": ["f", 1]},
        {"path": "/etc/passwd", "before": ["f", 1], "after": ["f", 2], "added": ["s-new:x"], "removed": ["s-old:x"]},
    ]
    assert guest_probe.diff({"entries": {}}, {"entries": {"/x": ["d"]}}) == [
        {"path": "/x", "before": None, "after": ["d"]}
    ]


def test_text_lines_and_target_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "t").write_text("a\nb\n")
    assert guest_probe.text_lines([str(tmp_path / "t"), str(tmp_path / "missing")]) == {str(tmp_path / "t"): ["a", "b"]}
    state = guest_probe.target_state(str(tmp_path / "t"))
    assert state is not None
    assert state["type"] == "file"
    assert state["sha256"] == hashlib.sha256(b"a\nb\n").hexdigest()
    assert guest_probe.target_state(str(tmp_path))["type"] == "dir"  # type: ignore[index]
    os.mkfifo(tmp_path / "p")
    assert guest_probe.target_state(str(tmp_path / "p"))["type"] == "other"  # type: ignore[index]
    assert guest_probe.target_state(str(tmp_path / "missing")) is None
    assert guest_probe.target_state(str(tmp_path / "t" / "below-a-file")) is None
    monkeypatch.setattr(guest_probe, "HASH_LIMIT", 1)
    assert "sha256" not in guest_probe.target_state(str(tmp_path / "t"))  # type: ignore[operator]


def test_seed_writes_the_file_and_missing_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "settings.json"
    guest_probe.seed(str(target), UID, GID, 0o600, b"{}")
    assert target.read_bytes() == b"{}"
    assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "a").stat().st_mode & 0o777 == 0o700
    guest_probe.seed(str(target), UID, GID, 0o640, b"[]")
    assert (target.read_bytes(), target.stat().st_mode & 0o777) == (b"[]", 0o640)
    (tmp_path / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError, match=r"symbolic link"):
        guest_probe.seed(str(tmp_path / "link"), UID, GID, 0o600, b"x")
    assert not (tmp_path / "elsewhere").exists()


# -- packet filter ---------------------------------------------------------------------------------------------


def nft_rule(table: str, chain: str, comment: str | None, packets: int | None, *, drop: bool = False) -> dict[str, Any]:
    expr: list[dict[str, Any]] = [{"match": {"left": {"meta": {"key": "skuid"}}, "right": {"range": [20000, 29999]}}}]
    if packets is not None:
        expr.append({"counter": {"packets": packets, "bytes": packets * 60}})
    if drop:
        expr.append({"drop": None})
    rule: dict[str, Any] = {"family": "inet", "table": table, "chain": chain, "handle": 1, "expr": expr}
    if comment is not None:
        rule["comment"] = comment
    return {"rule": rule}


def test_nft_counters_per_uid_and_the_session_drops() -> None:
    document = {
        "nftables": [
            {"metainfo": {"version": "1.0.9"}},
            {"table": {"name": "atk_audit"}},
            nft_rule("atk_audit", "attempt", "uid-20001", 3),
            nft_rule("atk_audit", "egress", "uid-20001", 0),
            nft_rule("atk_audit", "attempt", "uid-20002", 1),
            nft_rule("atk_audit", "attempt", "other", 9),
            nft_rule("atk_audit", "attempt", None, None),
            nft_rule("agent_sessions", "out", None, 4, drop=True),
            nft_rule("agent_sessions", "out", None, 7),
            {"rule": "not an object"},
        ]
    }
    assert guest_probe.nft_counters(document) == {
        "uids": {"20001": {"attempt": 3, "egress": 0}, "20002": {"attempt": 1, "egress": -1}},
        "dropped": 4,
    }
    assert guest_probe.nft_counters({}) == {"uids": {}, "dropped": 0}


def test_nft_commands_load_and_track_through_nft(tmp_path: Path) -> None:
    runs = Runs()
    assert probe(["nft-setup"], tmp_path, runs) == (0, {})
    assert probe(["nft-track", "20007"], tmp_path, runs) == (0, {})
    assert runs.calls[0] == (["nft", "-f", "-"], guest_probe.AUDIT_TABLE)
    assert runs.calls[1] == (
        ["nft", "-f", "-"],
        'add rule inet atk_audit attempt meta skuid 20007 counter comment "uid-20007"\n'
        'add rule inet atk_audit egress meta skuid 20007 counter comment "uid-20007"\n',
    )
    assert "priority -10" in guest_probe.AUDIT_TABLE
    assert "priority 10" in guest_probe.AUDIT_TABLE
    assert probe(["nft-track", "1000"], tmp_path, runs) == (1, {"error": "ValueError: not a session UID"})
    listing = Runs(json.dumps({"nftables": [nft_rule("atk_audit", "egress", "uid-20007", 2)]}))
    assert probe(["nft-counters"], tmp_path, listing) == (
        0,
        {"dropped": 0, "uids": {"20007": {"attempt": -1, "egress": 2}}},
    )
    assert listing.calls == [(["nft", "-j", "list", "ruleset"], None)]


def test_run_command_returns_stdout_and_raises_on_failure() -> None:
    assert (
        guest_probe.run_command([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], "hi") == "hi"
    )
    with pytest.raises(subprocess.CalledProcessError):
        guest_probe.run_command([sys.executable, "-c", "raise SystemExit(3)"])


# -- processes and transcripts ---------------------------------------------------------------------------------


def fake_proc(
    root: Path, pid: int, exe: str, cmdline: bytes, cgroup: str = "0::/system.slice/agent-session-x.service\n"
) -> None:
    base = root / str(pid)
    base.mkdir(parents=True)
    (base / "exe").symlink_to(exe)
    (base / "cmdline").write_bytes(cmdline)
    (base / "cgroup").write_text(cgroup)


def test_session_processes_reads_executable_and_command_line(tmp_path: Path) -> None:
    fake_proc(tmp_path, 41, "/opt/claude/claude", b"/opt/claude/claude\0--restricted\0")
    (tmp_path / "self").mkdir()
    (tmp_path / "42").mkdir()  # ended between listdir and readlink
    found = guest_probe.session_processes(str(tmp_path), uids={UID})
    assert found == [{"pid": 41, "uid": UID, "user": guest_probe.user_name(UID), "exe": "/opt/claude/claude",
                      "cmdline": "/opt/claude/claude --restricted", "unit": "agent-session-x.service"}]  # fmt: skip
    assert guest_probe.session_processes(str(tmp_path)) == []  # not a session UID
    assert guest_probe.user_name(2**31 - 3) == "?"


def test_sample_records_each_process_once_until_the_stop_file(tmp_path: Path) -> None:
    proc, out, stop = tmp_path / "proc", tmp_path / "samples.jsonl", tmp_path / "stop"
    fake_proc(proc, 7, "/opt/claude/claude", b"claude\0")
    rounds: list[float] = []

    def sleep(period: float) -> None:
        rounds.append(period)
        if len(rounds) == 1:
            fake_proc(proc, 8, "/usr/bin/sleep", b"sleep\x001\0")
        if len(rounds) == 3:
            stop.touch()

    assert guest_probe.sample(str(out), str(stop), proc=str(proc), uids={UID}, sleep=sleep) == 3
    samples = guest_probe.read_lines(str(out))
    assert [(s["pid"], s["exe"]) for s in samples] == [(7, "/opt/claude/claude"), (8, "/usr/bin/sleep")]
    assert rounds == [guest_probe.SAMPLE_PERIOD] * 3
    assert probe(["samples", str(out)], tmp_path)[1]["samples"] == samples
    assert guest_probe.read_lines(str(tmp_path / "missing")) == []


def test_sample_command_stops_at_once_when_the_stop_file_exists(tmp_path: Path) -> None:
    (tmp_path / "stop").touch()
    assert probe(["sample", str(tmp_path / "s.jsonl"), str(tmp_path / "stop")], tmp_path) == (0, {"rounds": 0})


def test_transcript_reads_every_project_file_and_skips_broken_lines(tmp_path: Path) -> None:
    project = tmp_path / "home" / ".claude" / "projects" / "-srv-x-work"
    project.mkdir(parents=True)
    (project / "b.jsonl").write_text('{"n": 2}\n')
    (project / "a.jsonl").write_text('{"n": 1}\nnot json\n\n')
    assert probe(["transcript", str(tmp_path)], tmp_path) == (0, {"lines": [{"n": 1}, {"n": 2}]})
    assert guest_probe.transcript(str(tmp_path / "none")) == []


# -- command line ----------------------------------------------------------------------------------------------


def test_snapshot_and_diff_commands_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, state = tmp_path / "root", tmp_path / "state"
    (root / "skip").mkdir(parents=True)
    state.mkdir()
    passwd = tmp_path / "passwd"
    passwd.write_text("root:x\n")
    monkeypatch.setattr(guest_probe, "ACCOUNT_FILES", (str(passwd),))
    assert probe(["snapshot", "before", "--exclude", str(root / "skip"), str(root)], state) == (0, {"entries": 1})
    (root / "new").write_text("x")
    passwd.write_text("root:x\ns-0123456789ab:x\n")
    os.utime(passwd, ns=(1, 1))
    assert probe(["snapshot", "after", str(root), str(passwd)], state)[1] == {"entries": 4}
    code, result = probe(["diff", "before", "after"], state)
    assert code == 0
    changes = {change["path"]: change for change in result["changes"]}
    assert set(changes) == {str(root / "new"), str(root / "skip"), str(passwd)}
    assert changes[str(passwd)]["added"] == ["s-0123456789ab:x"]
    assert probe(["diff", "before", "../x"], state) == (1, {"error": "ValueError: snapshot names are alphanumeric"})


def test_targets_and_seed_commands(tmp_path: Path) -> None:
    encoded = base64.b64encode(b"poison").decode()
    target = tmp_path / "w" / ".mcp.json"
    assert probe(["seed", str(target), str(UID), str(GID), "600", encoded], tmp_path) == (0, {})
    code, result = probe(["targets", str(target), str(tmp_path / "gone")], tmp_path)
    assert code == 0
    assert result[str(target)]["sha256"] == hashlib.sha256(b"poison").hexdigest()
    assert result[str(tmp_path / "gone")] is None


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        ([], "ValueError: unknown command "),
        (["nope"], "ValueError: unknown command nope"),
        (["seed", "only-one"], "ValueError: not enough values to unpack (expected 5, got 1)"),
        (["samples"], "IndexError: list index out of range"),
    ],
)
def test_invalid_commands_print_an_error(tmp_path: Path, argv: list[str], error: str) -> None:
    assert probe(argv, tmp_path) == (1, {"error": error})


def test_main_reads_sys_argv_and_writes_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["guest_probe.py", "targets", str(tmp_path / "none")])
    assert guest_probe.main() == 0
    assert json.loads(capsys.readouterr().out) == {str(tmp_path / "none"): None}


def test_failed_nft_is_an_error(tmp_path: Path) -> None:
    def failing(argv: Sequence[str], stdin: str | None = None) -> str:
        raise subprocess.CalledProcessError(1, list(argv))

    out = io.StringIO()
    assert guest_probe.main(["nft-setup"], out=out, run=failing, state_dir=str(tmp_path)) == 1
    assert json.loads(out.getvalue())["error"].startswith("CalledProcessError:")


# -- ATS-02: links, devices, tokens, the session-side client, pasted prompts -----------------------------------


def test_unit_of_reads_the_last_cgroup_segment() -> None:
    assert guest_probe.unit_of("0::/system.slice/atk-client-ab12.service\n") == "atk-client-ab12.service"
    assert guest_probe.unit_of("12:pids:/\n0::/user.slice/user-1.slice/\n") == "user-1.slice"
    assert guest_probe.unit_of("0::/\n") == ""
    assert guest_probe.unit_of("") == ""


def test_link_fifo_and_device_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.txt"
    target.write_text("t")
    assert probe(["symlink", str(tmp_path / "l"), "/etc", str(UID), str(GID)], tmp_path) == (0, {})
    assert os.readlink(tmp_path / "l") == "/etc"
    assert probe(["hardlink", str(tmp_path / "h"), str(target)], tmp_path) == (0, {})
    assert os.stat(tmp_path / "h").st_ino == target.stat().st_ino
    assert probe(["mkfifo", str(tmp_path / "p"), str(UID), str(GID)], tmp_path) == (0, {})
    assert (tmp_path / "p").is_fifo()
    made: list[tuple[str, int, int]] = []
    monkeypatch.setattr("os.mknod", lambda path, mode, device: made.append((path, mode, device)))
    monkeypatch.setattr("os.chmod", lambda path, mode: None)
    assert probe(["mknull", str(tmp_path / "n")], tmp_path) == (0, {})
    assert made == [(str(tmp_path / "n"), 0o666 | stat.S_IFCHR, os.makedev(1, 3))]


def test_session_token_reads_the_environment_of_a_process_of_the_user(tmp_path: Path) -> None:
    user = pwd.getpwuid(UID).pw_name
    for pid, environ in (
        ("10", b"HOME=/x\0PLATFORM_SESSION_TOKEN=\0"),
        ("11", b"PATH=/usr/bin\0PLATFORM_SESSION_TOKEN=tok-123\0"),
    ):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "environ").write_bytes(environ)
    (tmp_path / "12").mkdir()  # no environ (ended)
    (tmp_path / "self").mkdir()
    code, out = 0, guest_probe.execute(["session-token", user], proc=str(tmp_path))
    assert (code, out) == (0, {"token": "tok-123"})
    os.remove(tmp_path / "11" / "environ")
    with pytest.raises(ValueError, match="no process"):
        guest_probe.session_token(user, str(tmp_path))
    with pytest.raises(ValueError, match="unknown user"):
        guest_probe.session_token("no-such-user-atk", str(tmp_path))


def test_session_token_skips_processes_of_other_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "20").mkdir()
    (tmp_path / "20" / "environ").write_bytes(b"PLATFORM_SESSION_TOKEN=other\0")
    monkeypatch.setattr("os.stat", lambda path: os.stat_result((0, 0, 0, 0, UID + 1, 0, 0, 0, 0, 0)))
    with pytest.raises(ValueError, match="no process"):
        guest_probe.session_token(pwd.getpwuid(UID).pw_name, str(tmp_path))


def test_client_runs_the_session_client_in_its_own_unit(tmp_path: Path) -> None:
    runs = Runs(stdout=json.dumps({"answers": [{"label": "x", "status": 401}]}))
    spec = base64.b64encode(json.dumps({"token": "tok", "requests": [{"path": "/mcp"}]}).encode()).decode()
    assert probe(["client", "20001", "20001", spec], tmp_path, runs) == (
        0,
        {"answers": [{"label": "x", "status": 401}]},
    )
    [(argv, stdin)] = runs.calls
    assert argv[:5] == ["systemd-run", "--quiet", "--wait", "--pipe", "--collect"]
    assert argv[5].startswith("--unit=atk-client-")
    assert len(argv[5]) == len("--unit=atk-client-") + 12
    assert argv[6:] == ["--uid=20001", "--gid=20001", "--setenv=ATK_TOKEN=tok", "/usr/bin/python3", "-I",
                        guest_probe.CLIENT]  # fmt: skip
    assert json.loads(stdin or "") == [{"path": "/mcp"}]


def test_paste_clears_pastes_and_presses_enter(tmp_path: Path) -> None:
    runs = Runs()
    slept: list[float] = []
    text = base64.b64encode(b'<pasted_content id="x">\nhi\n</pasted_content id="x">').decode()
    sid = "01a20253-25bb-76fc-8ab6-83805076021e"
    out = guest_probe.execute(["paste", sid, text], run=runs, state_dir=str(tmp_path), sleep=slept.append)
    assert out == {}
    base = ["tmux", "-S", guest_probe.TMUX_SOCKET]
    target = f"=s-{sid}:"
    assert runs.calls == [
        ([*base, "send-keys", "-t", target, "C-e", "C-u", "C-e", "C-u", "C-e", "C-u"], None),
        ([*base, "load-buffer", "-b", "atk-paste", "-"], '<pasted_content id="x">\nhi\n</pasted_content id="x">'),
        ([*base, "paste-buffer", "-p", "-d", "-b", "atk-paste", "-t", target], None),
        ([*base, "send-keys", "-t", target, "Enter"], None),
    ]
    assert slept == [0.3]


# -- extended OS probe (ATS-03) ----------------------------------------------------------------------------------


def probe_context(tmp_path: Path, run: Any) -> guest_probe.Context:
    return guest_probe.Context(run, str(tmp_path / "state"), proc=str(tmp_path / "proc"), root=str(tmp_path / "root"))


def test_api_transient_hidden(tmp_path: Path) -> None:
    root, proc = tmp_path / "root", tmp_path / "proc"
    (root / "run/systemd/transient").mkdir(parents=True)
    ctx = probe_context(tmp_path, lambda argv, stdin: "42\n")
    assert guest_probe.api_transient_hidden(ctx)["outcome"] == "no_transient_units"
    (root / "run/systemd/transient/agent-session-x.service").write_text("Environment=PLATFORM_SESSION_TOKEN=x")
    assert guest_probe.api_transient_hidden(ctx) == {
        "check": "api_transient_hidden",
        "outcome": "hidden",
        "expected": ["hidden"],
        "ok": True,
    }  # fmt: skip  (no /proc/42/root/run/systemd/transient: ENOENT)
    seen = proc / "42/root/run/systemd/transient"
    seen.mkdir(parents=True)
    assert guest_probe.api_transient_hidden(ctx)["outcome"] == "hidden"
    (seen / "agent-session-x.service").write_text("x")
    assert guest_probe.api_transient_hidden(ctx)["outcome"] == "visible:1"
    (seen / "agent-session-x.service").unlink()
    seen.rmdir()
    seen.write_text("not a directory")
    found = guest_probe.api_transient_hidden(ctx)
    assert (found["outcome"], found["ok"]) == ("visible:1", False)


def test_boot_jobs_masked(tmp_path: Path) -> None:
    states = {"pollinate.service": "UnitFileState=masked\nActiveState=inactive\n"}

    def run(argv: Sequence[str], stdin: str | None) -> str:
        assert list(argv[:5]) == ["systemctl", "show", "-p", "UnitFileState", "-p"]
        return states.get(argv[-1], "UnitFileState=masked\nActiveState=inactive\n")

    ctx = probe_context(tmp_path, run)
    found = guest_probe.boot_jobs_masked(ctx)
    assert (found["outcome"], found["ok"]) == ("masked", True)
    assert set(found["units"]) == set(guest_probe.BOOT_JOBS)
    states["pollinate.service"] = "UnitFileState=enabled\nActiveState=active\nnoise\n"
    states["apt-news.service"] = ""
    found = guest_probe.boot_jobs_masked(ctx)
    assert (found["outcome"], found["ok"]) == ("active:apt-news.service,pollinate.service", False)
    assert found["units"]["apt-news.service"] == "?/?"


def test_load_file_and_guest_selftest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "m.py").write_text("VALUE = 3\n")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    assert guest_probe.load_file("atk_test_module", str(tmp_path / "m.py")).VALUE == 3
    assert sys.dont_write_bytecode is True
    assert not (tmp_path / "__pycache__").exists()
    with pytest.raises(ValueError, match="cannot be loaded"):
        guest_probe.load_file("atk_test_bad", str(tmp_path / "m.txt"))
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != guest_probe.GUEST_ROOT])
    module = guest_probe.guest_selftest()
    assert module.__name__ == "agent_guest.sandbox.selftest"
    assert sys.path[0] == guest_probe.GUEST_ROOT
    guest_probe.guest_selftest()
    assert sys.path.count(guest_probe.GUEST_ROOT) == 1


class FakeSelftest:
    """``agent_guest.sandbox.selftest`` as ``extended_selftest`` uses it; ``run_selftest`` feeds probe lines to the
    module's ``evaluate`` (which the probe replaces for the run)."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.configs: list[Any] = []
        self.probe = SimpleNamespace(
            accepts=lambda accepted, outcome: outcome in accepted,
            EXPECTED={"exec_sh": ("errno:EACCES",)},
            listening_unix=lambda text: ["@iscsi"] if "iscsi" in text else [],
        )
        self.unit = SimpleNamespace(
            interpreter_paths=lambda python: ("/usr/bin/python3.12", "/usr/lib/python3.12/dyn"),
            load_exec_paths=lambda: ("/lib/x.so",),
        )
        self.evaluate = self.original

    @staticmethod
    def original(lines: Any) -> list[dict[str, Any]]:
        return [{"check": "original"}]

    def SelftestConfig(self, **values: Any) -> dict[str, Any]:
        return values

    def run_selftest(self, config: Any) -> dict[str, Any]:
        self.configs.append(config)
        checks = self.evaluate(self.lines)
        return {"ok": all(check["ok"] for check in checks), "checks": checks}


def test_extended_selftest_evaluates_the_guest_and_the_extra_checks() -> None:
    lines = [
        json.dumps({"check": "exec_sh", "outcome": "errno:EACCES"}),
        json.dumps({"check": "exec_sh", "outcome": "ok"}),  # the first observation counts
        "garbage",
        json.dumps({"other": 1}),
        json.dumps([1]),
        json.dumps({"check": "ptrace", "outcome": "ok"}),
    ]
    fake = FakeSelftest(lines)
    report = guest_probe.extended_selftest(
        fake, {"exec_sh": ("errno:EACCES",), "ptrace": ("signal:SIGSYS",), "x": ("y",)}
    )
    assert report["checks"] == [
        {"check": "exec_sh", "outcome": "errno:EACCES", "expected": ["errno:EACCES"], "ok": True},
        {"check": "ptrace", "outcome": "ok", "expected": ["signal:SIGSYS"], "ok": False},
        {"check": "x", "outcome": "missing", "expected": ["y"], "ok": False},
    ]
    assert report["ok"] is False
    assert fake.evaluate == FakeSelftest.original
    assert fake.configs == [
        {
            "python": "/usr/bin/python3.12", "lib_dynload": "/usr/lib/python3.12/dyn", "exec_paths": ("/lib/x.so",),
            "probe": guest_probe.UNIT_PROBE, "extra_ro": (guest_probe.PUBLIC_DIR,),
        }
    ]  # fmt: skip


def test_os_extended_combines_the_unit_and_the_root_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "proc" / "net").mkdir(parents=True)
    (tmp_path / "proc" / "net" / "unix").write_text("header\nffff iscsi\n")
    (tmp_path / "root" / "run/systemd/transient").mkdir(parents=True)
    (tmp_path / "root" / "run/systemd/transient/agent-session-x.service").write_text("x")

    def run(argv: Sequence[str], stdin: str | None) -> str:
        if argv[0] == "ss":
            return "tcp LISTEN 127.0.0.1:7700\n"
        if "MainPID" in argv:
            return "42\n"
        return "UnitFileState=masked\nActiveState=inactive\n"

    fake = FakeSelftest([json.dumps({"check": "exec_sh", "outcome": "errno:EACCES"})])
    unit = SimpleNamespace(EXPECTED={})
    found = guest_probe.os_extended(probe_context(tmp_path, run), fake, unit)
    assert found["ok"] is True
    assert found["sockets"] == ["@iscsi"]
    assert json.loads((tmp_path / "root" / guest_probe.SOCKETS_FILE.lstrip("/")).read_text()) == ["@iscsi"]
    assert stat.S_IMODE((tmp_path / "root" / guest_probe.SOCKETS_FILE.lstrip("/")).stat().st_mode) == 0o644
    assert [check["check"] for check in found["root_checks"]] == ["api_transient_hidden", "boot_jobs_masked"]
    assert found["listeners"] == ["tcp LISTEN 127.0.0.1:7700"]
    unit.EXPECTED = {"ptrace": ("signal:SIGSYS",)}
    assert guest_probe.os_extended(probe_context(tmp_path, run), fake, unit)["ok"] is False
    loaded: list[str] = []
    monkeypatch.setattr(guest_probe, "guest_selftest", lambda: fake)

    def load(name: str, path: str) -> Any:
        loaded.append(path)
        return unit

    monkeypatch.setattr(guest_probe, "load_file", load)
    found = guest_probe.execute(["os-extended"], run=run, proc=str(tmp_path / "proc"), root=str(tmp_path / "root"))
    assert found["unit"]["checks"][0]["check"] == "exec_sh"
    assert loaded == [str(tmp_path / "root") + guest_probe.UNIT_PROBE]
