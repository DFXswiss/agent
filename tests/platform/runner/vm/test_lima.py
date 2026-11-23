"""U-RUN-vm-lima: typed `limactl` wrapper (05-runner.md sections 3.1-3.5, ADR-027 section 7).

The command is injected; answers are the recordings of Lima 2.3.0 in `fixtures/`. `run_limactl` runs a real child
process (a stand-in `limactl` script on `PATH`).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm import lima
from agent_runner.vm.lima import Lima, LimaError, LimaNotFound, LimaNotInstalled, LimaTimeout, LimaVersionError

from .conftest import FakeLimactl, fixture_text, recorded

PREFIX = "wp-test-"


def answer(code: int = 0, out: str = "", err: str = "") -> lima.LimactlRun:
    calls: list[tuple[list[str], float]] = []

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(["limactl", *argv], code, out, err)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def fake_binary(tmp_path: Path, body: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "limactl"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {"PATH": str(bindir), "MARK": "env-passed"}


class TestRunLimactl:
    def test_runs_the_binary_from_path_with_the_environment(self, tmp_path: Path) -> None:
        env = fake_binary(tmp_path, 'echo "$MARK $*"; echo err >&2; exit 3\n')
        result = lima.run_limactl(["list", "--json"], 5.0, env=env)
        assert (result.returncode, result.stdout, result.stderr) == (3, "env-passed list --json\n", "err\n")

    def test_missing_binary_answers_127(self, tmp_path: Path) -> None:
        result = lima.run_limactl(["--version"], 5.0, env={"PATH": str(tmp_path)})
        assert result.returncode == lima.EXIT_NOT_FOUND
        with pytest.raises(LimaNotInstalled):
            Lima(lambda argv, timeout: result).version()

    def test_timeout_answers_124(self, tmp_path: Path) -> None:
        env = fake_binary(tmp_path, "exec /bin/sleep 5\n")
        result = lima.run_limactl(["start", "x"], 0.2, env=env)
        assert result.returncode == lima.EXIT_TIMEOUT
        with pytest.raises(LimaTimeout) as caught:
            Lima(lambda argv, timeout: result, prefix="").stop("x")
        assert caught.value.code == "lima_timeout"

    def test_default_environment_is_the_process_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = fake_binary(tmp_path, 'echo "$MARK"\n')
        monkeypatch.setenv("PATH", env["PATH"])
        monkeypatch.setenv("MARK", "from-process")
        assert lima.run_limactl(["--version"], 5.0).stdout == "from-process\n"


class TestParsing:
    def test_version_of_the_recording(self) -> None:
        assert lima.parse_version(fixture_text("version.txt")) == (2, 3, 0)
        assert lima.parse_version("limactl version v2.10.1-beta") == (2, 10, 1)

    def test_unknown_version_text(self) -> None:
        with pytest.raises(LimaVersionError):
            lima.parse_version("lima 2")

    def test_list_of_the_recording(self) -> None:
        instances = lima.parse_list(fixture_text("list.jsonl") + "\n\n")
        assert [item.status for item in instances] == ["Stopped"] * 3
        assert instances[1].name.startswith("wp-vm01-golden-")
        assert instances[1].cpus == 4
        assert instances[1].memory_bytes == 8 * 1024**3
        assert instances[1].disk_bytes == 20 * 1024**3
        assert not instances[1].running
        assert instances[1].control_socket == os.path.join(instances[1].dir, "sock", "agent-guest.sock")
        assert lima.parse_list("") == []

    @pytest.mark.parametrize(
        "line",
        [
            "not json",
            "[1]",
            '{"name": "a", "status": "Running"}',
            '{"name": "a", "status": "Running", "dir": 3}',
            '{"name": "a", "status": "Running", "dir": "/d", "cpus": "4"}',
            '{"name": "a", "status": "Running", "dir": "/d", "cpus": true}',
        ],
    )
    def test_invalid_list_output(self, line: str) -> None:
        with pytest.raises(LimaError) as caught:
            lima.parse_list(line)
        assert caught.value.code == "lima_output_invalid"

    def test_running_and_defaults(self) -> None:
        (item,) = lima.parse_list('{"name": "a", "status": "Running", "dir": "/d"}')
        assert item.running
        assert (item.cpus, item.memory_bytes, item.disk_bytes) == (0, 0, 0)

    def test_memory_gib(self) -> None:
        assert lima.memory_gib(8192) == "8"
        assert lima.memory_gib(1536) == "1.5"


class TestNames:
    def test_prefix_and_validation(self) -> None:
        wrapper = Lima(answer(), prefix=PREFIX)
        assert wrapper.name("golden-2.1.330-0.1.0-20261121") == "wp-test-golden-2.1.330-0.1.0-20261121"
        assert wrapper.owns("wp-test-arp-1a2b3c4d")
        for name in ("golden", "wp-other-x", "wp-test-", "wp-test-Upper", "wp-test-a..b", "wp-test-" + "a" * 80):
            assert not wrapper.owns(name)
        with pytest.raises(LimaError) as caught:
            wrapper.name("bad name")
        assert (caught.value.code, caught.value.name) == ("lima_foreign_instance", "name")

    @pytest.mark.parametrize(
        "call",
        [
            lambda w: w.create("golden", "/t.yaml"),
            lambda w: w.stop("golden"),
            lambda w: w.delete("golden"),
            lambda w: w.rename("wp-test-a", "golden"),
            lambda w: w.rename("golden", "wp-test-a"),
            lambda w: w.clone("wp-test-a", "golden"),
            lambda w: w.clone("golden", "wp-test-a"),
            lambda w: w.shell("golden", ["true"]),
            lambda w: w.copy_to("/f", "golden", "/tmp/f"),
            lambda w: w.get("golden"),
            lambda w: w.start("golden"),
        ],
    )
    def test_foreign_instances_are_never_touched(self, call: object) -> None:
        run = answer()
        with pytest.raises(LimaError) as caught:
            call(Lima(run, prefix=PREFIX))  # type: ignore[operator]
        assert caught.value.code == "lima_foreign_instance"
        assert run.calls == []  # type: ignore[attr-defined]


class TestOperations:
    def test_version_check(self) -> None:
        assert Lima(answer(out="limactl version 2.3.0\n")).check_version() == (2, 3, 0)
        with pytest.raises(LimaVersionError) as caught:
            Lima(answer(out="limactl version 2.2.0\n")).check_version()
        assert caught.value.code == "lima_version"

    def test_create_with_and_without_sizes(self) -> None:
        run = answer()
        wrapper = Lima(run, prefix=PREFIX, timeouts={"create": 7.0})
        wrapper.create("wp-test-a", "/t/golden.yaml", cpus=2, memory_mib=4096, disk_gib=30)
        wrapper.create("wp-test-b", "/t/golden.yaml")
        assert run.calls == [  # type: ignore[attr-defined]
            (
                ["create", "-y", "--name=wp-test-a", "--cpus", "2", "--memory", "4", "--disk", "30", "/t/golden.yaml"],
                7.0,
            ),
            (["create", "-y", "--name=wp-test-b", "/t/golden.yaml"], 7.0),
        ]

    def test_create_failure_keeps_the_stderr_tail_out_of_the_message(self) -> None:
        code, _, err = recorded("create-bad.txt")
        with pytest.raises(LimaError) as caught:
            Lima(answer(code, err=err * 50), prefix=PREFIX).create("wp-test-a", "/nonexistent.yaml")
        assert (caught.value.code, caught.value.name, caught.value.returncode) == ("lima_failed", "create", 1)
        assert "nonexistent" not in str(caught.value)
        assert len(caught.value.detail) == lima.DETAIL_LIMIT

    def test_start_with_params(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-a")
        Lima(fake_limactl, prefix=PREFIX).start("wp-test-a", params={"B_KEY": "v2", "A_KEY": "tok-_.=+/~"}, timeout=9)
        argv, timeout = fake_limactl.calls[-1]
        assert argv == [
            "start",
            "-y",
            "--set",
            '.param.A_KEY="tok-_.=+/~"',
            "--set",
            '.param.B_KEY="v2"',
            "wp-test-a",
        ]
        assert timeout == 9
        assert fake_limactl.instances["wp-test-a"]["status"] == "Running"

    def test_start_of_a_missing_instance_never_creates_one(self, fake_limactl: FakeLimactl) -> None:
        with pytest.raises(LimaNotFound) as caught:
            Lima(fake_limactl, prefix=PREFIX).start("wp-test-a")
        assert caught.value.code == "lima_instance_missing"
        assert fake_limactl.ops() == ["list"]

    @pytest.mark.parametrize("params", [{"lower": "v"}, {"KEY": 'with"quote'}, {"KEY": "x" * 513}])
    def test_start_refuses_unsafe_params(self, fake_limactl: FakeLimactl, params: dict[str, str]) -> None:
        fake_limactl.add("wp-test-a")
        with pytest.raises(LimaError) as caught:
            Lima(fake_limactl, prefix=PREFIX).start("wp-test-a", params=params)
        assert caught.value.code == "lima_param_invalid"

    def test_stop_and_its_missing_instance(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-a", "Running")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        wrapper.stop("wp-test-a")
        assert fake_limactl.instances["wp-test-a"]["status"] == "Stopped"
        with pytest.raises(LimaNotFound):
            wrapper.stop("wp-test-b")

    def test_delete_reports_missing_instances(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-a")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        assert wrapper.delete("wp-test-a") is True
        assert fake_limactl.calls[-1][0] == ["delete", "-f", "wp-test-a"]
        assert wrapper.delete("wp-test-a") is False
        with pytest.raises(LimaNotFound):
            wrapper.delete("wp-test-a", missing_ok=False)

    def test_delete_removes_the_leftover_of_an_interrupted_clone(self, fake_limactl: FakeLimactl) -> None:
        # Lima 2.3.0 lists an instance directory without lima.yaml as Broken and refuses `delete -f` on it.
        directory = fake_limactl.add_broken("wp-test-arp-1")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        assert wrapper.get("wp-test-arp-1") is not None
        assert wrapper.delete("wp-test-arp-1") is True
        assert not directory.exists()
        assert wrapper.get("wp-test-arp-1") is None
        assert fake_limactl.ops() == ["list", "delete", "list", "list"]

    def test_delete_keeps_refusing_anything_but_such_a_leftover(
        self, fake_limactl: FakeLimactl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        refused = recorded("delete-broken.txt")
        # The directory holds a lima.yaml (Lima could not read it): kept.
        directory = fake_limactl.add_broken("wp-test-arp-1")
        directory.joinpath("lima.yaml").write_text("x", encoding="utf-8")
        with pytest.raises(LimaError) as caught:
            wrapper.delete("wp-test-arp-1")
        assert (caught.value.code, type(caught.value)) == ("lima_failed", LimaError)
        assert directory.joinpath("lima.yaml").exists()
        # Not Broken (any other refusal text with the same words): kept.
        fake_limactl.add("wp-test-arp-2", status="Running")
        fake_limactl.fail["delete"] = refused
        with pytest.raises(LimaError):
            wrapper.delete("wp-test-arp-2")
        # A directory that is not the instance's own: kept.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        fake_limactl.add_broken("wp-test-arp-3")
        fake_limactl.instances["wp-test-arp-3"]["dir"] = str(elsewhere)
        with pytest.raises(LimaError):
            wrapper.delete("wp-test-arp-3")
        assert elsewhere.exists()
        # Not directly in Lima's home (or a relative path): kept.
        fake_limactl.add_broken("wp-test-arp-4")
        fake_limactl.instances["wp-test-arp-4"]["LimaHome"] = str(tmp_path / "other-home")
        with pytest.raises(LimaError):
            wrapper.delete("wp-test-arp-4")
        monkeypatch.chdir(tmp_path)
        Path("wp-test-arp-4").mkdir()
        fake_limactl.instances["wp-test-arp-4"]["dir"] = "wp-test-arp-4"
        with pytest.raises(LimaError):
            wrapper.delete("wp-test-arp-4")
        assert Path("wp-test-arp-4").is_dir()
        del fake_limactl.instances["wp-test-arp-4"]["LimaHome"]
        with pytest.raises(LimaError):
            wrapper.delete("wp-test-arp-4")
        # Gone meanwhile: nothing removed, reported like a missing instance.
        fake_limactl.fail["delete"] = refused
        assert wrapper.delete("wp-test-arp-9") is False
        fake_limactl.fail["delete"] = refused
        with pytest.raises(LimaNotFound):
            wrapper.delete("wp-test-arp-9", missing_ok=False)
        # A plain "missing" answer stays LimaNotFound.
        fake_limactl.fail["delete"] = (1, "", 'time=x level=fatal msg="No instance matching wp-test-arp-8"\n')
        with pytest.raises(LimaNotFound):
            wrapper.delete("wp-test-arp-8")

    def test_the_refusal_is_recognised_without_the_warning_line(self, fake_limactl: FakeLimactl) -> None:
        directory = fake_limactl.add_broken("wp-test-arp-1")
        code, _out, err = recorded("delete-broken.txt")
        fatal_only = "\n".join(line for line in err.splitlines() if "level=fatal" in line) + "\n"
        fake_limactl.fail["delete"] = (code, "", fatal_only.replace("wp-kor03-ck-3", "wp-test-arp-1"))
        assert Lima(fake_limactl, prefix=PREFIX).delete("wp-test-arp-1") is True
        assert not directory.exists()

    def test_delete_reports_a_directory_that_cannot_be_removed(
        self, fake_limactl: FakeLimactl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_limactl.add_broken("wp-test-arp-1")
        monkeypatch.setattr(shutil, "rmtree", lambda _path, ignore_errors=False: None)
        with pytest.raises(LimaError) as caught:
            Lima(fake_limactl, prefix=PREFIX).delete("wp-test-arp-1")
        assert caught.value.code == "lima_failed"

    def test_clone(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-golden-1")
        fake_limactl.add("cache-golden-0123456789ab")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        wrapper.clone("wp-test-golden-1", "wp-test-arp-1", sets=[".cpus = 2"])
        wrapper.clone("cache-golden-0123456789ab", "wp-test-guest-ci")
        assert fake_limactl.calls[0][0] == [
            "clone",
            "--tty=false",
            "--set",
            ".cpus = 2",
            "wp-test-golden-1",
            "wp-test-arp-1",
        ]
        assert {"wp-test-arp-1", "wp-test-guest-ci"} <= set(fake_limactl.instances)
        with pytest.raises(LimaNotFound):
            wrapper.clone("wp-test-nosuch", "wp-test-x")
        with pytest.raises(LimaError):
            wrapper.clone("cache-golden-Bad", "wp-test-x")

    def test_shared_finds_a_cache_outside_the_prefix(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("cache-golden-0123456789ab")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        found = wrapper.shared("cache-golden-0123456789ab")
        assert found is not None
        assert found.status == "Stopped"
        assert wrapper.shared("cache-golden-ba9876543210") is None
        for name in ("wp-test-golden-1", "cache-golden-Bad"):
            with pytest.raises(LimaError) as caught:
                wrapper.shared(name)
            assert caught.value.code == "lima_foreign_instance"

    def test_rename_and_existing_target(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-a")
        fake_limactl.add("wp-test-b")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        wrapper.rename("wp-test-a", "wp-test-c")
        assert set(fake_limactl.instances) == {"wp-test-b", "wp-test-c"}
        with pytest.raises(LimaError) as caught:
            wrapper.rename("wp-test-b", "wp-test-c")
        assert caught.value.code == "lima_failed"

    def test_get_lists_only_the_named_instance(self, fake_limactl: FakeLimactl) -> None:
        fake_limactl.add("wp-test-a", "Running")
        fake_limactl.add("wp-test-b")
        wrapper = Lima(fake_limactl, prefix=PREFIX)
        found = wrapper.get("wp-test-a")
        assert found is not None
        assert (found.name, found.status) == ("wp-test-a", "Running")
        # Lima 2.3.0 ends `list NAME` of a missing instance with "unmatched instances" (exit 1): no instance.
        assert wrapper.get("wp-test-c") is None
        assert [argv for argv, _ in fake_limactl.calls] == [
            ["list", "--json", "wp-test-a"],
            ["list", "--json", "wp-test-c"],
        ]

    def test_get_refuses_a_foreign_name_and_keeps_other_failures(self) -> None:
        with pytest.raises(LimaError) as caught:
            Lima(answer(out=""), prefix=PREFIX).get("other-a")
        assert caught.value.code == "lima_foreign_instance"
        with pytest.raises(LimaError) as caught:
            Lima(answer(code=1, err="boom\n"), prefix=PREFIX).get("wp-test-a")
        assert (caught.value.code, caught.value.operation) == ("lima_failed", "list")

    def test_list_reports_only_own_instances(self) -> None:
        wrapper = Lima(answer(out=fixture_text("list.jsonl")), prefix="wp-vm01-")
        names = [item.name for item in wrapper.list()]
        assert names == ["wp-vm01-golden-2.1.330-0.1.0-20261121", "wp-vm01-golden-2.1.330-0.1.0-20261121-2"]
        assert wrapper.get(names[1]) is not None
        assert wrapper.get("wp-vm01-other") is None
        assert len(Lima(answer(out=fixture_text("list.jsonl"))).list()) == 3

    def test_shell(self) -> None:
        run = answer(out="ok\n")
        result = Lima(run, prefix=PREFIX).shell("wp-test-a", ["sudo", "true"], timeout=3)
        assert result.stdout == "ok\n"
        assert run.calls == [(["shell", "--workdir", "/", "wp-test-a", "sudo", "true"], 3)]  # type: ignore[attr-defined]
        with pytest.raises(LimaError) as caught:
            Lima(run, prefix=PREFIX).shell("wp-test-a", [])
        assert caught.value.code == "lima_argv_invalid"

    def test_shell_guest_failure_with_and_without_check(self) -> None:
        run = answer(2, "partial", "guest says no")
        with pytest.raises(LimaError) as caught:
            Lima(run, prefix=PREFIX).shell("wp-test-a", ["false"])
        assert (caught.value.code, caught.value.returncode) == ("lima_failed", 2)
        result = Lima(run, prefix=PREFIX).shell("wp-test-a", ["false"], check=False)
        assert (result.returncode, result.stdout) == (2, "partial")
        assert run.calls[-1][1] == lima.DEFAULT_TIMEOUTS["shell"]  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("answer_args", "error"),
        [
            (recorded("shell-missing.txt"), LimaNotFound),
            ((lima.EXIT_NOT_FOUND, "", "limactl: not found"), LimaNotInstalled),
            ((lima.EXIT_TIMEOUT, "", "limactl: timed out"), LimaTimeout),
        ],
    )
    def test_shell_without_check_still_raises_lima_failures(
        self, answer_args: tuple[int, str, str], error: type[LimaError]
    ) -> None:
        run = answer(*answer_args)
        with pytest.raises(error):
            Lima(run, prefix=PREFIX).shell("wp-test-a", ["true"], check=False, timeout=1)
        assert len(run.calls) == 1  # type: ignore[attr-defined]

    def test_guest_exit_127_is_the_guest_command(self) -> None:
        run = answer(lima.EXIT_NOT_FOUND, "", "bash: install.sh: command not found")
        with pytest.raises(LimaError) as caught:
            Lima(run, prefix=PREFIX).shell("wp-test-a", ["install.sh"])
        assert (caught.value.code, caught.value.returncode) == ("lima_failed", 127)
        result = Lima(run, prefix=PREFIX).shell("wp-test-a", ["install.sh"], check=False)
        assert result.returncode == lima.EXIT_NOT_FOUND
        assert len(run.calls) == 2  # type: ignore[attr-defined]

    def test_guest_exit_124_is_not_a_timeout(self) -> None:
        with pytest.raises(LimaError) as caught:
            Lima(answer(lima.EXIT_TIMEOUT, "", "guest"), prefix=PREFIX).shell("wp-test-a", ["x"])
        assert caught.value.code == "lima_failed"

    def test_copy_to(self) -> None:
        run = answer()
        wrapper = Lima(run, prefix=PREFIX)
        wrapper.copy_to("/host/file.tar", "wp-test-a", "/tmp/file.tar")
        assert run.calls == [(["copy", "/host/file.tar", "wp-test-a:/tmp/file.tar"], 300.0)]  # type: ignore[attr-defined]
        with pytest.raises(LimaError) as caught:
            wrapper.copy_to("/host/file", "wp-test-a", "relative")
        assert caught.value.code == "lima_argv_invalid"

    def test_errors_are_runner_errors_without_values(self) -> None:
        error = LimaError("lima_failed", "start", returncode=1, detail="secret-ish text")
        assert isinstance(error, RunnerError)
        assert str(error) == "lima_failed start"
