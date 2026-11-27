"""`agent-runner image build|list|retire` (docs/platform/spec/05-runner.md sections 1.2 and 3.1).

| Command | Effect |
|---|---|
| `image build [--claude-version V] [--tag TAG] [--test-model-port PORT]` | build a golden (`golden.Builder.build`) |
| `image list` | one fact per registered golden, with the state of its Lima instance |
| `image retire VERSION` | delete a non-current golden no VM references |
| `image adopt LIMA_NAME` | register a stopped shared golden cache `cache-golden-<sha12>` as the ready golden |
| `pool status` | pool VMs, refill state and capacity (`cli_pool.py`, VM-02) |

`--tag` replaces `<claude>-<guest>-<yyyymmdd>` in the instance name, so `AGENT_RUNNER_VM_PREFIX=cache-` and
`--tag <sha12>` build the shared cache `cache-golden-<sha12>` of `tools/wp/golden-cache.sh` (ADR-027 section 7).
`adopt` (TEST_MODE only) lets a test runner (E2E, ADR-027 section 7) start its pool from the shared cache of the same
tree instead of building (`golden.Builder.adopt`); the cache itself is never changed or deleted by the runner.
`--test-model-port` (TEST_MODE only) builds a golden whose sessions may reach the model double on that port of
the Lima host (images/README.md).

The commands need no hub configuration. They use the runner's own store: a cluster that is not running is
started for the command and stopped afterwards; a running one (the daemon's) is used and left running.
`limactl` is run with the command's environment (`PATH`) and per-operation time limits (`lima.run_limactl`). The
stderr tail of a failed Lima step is appended to `$AGENT_RUNNER_HOME/log/image.log` (0600).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

from agent_cli import pg
from agent_runner.config import Config
from agent_runner.errors import RunnerError
from agent_runner.identity import ensure_private_dir
from agent_runner.store.cluster import start_store, stop_store, store_exists
from agent_runner.store.migrate import migrate
from agent_runner.vm import capacity, cli_pool, golden, lima

EXIT_OK = 0
IMAGE_LOG = "image.log"
MAX_PORT = 65535


def _port(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_PORT:
        raise argparse.ArgumentTypeError("must be a TCP port")
    return int(value)


def register(subparsers: Any) -> None:
    image = subparsers.add_parser("image", help="build, list and retire golden images")
    actions = image.add_subparsers(dest="image_command", metavar="ACTION", required=True)
    build = actions.add_parser("build", help="build a golden image from images/ (05 section 3.1)")
    build.add_argument("--claude-version", help="expected Claude Code version (must equal the pin)")
    build.add_argument("--tag", help="name suffix instead of <claude>-<guest>-<yyyymmdd>")
    build.add_argument("--test-model-port", type=_port, help="TEST_MODE golden: model double port on the host")
    build.set_defaults(handler=cmd_build)
    listing = actions.add_parser("list", help="list the registered golden images")
    listing.set_defaults(handler=cmd_list)
    retire = actions.add_parser("retire", help="delete a golden image no VM uses")
    retire.add_argument("version")
    retire.set_defaults(handler=cmd_retire)
    adopt = actions.add_parser("adopt", help="register a stopped shared golden cache as the ready golden")
    adopt.add_argument("lima_name")
    adopt.set_defaults(handler=cmd_adopt)
    cli_pool.register(subparsers)


def make_lima(config: Config, env: Any) -> lima.Lima:
    return lima.Lima(functools.partial(lima.run_limactl, env=dict(env)), prefix=config.vm_prefix)


@contextlib.contextmanager
def store(config: Config, ctx: Any, *, running: Callable[[Path], bool] | None = None) -> Iterator[str]:
    """DSN of the runner store (migrated); started and stopped afterwards only when it does not run yet."""
    probe = running or pg.cluster_running
    try:
        was_running = probe(config.pg_dir)
    except (pg.PgError, OSError):
        raise RunnerError("store_unavailable") from None
    dsn = start_store(config.pg_dir, ctx.host.cluster)
    try:
        result = migrate(dsn)
        if result.status == "failed":
            raise RunnerError("migration_failed", result.step)
        yield dsn
    finally:
        if not was_running:
            stop_store(config.pg_dir, ctx.host.cluster)


@contextlib.contextmanager
def existing_store(config: Config, ctx: Any) -> Iterator[str | None]:
    """Like `store`, but None (nothing created) when the runner has no store yet."""
    if not store_exists(config.pg_dir, ctx.host.cluster):
        yield None
        return
    with store(config, ctx) as dsn:
        yield dsn


def _builder(config: Config, ctx: Any, dsn: str, scratch: Path) -> golden.Builder:
    def announce(name: str, fields: dict[str, str | int | bool]) -> None:
        ctx.out.fact(name, fields)

    def log(text: str) -> None:
        ensure_private_dir(config.log_dir)
        path = config.log_dir / IMAGE_LOG
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)

    return golden.Builder(
        lima=make_lima(config, ctx.host.env),
        dsn=dsn,
        records=config.home / "golden",
        scratch=scratch,
        announce=announce,
        log=log,
    )


def _prepare(config: Config) -> None:
    ensure_private_dir(config.home)
    ensure_private_dir(config.tmp_dir)


def cmd_build(args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    if args.test_model_port is not None and not config.test_mode:
        raise RunnerError("test_mode_required", "test-model-port")
    source = golden.load_source(config.golden_template)
    _prepare(config)
    with store(config, ctx) as dsn, tempfile.TemporaryDirectory(dir=config.tmp_dir, prefix="image-") as scratch:
        builder = _builder(config, ctx, dsn, Path(scratch))
        cpus, memory_mib = capacity.vm_size(config, cores=os.cpu_count(), physical_mib=capacity.physical_memory_mib())
        result = builder.build(
            source,
            golden.BuildOptions(
                cpus=cpus,
                memory_mib=memory_mib,
                disk_gib=config.vm_disk_gib,
                claude_version=args.claude_version,
                tag=args.tag,
                test_model_port=args.test_model_port,
            ),
        )
    info = result.build_info
    ctx.out.fact(
        "image",
        {
            "action": "build",
            "version": result.golden.version,
            "lima_name": result.golden.lima_name,
            "state": result.golden.state,
            "claude_version": result.golden.claude_version,
            "guest_version": result.golden.guest_version,
            "landlock_abi": info["landlock_abi"],
            "tui_start_ms": info["tui_start_ms"],
            "seconds": f"{result.seconds:.3f}",
            "record": str(result.record),
        },
    )
    return EXIT_OK


def cmd_list(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    with existing_store(config, ctx) as dsn:
        if dsn is None:
            rows: Sequence[golden.Golden] = []
        else:
            with golden.connect(dsn) as conn:
                rows = golden.list_goldens(conn)
    instances = {instance.name: instance.status for instance in make_lima(config, ctx.host.env).list()} if rows else {}
    for row in rows:
        ctx.out.fact(
            "image",
            {
                "version": row.version,
                "lima_name": row.lima_name,
                "state": row.state,
                "claude_version": row.claude_version,
                "guest_version": row.guest_version,
                "built_at": "" if row.built_at is None else row.built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "instance": instances.get(row.lima_name, "missing"),
            },
        )
    ctx.out.fact("images", {"count": len(rows)})
    return EXIT_OK


def cmd_retire(args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    with existing_store(config, ctx) as dsn:
        if dsn is None:
            raise RunnerError("golden_unknown")
        _prepare(config)
        retired = _builder(config, ctx, dsn, config.tmp_dir).retire(args.version)
    ctx.out.fact("image", {"action": "retire", "version": retired.version, "lima_name": retired.lima_name})
    return EXIT_OK


def cmd_adopt(args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    if not config.test_mode:
        raise RunnerError("test_mode_required", "adopt")
    source = golden.load_source(config.golden_template)
    _prepare(config)
    with store(config, ctx) as dsn:
        adopted = _builder(config, ctx, dsn, config.tmp_dir).adopt(source, args.lima_name)
    ctx.out.fact("image", {"action": "adopt", "version": adopted.version, "lima_name": adopted.lima_name})
    return EXIT_OK
