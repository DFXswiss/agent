"""Compose companion harness from a pristine verified snapshot."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .common import (
    COMMON_KEYS,
    DOCKER_HEAVY_LOCK,
    REPO_NAME_RE,
    JobError,
    JobRuntime,
    CommonConfig,
    expand_argv,
    expand_placeholders,
    loads_strict_json,
    origin_matches_repository,
    parse_common_config,
    parse_docker_host_port,
    reject_unknown_keys,
    require_env_name,
    require_finite_number,
    require_mapping,
    require_argv,
    require_docker_id,
    require_rel_path,
    require_str,
    require_str_list,
    run_lifecycle,
    safe_join,
    validate_argv_placeholders,
    validate_placeholders,
)

COMPOSE_KEYS = COMMON_KEYS | frozenset(
    {
        "companion",
        "files",
        "env_file",
        "up",
        "builds",
        "ports",
        "test_service",
        "test_image",
        "artifacts",
    }
)
COMPANION_KEYS = frozenset({"directory_env", "ref_env", "ref", "repository"})
BUILD_KEYS = frozenset({"argv", "image"})
PORT_KEYS = frozenset({"service", "port", "env"})
ARTIFACT_KEYS = frozenset({"source", "destination"})


def parse_compose_config(text: str) -> tuple[CommonConfig, dict[str, Any]]:
    raw = loads_strict_json(text)
    obj = require_mapping(raw, "config")
    reject_unknown_keys(obj, COMPOSE_KEYS, "compose config")
    for key in ("companion", "files", "test_service", "artifacts"):
        if key not in obj:
            raise JobError(f"compose config requires {key}")

    companion = require_mapping(obj["companion"], "companion")
    reject_unknown_keys(companion, COMPANION_KEYS, "companion")
    missing = COMPANION_KEYS - set(companion)
    if missing:
        raise JobError(f"companion missing keys: {', '.join(sorted(missing))}")
    companion_cfg = {
        "directory_env": require_env_name(companion["directory_env"], "companion.directory_env"),
        "ref_env": require_env_name(companion["ref_env"], "companion.ref_env"),
        "ref": require_str(companion["ref"], "companion.ref"),
        "repository": require_str(companion["repository"], "companion.repository"),
    }
    if REPO_NAME_RE.fullmatch(companion_cfg["repository"]) is None:
        raise JobError("companion.repository must be owner/name")

    files = [require_rel_path(item, "files[]") for item in require_str_list(obj["files"], "files")]
    if not files:
        raise JobError("files must be non-empty")

    env_file = None
    if "env_file" in obj:
        env_file = require_rel_path(obj["env_file"], "env_file")

    up = None
    if "up" in obj:
        up = require_argv(obj["up"], "up")
        validate_argv_placeholders(
            up,
            label="up",
            allow_companion=True,
            allow_compose_token=True,
        )

    builds: list[dict[str, Any]] = []
    if "builds" in obj:
        if not isinstance(obj["builds"], list):
            raise JobError("builds must be an array")
        for index, item in enumerate(obj["builds"]):
            build = require_mapping(item, f"builds[{index}]")
            reject_unknown_keys(build, BUILD_KEYS, f"builds[{index}]")
            if "argv" not in build or "image" not in build:
                raise JobError(f"builds[{index}] requires argv and image")
            argv = require_argv(build["argv"], f"builds[{index}].argv")
            validate_argv_placeholders(
                argv,
                label=f"builds[{index}].argv",
                allow_companion=True,
                allow_compose_token=True,
            )
            image = require_str(build["image"], f"builds[{index}].image")
            validate_placeholders(
                image,
                label=f"builds[{index}].image",
                allow_companion=True,
            )
            builds.append(
                {"argv": argv, "image": image}
            )

    ports: list[dict[str, Any]] = []
    if "ports" in obj:
        if not isinstance(obj["ports"], list):
            raise JobError("ports must be an array")
        for index, item in enumerate(obj["ports"]):
            port = require_mapping(item, f"ports[{index}]")
            reject_unknown_keys(port, PORT_KEYS, f"ports[{index}]")
            missing_port = PORT_KEYS - set(port)
            if missing_port:
                raise JobError(f"ports[{index}] missing keys: {', '.join(sorted(missing_port))}")
            number = require_finite_number(port["port"], f"ports[{index}].port")
            if number != int(number) or int(number) <= 0 or int(number) > 65535:
                raise JobError(f"ports[{index}].port must be an integer 1..65535")
            ports.append(
                {
                    "service": require_str(port["service"], f"ports[{index}].service"),
                    "port": int(number),
                    "env": require_env_name(port["env"], f"ports[{index}].env"),
                }
            )

    test_service = require_str(obj["test_service"], "test_service")
    test_image = None
    if "test_image" in obj:
        test_image = require_str(obj["test_image"], "test_image")
        validate_placeholders(test_image, label="test_image", allow_companion=True)

    if not isinstance(obj["artifacts"], list):
        raise JobError("artifacts must be an array")
    artifacts_cfg: list[dict[str, str]] = []
    for index, item in enumerate(obj["artifacts"]):
        art = require_mapping(item, f"artifacts[{index}]")
        reject_unknown_keys(art, ARTIFACT_KEYS, f"artifacts[{index}]")
        if set(art) != ARTIFACT_KEYS:
            raise JobError(f"artifacts[{index}] requires source and destination")
        source = require_str(art["source"], f"artifacts[{index}].source")
        if (
            not source.startswith("/")
            or ".." in PurePosixPath(source).parts
            or "\\" in source
        ):
            raise JobError(
                f"artifacts[{index}].source must be a safe absolute container path"
            )
        dest = require_rel_path(art["destination"], f"artifacts[{index}].destination")
        artifacts_cfg.append({"source": source, "destination": dest})

    common = parse_common_config(obj, allow_companion=True)
    return common, {
        "companion": companion_cfg,
        "files": files,
        "env_file": env_file,
        "up": up,
        "builds": builds,
        "ports": ports,
        "test_service": test_service,
        "test_image": test_image,
        "artifacts": artifacts_cfg,
    }


def _compose_prefix(
    runtime: JobRuntime,
    *,
    companion: Path,
    files: list[str],
    env_file: str | None,
) -> list[str]:
    docker = shutil.which("docker", path=runtime.env.get("PATH"))
    if not docker:
        raise JobError("required command not found: docker")
    argv = [docker, "compose", "-p", runtime.project]
    if env_file:
        path = safe_join(companion, env_file)
        if path.is_file():
            argv.extend(["--env-file", str(path)])
    for rel in files:
        argv.extend(["-f", str(safe_join(companion, rel))])
    return argv


def _expand_compose_argv(runtime: JobRuntime, argv: list[str], prefix: list[str]) -> list[str]:
    if argv and argv[0] == "{compose}":
        rest = expand_argv(
            argv[1:],
            mapping=runtime.mapping,
            images=runtime.images,
            allow_compose_token=False,
        )
        return prefix + rest
    return expand_argv(
        argv,
        mapping=runtime.mapping,
        images=runtime.images,
        allow_compose_token=False,
    )


def _env_get(runtime: JobRuntime, name: str) -> str:
    """Read selectors from the runtime's explicitly supplied and scoped environment."""
    return runtime.env.get(name, "")


def _runtime_git(runtime: JobRuntime, args: list[str], *, cwd: Path) -> str:
    completed = runtime.run_argv(
        ["git", *args],
        cwd=cwd,
        timeout_s=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise JobError(f"git {' '.join(args)} failed: {detail or completed.returncode}")
    return completed.stdout


def _verify_companion(runtime: JobRuntime, cfg: Mapping[str, Any]) -> tuple[Path, str]:
    companion_cfg = cfg["companion"]
    directory = _env_get(runtime, companion_cfg["directory_env"])
    if not directory:
        raise JobError(f"required environment variable missing: {companion_cfg['directory_env']}")
    source = Path(directory).resolve()
    if not source.is_dir():
        raise JobError("companion directory is not a directory")
    top = _runtime_git(runtime, ["rev-parse", "--show-toplevel"], cwd=source).strip()
    if Path(top).resolve() != source:
        raise JobError("companion directory_env must be a repository/worktree root")
    origin = _runtime_git(runtime, ["remote", "get-url", "origin"], cwd=source).strip()
    if not origin_matches_repository(origin, companion_cfg["repository"]):
        raise JobError(f"companion origin must be {companion_cfg['repository']}; got {origin}")
    status = _runtime_git(
        runtime, ["status", "--porcelain", "--untracked-files=all"], cwd=source
    )
    if status.strip():
        raise JobError("companion checkout must be clean")
    expected_ref = _env_get(runtime, companion_cfg["ref_env"]) or companion_cfg["ref"]
    services_sha = _runtime_git(
        runtime, ["rev-parse", "--verify", "HEAD^{commit}"], cwd=source
    ).strip()
    tip = _runtime_git(
        runtime,
        ["rev-parse", "--verify", "--end-of-options", f"{expected_ref}^{{commit}}"],
        cwd=source,
    ).strip()
    if services_sha != tip:
        raise JobError(f"companion HEAD {services_sha} differs from {expected_ref} ({tip})")
    print(
        f"a38: companion {companion_cfg['repository']} commit={services_sha} ref={expected_ref}",
        flush=True,
    )
    return source, services_sha


def _safe_archive_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise JobError(f"companion archive contains unsafe path: {name}")
    return path


def _link_target(member: tarfile.TarInfo) -> PurePosixPath:
    target = PurePosixPath(member.linkname)
    if not member.linkname or "\x00" in member.linkname or target.is_absolute():
        raise JobError(f"companion archive contains unsafe link: {member.name}")
    combined = target if member.islnk() else PurePosixPath(member.name).parent / target
    normalized: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                raise JobError(f"companion archive link escapes snapshot: {member.name}")
            normalized.pop()
        else:
            normalized.append(part)
    if not normalized:
        raise JobError(f"companion archive link has invalid target: {member.name}")
    return PurePosixPath(*normalized)


def _archive_companion(runtime: JobRuntime, source: Path, sha: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = runtime.run_argv(
        ["git", "archive", sha],
        cwd=str(source),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise JobError(f"git archive of companion failed: {detail or proc.returncode}")
    with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as archive:
        members = archive.getmembers()
        names = {_safe_archive_name(member.name) for member in members}
        for member in members:
            _safe_archive_name(member.name)
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise JobError(f"companion archive contains unsupported entry: {member.name}")
            if member.issym() or member.islnk():
                target = _link_target(member)
                if member.islnk() and target not in names:
                    raise JobError(f"companion archive hard link target is missing: {member.name}")

        hardlinks: list[tuple[Path, Path]] = []
        for member in members:
            rel = _safe_archive_name(member.name)
            target = dest.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise JobError(f"cannot read companion archive entry: {member.name}")
                with source_file, target.open("wb") as output:
                    shutil.copyfileobj(source_file, output)
                target.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                link = _link_target(member)
                hardlinks.append((target, dest.joinpath(*link.parts)))
        for target, link in hardlinks:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_file():
                raise JobError("companion archive hard link target is not a regular file")
            target.hardlink_to(link)


def _body(runtime: JobRuntime, cfg: Mapping[str, Any]) -> int:
    runtime.isolate_docker_config()
    docker = shutil.which("docker", path=runtime.env.get("PATH"))
    if not docker:
        raise JobError("required command not found: docker")
    version = runtime.run_argv([docker, "compose", "version"], check=False)
    if version.returncode != 0:
        raise JobError("docker compose is unavailable")

    source, services_sha = _verify_companion(runtime, cfg)
    companion = runtime.work / "companion"
    _archive_companion(runtime, source, services_sha, companion)
    runtime.mapping["companion"] = str(companion)

    for rel in cfg["files"]:
        path = safe_join(companion, rel)
        if not path.is_file():
            raise JobError(f"companion snapshot lacks compose file: {rel}")

    state: dict[str, Any] = {
        "stack_started": False,
        "test_id": None,
        "test_volumes": [],
    }
    test_container = f"{runtime.project}-tests"

    def compose_argv(*extra: str) -> list[str]:
        return _compose_prefix(
            runtime,
            companion=companion,
            files=cfg["files"],
            env_file=cfg["env_file"],
        ) + list(extra)

    def cleanup(original: int) -> int:
        del original  # primary status is preserved by the runtime wrapper
        failed = 0
        if state["stack_started"]:
            if not runtime.interrupted:
                try:
                    log_path = safe_join(runtime.artifacts, "compose-stack.log")
                    with log_path.open("w", encoding="utf-8") as handle:
                        runtime.bounded(
                            20,
                            compose_argv("logs", "--no-color"),
                            stdout=handle,
                            stderr=subprocess.STDOUT,
                        )
                except (JobError, OSError) as exc:
                    print(f"a38: warning: could not capture Compose logs: {exc}", file=sys.stderr)
                cid = state["test_id"]
                if cid:
                    for art in cfg["artifacts"]:
                        try:
                            dest = safe_join(runtime.artifacts, art["destination"])
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            copy = runtime.bounded(
                                30,
                                [docker, "cp", f"{cid}:{art['source']}", str(dest)],
                            )
                            if copy.returncode != 0 or not dest.exists():
                                failed = 1
                        except (JobError, OSError):
                            failed = 1
                    try:
                        rm = runtime.bounded(20, [docker, "rm", "-f", cid])
                    except OSError as exc:
                        print(f"a38: warning: could not remove owned test container: {exc}", file=sys.stderr)
                    else:
                        if rm.returncode != 0:
                            print(
                                f"a38: warning: could not remove owned test container {cid}; "
                                "inspect it on this host",
                                file=sys.stderr,
                            )
            try:
                down = runtime.bounded(
                    30,
                    compose_argv("down", "-v", "--remove-orphans"),
                )
            except (JobError, OSError) as exc:
                print(
                    f"a38: warning: could not fully remove owned Compose project "
                    f"{runtime.project}: {exc}",
                    file=sys.stderr,
                )
            else:
                if down.returncode != 0:
                    print(
                        f"a38: warning: could not fully remove owned Compose project "
                        f"{runtime.project}; inspect it on this host",
                        file=sys.stderr,
                    )
        for volume in state["test_volumes"]:
            try:
                rm_volume = runtime.bounded(20, [docker, "volume", "rm", volume])
            except OSError as exc:
                print(f"a38: warning: could not remove owned test volume: {exc}", file=sys.stderr)
            else:
                if rm_volume.returncode != 0:
                    print(
                        f"a38: warning: could not remove owned test volume {volume}; "
                        "inspect it on this host",
                        file=sys.stderr,
                    )
        for image in runtime.owned_images:
            try:
                rm_img = runtime.bounded(20, [docker, "image", "rm", image])
            except OSError as exc:
                print(f"a38: warning: could not remove owned image: {exc}", file=sys.stderr)
            else:
                if rm_img.returncode != 0:
                    print(
                        f"a38: warning: could not remove owned image {image}; inspect it on this host",
                        file=sys.stderr,
                    )
        try:
            head_result = runtime.bounded(10, ["git", "rev-parse", "HEAD"], cwd=source)
            status_result = runtime.bounded(
                10,
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=source,
            )
            if (
                head_result.returncode != 0
                or status_result.returncode != 0
                or head_result.stdout.strip() != services_sha
                or status_result.stdout.strip()
            ):
                failed = 1
        except (JobError, OSError):
            failed = 1
        if failed:
            print(
                "a38: compose cleanup, artifact collection, or source integrity failed",
                file=sys.stderr,
            )
        return failed

    runtime.set_job_cleanup(cleanup)

    texts = list(runtime.common.env.values())
    for build in cfg["builds"]:
        texts.extend(build["argv"])
        texts.append(build["image"])
    if cfg["test_image"]:
        texts.append(cfg["test_image"])
    if cfg["up"]:
        texts.extend(cfg["up"])
    runtime.ensure_image_placeholders(texts)
    runtime.refresh_configured_env()

    prefix = _compose_prefix(
        runtime, companion=companion, files=cfg["files"], env_file=cfg["env_file"]
    )

    for build in cfg["builds"]:
        image_ref = expand_placeholders(
            build["image"], mapping=runtime.mapping, images=runtime.images
        )
        if runtime.run_id not in image_ref:
            raise JobError("build image must include {image:NAME} or {project} for unique ownership")
        argv = _expand_compose_argv(runtime, list(build["argv"]), prefix)
        completed = runtime.run_argv(argv, stdout=sys.stdout, stderr=sys.stderr, check=False)
        if completed.returncode != 0:
            raise JobError(f"image build failed for {image_ref}")
        runtime.track_image(image_ref)

    if cfg["test_image"]:
        expected_test_image = expand_placeholders(
            cfg["test_image"], mapping=runtime.mapping, images=runtime.images
        )
        if expected_test_image not in runtime.owned_images:
            raise JobError("test_image must identify an image built by this run")

    # Mark stack ownership before up so start failures still clean owned resources.
    state["stack_started"] = True
    if cfg["up"]:
        up_argv = _expand_compose_argv(runtime, list(cfg["up"]), prefix)
        up_run = runtime.run_argv(up_argv, stdout=sys.stdout, stderr=sys.stderr, check=False)
        if up_run.returncode != 0:
            raise JobError("compose up command failed")

    prefix = _compose_prefix(
        runtime, companion=companion, files=cfg["files"], env_file=cfg["env_file"]
    )

    for port in cfg["ports"]:
        completed = runtime.run_argv(
            prefix + ["port", port["service"], str(port["port"])],
            check=False,
        )
        if completed.returncode != 0:
            raise JobError(f"failed to resolve host port for {port['service']}:{port['port']}")
        host_port = parse_docker_host_port(
            completed.stdout, f"{port['service']}:{port['port']}"
        )
        runtime.env[port["env"]] = host_port
        if cfg["env_file"]:
            env_path = safe_join(companion, cfg["env_file"])
            env_path.parent.mkdir(parents=True, exist_ok=True)
            with env_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{port['env']}={host_port}\n")

    # The up helper or the port export above may have created the configured
    # env file, so resolve the exact Compose context again before the test run.
    prefix = _compose_prefix(
        runtime, companion=companion, files=cfg["files"], env_file=cfg["env_file"]
    )

    create_argv = prefix + [
        "run",
        "--no-start",
        "--name",
        test_container,
        cfg["test_service"],
    ]
    created = runtime.run_argv(
        create_argv, stdout=sys.stdout, stderr=sys.stderr, check=False
    )
    if created.returncode != 0:
        raise JobError("failed to create Compose test container")
    inspect = runtime.run_argv(
        [docker, "inspect", "--format", "{{.Id}}", test_container], check=False
    )
    if inspect.returncode != 0:
        raise JobError("failed to record Compose test container ownership")
    test_id = require_docker_id(inspect.stdout.strip(), "docker inspect")
    state["test_id"] = test_id
    mounts = runtime.run_argv(
        [
            docker,
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}',
            test_id,
        ],
        check=False,
    )
    if mounts.returncode != 0:
        raise JobError("failed to record Compose test volume ownership")
    for volume in [line.strip() for line in mounts.stdout.splitlines() if line.strip()]:
        label = runtime.run_argv(
            [
                docker,
                "volume",
                "inspect",
                "--format",
                '{{index .Labels "com.docker.compose.project"}}',
                volume,
            ],
            check=False,
        )
        if label.returncode == 0 and label.stdout.strip() == runtime.project:
            state["test_volumes"].append(volume)

    completed = runtime.run_argv(
        [docker, "start", "-a", test_id],
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode
    print("a38: compose test service passed", flush=True)
    return 0


def run_compose(
    config_text: str,
    *,
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    common, parsed = parse_compose_config(config_text)

    def body(runtime: JobRuntime) -> int:
        return _body(runtime, parsed)

    return run_lifecycle(
        adapter="compose",
        common=common,
        body=body,
        cwd=cwd,
        lock_root=lock_root,
        environ=environ,
        default_lock=DOCKER_HEAVY_LOCK,
        allow_companion=True,
    )
