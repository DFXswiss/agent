"""U-RUN-vm-golden: golden image build, registry and retire (05-runner.md sections 3.1, 4.5, 15; 02 section 4).

`limactl` is the stateful fake of `conftest.py` (recorded Lima 2.3.0 output), the store a real PostgreSQL database
with the runner schema, the health check a real HTTP server on a Unix socket where the forwarded control socket
would be. Every build step has a failure test that proves the instance and the row are removed again.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tarfile
import tempfile
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm import golden
from agent_runner.vm.golden import Builder, BuildOptions, Source
from agent_runner.vm.lima import Lima, LimaError

from .conftest import CLAUDE, GUEST, TOKEN, FakeLimactl, default_shell_answers

PREFIX = "wp-test-"
NOW = datetime(2026, 11, 21, 18, 4, 40, tzinfo=UTC)
VERSION = f"{CLAUDE}-{GUEST}-20261121"
OPTIONS = BuildOptions(cpus=4, memory_mib=8192, disk_gib=20)


def healthy(**override: Any) -> dict[str, Any]:
    body = {"state": "unassigned", "guest_version": GUEST, "claude_version": CLAUDE, "landlock_abi": 4}
    body.update(override)
    return body


class FakeHttp:
    """Answers of the control socket in order (the last one repeats); an exception instance is raised."""

    def __init__(self, *answers: tuple[int, bytes] | OSError) -> None:
        self.answers = list(answers) or [(200, json.dumps(healthy()).encode())]
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, path: str, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((path, url, dict(headers), timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, OSError):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        self.now += 0.5
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def source() -> Source:
    return golden.load_source()


@pytest.fixture
def facts() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def make_builder(
    fake_limactl: FakeLimactl, store_dsn: str, tmp_path: Path, facts: list[tuple[str, dict[str, Any]]]
) -> Any:
    def make(http: FakeHttp | None = None, **overrides: Any) -> Builder:
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        clock = Clock()
        fields: dict[str, Any] = {
            "lima": Lima(fake_limactl, prefix=PREFIX),
            "dsn": store_dsn,
            "records": tmp_path / "home" / "golden",
            "scratch": scratch,
            "announce": lambda name, values: facts.append((name, values)),
            "clock": clock,
            "now": lambda: NOW,
            "token_hex": lambda n: "ab" * n,
            "http_get": http or FakeHttp(),
            "sleep": clock.sleep,
        }
        fields.update(overrides)
        return Builder(**fields)

    return make


def rows(dsn: str) -> list[dict[str, Any]]:
    with golden.connect(dsn) as conn:
        return [vars(row) for row in golden.list_goldens(conn)]


def insert(dsn: str, version: str, name: str, state: str, built_at: datetime | None = NOW) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
            " state) VALUES (%s, %s, %s, %s, 'sha256:x', %s, %s)",
            (version, name, CLAUDE, GUEST, built_at, state),
        )


# -- source and bundle ---------------------------------------------------------------------------------------


class TestSource:
    def test_checkout_is_the_default_source(self, source: Source) -> None:
        versions = json.loads((golden.default_root() / "images" / "versions.json").read_text(encoding="utf-8"))
        assert source.claude_version == versions["claude_code"] == CLAUDE
        assert source.guest_version == GUEST
        assert source.image_digest == versions["ubuntu_image_digest"]
        assert source.template == golden.default_root() / "images" / "golden.yaml"
        assert golden.load_source(source.template).root == source.root
        assert golden.load_source(root=source.root).template == source.template

    @staticmethod
    def tree(tmp_path: Path) -> Path:
        root = tmp_path / "tree"
        (root / "images" / "provision").mkdir(parents=True)
        (root / "src" / "agent_guest").mkdir(parents=True)
        (root / "contracts").mkdir()
        (root / "images" / "golden.yaml").write_text("vmType: vz\n", encoding="utf-8")
        (root / "images" / "provision" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "images" / "versions.json").write_text(
            json.dumps({"claude_code": "2.1.400", "ubuntu_image_digest": "sha256:ab"}), encoding="utf-8"
        )
        (root / "src" / "agent_guest" / "__init__.py").write_text('"""G."""\n\n__version__ = "0.2.0"\n', "utf-8")
        return root

    def test_configured_template_brings_its_tree(self, tmp_path: Path) -> None:
        root = self.tree(tmp_path)
        loaded = golden.load_source(root / "images" / "golden.yaml")
        assert (loaded.root, loaded.claude_version, loaded.guest_version) == (root, "2.1.400", "0.2.0")

    @pytest.mark.parametrize(
        "damage",
        [
            lambda r: (r / "images" / "versions.json").unlink(),
            lambda r: (r / "images" / "versions.json").write_text("{", encoding="utf-8"),
            lambda r: (r / "images" / "versions.json").write_text("[]", encoding="utf-8"),
            lambda r: (r / "images" / "versions.json").write_text('{"claude_code": 2}', encoding="utf-8"),
            lambda r: (r / "images" / "versions.json").write_text(
                '{"claude_code": "a b", "ubuntu_image_digest": "x"}', encoding="utf-8"
            ),
            lambda r: (r / "src" / "agent_guest" / "__init__.py").write_text("x = 1\n", encoding="utf-8"),
            lambda r: (r / "src" / "agent_guest" / "__init__.py").unlink(),
            lambda r: (r / "images" / "provision" / "install.sh").unlink(),
            lambda r: shutil.rmtree(r / "contracts"),
            lambda r: (r / "images" / "golden.yaml").unlink(),
        ],
    )
    def test_incomplete_trees_are_refused(self, tmp_path: Path, damage: Any) -> None:
        root = self.tree(tmp_path)
        damage(root)
        with pytest.raises(RunnerError) as caught:
            golden.load_source(root / "images" / "golden.yaml")
        assert caught.value.code == "golden_source_missing"


class TestBundle:
    def test_members_modes_and_digest(self, tmp_path: Path) -> None:
        root = TestSource.tree(tmp_path)
        guest = root / "src" / "agent_guest"
        (guest / "__pycache__").mkdir()
        (guest / "__pycache__" / "x.pyc").write_bytes(b"x")
        (guest / "stale.pyc").write_bytes(b"x")
        (guest / "._apple").write_bytes(b"x")
        (guest / ".DS_Store").write_bytes(b"x")
        (guest / ".hidden").mkdir()
        (guest / ".hidden" / "notes.py").write_bytes(b"x")
        (guest / "link.py").symlink_to(guest / "__init__.py")
        (guest / "broken").symlink_to(tmp_path / "missing")
        for sub in ("guest", "node_modules", "fixtures", "vectors", "tools", "changes"):
            (root / "contracts" / sub).mkdir()
            (root / "contracts" / sub / "a.json").write_text("{}", encoding="utf-8")
        # tools/ and changes/ are skipped at the top level only (the MCP tool schemas live in contracts/mcp/tools/);
        # node_modules, fixtures and vectors at every depth.
        for nested in ("mcp/tools", "mcp/changes", "mcp/node_modules", "ops/fixtures", "ops/vectors"):
            (root / "contracts" / nested).mkdir(parents=True)
            (root / "contracts" / nested / "b.json").write_text("{}", encoding="utf-8")
        (root / "contracts" / "README.md").write_text("x", encoding="utf-8")
        (root / "contracts" / "common.json").write_text("{}", encoding="utf-8")
        source = golden.load_source(root / "images" / "golden.yaml")
        first = golden.write_bundle(source, tmp_path / "a.tar")
        with tarfile.open(tmp_path / "a.tar") as archive:
            members = {info.name: info for info in archive.getmembers()}
        assert sorted(members) == [
            "contracts/common.json",
            "contracts/guest/a.json",
            "contracts/mcp/changes/b.json",
            "contracts/mcp/tools/b.json",
            "images/golden.yaml",
            "images/provision/install.sh",
            "images/versions.json",
            "src/agent_guest/__init__.py",
        ]
        assert all(info.uid == 0 and info.uname == "root" for info in members.values())
        assert members["images/golden.yaml"].mode == 0o644
        os.chmod(root / "images" / "provision" / "install.sh", 0o755)
        os.utime(root / "images" / "golden.yaml", (1, 1))
        second = golden.write_bundle(source, tmp_path / "b.tar")
        with tarfile.open(tmp_path / "b.tar") as archive:
            assert archive.getmember("images/provision/install.sh").mode == 0o755
        assert second != first
        os.chmod(root / "images" / "provision" / "install.sh", 0o644)
        assert golden.write_bundle(source, tmp_path / "c.tar") == first

    def test_the_agent_tool_of_the_main_orchestrator_is_bundled(self, tmp_path: Path) -> None:
        """05-runner.md section 17.4: its sources and the two legacy files it imports, nothing else of agent_cli."""
        root = TestSource.tree(tmp_path)
        for part in ("agent_platform", "agent_runner/crypto"):
            (root / "src" / part).mkdir(parents=True)
            (root / "src" / part / "__init__.py").write_text("", encoding="utf-8")
            (root / "src" / part / "__pycache__").mkdir()
            (root / "src" / part / "__pycache__" / "x.pyc").write_bytes(b"x")
        (root / "src" / "agent_cli").mkdir()
        (root / "src" / "agent_cli" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "agent_cli" / "main.py").write_text("", encoding="utf-8")
        (root / "src" / "agent_cli" / "keys.py").symlink_to(root / "src" / "agent_cli" / "main.py")
        source = golden.load_source(root / "images" / "golden.yaml")
        golden.write_bundle(source, tmp_path / "bundle.tar")
        with tarfile.open(tmp_path / "bundle.tar") as archive:
            names = [
                name
                for name in archive.getnames()
                if name.startswith(("src/agent_platform", "src/agent_r", "src/agent_cli"))
            ]
        assert names == [
            "src/agent_cli/__init__.py",
            "src/agent_platform/__init__.py",
            "src/agent_runner/crypto/__init__.py",
        ]

    def test_the_checkout_bundle_holds_what_the_installer_and_guest_need(self, source: Source, tmp_path: Path) -> None:
        golden.write_bundle(source, tmp_path / "bundle.tar")
        with tarfile.open(tmp_path / "bundle.tar") as archive:
            names = set(archive.getnames())
        assert {
            "images/provision/install.sh",
            "src/agent_guest/control.py",
            "contracts/common.json",
            "contracts/mcp/tools/get_context.input.json",
            "contracts/mcp/tools/request_operation.output.json",
            # the main orchestrator's agent tool (05-runner.md section 17.4)
            "images/mcp-orchestrator.json",
            "images/orchestrator/agent-platform",
            "src/agent_platform/cli.py",
            "src/agent_platform/guide.md",
            "src/agent_runner/crypto/agent.py",
            "src/agent_runner/sync/http.py",
            "src/agent_cli/__init__.py",
            "src/agent_cli/keys.py",
        } <= names
        assert not any(name.startswith("src/agent_cli/") and name not in golden_tool_files() for name in names)
        assert not any(name.startswith(("contracts/tools/", "contracts/vectors/")) for name in names)
        assert any(name.startswith("contracts/guest/") for name in names)
        # every MCP tool of the guest finds its schemas (tools/list and tools/call load them at run time; without them
        # Claude Code cannot list a single platform tool)
        tools = sorted(path.stem for path in (source.root / "src" / "agent_guest" / "mcp_tools").glob("[a-z]*.py"))
        assert tools
        for tool in tools:
            assert {f"contracts/mcp/tools/{tool}.input.json", f"contracts/mcp/tools/{tool}.output.json"} <= names
        assert not any(
            name.startswith(("contracts/tools/", "contracts/vectors/", "contracts/changes/")) for name in names
        )
        assert not any("/fixtures/" in name or "node_modules" in name or name.endswith(".pyc") for name in names)


# -- build ---------------------------------------------------------------------------------------------------


class TestBuild:
    def test_build_success(
        self,
        make_builder: Any,
        fake_limactl: FakeLimactl,
        store_dsn: str,
        source: Source,
        facts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        http = FakeHttp(OSError("not yet"), (503, b""), (200, json.dumps(healthy()).encode()))
        builder = make_builder(http)
        result = builder.build(source, BuildOptions(cpus=2, memory_mib=4096, disk_gib=30))
        final = f"{PREFIX}golden-{VERSION}"
        build_name = f"{PREFIX}golden-build-abababab"
        assert result.golden.version == VERSION
        assert result.golden.lima_name == final
        assert result.golden.state == "ready"
        assert result.build_info == {
            "built_at": "2026-11-21T18:04:40Z",
            "claude_version": CLAUDE,
            "exec_paths_sha256": "66a6c4ac95894cf4fb078a9fc7f2964f6fec21cf18b8448a494967a6276b16a2",
            "guest_version": GUEST,
            "landlock_abi": 4,
            "source_sha256": result.build_info["source_sha256"],
            "tui_start_ms": 180,
        }
        assert list(fake_limactl.instances) == [final]
        assert fake_limactl.instances[final]["status"] == "Stopped"
        create = next(argv for argv, _ in fake_limactl.calls if argv[0] == "create")
        assert create == [
            "create", "-y", f"--name={build_name}", "--cpus", "2", "--memory", "4", "--disk", "30",
            str(source.template),
        ]  # fmt: skip
        assert ["rename", build_name, final] in [argv for argv, _ in fake_limactl.calls]
        copy = next(argv for argv, _ in fake_limactl.calls if argv[0] == "copy")
        assert copy[2] == f"{build_name}:{golden.GUEST_BUNDLE}"
        assert not Path(copy[1]).exists()
        seal = next(argv for argv, _ in fake_limactl.calls if argv[0] == "shell" and "systemctl stop" in " ".join(argv))
        assert seal[-2:] == [golden.TOKEN_FILE, golden.GUEST_BUNDLE_DIR]
        assert "/var/lib/agent-guest/journal.db" in seal[-4]
        assert fake_limactl.shell_steps == [
            "extract", "install", "contracts", "inspect", "build_file", "token", "api", "claude", "selftest", "tui",
            "build_file", "seal",
        ]  # fmt: skip
        build_files = [argv for argv, _ in fake_limactl.calls if argv[0] == "shell" and "printf" in " ".join(argv)]
        assert json.loads(build_files[-1][-1]) == result.build_info
        assert "tui_start_ms" not in json.loads(build_files[0][-1])
        install = next(argv for argv, timeout in fake_limactl.calls if "install.sh" in " ".join(argv))
        assert "--test-model-port" not in install
        assert [call[2] for call in http.calls] == [{"Authorization": f"Bearer {TOKEN}"}] * 3
        assert http.calls[0][0] == os.path.join(str(fake_limactl.lima_dir / build_name), "sock", "agent-guest.sock")
        record = json.loads(result.record.read_text(encoding="utf-8"))
        assert record["build"] == result.build_info
        assert record["lima_name"] == final
        assert set(record["steps"]) == {
            "create", "start", "bundle", "install", "inspect", "services", "claude", "selftest", "tui", "stop",
            "rename", "seal",
        }  # fmt: skip
        assert oct(result.record.stat().st_mode & 0o777) == "0o600"
        assert [row["state"] for row in rows(store_dsn)] == ["ready"]
        assert facts[0] == ("image.build", {"version": VERSION, "lima_name": build_name, "state": "building"})
        assert ("image.step", {"step": "create", "seconds": "0.500"}) in facts
        assert result.seconds > 0

    def test_second_build_retires_the_first_and_gets_a_suffix(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        first = make_builder().build(source, OPTIONS)
        later = datetime(2026, 11, 21, 19, 0, tzinfo=UTC)
        second = make_builder(now=lambda: later).build(source, OPTIONS)
        assert second.golden.version == f"{VERSION}-2"
        assert {row["version"]: row["state"] for row in rows(store_dsn)} == {
            first.golden.version: "retired",
            second.golden.version: "ready",
        }
        with golden.connect(store_dsn) as conn:
            current = golden.ready_golden(conn)
        assert current is not None
        assert current.version == second.golden.version
        assert first.build_info["source_sha256"] == second.build_info["source_sha256"]

    def test_a_tag_names_exactly_one_instance(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        options = BuildOptions(cpus=4, memory_mib=8192, disk_gib=20, tag="0123456789ab")
        insert(store_dsn, "0123456789ab", f"{PREFIX}golden-0123456789ab", "ready")
        # The registered instance is gone (deleted from outside): the stale row is replaced, no suffix.
        assert make_builder().build(source, options).golden.lima_name == f"{PREFIX}golden-0123456789ab"
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, options)
        assert caught.value.code == "golden_exists"
        assert [row["version"] for row in rows(store_dsn)] == ["0123456789ab"]

    def test_tag_and_test_model_port(self, make_builder: Any, fake_limactl: FakeLimactl, source: Source) -> None:
        options = BuildOptions(cpus=4, memory_mib=8192, disk_gib=20, tag="0123456789ab", test_model_port=20330)
        result = make_builder().build(source, options)
        assert result.golden.lima_name == f"{PREFIX}golden-0123456789ab"
        install = next(argv for argv, _ in fake_limactl.calls if "install.sh" in " ".join(argv))
        assert install[-2:] == ["--test-model-port", "20330"]

    def test_suffix_when_only_the_instance_name_exists(
        self, make_builder: Any, fake_limactl: FakeLimactl, source: Source
    ) -> None:
        fake_limactl.add(f"{PREFIX}golden-{VERSION}")
        assert make_builder().build(source, OPTIONS).golden.version == f"{VERSION}-2"

    def test_versions_exhausted(self, make_builder: Any, store_dsn: str, source: Source) -> None:
        insert(store_dsn, VERSION, f"{PREFIX}golden-{VERSION}", "retired")
        for index in range(2, golden.MAX_SUFFIX + 1):
            insert(store_dsn, f"{VERSION}-{index}", f"{PREFIX}golden-{VERSION}-{index}", "retired")
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, OPTIONS)
        assert caught.value.code == "golden_version_exhausted"

    def test_claude_version_must_be_the_pin(self, make_builder: Any, fake_limactl: FakeLimactl, source: Source) -> None:
        builder = make_builder()
        with pytest.raises(RunnerError) as caught:
            builder.build(source, BuildOptions(cpus=4, memory_mib=8192, disk_gib=20, claude_version="2.1.331"))
        assert caught.value.code == "claude_version_unpinned"
        result = builder.build(source, BuildOptions(cpus=4, memory_mib=8192, disk_gib=20, claude_version=CLAUDE))
        assert result.golden.state == "ready"

    @pytest.mark.parametrize("tag", ["Upper", "a b", "-x", "x-"])
    def test_invalid_tag(self, make_builder: Any, fake_limactl: FakeLimactl, source: Source, tag: str) -> None:
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, BuildOptions(cpus=4, memory_mib=8192, disk_gib=20, tag=tag))
        assert (caught.value.code, caught.value.name) == ("usage", "tag")
        assert fake_limactl.calls == []

    def test_old_lima_is_refused_before_anything_happens(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        fake_limactl.version = "limactl version 2.2.0\n"
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, OPTIONS)
        assert caught.value.code == "lima_version"
        assert fake_limactl.ops() == ["--version"]
        assert rows(store_dsn) == []

    def test_stale_state_of_a_crashed_build_is_removed(
        self,
        make_builder: Any,
        fake_limactl: FakeLimactl,
        store_dsn: str,
        source: Source,
        facts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        insert(store_dsn, "old-build", f"{PREFIX}golden-build-00000000", "building", None)
        insert(store_dsn, "foreign-build", "other-golden-build-1", "building", None)
        insert(store_dsn, "renamed", f"{PREFIX}golden-build-22222222", "building", None)
        fake_limactl.add(f"{PREFIX}golden-renamed")
        fake_limactl.add(f"{PREFIX}golden-build-00000000")
        fake_limactl.add(f"{PREFIX}golden-build-11111111")
        fake_limactl.add(f"{PREFIX}golden-keep")
        make_builder().build(source, OPTIONS)
        # The unrecorded build instance belongs to another store (a parallel build with the same prefix).
        assert sorted(fake_limactl.instances) == [
            f"{PREFIX}golden-{VERSION}",
            f"{PREFIX}golden-build-11111111",
            f"{PREFIX}golden-keep",
        ]
        assert [row["version"] for row in rows(store_dsn)] == [VERSION]
        assert ("image.stale", {"version": "old-build", "lima_name": f"{PREFIX}golden-build-00000000"}) in facts

    def test_concurrent_builds_are_refused(self, make_builder: Any, store_dsn: str, source: Source) -> None:
        with psycopg.connect(store_dsn, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (golden.BUILD_LOCK,))
            with pytest.raises(RunnerError) as caught:
                make_builder().build(source, OPTIONS)
        assert caught.value.code == "image_busy"

    def test_unreachable_store(self, make_builder: Any, source: Source) -> None:
        with pytest.raises(RunnerError) as caught:
            make_builder(dsn="postgresql://nobody@127.0.0.1:1/none?connect_timeout=1").build(source, OPTIONS)
        assert caught.value.code == "store_unavailable"


def _answer(step: str, value: tuple[int, str, str]) -> Any:
    def apply(fake: FakeLimactl) -> None:
        fake.shell_answers[step] = value

    return apply


def _fail(op: str, value: tuple[int, str, str] = (1, "", "boom")) -> Any:
    def apply(fake: FakeLimactl) -> None:
        fake.fail[op] = value

    return apply


def _inspect(**body: Any) -> tuple[int, str, str]:
    base: dict[str, Any] = {"exec_paths_sha256": "aa", "landlock_abi": 4}
    base.update(body)
    return 0, json.dumps({key: value for key, value in base.items() if value is not None}), ""


class TestBuildFailures:
    @pytest.mark.parametrize(
        ("setup", "code", "name"),
        [
            (_fail("create"), "lima_failed", "create"),
            (_fail("start"), "lima_failed", "start"),
            (_fail("copy"), "lima_failed", "copy"),
            (_answer("extract", (2, "", "tar: error")), "lima_failed", "shell"),
            (_answer("install", (1, "", "agent-golden: sha256 mismatch")), "lima_failed", "shell"),
            (_answer("contracts", (1, "", "")), "lima_failed", "shell"),
            (_answer("inspect", (0, "garbage", "")), "golden_smoke_failed", "inspect"),
            (_answer("inspect", (0, "[1, 2]", "")), "golden_smoke_failed", "inspect"),
            (_answer("inspect", (0, "", "")), "golden_smoke_failed", "inspect"),
            (_answer("inspect", _inspect(landlock_abi="4")), "golden_smoke_failed", "inspect"),
            (_answer("inspect", _inspect(exec_paths_sha256=None)), "golden_smoke_failed", "inspect"),
            (_answer("inspect", _inspect(landlock_abi=3)), "landlock_abi_unsupported", None),
            (_answer("token", (0, "short\n", "")), "golden_smoke_failed", "token"),
            (_answer("api", (7, "", "")), "golden_smoke_failed", "api"),
            (_answer("api", (0, '{"state": "down"}', "")), "golden_smoke_failed", "api"),
            (_answer("claude", (0, "2.1.329 (Claude Code)\n", "")), "golden_smoke_failed", "claude_version"),
            (_answer("selftest", (0, json.dumps({"ok": False}), "")), "golden_smoke_failed", "selftest"),
            (_answer("selftest", (1, "", "Traceback")), "golden_smoke_failed", "selftest"),
            (_answer("selftest", (0, "not json\n", "")), "golden_smoke_failed", "selftest"),
            (_answer("tui", (0, json.dumps({"ok": False, "tui_start_ms": None}), "")), "golden_smoke_failed", "tui"),
            (_answer("tui", (0, json.dumps({"ok": True, "tui_start_ms": 1.5}), "")), "golden_smoke_failed", "tui"),
            (_answer("seal", (1, "", "")), "lima_failed", "shell"),
            (_fail("stop"), "lima_failed", "stop"),
            (_fail("rename"), "lima_failed", "rename"),
        ],
    )
    def test_failure_removes_instance_and_row(
        self,
        setup: Any,
        *,
        make_builder: Any,
        fake_limactl: FakeLimactl,
        store_dsn: str,
        source: Source,
        code: str,
        name: str | None,
    ) -> None:
        insert(store_dsn, "previous", f"{PREFIX}golden-previous", "ready")
        fake_limactl.add(f"{PREFIX}golden-previous")
        setup(fake_limactl)
        builder = make_builder()
        with pytest.raises(RunnerError) as caught:
            builder.build(source, OPTIONS)
        assert (caught.value.code, caught.value.name) == (code, name)
        assert list(fake_limactl.instances) == [f"{PREFIX}golden-previous"]
        assert [(row["version"], row["state"]) for row in rows(store_dsn)] == [("previous", "ready")]
        assert not (builder.records / f"{VERSION}.json").exists()
        assert list(builder.scratch.iterdir()) == []

    def test_selftest_failure_names_the_failed_checks(
        self, make_builder: Any, fake_limactl: FakeLimactl, source: Source, facts: list[tuple[str, dict[str, Any]]]
    ) -> None:
        report = {"ok": False, "checks": [{"check": "exec_sh", "ok": False}, {"check": "dns", "ok": True}, "x"]}
        fake_limactl.shell_answers["selftest"] = (1, json.dumps(report, indent=1), "")
        with pytest.raises(RunnerError):
            make_builder().build(source, OPTIONS)
        assert ("image.selftest", {"ok": False, "failed": "exec_sh"}) in facts

    def test_failure_after_the_rename_deletes_the_renamed_instance(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source, tmp_path: Path
    ) -> None:
        insert(store_dsn, "previous", f"{PREFIX}golden-previous", "ready")
        fake_limactl.add(f"{PREFIX}golden-previous")
        records = tmp_path / "records-file"
        records.write_text("not a directory", encoding="utf-8")
        with pytest.raises(FileExistsError):
            make_builder(records=records).build(source, OPTIONS)
        assert list(fake_limactl.instances) == [f"{PREFIX}golden-previous"]
        assert [(row["version"], row["state"]) for row in rows(store_dsn)] == [("previous", "ready")]

    def test_failed_steps_are_announced_and_lima_output_logged(
        self, make_builder: Any, fake_limactl: FakeLimactl, source: Source, facts: list[tuple[str, dict[str, Any]]]
    ) -> None:
        logged: list[str] = []
        fake_limactl.shell_answers["install"] = (1, "", "agent-golden: sha256 mismatch for node")
        with pytest.raises(LimaError):
            make_builder(log=logged.append).build(source, OPTIONS)
        assert ("image.failed", {"step": "install"}) in facts
        assert logged == ["image build step install: limactl shell exit 1\nagent-golden: sha256 mismatch for node\n"]

    @pytest.mark.parametrize(
        ("setup", "step"),
        [(_answer("tui", (0, json.dumps({"ok": False}), "")), "tui"), (_fail("start", (1, "", "")), "start")],
    )
    def test_failures_without_lima_output_log_nothing(
        self,
        setup: Any,
        step: str,
        *,
        make_builder: Any,
        fake_limactl: FakeLimactl,
        source: Source,
        facts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        logged: list[str] = []
        setup(fake_limactl)
        with pytest.raises(RunnerError):
            make_builder(log=logged.append).build(source, OPTIONS)
        assert ("image.failed", {"step": step}) in facts
        assert logged == []

    def test_the_golden_has_no_socket_directory_left(
        self, make_builder: Any, fake_limactl: FakeLimactl, lima_dir: Path, source: Source
    ) -> None:
        result = make_builder().build(source, OPTIONS)
        directory = lima_dir / result.golden.lima_name
        assert not (directory / "sock").exists()

    def test_missing_socket_directory_is_fine(
        self, make_builder: Any, fake_limactl: FakeLimactl, lima_dir: Path, source: Source
    ) -> None:
        def remove(argv: list[str]) -> None:
            if argv[0] == "stop":
                sock = lima_dir / argv[1] / "sock"
                sock.chmod(0o700)
                sock.rmdir()

        fake_limactl.hook = remove
        assert make_builder().build(source, OPTIONS).golden.state == "ready"

    def test_instance_vanishing_at_stop_fails_the_build(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        def vanish(argv: list[str]) -> None:
            if argv[0] == "list" and fake_limactl.calls[-2][0][0] == "stop":
                fake_limactl.instances.clear()

        fake_limactl.hook = vanish
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, OPTIONS)
        assert (caught.value.code, caught.value.name) == ("golden_smoke_failed", "stop")
        assert rows(store_dsn) == []

    def test_interrupt_cleans_up_too(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        def interrupt(argv: list[str]) -> None:
            if argv[0] == "stop":
                raise KeyboardInterrupt

        fake_limactl.hook = interrupt
        with pytest.raises(KeyboardInterrupt):
            make_builder().build(source, OPTIONS)
        assert fake_limactl.instances == {}
        assert rows(store_dsn) == []

    def test_failing_delete_during_cleanup_keeps_the_original_error(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, source: Source
    ) -> None:
        fake_limactl.shell_answers["tui"] = (0, json.dumps({"ok": False}), "")
        fake_limactl.fail["delete"] = (1, "", "delete failed")
        with pytest.raises(RunnerError) as caught:
            make_builder().build(source, OPTIONS)
        assert (caught.value.code, caught.value.name) == ("golden_smoke_failed", "tui")
        assert rows(store_dsn) == []


class TestHealth:
    def run(self, make_builder: Any, source: Source, http: FakeHttp, **overrides: Any) -> RunnerError:
        with pytest.raises(RunnerError) as caught:
            make_builder(http, **overrides).build(source, OPTIONS)
        return caught.value

    def test_timeout_while_unreachable(self, make_builder: Any, source: Source) -> None:
        http = FakeHttp(OSError("refused"))
        error = self.run(make_builder, source, http, health_timeout=1.0)
        assert (error.code, error.name) == ("golden_smoke_failed", "health")
        assert len(http.calls) > 1

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            json.dumps(healthy(state="broken")).encode(),
            json.dumps(healthy(landlock_abi=5)).encode(),
        ],
    )
    def test_wrong_answers(self, make_builder: Any, source: Source, body: bytes) -> None:
        error = self.run(make_builder, source, FakeHttp((200, body)))
        assert (error.code, error.name) == ("golden_smoke_failed", "health")

    def test_instance_vanished(self, make_builder: Any, fake_limactl: FakeLimactl, source: Source) -> None:
        def vanish(argv: list[str]) -> None:
            if "token_urlsafe" in " ".join(argv):
                fake_limactl.instances.clear()

        fake_limactl.hook = vanish
        error = self.run(make_builder, source, FakeHttp())
        assert (error.code, error.name) == ("golden_smoke_failed", "health")


class UnixServer:
    """A real HTTP server on a Unix socket (stands in for the forwarded guest control socket)."""

    def __init__(self, path: str, raw: bytes | None = None) -> None:
        self.path = path
        self.headers: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.headers.append(self.headers.get("Authorization", ""))
                if raw is not None:
                    self.wfile.write(raw)
                    return
                body = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        class Server(HTTPServer):
            address_family = socket.AF_UNIX

            def server_bind(self) -> None:
                self.socket.bind(outer.path)
                self.server_address = outer.path  # type: ignore[assignment]
                self.server_name, self.server_port = "localhost", 0

        self.server = Server(path, Handler, bind_and_activate=True)  # type: ignore[arg-type]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory with a short path (Unix socket paths are limited to 104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="vm01-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class TestUnixHttp:
    def test_get_over_a_unix_socket(self, short_dir: Path) -> None:
        server = UnixServer(str(short_dir / "c.sock"))
        try:
            status, body = golden.unix_http_get(server.path, "/v1/health", {"Authorization": "Bearer x"}, 5.0)
        finally:
            server.close()
        assert (status, json.loads(body)) == (200, {"path": "/v1/health"})
        assert server.headers == ["Bearer x"]

    def test_missing_socket(self, short_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            golden.unix_http_get(str(short_dir / "none.sock"), "/", {}, 1.0)

    def test_garbage_answer_is_an_os_error(self, short_dir: Path) -> None:
        server = UnixServer(str(short_dir / "g.sock"), raw=b"garbage\r\n\r\n")
        try:
            with pytest.raises(OSError, match="garbage"):
                golden.unix_http_get(server.path, "/", {}, 5.0)
        finally:
            server.close()


# -- registry and retire ---------------------------------------------------------------------------------------


class TestRegistry:
    def test_list_order_ready_and_references(self, store_dsn: str) -> None:
        insert(store_dsn, "a", f"{PREFIX}golden-a", "retired", datetime(2026, 1, 1, tzinfo=UTC))
        insert(store_dsn, "b", f"{PREFIX}golden-b", "ready", datetime(2026, 2, 1, tzinfo=UTC))
        insert(store_dsn, "c", f"{PREFIX}golden-build-1", "building", None)
        with golden.connect(store_dsn) as conn:
            assert [g.version for g in golden.list_goldens(conn)] == ["c", "b", "a"]
            ready = golden.ready_golden(conn)
            assert ready is not None
            assert ready.version == "b"
            conn.execute(
                "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib)"
                " VALUES ('vm-0000000a', 'arp-a', 'a', 'running', 'h', 4, 8192),"
                " ('vm-0000000b', 'arp-b', 'a', 'deleted', 'h', 4, 8192)"
            )
            assert golden.references(conn, "a") == 1
            assert golden.references(conn, "b") == 0
            conn.execute("UPDATE runner.golden SET state = 'retired' WHERE version = 'b'")
            assert golden.ready_golden(conn) is None


class TestRetire:
    def test_retire_deletes_instance_row_and_record(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str
    ) -> None:
        insert(store_dsn, "old", f"{PREFIX}golden-old", "retired")
        insert(store_dsn, "new", f"{PREFIX}golden-new", "ready")
        fake_limactl.add(f"{PREFIX}golden-old")
        builder = make_builder()
        builder.records.mkdir(parents=True)
        (builder.records / "old.json").write_text("{}", encoding="utf-8")
        retired = builder.retire("old")
        assert (retired.version, retired.state) == ("old", "retired")
        assert fake_limactl.instances == {}
        assert [row["version"] for row in rows(store_dsn)] == ["new"]
        assert not (builder.records / "old.json").exists()

    def test_retire_of_an_older_ready_golden_whose_instance_is_gone(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str
    ) -> None:
        insert(store_dsn, "old", f"{PREFIX}golden-old", "ready", datetime(2026, 1, 1, tzinfo=UTC))
        insert(store_dsn, "new", f"{PREFIX}golden-new", "ready", datetime(2026, 2, 1, tzinfo=UTC))
        make_builder().retire("old")
        assert [row["version"] for row in rows(store_dsn)] == ["new"]

    def test_foreign_instance_names_are_left_alone(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str
    ) -> None:
        insert(store_dsn, "old", "other-golden-old", "retired")
        make_builder().retire("old")
        assert fake_limactl.ops() == []
        assert rows(store_dsn) == []

    @pytest.mark.parametrize(
        ("setup", "code"),
        [
            ("", "golden_unknown"),
            ("ready", "golden_current"),
            ("building", "image_busy"),
            ("in_use", "golden_in_use"),
        ],
    )
    def test_refusals(
        self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str, setup: str, code: str
    ) -> None:
        if setup == "ready":
            insert(store_dsn, "v", f"{PREFIX}golden-v", "ready")
        elif setup == "building":
            insert(store_dsn, "v", f"{PREFIX}golden-build-1", "building", None)
        elif setup == "in_use":
            insert(store_dsn, "v", f"{PREFIX}golden-v", "retired")
            with psycopg.connect(store_dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus,"
                    " memory_mib) VALUES ('vm-0000000a', 'arp-a', 'v', 'stopped', 'h', 4, 8192)"
                )
        fake_limactl.add(f"{PREFIX}golden-v")
        with pytest.raises(RunnerError) as caught:
            make_builder().retire("v")
        assert caught.value.code == code
        assert f"{PREFIX}golden-v" in fake_limactl.instances

    def test_retire_waits_for_no_build(self, make_builder: Any, store_dsn: str) -> None:
        insert(store_dsn, "old", f"{PREFIX}golden-old", "retired")
        with psycopg.connect(store_dsn, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (golden.BUILD_LOCK,))
            with pytest.raises(RunnerError) as caught:
                make_builder().retire("old")
        assert caught.value.code == "image_busy"

    def test_lima_failure_keeps_the_row(self, make_builder: Any, fake_limactl: FakeLimactl, store_dsn: str) -> None:
        insert(store_dsn, "old", f"{PREFIX}golden-old", "retired")
        fake_limactl.fail["delete"] = (1, "", "busy")
        with pytest.raises(LimaError):
            make_builder().retire("old")
        assert [row["version"] for row in rows(store_dsn)] == ["old"]


def test_default_shell_answers_cover_every_step() -> None:
    assert set(default_shell_answers()) >= {"inspect", "tui", "selftest", "token", "seal"}


def golden_tool_files() -> set[str]:
    return set(golden.TOOL_FILES)
