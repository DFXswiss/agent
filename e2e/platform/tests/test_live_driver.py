"""The operator-side live driver (`live/driver.py`, LVT-01): secret sources, the hand-over on standard input, the
artefact and log scan and the profile comparison - against a scripted process runner (no keychain, no probe)."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pytest
from live import driver

KEY = "or-key-0123456789abcdef"
TOKEN = "sub-token-0123456789abcdef"
GH = "gh-token-0123456789abcdef"
API = "api-key-0123456789abcdef"


class Runner:
    """Answers per program; records argv and options; the command writes what it got on standard input."""

    def __init__(self, tmp: Path, **answers: Any) -> None:
        self.tmp = tmp
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.answers = answers
        self.stdin = b""
        self.leak = ""
        self.compare: tuple[int, str] = (0, json.dumps({"unchanged": True, "differences": []}))
        self.over_run: tuple[int, str] | None = None  # answer of the whole-run comparison (default: as `compare`)

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kw))
        name = Path(argv[0]).name
        if len(argv) > 1 and Path(argv[1]).name == "probe.py":
            return self.probe(argv)
        if name == "security":
            rc, out = self.answers.get(f"security:{argv[3]}", (44, ""))
            return subprocess.CompletedProcess(argv, rc, out, "")
        if name == "gh":
            rc, out = self.answers.get("gh", (0, GH + "\n"))
            return subprocess.CompletedProcess(argv, rc, out, "")
        if name == "fetch":
            (self.tmp / "artifacts").mkdir(exist_ok=True)
            (self.tmp / "artifacts" / "out.txt").write_text(f"result {self.leak}\n")
            return subprocess.CompletedProcess(argv, 0)
        if name == "missing":
            raise FileNotFoundError(argv[0])
        self.stdin = kw["input"]
        kw["stdout"].write(b"run output " + self.leak.encode())
        return subprocess.CompletedProcess(argv, int(self.answers.get("command", 0)))

    def probe(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = argv[2]
        if command == "snapshot":
            rc = self.answers.get("snapshot", [0]).pop(0) if self.answers.get("snapshot") else 0
            return subprocess.CompletedProcess(argv, rc, None, "")
        if command == "check":
            return subprocess.CompletedProcess(argv, self.answers.get("check", 0), None, "")
        if command == "token":
            rc, out = self.answers.get("token", (0, TOKEN + "\n"))
            return subprocess.CompletedProcess(argv, rc, out, "")
        rc, out = self.over_run if self.over_run is not None and argv[4].endswith("after.json") else self.compare
        return subprocess.CompletedProcess(argv, rc, out, "")


def base(tmp: Path, *extra: str) -> list[str]:
    return ["--result", str(tmp / "result.json"), "--artifacts", str(tmp / "artifacts"), *extra]


def result(tmp: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tmp / "result.json").read_text())
    return data


PROBE = ("--probe", "/agent/probe.py", "--probe-python", "/agent/python")


def test_full_hand_over(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "chosen"
    profile.mkdir(parents=True)
    runner = Runner(tmp_path, **{"security:openrouter-api-key": (0, KEY + "\n"), "security:anthropic": (0, API)})
    environ = {
        "AGENT_LIVE_GITHUB_REPO": "octo/live",
        "AGENT_LIVE_EXCLUDED_PROFILES": "never, other",
        "AGENT_LIVE_EMPTY": "",
        "PATH": "/bin",
    }
    argv = base(
        tmp_path,
        "--log",
        str(tmp_path / "logs" / "run.log"),
        "--fetch",
        "fetch now",
        "--openrouter-service",
        "openrouter-api-key",
        "--anthropic-service",
        "anthropic",
        "--github-token-command",
        "gh auth token",
        "--profile",
        str(profile),
        "--exclude-profile",
        "also-never",
        *PROBE,
        "--",
        "ssh",
        "runner",
        "run.sh",
    )
    assert driver.main(argv, environ, runner) == driver.EXIT_OK
    lines = runner.stdin.decode().split("\n")
    assert lines == [
        f"E2E_LIVE_ANTHROPIC_KEY={API}",
        f"E2E_LIVE_GITHUB_TOKEN={GH}",
        f"E2E_LIVE_OPENROUTER_KEY={KEY}",
        f"E2E_LIVE_SUBSCRIPTION_TOKEN={TOKEN}",
        "AGENT_LIVE_GITHUB_REPO=octo/live",
        "",
        "",
    ]
    summary = result(tmp_path)
    assert summary["command_rc"] == 0
    assert summary["fetch_rc"] == 0
    assert summary["findings"] == []
    clean = {"unchanged": True, "differences": [], "volatile": [], "blocking": [], "ok": True}
    assert summary["profile"] == {"read": clean, "run": clean, "ok": True}
    assert summary["settings"] == ["AGENT_LIVE_GITHUB_REPO"]
    text = (tmp_path / "result.json").read_text() + (tmp_path / "logs" / "run.log").read_text()
    assert not any(secret in text for secret in (KEY, TOKEN, GH, API))
    probes = [argv for argv, _kw in runner.calls if argv[0] == "/agent/python"]
    assert [argv[2] for argv in probes] == ["snapshot", "check", "token", "snapshot", "snapshot", "compare", "compare"]
    assert probes[0][-6:] == ["--exclude", "also-never", "--exclude", "never", "--exclude", "other"]
    assert "--profile" not in probes[-1]
    command = next(argv for argv, _kw in runner.calls if argv[0] == "ssh")
    assert command == ["ssh", "runner", "run.sh"]
    for _argv, kw in runner.calls:
        assert kw["env"] == {"PATH": "/bin"}  # no live value reaches a child process
    assert not any(secret in " ".join(argv) for argv, _kw in runner.calls for secret in (KEY, TOKEN, GH, API))


def test_environment_sources_win_and_nothing_else_is_asked(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    environ = {"AGENT_LIVE_OPENROUTER_KEY": KEY, "AGENT_LIVE_GITHUB_TOKEN": GH, "AGENT_LIVE_ANTHROPIC_API_KEY": API}
    argv = base(tmp_path, "--openrouter-service", "s", "--github-token-command", "gh auth token", "--", "cmd")
    assert driver.main(argv, environ, runner) == driver.EXIT_OK
    assert [argv[0] for argv, _kw in runner.calls] == ["cmd"]
    assert result(tmp_path)["secrets"] == ["E2E_LIVE_ANTHROPIC_KEY", "E2E_LIVE_GITHUB_TOKEN", "E2E_LIVE_OPENROUTER_KEY"]
    assert result(tmp_path)["profile"] is None


def test_without_any_source_the_run_gets_only_the_end_mark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner(tmp_path)
    monkeypatch.setattr("os.environ", {"AGENT_LIVE_GITHUB_REPO": "octo/live"})
    assert driver.main(base(tmp_path, "--", "cmd"), run=runner) == driver.EXIT_OK
    assert runner.stdin == b"AGENT_LIVE_GITHUB_REPO=octo/live\n\n"


@pytest.mark.parametrize(
    ("extra", "answers", "message"),
    [
        (["--openrouter-service", "s"], {}, "keychain item 's' not readable"),
        (["--openrouter-service", "s"], {"security:s": (0, "  ")}, "keychain item 's' not readable"),
        (["--github-token-command", "gh auth token"], {"gh": (1, "")}, "'gh' gave no value"),
        (["--profile", "/p/chosen"], {}, "--profile needs --probe"),
        (["--profile", "/p/never", *PROBE], {}, "the operator excluded this profile"),
        (["--profile", "/p/chosen", *PROBE], {"snapshot": [3]}, "the profile snapshot failed"),
        (["--profile", "/p/chosen", *PROBE], {"check": 1}, "not valid for 7200 s"),
        (["--profile", "/p/chosen", *PROBE], {"token": (0, "\n")}, "could not be read"),
        (["--profile", "/p/chosen", *PROBE], {"token": (1, TOKEN)}, "could not be read"),
    ],
)
def test_unusable_sources_stop_before_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], answers: dict[str, Any], message: str
) -> None:
    runner = Runner(tmp_path, **answers)
    environ = {"AGENT_LIVE_EXCLUDED_PROFILES": "never"}
    assert driver.main(base(tmp_path, *extra, "--", "cmd"), environ, runner) == driver.EXIT_USAGE
    err = capsys.readouterr().err
    assert message in err
    assert TOKEN not in err
    assert "cmd" not in [argv[0] for argv, _kw in runner.calls]
    assert not (tmp_path / "result.json").exists()


def test_a_leak_in_the_artefacts_or_the_log_fails(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    runner.leak = GH
    argv = base(tmp_path, "--fetch", "fetch", "--", "cmd")
    assert driver.main(argv, {"AGENT_LIVE_GITHUB_TOKEN": GH}, runner) == driver.EXIT_FAILED
    findings = result(tmp_path)["findings"]
    assert findings == [f"{tmp_path / 'artifacts' / 'out.txt'}: generated run secret", "log: generated run secret"]
    assert GH not in (tmp_path / "result.json").read_text()


def test_a_leak_inside_a_trace_archive_fails(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    with zipfile.ZipFile(tmp_path / "artifacts" / "trace.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resources/body.json", json.dumps({"token": TOKEN}))
    runner = Runner(tmp_path)
    assert driver.main(base(tmp_path, "--", "cmd"), {"AGENT_LIVE_GITHUB_TOKEN": TOKEN}, runner) == driver.EXIT_FAILED
    assert result(tmp_path)["findings"] == [
        f"{tmp_path / 'artifacts' / 'trace.zip'}!resources/body.json: generated run secret"
    ]


def test_failing_command_and_unstartable_command(tmp_path: Path) -> None:
    runner = Runner(tmp_path, command=1)
    assert driver.main(base(tmp_path, "--", "cmd"), {}, runner) == driver.EXIT_FAILED
    assert result(tmp_path)["command_rc"] == 1
    assert driver.main(base(tmp_path, "--fetch", "fetch", "--", "missing"), {}, runner) == driver.EXIT_FAILED
    summary = result(tmp_path)
    assert (summary["command_rc"], summary["command_error"], summary["fetch_rc"]) == (None, "FileNotFoundError", 0)


@pytest.mark.parametrize(
    ("compare", "snapshots"),
    [
        ((1, json.dumps({"unchanged": False, "differences": ["changed: file a"]})), [0, 0, 0]),
        ((0, "garbage"), [0, 0, 0]),
        ((1, json.dumps({"unchanged": True})), [0, 0, 0]),
    ],
)
def test_a_changed_or_unproven_profile_at_the_read_fails(
    tmp_path: Path, compare: tuple[int, str], snapshots: list[int]
) -> None:
    profile = tmp_path / "chosen"
    profile.mkdir()
    runner = Runner(tmp_path, snapshot=list(snapshots))
    runner.compare = compare
    runner.over_run = (0, json.dumps({"unchanged": True, "differences": []}))
    argv = base(tmp_path, "--profile", str(profile), *PROBE, "--", "cmd")
    assert driver.main(argv, {}, runner) == driver.EXIT_FAILED
    report = result(tmp_path)["profile"]
    assert (report["ok"], report["read"]["ok"], report["run"]["ok"]) == (False, False, True)
    assert report["read"]["blocking"]


def test_a_failing_last_snapshot_fails(tmp_path: Path) -> None:
    profile = tmp_path / "chosen"
    profile.mkdir()
    runner = Runner(tmp_path, snapshot=[0, 0, 5])
    argv = base(tmp_path, "--profile", str(profile), *PROBE, "--", "cmd")
    assert driver.main(argv, {}, runner) == driver.EXIT_FAILED
    assert result(tmp_path)["profile"] == {"ok": False, "error": "the profile snapshot failed"}


def test_a_failing_snapshot_after_the_read_stops_before_the_run(tmp_path: Path) -> None:
    profile = tmp_path / "chosen"
    profile.mkdir()
    runner = Runner(tmp_path, snapshot=[0, 5])
    argv = base(tmp_path, "--profile", str(profile), *PROBE, "--", "cmd")
    assert driver.main(argv, {}, runner) == driver.EXIT_USAGE
    assert "cmd" not in [argv[0] for argv, _kw in runner.calls]


def test_concurrent_session_activity_is_reported_but_not_failing(tmp_path: Path) -> None:
    profile = tmp_path / "chosen"
    profile.mkdir()
    runner = Runner(tmp_path)
    differences = [
        "changed: projects/a/b.jsonl",
        "added: shell-snapshots/s.sh",
        "removed: projects/x",
        "changed: remote-settings.json.synced-at",
    ]
    runner.compare = (1, json.dumps({"unchanged": False, "differences": differences}))
    over_run = [*differences, "changed: .claude.json", "added: sessions/1.json"]
    runner.over_run = (1, json.dumps({"unchanged": False, "differences": over_run}))
    argv = base(tmp_path, "--profile", str(profile), "--volatile", "projects/", "--volatile", "shell-snapshots/")
    argv += ["--volatile", "remote-settings.json"]
    assert driver.main([*argv, *PROBE, "--", "cmd"], {}, runner) == driver.EXIT_OK
    read, run = result(tmp_path)["profile"]["read"], result(tmp_path)["profile"]["run"]
    assert (read["ok"], read["unchanged"], read["volatile"], read["blocking"]) == (True, False, differences, [])
    assert (run["ok"], run["volatile"], run["blocking"]) == (True, over_run, [])
    # at the read: any other file change blocks; over the run: only a keychain change blocks
    for blocking in ("changed: .claude.json", "keychain attribute changed: mdat", "changed: projectsX/a"):
        runner.compare = (1, json.dumps({"unchanged": False, "differences": [*differences, blocking]}))
        assert driver.main([*argv, *PROBE, "--", "cmd"], {}, runner) == driver.EXIT_FAILED
        assert result(tmp_path)["profile"]["read"]["blocking"] == [blocking]
    runner.compare = (0, json.dumps({"unchanged": True, "differences": []}))
    runner.over_run = (1, json.dumps({"unchanged": False, "differences": ["keychain attribute changed: mdat"]}))
    assert driver.main([*argv, *PROBE, "--", "cmd"], {}, runner) == driver.EXIT_FAILED
    assert result(tmp_path)["profile"]["run"]["blocking"] == ["keychain attribute changed: mdat"]


def test_main_module_guard() -> None:
    assert driver.parser().prog == "python -m live.driver"


@pytest.mark.parametrize("prefix", ["", "/", "/abs/", "../x/", ".claude/", "a//", "projects/../", "a/.hidden"])
def test_volatile_prefixes_are_directory_prefixes(tmp_path: Path, prefix: str) -> None:
    runner = Runner(tmp_path)
    assert driver.main(base(tmp_path, "--volatile", prefix, "--", "cmd"), {}, runner) == driver.EXIT_USAGE
    assert runner.calls == []


class FakeGitHub:
    """Answers the cleaner's requests; records method and path."""

    def __init__(self, **answers: tuple[int, Any]) -> None:
        self.seen: list[tuple[str, str, Any]] = []
        self.answers = answers

    def __call__(self, request: Any) -> tuple[int, Any]:
        path = request.full_url.removeprefix(driver.GITHUB_API)
        body = json.loads(request.data) if request.data else None
        self.seen.append((request.get_method(), path, body))
        assert request.headers["Authorization"] == f"Bearer {GH}"
        if request.get_method() == "GET" and path == "/repos/octo/live":
            return self.answers.get("repo", (200, {"private": True}))
        if path.startswith("/repos/octo/live/pulls?"):
            pulls = [{"number": 7, "head": {"ref": "live02/a"}}, {"number": 8, "head": {"ref": "feature"}}]
            return self.answers.get("pulls", (200, pulls))
        if path.startswith("/repos/octo/live/git/matching-refs/"):
            return self.answers.get("refs", (200, [{"ref": "refs/heads/live02/a"}, {"ref": "refs/heads/live02/b"}]))
        if request.get_method() == "PATCH":
            return self.answers.get("close", (200, {}))
        return self.answers.get("delete", (204, None))


ENV = {"AGENT_LIVE_GITHUB_TOKEN": GH, "AGENT_LIVE_GITHUB_REPO": "octo/live"}


def test_github_fallback_cleanup(tmp_path: Path) -> None:
    github = FakeGitHub()
    argv = base(tmp_path, "--github-cleanup", "live02/", "--", "cmd")
    assert driver.main(argv, ENV, Runner(tmp_path), github) == driver.EXIT_OK
    assert result(tmp_path)["github_cleanup"] == {"closed": [7], "deleted": ["live02/a", "live02/b"], "errors": []}
    assert [(m, p) for m, p, _b in github.seen if m != "GET"] == [
        ("PATCH", "/repos/octo/live/pulls/7"),
        ("DELETE", "/repos/octo/live/git/refs/heads/live02/a"),
        ("DELETE", "/repos/octo/live/git/refs/heads/live02/b"),
    ]
    assert github.seen[0][:2] == ("GET", "/repos/octo/live")
    assert github.seen[2][2] == {"state": "closed"}


@pytest.mark.parametrize(
    ("answers", "errors"),
    [
        ({"pulls": (500, None)}, ["list pull requests: 500"]),
        ({"refs": (404, None)}, ["list branches: 404"]),
        ({"close": (422, None)}, ["close: 422"]),
        ({"delete": (403, None)}, ["delete: 403", "delete: 403"]),
    ],
)
def test_github_cleanup_failures_fail_the_driver(
    tmp_path: Path, answers: dict[str, tuple[int, Any]], errors: list[str]
) -> None:
    argv = base(tmp_path, "--github-cleanup", "live02/", "--", "cmd")
    assert driver.main(argv, ENV, Runner(tmp_path), FakeGitHub(**answers)) == driver.EXIT_FAILED
    assert result(tmp_path)["github_cleanup"]["errors"] == errors


@pytest.mark.parametrize(
    ("environ", "answers", "message"),
    [
        ({"AGENT_LIVE_GITHUB_REPO": "octo/live"}, {}, "needs the GitHub token"),
        ({"AGENT_LIVE_GITHUB_TOKEN": GH}, {}, "needs the GitHub token"),
        (ENV, {"repo": (200, {"private": False})}, "not a private repository"),
        (ENV, {"repo": (404, None)}, "not a private repository"),
    ],
)
def test_github_cleanup_needs_a_private_repository(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    answers: dict[str, tuple[int, Any]],
    message: str,
) -> None:
    runner = Runner(tmp_path)
    argv = base(tmp_path, "--github-cleanup", "live02/", "--", "cmd")
    assert driver.main(argv, environ, runner, FakeGitHub(**answers)) == driver.EXIT_USAGE
    assert message in capsys.readouterr().err
    assert "cmd" not in [argv[0] for argv, _kw in runner.calls]


class Answer:
    def __init__(self, status: int, data: bytes) -> None:
        self.status, self.data = status, data

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def test_urlopen_json(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [Answer(200, b'{"a": 1}'), Answer(204, b"")]

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            assert timeout == driver.GITHUB_TIMEOUT_S
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: Opener())
    request = urllib.request.Request("https://api.github.com/x")
    assert driver.urlopen_json(request) == (200, {"a": 1})
    assert driver.urlopen_json(request) == (204, None)
    answers.append(urllib.error.HTTPError("https://api.github.com/x", 404, "nf", {}, io.BytesIO(b"")))  # type: ignore[arg-type]
    assert driver.urlopen_json(request) == (404, None)
