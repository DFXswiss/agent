"""U-RUN-vm-image-cli: `agent-runner image build|list|retire` (05-runner.md sections 1.2 and 3.1).

The commands run end to end through `agent_runner.cli.run_cli`: `limactl` is a stand-in binary on `PATH` (the
stateful fake of `conftest.py`, state in a file), the store a real PostgreSQL database behind injected cluster
tools, the forwarded control socket a real Unix-socket HTTP server.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_cli import pg
from agent_runner.cli import run_cli
from agent_runner.errors import RunnerError
from agent_runner.store.cluster import ClusterTools
from agent_runner.store.migrate import MigrationResult
from agent_runner.vm import cli as image_cli
from agent_runner.vm import golden

from .conftest import CLAUDE, GUEST, STATE_ENV, FakeLimactl, install_binary
from .test_golden import UnixServer, healthy

PREFIX = "wp-cli-"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="vm01c-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def control_server(short_dir: Path) -> Iterator[Path]:
    """Healthy control socket at `<dir>/sock/agent-guest.sock`; returns `<dir>` (every fake instance's dir)."""
    (short_dir / "sock").mkdir()
    body = json.dumps(healthy()).encode()
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
    server = UnixServer(str(short_dir / "sock" / "agent-guest.sock"), raw=raw)
    yield short_dir
    server.close()


class Env:
    def __init__(self, tmp_path: Path, home: Path, fake: FakeLimactl) -> None:
        self.state = tmp_path / "limactl-state.json"
        self.fake = fake
        fake.save(self.state)
        install_binary(tmp_path / "bin")
        self.values = {
            "AGENT_RUNNER_HOME": str(home),
            "AGENT_RUNNER_VM_PREFIX": PREFIX,
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            STATE_ENV: str(self.state),
        }

    def lima(self) -> FakeLimactl:
        return FakeLimactl.load(self.state)


@pytest.fixture
def stops() -> list[Path]:
    return []


@pytest.fixture
def run(
    *,
    make_host: Callable[..., Any],
    store_dsn: str,
    runner_home: Path,
    tmp_path: Path,
    fake_limactl: FakeLimactl,
    stops: list[Path],
) -> Any:
    env = Env(tmp_path, runner_home, fake_limactl)
    exists = {"value": True}

    def call(*argv: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
        tools = ClusterTools(
            ensure=lambda path: "postgresql://unused",
            stop=stops.append,
            create_database=lambda admin, name: store_dsn,
            exists=lambda path: exists["value"],
        )
        captured = make_host({**env.values, **(extra_env or {})}, cluster=tools)
        code = run_cli(list(argv), captured.host)
        return code, captured.stdout.getvalue(), captured.stderr.getvalue()

    call.env = env  # type: ignore[attr-defined]
    call.exists = exists  # type: ignore[attr-defined]
    return call


def facts(stdout: str, name: str) -> list[dict[str, str]]:
    found = []
    for line in stdout.splitlines():
        head, _, rest = line.partition(" ")
        if head == name:
            found.append(dict(item.split("=", 1) for item in rest.split(" ")))
    return found


class TestBuild:
    def test_build_then_list(
        self, run: Any, fake_limactl: FakeLimactl, control_server: Path, stops: list[Path]
    ) -> None:
        fake_limactl.fixed_dir = control_server
        run.env.fake.save(run.env.state)
        code, out, err = run("image", "build", "--claude-version", CLAUDE)
        assert (code, err) == (0, "")
        (built,) = facts(out, "runner.image")
        name = f"{PREFIX}golden-{CLAUDE}-{GUEST}-"
        assert built["action"] == "build"
        assert built["lima_name"].startswith(name)
        assert built["state"] == "ready"
        assert (built["landlock_abi"], built["tui_start_ms"]) == ("4", "180")
        assert Path(built["record"]).is_file()
        steps = [fact["step"] for fact in facts(out, "runner.image.step")]
        assert steps[:3] == ["create", "start", "bundle"]
        assert steps[-2:] == ["stop", "rename"]
        lima = run.env.lima()
        assert list(lima.instances) == [built["lima_name"]]
        assert ["--version"] in [argv for argv, _ in lima.calls]
        assert len(stops) == 1
        assert list((Path(run.env.values["AGENT_RUNNER_HOME"]) / "tmp").iterdir()) == []

        code, out, _ = run("image", "list")
        assert code == 0
        (listed,) = facts(out, "runner.image")
        assert (listed["version"], listed["state"], listed["instance"]) == (built["version"], "ready", "Stopped")
        assert listed["built_at"].endswith("Z")
        assert facts(out, "runner.images") == [{"count": "1"}]

        code, out, _ = run("--json", "image", "list")
        document = json.loads(out)
        assert document["ok"] is True
        assert document["facts"][0]["lima_name"] == built["lima_name"]

    def test_test_model_port_needs_test_mode(self, run: Any) -> None:
        code, _, err = run("image", "build", "--test-model-port", "20330")
        assert code == 1
        assert err == "runner.error code=test_mode_required name=test-model-port\n"

    def test_test_model_port_in_test_mode(self, run: Any, fake_limactl: FakeLimactl, control_server: Path) -> None:
        fake_limactl.fixed_dir = control_server
        run.env.fake.save(run.env.state)
        code, _, err = run("image", "build", "--test-model-port", "20330", "--tag", "abc", extra_env={
            "AGENT_RUNNER_TEST_MODE": "1"})  # fmt: skip
        assert (code, err) == (0, "")
        lima = run.env.lima()
        assert list(lima.instances) == [f"{PREFIX}golden-abc"]
        install = next(argv for argv, _ in lima.calls if "install.sh" in " ".join(argv))
        assert install[-2:] == ["--test-model-port", "20330"]

    @pytest.mark.parametrize("port", ["0", "65536", "x"])
    def test_invalid_port_is_a_usage_error(self, run: Any, port: str) -> None:
        code, _, err = run("image", "build", "--test-model-port", port)
        assert code == 2
        assert "must be a TCP port" in err

    def test_failed_build_reports_the_step(self, run: Any, fake_limactl: FakeLimactl, store_dsn: str) -> None:
        fake_limactl.fail["start"] = (1, "", "boom")
        run.env.fake.save(run.env.state)
        code, _, err = run("image", "build")
        assert (code, err) == (1, "runner.error code=lima_failed name=start\n")
        assert run.env.lima().instances == {}
        log = Path(run.env.values["AGENT_RUNNER_HOME"]) / "log" / "image.log"
        assert log.read_text(encoding="utf-8") == "image build step start: limactl start exit 1\nboom\n"
        assert oct(log.stat().st_mode & 0o777) == "0o600"
        with psycopg.connect(store_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM runner.golden").fetchone() == (0,)

    def test_missing_limactl(self, run: Any) -> None:
        code, _, err = run("image", "build", extra_env={"PATH": "/nonexistent"})
        assert (code, err) == (1, "runner.error code=lima_missing name=version\n")

    def test_missing_template(self, run: Any, tmp_path: Path) -> None:
        template = tmp_path / "t" / "images" / "golden.yaml"
        template.parent.mkdir(parents=True)
        template.write_text("x", encoding="utf-8")
        code, _, err = run("image", "build", extra_env={"AGENT_RUNNER_GOLDEN_TEMPLATE": str(template)})
        assert (code, err) == (1, "runner.error code=golden_source_missing\n")


class TestListAndRetire:
    def test_list_and_retire_without_a_store(self, run: Any) -> None:
        run.exists["value"] = False
        code, out, _ = run("image", "list")
        assert (code, out) == (0, "runner.images count=0\n")
        code, _, err = run("image", "retire", "1.0")
        assert (code, err) == (1, "runner.error code=golden_unknown\n")
        assert run.env.lima().calls == []

    def test_list_marks_missing_instances_and_retire(self, run: Any, store_dsn: str) -> None:
        with psycopg.connect(store_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
                " VALUES ('old', %s, %s, %s, 'sha256:x', 'retired')",
                (f"{PREFIX}golden-old", CLAUDE, GUEST),
            )
        code, out, _ = run("image", "list")
        (listed,) = facts(out, "runner.image")
        assert (listed["instance"], listed["built_at"]) == ("missing", '""')
        code, out, err = run("image", "retire", "old")
        assert (code, err) == (0, "")
        assert facts(out, "runner.image") == [
            {"action": "retire", "version": "old", "lima_name": f"{PREFIX}golden-old"}
        ]
        code, out, _ = run("image", "list")
        assert out == "runner.images count=0\n"

    def test_retire_refusal(self, run: Any) -> None:
        code, _, err = run("image", "retire", "nope")
        assert (code, err) == (1, "runner.error code=golden_unknown\n")

    def test_image_needs_an_action(self, run: Any) -> None:
        code, _, err = run("image")
        assert code == 2
        assert "ACTION" in err


TEST_MODE = {"AGENT_RUNNER_TEST_MODE": "1"}


class TestAdopt:
    @property
    def cache(self) -> str:
        """The shared cache of this checkout (its tag is the checkout's `cache_tag`)."""
        return f"cache-golden-{golden.cache_tag(golden.default_root())}"

    def rows(self, dsn: str) -> list[tuple[str, str, str]]:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute("SELECT version, lima_name, state FROM runner.golden ORDER BY version")
            ]

    def add(self, run: Any, name: str, status: str = "Stopped") -> None:
        lima = run.env.lima()
        lima.add(name, status=status)
        lima.save(run.env.state)

    def test_adopt_registers_the_cache_as_the_ready_golden(self, run: Any, store_dsn: str) -> None:
        cache, tag = self.cache, self.cache.removeprefix("cache-golden-")
        self.add(run, cache)
        with psycopg.connect(store_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
                " state) VALUES ('own', %s, %s, %s, 'sha256:x', now(), 'ready')",
                (f"{PREFIX}golden-own", CLAUDE, GUEST),
            )
        code, out, err = run("image", "adopt", cache, extra_env=TEST_MODE)
        assert (code, err) == (0, "")
        adopted = {"version": tag, "lima_name": cache}
        assert facts(out, "runner.image.adopt") == [{**adopted, "state": "ready"}]
        assert facts(out, "runner.image") == [{"action": "adopt", **adopted}]
        own = ("own", f"{PREFIX}golden-own", "retired")
        assert self.rows(store_dsn) == sorted([(tag, cache, "ready"), own])
        with golden.connect(store_dsn) as conn:
            ready = golden.ready_golden(conn)
        assert ready is not None
        assert (ready.version, ready.claude_version, ready.guest_version) == (tag, CLAUDE, GUEST)
        # Adopting again refreshes the row; the cache instance is never touched.
        assert run("image", "adopt", cache, extra_env=TEST_MODE)[0] == 0
        assert (tag, cache, "ready") in self.rows(store_dsn)
        assert [call[0][0] for call in run.env.lima().calls] == ["--version", "list"] * 2
        # The cache is not the store's own instance: retiring it later deletes only the row, never the instance.
        with psycopg.connect(store_dsn, autocommit=True) as conn:
            conn.execute("UPDATE runner.golden SET state = 'retired' WHERE version = %s", (tag,))
            conn.execute("UPDATE runner.golden SET state = 'ready', built_at = now() WHERE version = 'own'")
        code, _, err = run("image", "retire", tag)
        assert (code, err) == (0, "")
        assert "delete" not in [call[0][0] for call in run.env.lima().calls]
        assert cache in run.env.lima().instances

    def test_refusals(self, run: Any, store_dsn: str) -> None:
        cache = self.cache
        code, _, err = run("image", "adopt", cache)
        assert (code, err) == (1, "runner.error code=test_mode_required name=adopt\n")
        code, _, err = run("image", "adopt", cache, extra_env=TEST_MODE)
        assert (code, err) == (1, f"runner.error code=golden_missing name={cache}\n")
        self.add(run, cache, status="Running")
        code, _, err = run("image", "adopt", cache, extra_env=TEST_MODE)
        assert (code, err) == (1, f"runner.error code=golden_not_stopped name={cache}\n")
        # A cache of other inputs (another tag) would carry other versions: refused.
        self.add(run, "cache-golden-0123456789ab")
        code, _, err = run("image", "adopt", "cache-golden-0123456789ab", extra_env=TEST_MODE)
        assert (code, err) == (1, "runner.error code=golden_stale name=cache-golden-0123456789ab\n")
        code, _, err = run("image", "adopt", f"{PREFIX}golden-own", extra_env=TEST_MODE)
        assert (code, err) == (1, "runner.error code=lima_foreign_instance name=list\n")
        assert self.rows(store_dsn) == []


def test_cache_tag_follows_the_shared_rule(tmp_path: Path) -> None:
    for rel, text in (("images/golden.yaml", "a"), ("src/agent_guest/__init__.py", "b"), ("x/ignored.txt", "c")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    lines = "".join(
        f"{hashlib.sha256(text.encode()).hexdigest()}  {rel}\n"
        for rel, text in sorted((("images/golden.yaml", "a"), ("src/agent_guest/__init__.py", "b")))
    )
    base = golden.cache_tag(tmp_path)
    assert base == hashlib.sha256(lines.encode()).hexdigest()[:12]
    # Skipped directories and files do not count; the optional inputs count once they exist.
    for rel in ("src/agent_guest/__pycache__/m.pyc", "images/.DS_Store", "images/node_modules/x"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("skip")
    assert golden.cache_tag(tmp_path) == base
    (tmp_path / "src" / "agent_runner" / "vm").mkdir(parents=True)
    (tmp_path / "src" / "agent_runner" / "vm" / "lima.py").write_text("d")
    assert golden.cache_tag(tmp_path) != base


class TestStore:
    def ctx(self, make_host: Callable[..., Any], store_dsn: str, stops: list[Path]) -> Any:
        tools = ClusterTools(
            ensure=lambda path: "postgresql://unused",
            stop=stops.append,
            create_database=lambda admin, name: store_dsn,
            exists=lambda path: True,
        )
        return type("Ctx", (), {"host": make_host({}, cluster=tools).host})()

    def config(self, home: Path) -> Any:
        return type("Config", (), {"pg_dir": home / "pg"})()

    def test_a_running_store_is_left_running(
        self, make_host: Callable[..., Any], store_dsn: str, stops: list[Path], tmp_path: Path
    ) -> None:
        ctx = self.ctx(make_host, store_dsn, stops)
        with image_cli.store(self.config(tmp_path), ctx, running=lambda path: True) as dsn:
            assert dsn == store_dsn
        assert stops == []

    def test_probe_failure(
        self, make_host: Callable[..., Any], store_dsn: str, stops: list[Path], tmp_path: Path
    ) -> None:
        def broken(path: Path) -> bool:
            raise pg.PgError("probe failed")

        with (
            pytest.raises(RunnerError) as caught,
            image_cli.store(self.config(tmp_path), self.ctx(make_host, store_dsn, stops), running=broken),
        ):
            pass
        assert caught.value.code == "store_unavailable"

    def test_failed_migration_stops_the_store(
        self,
        make_host: Callable[..., Any],
        store_dsn: str,
        stops: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(image_cli, "migrate", lambda dsn: MigrationResult("failed", 0, "0002_x"))
        with (
            pytest.raises(RunnerError) as caught,
            image_cli.store(self.config(tmp_path), self.ctx(make_host, store_dsn, stops), running=lambda path: False),
        ):
            pass
        assert (caught.value.code, caught.value.name) == ("migration_failed", "0002_x")
        assert stops == [tmp_path / "pg"]


def test_make_lima_uses_the_prefix_and_environment(tmp_path: Path) -> None:
    config = type("Config", (), {"vm_prefix": "wp-x-"})()
    wrapper = image_cli.make_lima(config, {"PATH": str(tmp_path)})
    assert wrapper.prefix == "wp-x-"
    with pytest.raises(RunnerError) as caught:
        wrapper.version()
    assert caught.value.code == "lima_missing"
    assert golden.MIN_LANDLOCK_ABI == 4
