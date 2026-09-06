from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

from agent_cli import a38
from agent_cli import a38_jobs
from agent_cli.a38_job_adapters import commands, compose, http_smoke, immutable
from agent_cli.a38_job_adapters.common import (
    DIAGNOSTIC_TIMEOUT_S,
    CommonConfig,
    JobError,
    JobRuntime,
    npm_lock_name,
    parse_common_config,
    safe_join,
)

pytestmark = pytest.mark.no_pg

HEAD_A = "a" * 40
DOCKER_ID_A = "a" * 64
DOCKER_ID_B = "b" * 64


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=True)


def _git(cwd: Path, *argv: str) -> str:
    return _run(["git", *argv], cwd).stdout.strip()


def _commit(cwd: Path, message: str) -> str:
    _run(["git", "add", "-A"], cwd)
    _run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            message,
        ],
        cwd,
    )
    return _git(cwd, "rev-parse", "HEAD")


def _repo(path: Path, *, origin: str = "https://github.com/example/app.git") -> tuple[str, str]:
    path.mkdir(parents=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "a38@example.invalid"], path)
    _run(["git", "config", "user.name", "A38 Fixture"], path)
    (path / "README.md").write_text("example\n", encoding="utf-8")
    first = _commit(path, "first")
    (path / "README.md").write_text("example two\n", encoding="utf-8")
    head = _commit(path, "second")
    _run(["git", "remote", "add", "origin", origin], path)
    return first, head


def _env(base: str, head: str, **extra: str) -> dict[str, str]:
    value = {"PATH": os.environ.get("PATH", ""), "A38_BASE_SHA": base, "A38_HEAD_SHA": head}
    value.update(extra)
    return value


def _artifacts_from(output: str) -> Path:
    matches = re.findall(r"^a38: artifacts: (.+)$", output, flags=re.MULTILINE)
    assert matches
    return Path(matches[-1])


def _executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_cli_dispatch_and_parser_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def runner(config: str, **kwargs: object) -> int:
        seen.update(config=config, kwargs=kwargs)
        return 23

    monkeypatch.setitem(a38_jobs._RUNNERS, "commands", runner)
    assert a38_jobs.run_job("commands", '{"steps":[["true"]]}') == 23
    assert seen["config"] == '{"steps":[["true"]]}'
    parsed = a38.build_parser().parse_args(
        ["job", "immutable", "--config", '{"path":"schema"}']
    )
    assert parsed.adapter == "immutable"
    assert parsed.func is a38_jobs._cmd_job
    with pytest.raises(JobError, match="unknown adapter"):
        a38_jobs.run_job("other", "{}")


@pytest.mark.parametrize(
    "text,match",
    [
        ('{"steps":[["true"]],"steps":[]}', "duplicate key"),
        ('{"steps":[["true"]],"x":NaN}', "non-finite"),
        ('{"steps":[["echo","{missing}"]]}', "unknown placeholder"),
        ('{"steps":[["echo",""]]}', "non-empty"),
        ('{"steps":[["echo","bad\\u0000arg"]]}', "NUL"),
        ('{"steps":[["echo","{repo"]]}', "malformed"),
        ('{"steps":[["echo","{companion}"]]}', "unsupported"),
    ],
)
def test_commands_strict_validation_happens_without_runtime(text: str, match: str) -> None:
    with mock.patch.object(commands, "run_lifecycle") as lifecycle:
        with pytest.raises(JobError, match=match):
            commands.run_commands(text)
        lifecycle.assert_not_called()


def test_all_nested_compose_argv_are_validated_before_runtime() -> None:
    config = {
        "companion": {
            "directory_env": "EXAMPLE_DIR",
            "ref_env": "EXAMPLE_REF",
            "ref": "main",
            "repository": "example/services",
        },
        "files": ["stack/compose.yml"],
        "up": ["echo", "{compose}"],
        "builds": [],
        "test_service": "tests",
        "artifacts": [],
    }
    with mock.patch.object(compose, "run_lifecycle") as lifecycle:
        with pytest.raises(JobError, match="first argv element"):
            compose.run_compose(json.dumps(config))
        lifecycle.assert_not_called()


def test_common_validation_node_canaries_and_unknown_keys() -> None:
    with pytest.raises(JobError, match="unknown keys"):
        parse_common_config({"npm": {"node_major": 24, "canaries": ["pkg/file"], "x": 1}})
    with pytest.raises(JobError, match="must not contain"):
        parse_common_config({"npm": {"node_major": 24, "canaries": ["../pkg"]}})
    with pytest.raises(JobError, match="positive integer"):
        parse_common_config({"npm": {"node_major": 24.5, "canaries": ["pkg/file"]}})


def test_commands_preserve_primary_and_run_diagnostics(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    base, head = _repo(tmp_path / "repo")
    marker = tmp_path / "advisory-ran"
    config = {
        "unset": ["GH_TOKEN"],
        "unset_prefixes": ["EXAMPLE_SECRET_"],
        "env": {"RESULT_DIR": "{artifacts}/result"},
        "steps": [[sys.executable, "-c", "import sys; sys.exit(7)"]],
        "failure_steps": [
            {
                "argv": [sys.executable, "-c", "print('generic failure summary')"],
                "stdout": "{artifacts}/summary.md",
            }
        ],
        "advisory_steps": [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('yes'); raise SystemExit(9)",
            ]
        ],
    }
    status = commands.run_commands(
        json.dumps(config),
        cwd=tmp_path / "repo",
        lock_root=tmp_path / "locks",
        environ=_env(base, head, GH_TOKEN="drop", EXAMPLE_SECRET_ONE="drop"),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == 7, captured.err
        assert (artifacts / "summary.md").read_text(encoding="utf-8") == "generic failure summary\n"
        assert marker.read_text(encoding="utf-8") == "yes"
        assert "warning: advisory step failed with exit 9" in captured.err
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


def test_commands_success_skips_failure_and_scopes_environment(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base, head = _repo(tmp_path / "repo")
    failure_marker = tmp_path / "failure-marker"
    script = tmp_path / "inspect-env.py"
    script.write_text(
        "import json, os\n"
        "keys = ['GH_TOKEN', 'NODE_OPTIONS', 'EXAMPLE_SECRET_ONE', 'EXPLICIT', 'A38_HEAD_SHA']\n"
        "print(json.dumps({key: os.environ.get(key) for key in keys}))\n",
        encoding="utf-8",
    )
    config = {
        "unset_prefixes": ["EXAMPLE_SECRET_"],
        "env": {"EXPLICIT": "{head}"},
        "steps": [
            {"argv": [sys.executable, str(script)], "stdout": "{artifacts}/env.json"}
        ],
        "failure_steps": [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(failure_marker)!r}).touch()",
            ]
        ],
        "advisory_steps": [[sys.executable, "-c", "raise SystemExit(0)"]],
    }
    status = commands.run_commands(
        json.dumps(config),
        cwd=tmp_path / "repo",
        lock_root=tmp_path / "locks",
        environ=_env(
            base,
            head,
            GH_TOKEN="secret",
            NODE_OPTIONS="--inspect",
            EXAMPLE_SECRET_ONE="secret",
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        values = json.loads((artifacts / "env.json").read_text(encoding="utf-8"))
        assert status == 0, captured.err
        assert values == {
            "GH_TOKEN": None,
            "NODE_OPTIONS": None,
            "EXAMPLE_SECRET_ONE": None,
            "EXPLICIT": head,
            "A38_HEAD_SHA": head,
        }
        assert not failure_marker.exists()
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


def test_diagnostic_steps_have_hard_timeout(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DIAGNOSTIC_TIMEOUT_S == 15
    base, head = _repo(tmp_path / "repo")
    monkeypatch.setattr(commands, "DIAGNOSTIC_TIMEOUT_S", 0.05)
    config = {
        "steps": [[sys.executable, "-c", "raise SystemExit(4)"]],
        "failure_steps": [[sys.executable, "-c", "import time; time.sleep(30)"]],
    }
    started = time.monotonic()
    status = commands.run_commands(
        json.dumps(config),
        cwd=tmp_path / "repo",
        lock_root=tmp_path / "locks",
        environ=_env(base, head),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    shutil.rmtree(artifacts, ignore_errors=True)
    assert status == 4, captured.err
    assert time.monotonic() - started < 5


def test_runtime_dirs_ids_and_lock_ownership(tmp_path: Path) -> None:
    base, head = _repo(tmp_path / "repo")
    runtime = JobRuntime(
        adapter="commands",
        common=CommonConfig(),
        cwd=tmp_path / "repo",
        lock_root=tmp_path / "locks",
        environ=_env(base, head, A38_LOCK_POLL_SECONDS="0.01"),
    )
    artifacts = runtime.artifacts
    try:
        assert runtime.work is not None and runtime.artifacts is not None
        with pytest.raises(ValueError):
            runtime.work.relative_to(runtime.root)
        assert re.fullmatch(r"a38-[a-f0-9]{24}", runtime.run_id)
        runtime.lock_acquire("owned", budget_s=0.1)
        holder = runtime.lock_root / "owned.lock" / "holder"
        assert f"run_id={runtime.run_id}" in holder.read_text(encoding="utf-8")
        holder.write_text("pid=999999\nrun_id=foreign\n", encoding="utf-8")
        with pytest.raises(JobError, match="refusing to release"):
            runtime.lock_release("owned")
        runtime._held_locks.clear()
        foreign = runtime.lock_root / "foreign.lock"
        foreign.mkdir()
        (foreign / "holder").write_text("pid=999999\nrun_id=foreign\n", encoding="utf-8")
        with pytest.raises(JobError, match="not acquired"):
            runtime.lock_acquire("foreign", budget_s=0.03)
        assert foreign.is_dir()
    finally:
        runtime.cleanup(1)
        shutil.rmtree(runtime.lock_root / "owned.lock", ignore_errors=True)
        shutil.rmtree(artifacts, ignore_errors=True)


def test_npm_cache_requires_stamp_version_arch_and_canaries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    (repo / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "node",
        "import sys\nprint('24' if 'process.versions.node' in sys.argv[-1] else 'v24.1.0')\n",
    )
    npm_log = tmp_path / "npm.log"
    _executable(
        bin_dir / "npm",
        "from pathlib import Path\n"
        "import os\n"
        "root=Path.cwd(); p=root/'node_modules'/'example-package'/'package.json'\n"
        "p.parent.mkdir(parents=True,exist_ok=True); p.write_text('{}')\n"
        "with Path(os.environ['FAKE_NPM_LOG']).open('a') as f: f.write('ci\\n')\n",
    )
    common = parse_common_config(
        {"npm": {"node_major": 24, "canaries": ["example-package/package.json"]}}
    )
    runtime = JobRuntime(
        adapter="commands",
        common=common,
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_env(
            base,
            head,
            PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            FAKE_NPM_LOG=str(npm_log),
        ),
    )
    artifacts = runtime.artifacts
    try:
        runtime.ensure_node_modules()
        runtime.ensure_node_modules()
        assert npm_log.read_text(encoding="utf-8").splitlines() == ["ci"]
        (repo / "node_modules" / "example-package" / "package.json").unlink()
        runtime.ensure_node_modules()
        assert npm_log.read_text(encoding="utf-8").splitlines() == ["ci", "ci"]
        assert npm_lock_name(repo) != npm_lock_name(tmp_path / "another-repo")
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def _fake_docker(path: Path) -> Path:
    return _executable(
        path,
        """import json, os, sys
from pathlib import Path
args=sys.argv[1:]
log=Path(os.environ['FAKE_DOCKER_LOG'])
with log.open('a', encoding='utf-8') as f: f.write(json.dumps(args)+'\\n')
if args[:2] == ['context','show']:
    print('default'); raise SystemExit(0)
if args[:2] == ['context','inspect']:
    print(os.environ.get('FAKE_DOCKER_ENDPOINT','unix:///tmp/example.sock')); raise SystemExit(0)
if args and args[0] == 'build':
    tag=args[args.index('-t')+1]
    if os.environ.get('FAKE_FAIL_TAG') and os.environ['FAKE_FAIL_TAG'] in tag: raise SystemExit(8)
    raise SystemExit(0)
if args[:2] == ['image','inspect']:
    print('sha256:'+'d'*64); raise SystemExit(0)
if args and args[0] == 'create':
    count=Path(os.environ['FAKE_DOCKER_COUNT'])
    n=int(count.read_text() or '0') if count.exists() else 0
    count.write_text(str(n+1))
    print(('a' if n == 0 else 'b')*64); raise SystemExit(0)
if args and args[0] == 'start':
    if '-a' in args and args[-1].startswith('a'): raise SystemExit(1)
    if '-a' in args: raise SystemExit(int(os.environ.get('FAKE_TEST_EXIT','0')))
    raise SystemExit(0)
if args and args[0] == 'port':
    print('127.0.0.1:'+os.environ.get('FAKE_DOCKER_PORT','49152')); raise SystemExit(0)
if args and args[0] == 'exec': raise SystemExit(0)
if args and args[0] == 'inspect':
    if '--format' in args:
        fmt=args[args.index('--format')+1]
        if fmt == '{{.Id}}': print('c'*64)
        elif '.Mounts' in fmt:
            if os.environ.get('FAKE_COMPOSE_MOUNTS_EXIT'):
                raise SystemExit(int(os.environ['FAKE_COMPOSE_MOUNTS_EXIT']))
            print('a38-example-volume')
        else: raise SystemExit(97)
    else:
        full_inspects=[]
        for line in log.read_text(encoding='utf-8').splitlines():
            call=json.loads(line)
            if call and call[0] == 'inspect' and '--format' not in call:
                full_inspects.append(call)
        final=len(full_inspects) > 1
        env_prefix='FAKE_COMPOSE_FINAL_INSPECT' if final else 'FAKE_COMPOSE_INSPECT'
        if os.environ.get(env_prefix+'_EXIT'):
            raise SystemExit(int(os.environ[env_prefix+'_EXIT']))
        if env_prefix+'_OUTPUT' in os.environ:
            sys.stdout.write(os.environ[env_prefix+'_OUTPUT'])
            raise SystemExit(0)
        project=service=None
        for line in reversed(log.read_text(encoding='utf-8').splitlines()):
            call=json.loads(line)
            if call and call[0] == 'compose' and 'create' in call:
                project=call[call.index('-p')+1]
                service=call[-1]
                break
        if project is None or service is None: raise SystemExit(97)
        candidate=args[-1]
        payload=[{
            'Id': os.environ.get(env_prefix+'_ID',candidate),
            'Config': {'Labels': {
                'com.docker.compose.project': os.environ.get(env_prefix+'_PROJECT',project),
                'com.docker.compose.service': os.environ.get(env_prefix+'_SERVICE',service),
            }},
        }]
        if final:
            payload[0]['State']=json.loads(os.environ.get('FAKE_COMPOSE_FINAL_STATE',json.dumps({
                'Status': 'exited',
                'Running': False,
                'StartedAt': '2026-01-02T03:04:05.123456789Z',
                'FinishedAt': '2026-01-02T03:04:06.987654321Z',
                'ExitCode': int(os.environ.get('FAKE_TEST_STATE_EXIT','0')),
            })))
        print(json.dumps(payload))
    raise SystemExit(0)
if args and args[0] == 'cp':
    dest=Path(args[-1]); dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.name == 'manifest.json': dest.write_text(os.environ['FAKE_MANIFEST'])
    else: dest.mkdir(parents=True,exist_ok=True); (dest/'result.txt').write_text('ok')
    raise SystemExit(0)
if args[:2] == ['volume','inspect']:
    for line in reversed(log.read_text(encoding='utf-8').splitlines()):
        call=json.loads(line)
        if '-p' in call:
            print(call[call.index('-p')+1]); break
    raise SystemExit(0)
if args and args[0] in {'rm','logs','volume'}: raise SystemExit(0)
if args[:2] == ['image','rm']: raise SystemExit(0)
if args and args[0] == 'compose':
    if 'version' in args: print('Docker Compose version v2'); raise SystemExit(0)
    if 'port' in args: print('127.0.0.1:49153'); raise SystemExit(0)
    if 'run' in args and '--no-start' in args: raise SystemExit(96)
    if 'create' in args:
        raise SystemExit(int(os.environ.get('FAKE_COMPOSE_CREATE_EXIT','0')))
    if 'ps' in args:
        if os.environ.get('FAKE_COMPOSE_PS_EXIT'):
            raise SystemExit(int(os.environ['FAKE_COMPOSE_PS_EXIT']))
        sys.stdout.write(os.environ.get('FAKE_COMPOSE_PS_OUTPUT','c'*64+'\\n'))
        raise SystemExit(0)
    if 'build' in args:
        image=os.environ.get('FAKE_COMPOSE_BUILD_IMAGE','')
        if image and os.environ.get('FAKE_FAIL_TAG') in image: raise SystemExit(8)
        raise SystemExit(0)
    if 'up' in args and '--exit-code-from' in args:
        create=None
        for line in reversed(log.read_text(encoding='utf-8').splitlines()):
            call=json.loads(line)
            if call and call[0] == 'compose' and 'create' in call:
                create=call
                break
        if create is None: raise SystemExit(98)
        service=create[-1]
        expected=create[:create.index('create')]+[
            'up','--no-build','--no-recreate','--no-color',
            '--no-log-prefix',
            '--exit-code-from',service,'--attach',service,service,
        ]
        if args != expected: raise SystemExit(98)
        raise SystemExit(int(os.environ.get('FAKE_COMPOSE_TEST_UP_EXIT','0')))
    if any(command in args for command in ('up','down','logs')): raise SystemExit(0)
    raise SystemExit(97)
raise SystemExit(97)
""",
    )


def _docker_env(base: str, head: str, bin_dir: Path, log: Path, **extra: str) -> dict[str, str]:
    values = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_HOST": "unix:///tmp/example.sock",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_COUNT": str(log.with_suffix(".count")),
    }
    values.update(extra)
    return _env(base, head, **values)


def _docker_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_postgres_records_create_id_before_start(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker.log"
    common = parse_common_config(
        {
            "postgres": {
                "image": "postgres:16",
                "user": "postgres",
                "password": "example-password",
                "database": "example_test",
                "url_env": "EXAMPLE_DATABASE_URL",
                "port_env": "EXAMPLE_DATABASE_PORT",
            }
        }
    )
    runtime = JobRuntime(
        adapter="commands",
        common=common,
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(base, head, bin_dir, log),
    )
    artifacts = runtime.artifacts
    try:
        runtime.postgres_start()
        calls = _docker_calls(log)
        assert calls[0][0] == "create"
        assert calls[1] == ["start", DOCKER_ID_A]
        assert runtime._pg_container == DOCKER_ID_A
        assert runtime.env["EXAMPLE_DATABASE_PORT"] == "49152"
        assert "127.0.0.1:49152" in runtime.env["EXAMPLE_DATABASE_URL"]
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def test_docker_context_precedence_and_plugin_safety(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    old_config = tmp_path / "docker-config"
    plugins = old_config / "cli-plugins"
    plugins.mkdir(parents=True)
    compose_plugin = _executable(plugins / "docker-compose", "raise SystemExit(0)\n")
    outside = _executable(tmp_path / "outside-buildx", "raise SystemExit(0)\n")
    (plugins / "docker-buildx").symlink_to(outside)
    (old_config / "config.json").write_text(
        '{"auths":{"registry.example.invalid":{"auth":"must-not-copy"}}}\n',
        encoding="utf-8",
    )
    log = tmp_path / "docker.log"
    runtime = JobRuntime(
        adapter="compose",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            DOCKER_CONTEXT="chosen",
            DOCKER_CONFIG=str(old_config),
        ),
    )
    artifacts = runtime.artifacts
    try:
        runtime.isolate_docker_config()
        calls = _docker_calls(log)
        assert calls[0][:3] == ["context", "inspect", "chosen"]
        isolated = Path(runtime.env["DOCKER_CONFIG"])
        isolated_plugins = isolated / "cli-plugins"
        compose_link = isolated_plugins / "docker-compose"
        buildx_link = isolated_plugins / "docker-buildx"
        assert compose_link.is_symlink()
        assert compose_link.readlink() == compose_plugin.resolve()
        assert buildx_link.is_symlink()
        assert buildx_link.readlink() == outside.resolve()
        assert sorted(path.name for path in isolated.iterdir()) == ["cli-plugins"]
        assert sorted(path.name for path in isolated_plugins.iterdir()) == [
            "docker-buildx",
            "docker-compose",
        ]
        assert all(path.is_symlink() for path in isolated_plugins.iterdir())
        assert not (isolated / "config.json").exists()
        assert "DOCKER_CONTEXT" not in runtime.env
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def _stub_host_plugin_resolution(
    monkeypatch: pytest.MonkeyPatch,
    replacements: dict[Path, Path] | None = None,
) -> list[Path]:
    replacements = replacements or {}
    original_resolve = Path.resolve
    host_candidates = {
        Path(prefix) / plugin
        for prefix in (
            "/opt/homebrew/lib/docker/cli-plugins",
            "/usr/local/lib/docker/cli-plugins",
        )
        for plugin in ("docker-compose", "docker-buildx")
    }
    attempts: list[Path] = []

    def controlled_resolve(path: Path, strict: bool = False) -> Path:
        if path in host_candidates:
            attempts.append(path)
            replacement = replacements.get(path)
            if replacement is None:
                raise FileNotFoundError(path)
            return original_resolve(replacement, strict=True)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", controlled_resolve)
    return attempts


@pytest.mark.parametrize(
    ("fixture_kind", "accepted"),
    [
        ("installed-symlink", True),
        ("installed-symlink-chain", True),
        ("dangling-symlink", False),
        ("symlink-cycle", False),
        ("directory-target", False),
        ("nonexecutable-regular", False),
        ("nonexecutable-target", False),
    ],
)
def test_docker_plugin_candidates_resolve_only_to_executable_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_kind: str,
    accepted: bool,
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    old_config = tmp_path / "docker-config"
    plugins = old_config / "cli-plugins"
    plugins.mkdir(parents=True)
    source = plugins / "docker-compose"
    resolved_target: Path | None = None

    if fixture_kind in {"installed-symlink", "installed-symlink-chain"}:
        installed = tmp_path / "installed"
        installed.mkdir()
        resolved_target = _executable(
            installed / "docker-compose-real",
            "raise SystemExit(0)\n",
        )
        if fixture_kind == "installed-symlink-chain":
            intermediate = installed / "docker-compose-current"
            intermediate.symlink_to(resolved_target)
            source.symlink_to(intermediate)
        else:
            source.symlink_to(resolved_target)
    elif fixture_kind == "dangling-symlink":
        source.symlink_to(tmp_path / "missing-compose")
    elif fixture_kind == "symlink-cycle":
        intermediate = tmp_path / "compose-cycle"
        source.symlink_to(intermediate)
        intermediate.symlink_to(source)
    elif fixture_kind == "directory-target":
        directory = tmp_path / "compose-directory"
        directory.mkdir()
        source.symlink_to(directory, target_is_directory=True)
    elif fixture_kind == "nonexecutable-regular":
        source.write_text("not executable\n", encoding="utf-8")
        source.chmod(0o644)
    else:
        nonexecutable = tmp_path / "docker-compose-nonexecutable"
        nonexecutable.write_text("not executable\n", encoding="utf-8")
        nonexecutable.chmod(0o644)
        source.symlink_to(nonexecutable)

    log = tmp_path / "docker.log"
    runtime = JobRuntime(
        adapter="compose",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            DOCKER_CONFIG=str(old_config),
        ),
    )
    _stub_host_plugin_resolution(monkeypatch)
    artifacts = runtime.artifacts
    try:
        runtime.isolate_docker_config()
        isolated = Path(runtime.env["DOCKER_CONFIG"]) / "cli-plugins" / "docker-compose"
        if accepted:
            assert resolved_target is not None
            assert isolated.is_symlink()
            assert isolated.readlink() == resolved_target.resolve()
        else:
            assert not isolated.exists()
            assert not isolated.is_symlink()
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def test_docker_plugin_uses_next_candidate_after_invalid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    old_config = tmp_path / "docker-config"
    plugins = old_config / "cli-plugins"
    plugins.mkdir(parents=True)
    (plugins / "docker-compose").symlink_to(tmp_path / "missing-compose")
    fallback = _executable(tmp_path / "fallback-compose", "raise SystemExit(0)\n")
    opt_candidate = Path("/opt/homebrew/lib/docker/cli-plugins/docker-compose")
    usr_local_candidate = Path("/usr/local/lib/docker/cli-plugins/docker-compose")
    log = tmp_path / "docker.log"
    runtime = JobRuntime(
        adapter="compose",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            DOCKER_CONFIG=str(old_config),
        ),
    )
    attempts = _stub_host_plugin_resolution(
        monkeypatch,
        replacements={usr_local_candidate: fallback},
    )
    artifacts = runtime.artifacts
    try:
        runtime.isolate_docker_config()
        isolated = Path(runtime.env["DOCKER_CONFIG"]) / "cli-plugins" / "docker-compose"
        assert isolated.is_symlink()
        assert isolated.readlink() == fallback.resolve()
        assert attempts[:2] == [opt_candidate, usr_local_candidate]
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def test_remote_docker_host_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker.log"
    runtime = JobRuntime(
        adapter="compose",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(base, head, bin_dir, log, DOCKER_HOST="ssh://example.invalid"),
    )
    artifacts = runtime.artifacts
    try:
        with pytest.raises(JobError, match="local Docker Unix socket"):
            runtime.isolate_docker_config()
    finally:
        runtime.cleanup(1)
        shutil.rmtree(artifacts, ignore_errors=True)


def _companion_config(ref: str) -> dict[str, object]:
    return {
        "companion": {
            "directory_env": "EXAMPLE_SERVICES_DIR",
            "ref_env": "EXAMPLE_SERVICES_REF",
            "ref": ref,
            "repository": "example/services",
        },
        "files": ["stack/compose.yml"],
        "env_file": "stack/example.env",
        "up": ["{compose}", "up", "-d"],
        "builds": [
            {"argv": ["docker", "build", "-t", "{image:app}", "{repo}"], "image": "{image:app}"},
            {"argv": ["docker", "build", "-t", "{image:tests}", "{repo}"], "image": "{image:tests}"},
        ],
        "ports": [{"service": "gateway", "port": 8080, "env": "EXAMPLE_API_PORT"}],
        "test_service": "tests",
        "test_image": "{image:tests}",
        "artifacts": [{"source": "/work/results", "destination": "results"}],
        "env": {"EXAMPLE_COMPANION": "{companion}", "EXAMPLE_APP_IMAGE": "{image:app}"},
    }


@pytest.mark.parametrize(
    ("stderr", "stdout", "detail"),
    [
        ("compose plugin failed\n", "ignored stdout\n", "compose plugin failed"),
        (" \n", "compose fallback output\n", "compose fallback output"),
    ],
)
def test_compose_version_failure_reports_exit_code_and_captured_detail(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    stdout: str,
    detail: str,
) -> None:
    runtime = mock.Mock(spec=JobRuntime)
    runtime.env = {"PATH": "/example/bin"}
    runtime.run_argv.return_value = subprocess.CompletedProcess(
        ["/example/bin/docker", "compose", "version"],
        23,
        stdout,
        stderr,
    )
    monkeypatch.setattr(compose.shutil, "which", lambda *_args, **_kwargs: "/example/bin/docker")

    with mock.patch.object(compose, "_verify_companion") as verify_companion:
        with pytest.raises(JobError) as raised:
            compose._body(runtime, {})
        verify_companion.assert_not_called()

    assert str(raised.value) == f"docker compose is unavailable (exit code 23): {detail}"
    runtime.isolate_docker_config.assert_called_once_with()
    runtime.run_argv.assert_called_once_with(
        ["/example/bin/docker", "compose", "version"], check=False
    )


def _make_companion(path: Path) -> str:
    _repo(path, origin="https://github.com/example/services.git")
    (path / "stack").mkdir()
    (path / "stack" / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    (path / "stack" / "example.env").write_text("EXAMPLE_API_PORT=0\n", encoding="utf-8")
    (path / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    head = _commit(path, "add stack")
    (path / "ignored.tmp").write_text("must not enter snapshot\n", encoding="utf-8")
    return head


def test_companion_exact_origin_ref_clean_worktree_and_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    worktree = tmp_path / "services-worktree"
    _run(["git", "worktree", "add", "--detach", str(worktree), companion_head], source)
    runtime = JobRuntime(
        adapter="compose",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_env(
            base,
            head,
            EXAMPLE_SERVICES_DIR=str(worktree),
            EXAMPLE_SERVICES_REF=companion_head,
        ),
        allow_companion=True,
    )
    artifacts = runtime.artifacts
    try:
        verified, sha = compose._verify_companion(runtime, _companion_config(companion_head))
        assert verified == worktree.resolve()
        assert sha == companion_head
        snapshot = runtime.work / "snapshot"
        compose._archive_companion(runtime, verified, sha, snapshot)
        assert (snapshot / "stack" / "compose.yml").is_file()
        assert not (snapshot / "ignored.tmp").exists()
    finally:
        runtime.cleanup(0)
        shutil.rmtree(artifacts, ignore_errors=True)


def test_companion_rejects_dirty_wrong_origin_and_unresolved_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    common = CommonConfig()

    def runtime(**extra: str) -> JobRuntime:
        return JobRuntime(
            adapter="compose",
            common=common,
            cwd=repo,
            lock_root=tmp_path / "locks",
            environ=_env(base, head, EXAMPLE_SERVICES_DIR=str(source), **extra),
            allow_companion=True,
        )

    first = runtime(EXAMPLE_SERVICES_REF="does-not-exist")
    first_artifacts = first.artifacts
    try:
        with pytest.raises(JobError, match="git rev-parse"):
            compose._verify_companion(first, _companion_config(companion_head))
    finally:
        first.cleanup(1)
        shutil.rmtree(first_artifacts, ignore_errors=True)

    (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    second = runtime(EXAMPLE_SERVICES_REF=companion_head)
    second_artifacts = second.artifacts
    try:
        with pytest.raises(JobError, match="must be clean"):
            compose._verify_companion(second, _companion_config(companion_head))
    finally:
        second.cleanup(1)
        shutil.rmtree(second_artifacts, ignore_errors=True)

    (source / "untracked.txt").unlink()
    _run(["git", "remote", "set-url", "origin", "https://github.com/example/other.git"], source)
    third = runtime(EXAMPLE_SERVICES_REF=companion_head)
    third_artifacts = third.artifacts
    try:
        with pytest.raises(JobError, match="origin must be"):
            compose._verify_companion(third, _companion_config(companion_head))
    finally:
        third.cleanup(1)
        shutil.rmtree(third_artifacts, ignore_errors=True)


def test_snapshot_and_artifact_paths_reject_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(JobError, match="symlink"):
        safe_join(root, "link/result.txt")
    member = tarfile.TarInfo("stack/escape")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../outside"
    with pytest.raises(JobError, match="escapes snapshot"):
        compose._link_target(member)


@pytest.mark.parametrize("test_exit,expected", [(0, 0), (6, 6)])
def test_compose_copies_artifacts_before_down_on_success_and_failure(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    test_exit: int,
    expected: int,
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker.log"
    config = _companion_config(companion_head)
    status = compose.run_compose(
        json.dumps(config),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            FAKE_COMPOSE_TEST_UP_EXIT=str(test_exit),
            FAKE_TEST_STATE_EXIT=str(test_exit),
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == expected, captured.err
        calls = _docker_calls(log)
        cp_index = next(i for i, call in enumerate(calls) if call and call[0] == "cp")
        down_index = next(i for i, call in enumerate(calls) if "down" in call)
        assert cp_index < down_index
        down = calls[down_index]
        assert "--env-file" in down
        assert (artifacts / "results" / "result.txt").read_text(encoding="utf-8") == "ok"
        create_index = next(i for i, call in enumerate(calls) if "create" in call)
        create = calls[create_index]
        prefix = create[: create.index("create")]
        assert create[create.index("create") :] == [
            "create",
            "--no-build",
            "--no-recreate",
            "tests",
        ]
        ps_index = next(i for i, call in enumerate(calls) if "ps" in call)
        assert calls[ps_index] == prefix + [
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "tests",
        ]
        inspect_indexes = [
            i for i, call in enumerate(calls) if call == ["inspect", "c" * 64]
        ]
        selected_up_index = next(
            i for i, call in enumerate(calls) if "--exit-code-from" in call
        )
        assert calls[selected_up_index] == prefix + [
            "up",
            "--no-build",
            "--no-recreate",
            "--no-color",
            "--no-log-prefix",
            "--exit-code-from",
            "tests",
            "--attach",
            "tests",
            "tests",
        ]
        assert create_index < ps_index < inspect_indexes[0] < selected_up_index
        assert selected_up_index < inspect_indexes[1] < cp_index
        assert not any(call[:2] == ["start", "-a"] for call in calls)
        assert not any("--name" in call or "--no-start" in call for call in calls)
        volume_rm = next(i for i, call in enumerate(calls) if call[:2] == ["volume", "rm"])
        assert down_index < volume_rm
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


def _fake_compose_final_state(**updates: object) -> str:
    state: dict[str, object] = {
        "Status": "exited",
        "Running": False,
        "StartedAt": "2026-01-02T03:04:05.123456789Z",
        "FinishedAt": "2026-01-02T03:04:06.987654321Z",
        "ExitCode": 0,
    }
    state.update(updates)
    return json.dumps(state)


@pytest.mark.parametrize(
    ("case", "docker_env", "expected"),
    [
        (
            "created",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(Status="created")},
            1,
        ),
        (
            "never-started",
            {
                "FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(
                    StartedAt="0001-01-01T00:00:00Z"
                )
            },
            1,
        ),
        (
            "running",
            {
                "FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(
                    Status="running", Running=True
                )
            },
            1,
        ),
        ("malformed-inspect", {"FAKE_COMPOSE_FINAL_INSPECT_OUTPUT": "{"}, 1),
        ("missing-state", {"FAKE_COMPOSE_FINAL_STATE": "null"}, 1),
        ("state-wrong-type", {"FAKE_COMPOSE_FINAL_STATE": "[]"}, 1),
        (
            "running-wrong-type",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(Running=0)},
            1,
        ),
        (
            "exit-code-bool",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(ExitCode=True)},
            1,
        ),
        (
            "exit-code-string",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(ExitCode="0")},
            1,
        ),
        (
            "exit-code-out-of-range",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(ExitCode=256)},
            1,
        ),
        (
            "bad-started-at",
            {"FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(StartedAt="later")},
            1,
        ),
        (
            "zero-finished-at",
            {
                "FAKE_COMPOSE_FINAL_STATE": _fake_compose_final_state(
                    FinishedAt="0001-01-01T00:00:00.000000000Z"
                )
            },
            1,
        ),
        ("canonical-mismatch", {"FAKE_COMPOSE_FINAL_INSPECT_ID": "d" * 64}, 1),
        ("foreign-project", {"FAKE_COMPOSE_FINAL_INSPECT_PROJECT": "foreign"}, 1),
        ("foreign-service", {"FAKE_COMPOSE_FINAL_INSPECT_SERVICE": "foreign"}, 1),
        ("nonzero-final-exit", {"FAKE_TEST_STATE_EXIT": "7"}, 7),
    ],
)
def test_compose_validates_recorded_container_final_state_before_cleanup(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    case: str,
    docker_env: dict[str, str],
    expected: int,
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / f"docker-final-state-{case}.log"
    status = compose.run_compose(
        json.dumps(_companion_config(companion_head)),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            **docker_env,
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == expected, captured.err
        assert "a38: compose test service passed" not in captured.out
        calls = _docker_calls(log)
        selected_up_index = next(
            i for i, call in enumerate(calls) if "--exit-code-from" in call
        )
        final_inspect_index = [
            i for i, call in enumerate(calls) if call == ["inspect", "c" * 64]
        ][1]
        cp_index = next(i for i, call in enumerate(calls) if call and call[0] == "cp")
        rm_index = calls.index(["rm", "-f", "c" * 64])
        down_index = next(i for i, call in enumerate(calls) if "down" in call)
        assert selected_up_index < final_inspect_index < cp_index < rm_index < down_index
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


def test_compose_preserves_nonzero_primary_when_final_inspect_fails(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker-primary-final-inspect.log"
    status = compose.run_compose(
        json.dumps(_companion_config(companion_head)),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            FAKE_COMPOSE_TEST_UP_EXIT="35",
            FAKE_COMPOSE_FINAL_INSPECT_EXIT="36",
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == 35, captured.err
        assert "final Compose test container state (exit code 36)" in captured.err
        assert "a38: compose test service passed" not in captured.out
        calls = _docker_calls(log)
        selected_up_index = next(
            i for i, call in enumerate(calls) if "--exit-code-from" in call
        )
        final_inspect_index = [
            i for i, call in enumerate(calls) if call == ["inspect", "c" * 64]
        ][1]
        cp_index = next(i for i, call in enumerate(calls) if call and call[0] == "cp")
        assert selected_up_index < final_inspect_index < cp_index
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


@pytest.mark.parametrize(
    ("case", "docker_env"),
    [
        ("create-failure", {"FAKE_COMPOSE_CREATE_EXIT": "31"}),
        ("ps-failure", {"FAKE_COMPOSE_PS_EXIT": "32"}),
        ("empty", {"FAKE_COMPOSE_PS_OUTPUT": ""}),
        ("multiple", {"FAKE_COMPOSE_PS_OUTPUT": "c" * 64 + "\n" + "d" * 64 + "\n"}),
        ("truncated", {"FAKE_COMPOSE_PS_OUTPUT": "c" * 12 + "\n"}),
        ("name", {"FAKE_COMPOSE_PS_OUTPUT": "example-tests-1\n"}),
        ("malformed", {"FAKE_COMPOSE_PS_OUTPUT": "C" * 64 + "\n"}),
        ("inspect-failure", {"FAKE_COMPOSE_INSPECT_EXIT": "33"}),
        ("inspect-malformed", {"FAKE_COMPOSE_INSPECT_OUTPUT": "{"}),
        (
            "missing-labels",
            {
                "FAKE_COMPOSE_INSPECT_OUTPUT": json.dumps(
                    [{"Id": "c" * 64, "Config": {"Labels": {}}}]
                )
            },
        ),
        ("foreign-project", {"FAKE_COMPOSE_INSPECT_PROJECT": "foreign-project"}),
        ("foreign-service", {"FAKE_COMPOSE_INSPECT_SERVICE": "foreign-service"}),
        ("canonical-mismatch", {"FAKE_COMPOSE_INSPECT_ID": "d" * 64}),
    ],
)
def test_compose_rejects_unverified_container_without_adopting_it(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    case: str,
    docker_env: dict[str, str],
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / f"docker-{case}.log"
    status = compose.run_compose(
        json.dumps(_companion_config(companion_head)),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            **docker_env,
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == 1, captured.err
        calls = _docker_calls(log)
        create_index = next(i for i, call in enumerate(calls) if "create" in call)
        create = calls[create_index]
        prefix = create[: create.index("create")]
        assert create[create.index("create") :] == [
            "create",
            "--no-build",
            "--no-recreate",
            "tests",
        ]
        down_indexes = [i for i, call in enumerate(calls) if "down" in call]
        assert len(down_indexes) == 1
        assert calls[down_indexes[0]] == prefix + ["down", "-v", "--remove-orphans"]
        assert create_index < down_indexes[0]
        assert not any("--exit-code-from" in call for call in calls)
        assert not any(call and call[0] == "cp" for call in calls)
        assert not any(call[:2] == ["rm", "-f"] for call in calls)
        assert not any("--name" in call or "--no-start" in call for call in calls)
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


@pytest.mark.parametrize(
    ("failure_env", "expected"),
    [
        ({"FAKE_COMPOSE_MOUNTS_EXIT": "34"}, 1),
        (
            {"FAKE_COMPOSE_TEST_UP_EXIT": "35", "FAKE_TEST_STATE_EXIT": "35"},
            35,
        ),
    ],
)
def test_compose_retains_verified_id_for_later_failure_cleanup(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    failure_env: dict[str, str],
    expected: int,
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker-later-failure.log"
    status = compose.run_compose(
        json.dumps(_companion_config(companion_head)),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            **failure_env,
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    try:
        assert status == expected, captured.err
        calls = _docker_calls(log)
        ownership_inspect = calls.index(["inspect", "c" * 64])
        mount_inspect = next(
            i
            for i, call in enumerate(calls)
            if call[:2] == ["inspect", "--format"] and ".Mounts" in call[2]
        )
        cp_index = next(i for i, call in enumerate(calls) if call and call[0] == "cp")
        rm_index = calls.index(["rm", "-f", "c" * 64])
        down_index = next(i for i, call in enumerate(calls) if "down" in call)
        assert ownership_inspect < mount_inspect < cp_index < rm_index < down_index
        assert calls[cp_index][1].startswith("c" * 64 + ":")
        if "FAKE_COMPOSE_MOUNTS_EXIT" in failure_env:
            assert not any("--exit-code-from" in call for call in calls)
        else:
            selected_up_index = next(
                i for i, call in enumerate(calls) if "--exit-code-from" in call
            )
            final_inspect_index = [
                i for i, call in enumerate(calls) if call == ["inspect", "c" * 64]
            ][1]
            assert selected_up_index < final_inspect_index < cp_index
    finally:
        shutil.rmtree(artifacts, ignore_errors=True)


def test_compose_partial_build_cleans_only_successful_image(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    source = tmp_path / "services"
    companion_head = _make_companion(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir / "docker")
    log = tmp_path / "docker.log"
    config = _companion_config(companion_head)
    config["builds"] = [
        {"argv": ["docker", "build", "-t", "{image:one}", "{repo}"], "image": "{image:one}"},
        {"argv": ["docker", "build", "-t", "{image:two}", "{repo}"], "image": "{image:two}"},
    ]
    config.pop("test_image")
    status = compose.run_compose(
        json.dumps(config),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_docker_env(
            base,
            head,
            bin_dir,
            log,
            EXAMPLE_SERVICES_DIR=str(source),
            EXAMPLE_SERVICES_REF=companion_head,
            FAKE_FAIL_TAG="a38-two:",
        ),
    )
    captured = capfd.readouterr()
    artifacts = _artifacts_from(captured.out)
    shutil.rmtree(artifacts, ignore_errors=True)
    assert status == 1, captured.err
    removals = [call for call in _docker_calls(log) if call[:2] == ["image", "rm"]]
    assert len(removals) == 1 and removals[0][2].startswith("a38-one:")


class _SmokeHandler(BaseHTTPRequestHandler):
    username = "local-check"
    password = "example-password"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return self.headers.get("Authorization") == f"Basic {token}"

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")
            return
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/home")
            self.end_headers()
            return
        if self.path in {"/home", "/index.html", "/guide.html", "/manual.pdf"}:
            self.send_response(200)
            if self.path.endswith(".pdf"):
                self.send_header("Content-Disposition", "inline; filename=manual.pdf")
            self.end_headers()
            self.wfile.write(b"example")
            return
        self.send_response(404)
        self.end_headers()


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_authorized_redirect_cannot_leave_original_loopback_origin() -> None:
    received: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    target, target_thread = _serve(Target)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/foreign")
            self.end_headers()

    source, source_thread = _serve(Redirect)
    try:
        with pytest.raises(JobError, match="left the original"):
            http_smoke._http_request(
                f"http://127.0.0.1:{source.server_port}/",
                auth=("local-check", "example-password"),
                follow_redirects=True,
            )
        assert received == []
    finally:
        source.shutdown()
        target.shutdown()
        source_thread.join(timeout=3)
        target_thread.join(timeout=3)


def test_loopback_auth_requests_ignore_ambient_proxies() -> None:
    proxy_requests: list[str | None] = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            proxy_requests.append(self.headers.get("Authorization"))
            self.send_response(502)
            self.end_headers()

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"direct")

    proxy, proxy_thread = _serve(Proxy)
    target, target_thread = _serve(Target)
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    try:
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "NO_PROXY": "",
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "all_proxy": proxy_url,
                "no_proxy": "",
            },
            clear=False,
        ):
            code, body, _headers = http_smoke._http_request(
                f"http://127.0.0.1:{target.server_port}/",
                auth=("local-check", "example-password"),
            )
        assert code == 200 and body == b"direct"
        assert proxy_requests == []
    finally:
        proxy.shutdown()
        target.shutdown()
        proxy_thread.join(timeout=3)
        target_thread.join(timeout=3)


def test_http_smoke_full_auth_manifest_and_owned_ids(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    server, thread = _serve(_SmokeHandler)
    try:
        repo = tmp_path / "repo"
        base, head = _repo(repo)
        (repo / "Dockerfile.docs").write_text("FROM scratch\n", encoding="utf-8")
        head = _commit(repo, "add docs image")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_docker(bin_dir / "docker")
        log = tmp_path / "docker.log"
        manifest = {
            "artifacts": [
                {"category": "pages", "path": "guide.html"},
                {"category": "documents", "path": "manual.pdf"},
            ]
        }
        config = {
            "dockerfile": "Dockerfile.docs",
            "platform": "linux/amd64",
            "build_args": {"GIT_SHA": "{head}"},
            "container_port": 8080,
            "credentials": {
                "user_env": "EXAMPLE_DOCS_USER",
                "password_env": "EXAMPLE_DOCS_PASSWORD",
                "user": "local-check",
                "password": "example-password",
            },
            "health": {"path": "/health", "contains": "ready"},
            "root_path": "/",
            "manifest": {
                "path": "/srv/site/manifest.json",
                "artifacts_key": "artifacts",
                "category_key": "category",
                "path_key": "path",
                "index": "index.html",
                "pdf_category": "documents",
            },
        }
        status = http_smoke.run_http_smoke(
            json.dumps(config),
            cwd=repo,
            lock_root=tmp_path / "locks",
            environ=_docker_env(
                base,
                head,
                bin_dir,
                log,
                FAKE_DOCKER_PORT=str(server.server_port),
                FAKE_MANIFEST=json.dumps(manifest),
            ),
        )
        captured = capfd.readouterr()
        artifacts = _artifacts_from(captured.out)
        try:
            assert status == 0, captured.err
            assert "example-password" not in captured.out + captured.err
            calls = _docker_calls(log)
            creates = [call for call in calls if call and call[0] == "create"]
            starts = [call for call in calls if call and call[0] == "start"]
            removals = [call for call in calls if call[:2] == ["rm", "-f"]]
            assert len(creates) == 2
            assert starts[0][-1] == DOCKER_ID_A and starts[1][-1] == DOCKER_ID_B
            assert {call[-1] for call in removals} == {DOCKER_ID_A, DOCKER_ID_B}
            assert (artifacts / "http-smoke" / "manifest.json").is_file()
        finally:
            shutil.rmtree(artifacts, ignore_errors=True)
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_http_response_size_is_bounded() -> None:
    class Large(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 64)

    server, thread = _serve(Large)
    try:
        with mock.patch.object(http_smoke, "MAX_HTTP_BODY_BYTES", 32):
            with pytest.raises(JobError, match="exceeds"):
                http_smoke._http_request(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_immutable_comment_only_and_special_filenames(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "a38@example.invalid"], repo)
    _run(["git", "config", "user.name", "A38 Fixture"], repo)
    watched = repo / "schema" / "changes"
    watched.mkdir(parents=True)
    special = watched / "odd\tname\nwith:colon[1].sql"
    special.write_text("value\n// old note\n", encoding="utf-8")
    base = _commit(repo, "base")
    special.write_text("// new note\nvalue\n", encoding="utf-8")
    (watched / "new file.sql").write_text("new\n", encoding="utf-8")
    head = _commit(repo, "head")
    status = immutable.run_immutable(
        json.dumps({"path": "schema/changes", "exclude": [], "comment_prefix": "//"}),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_env(base, head),
    )
    artifacts = _artifacts_from(capsys.readouterr().out)
    shutil.rmtree(artifacts, ignore_errors=True)
    assert status == 0


def test_immutable_blocks_body_change_and_git_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    base, _head = _repo(repo)
    (repo / "README.md").write_text("changed body\n", encoding="utf-8")
    head = _commit(repo, "body change")
    config = {"path": "README.md", "comment_prefix": "//"}
    status = immutable.run_immutable(
        json.dumps(config),
        cwd=repo,
        lock_root=tmp_path / "locks",
        environ=_env(base, head),
    )
    first_artifacts = _artifacts_from(capsys.readouterr().out)
    shutil.rmtree(first_artifacts, ignore_errors=True)
    assert status == 1

    _common, parsed = immutable.parse_immutable_config(json.dumps(config))
    runtime = JobRuntime(
        adapter="immutable",
        common=CommonConfig(),
        cwd=repo,
        lock_root=tmp_path / "locks-two",
        environ=_env(base, head),
    )
    second_artifacts = runtime.artifacts
    monkeypatch.setattr(
        immutable,
        "_git_bytes",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, b"", b"synthetic git error"),
    )
    try:
        with pytest.raises(JobError, match="git diff failed"):
            immutable._body(runtime, parsed)
    finally:
        runtime.cleanup(1)
        shutil.rmtree(second_artifacts, ignore_errors=True)


def _wait_pid_gone(pid: int, timeout_s: float = 3) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _kill_fixture_group(ready: Path) -> None:
    if not ready.exists():
        return
    try:
        pids = [int(line) for line in ready.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return
    if pids:
        try:
            os.killpg(pids[0], signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in pids[1:]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _adapter_process(repo: Path, base: str, head: str, config: dict[str, object]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(_env(base, head))
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_cli.a38",
            "job",
            "commands",
            "--config",
            json.dumps(config),
        ],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_signal_terminates_active_child_and_grandchild_group(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    ready = tmp_path / "signal-ready"
    step = tmp_path / "active-step.py"
    step.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(f'{os.getpid()}\\n{child.pid}\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    process = _adapter_process(
        repo,
        base,
        head,
        {"steps": [[sys.executable, str(step), str(ready)]]},
    )
    stdout = ""
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 143, (stdout, stderr)
        assert time.monotonic() - started < 25
        step_pid, child_pid = [int(line) for line in ready.read_text().splitlines()]
        assert _wait_pid_gone(step_pid)
        assert _wait_pid_gone(child_pid)
    finally:
        _kill_fixture_group(ready)
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        if stdout:
            shutil.rmtree(_artifacts_from(stdout), ignore_errors=True)


def test_leader_exit_cleans_background_grandchild_before_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base, head = _repo(repo)
    ready = tmp_path / "orphan-ready"
    step = tmp_path / "leader-exit.py"
    step.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(f'{os.getpid()}\\n{child.pid}\\n')\n",
        encoding="utf-8",
    )
    process = _adapter_process(
        repo,
        base,
        head,
        {"steps": [[sys.executable, str(step), str(ready)]]},
    )
    stdout = ""
    try:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, (stdout, stderr)
        assert ready.exists()
        _leader_pid, child_pid = [int(line) for line in ready.read_text().splitlines()]
        assert _wait_pid_gone(child_pid), "background grandchild outlived successful adapter"
    finally:
        _kill_fixture_group(ready)
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        if stdout:
            shutil.rmtree(_artifacts_from(stdout), ignore_errors=True)
