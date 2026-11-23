"""Command line `agent-runner` (docs/platform/spec/05-runner.md section 1.2, ADR-027 section 4).

Core commands: `run`, `service install|uninstall`, `uninstall [--keep-identity]`, `--version`. Further commands
are discovered from `agent_runner/<area>/cli.py:register(subparsers)`; a registered parser sets
`handler=<callable(args, context) -> int>` with `set_defaults`.

Output: one line per fact `runner.<fact> key=value ...` on stdout; with `--json` (anywhere before `--`) a single
JSON document `{"ok": ..., "facts": [...]}`. Errors: `runner.error code=<code> [name=<NAME>]` on stderr (JSON:
in the document). Exit codes: 0 ok, 1 failure, 2 usage. Values printed are codes, counts, names and paths of the
runner itself; secrets, tokens and decrypted content are never part of a fact.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from agent_runner import __version__, lifecycle, registry, service
from agent_runner.config import HUB_NAMES, Config, load_config
from agent_runner.errors import RunnerError
from agent_runner.host import Host
from agent_runner.identity import Identity, load_identity
from agent_runner.login import profile as login_profile

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
FactValue = str | int | bool
_FACT_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class Output:
    """Collects facts and prints them as lines or, with `json_mode`, as one JSON document at the end."""

    host: Host
    json_mode: bool
    facts: list[dict[str, FactValue]] = field(default_factory=list)

    def fact(self, name: str, fields: dict[str, FactValue]) -> None:
        if not _FACT_RE.fullmatch(name) or not all(_KEY_RE.fullmatch(key) for key in fields):
            raise ValueError("fact names and keys are lower-case identifiers")
        record: dict[str, FactValue] = {"fact": f"runner.{name}", **fields}
        self.facts.append(record)
        if not self.json_mode:
            text = " ".join(f"{key}={_text(value)}" for key, value in fields.items())
            self.host.stdout.write(f"runner.{name}{' ' if text else ''}{text}\n")
            self.host.stdout.flush()

    def error(self, exc: RunnerError) -> None:
        if self.json_mode:
            return
        name = "" if exc.name is None else f" name={exc.name}"
        self.host.stderr.write(f"runner.error code={exc.code}{name}\n")
        self.host.stderr.flush()

    def finish(self, code: int, error: RunnerError | None = None) -> int:
        if self.json_mode:
            document: dict[str, object] = {"ok": code == EXIT_OK, "facts": self.facts}
            if error is not None:
                document["error"] = {"code": error.code, "name": error.name}
            self.host.stdout.write(json.dumps(document, sort_keys=True) + "\n")
            self.host.stdout.flush()
        return code


def _text(value: FactValue) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    if not text or any(ch.isspace() or ch in '"\\' for ch in text):
        return json.dumps(text)
    return text


@dataclass
class CommandContext:
    """What a command handler gets besides its parsed arguments."""

    host: Host
    out: Output

    def config(self, *, require: frozenset[str] = HUB_NAMES, tolerate_identity: bool = False) -> Config:
        """Load the configuration; the device kind comes from the identity in the configured home when present.

        With `tolerate_identity` (removal commands) a broken identity counts as "not enrolled", so a half-written
        or damaged identity can still be removed.
        """
        first = load_config(self.host.env, platform=self.host.platform, require=frozenset())
        try:
            identity = load_identity(first.home)
        except RunnerError:
            if not tolerate_identity:
                raise
            identity = None
        kind = identity.device.kind if identity is not None else None
        return load_config(self.host.env, platform=self.host.platform, device_kind=kind, require=require)

    def identity(self, config: Config) -> Identity | None:
        return load_identity(config.home)


Handler = Callable[[argparse.Namespace, CommandContext], int]


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors raise instead of exiting, so `main` controls output and exit code."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(self.format_usage(), message)


class _UsageError(Exception):
    def __init__(self, usage: str, message: str) -> None:
        super().__init__(message)
        self.usage = usage
        self.message = message


def build_parser(package: str = registry.ROOT_PACKAGE) -> argparse.ArgumentParser:
    parser = _Parser(prog="agent-runner", description="agent.dfx.swiss platform runner (host daemon)")
    parser.add_argument("--version", action="version", version=f"agent-runner {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    run = commands.add_parser("run", help="run the daemon in the foreground (launchd target)")
    run.set_defaults(handler=_cmd_run)

    svc = commands.add_parser("service", help="install or remove the LaunchDaemon")
    svc_commands = svc.add_subparsers(dest="service_command", metavar="ACTION", required=True, parser_class=_Parser)
    svc_install = svc_commands.add_parser("install", help="write the plist and load the daemon (root)")
    svc_install.add_argument("--user", help="unprivileged account that runs the daemon (default: SUDO_USER)")
    svc_install.add_argument(
        "--profile",
        choices=login_profile.PROFILES,
        help="platform: install the login extra and enable the mailbox automation; org: disable it",
    )
    svc_install.set_defaults(handler=_cmd_service_install)
    svc_uninstall = svc_commands.add_parser("uninstall", help="unload the daemon and remove the plist (root)")
    svc_uninstall.set_defaults(handler=_cmd_service_uninstall)

    remove = commands.add_parser("uninstall", help="remove service, VMs, store and (without the flag) identity")
    remove.add_argument("--keep-identity", action="store_true", help="keep device.json and keys/device.key")
    remove.set_defaults(handler=_cmd_uninstall)

    for _area, register in registry.cli_registrars(package):
        register(commands)
    return parser


def _cmd_run(_args: argparse.Namespace, ctx: CommandContext) -> int:
    config = ctx.config()
    identity = ctx.identity(config)

    def announce(name: str, fields: dict[str, str | int]) -> None:
        ctx.out.fact(name, dict(fields))

    return lifecycle.run_daemon(config, identity, ctx.host, announce=announce)


def _cmd_service_install(args: argparse.Namespace, ctx: CommandContext) -> int:
    config = login_profile.apply(ctx.config(), args.profile, ctx.host, user=args.user)
    path = service.install(config, ctx.host, user=args.user)
    ctx.out.fact("service", {"action": "install", "label": service.LABEL, "plist": str(path)})
    return EXIT_OK


def _cmd_service_uninstall(_args: argparse.Namespace, ctx: CommandContext) -> int:
    config = ctx.config(require=frozenset(), tolerate_identity=True)
    removed = service.uninstall(config, ctx.host)
    ctx.out.fact("service", {"action": "uninstall", "label": service.LABEL, "removed": removed})
    return EXIT_OK


def _cmd_uninstall(args: argparse.Namespace, ctx: CommandContext) -> int:
    config = ctx.config(require=frozenset(), tolerate_identity=True)
    facts = lifecycle.uninstall_runner(config, ctx.host, keep_identity=args.keep_identity)
    ctx.out.fact("uninstall", dict(facts))
    return EXIT_OK


def _split_json(argv: Sequence[str]) -> tuple[list[str], bool]:
    """Remove `--json` (before a `--` separator) from argv; returns the rest and whether it was present."""
    rest: list[str] = []
    json_mode = False
    for index, arg in enumerate(argv):
        if arg == "--":
            rest.extend(argv[index:])
            break
        if arg == "--json":
            json_mode = True
        else:
            rest.append(arg)
    return rest, json_mode


def run_cli(argv: Sequence[str], host: Host) -> int:
    """Parse and run one command; returns the exit code (`--version` and `--help` exit through SystemExit 0)."""
    rest, json_mode = _split_json(argv)
    out = Output(host=host, json_mode=json_mode)
    try:
        parser = build_parser(host.package)
        try:
            args = parser.parse_args(rest)
        except _UsageError as usage:
            host.stderr.write(f"{usage.usage}agent-runner: error: {usage.message}\n")
            return EXIT_USAGE
        handler: Handler | None = getattr(args, "handler", None)
        if handler is None:
            parser.print_help(host.stdout)
            return EXIT_OK
        return out.finish(handler(args, CommandContext(host=host, out=out)))
    except RunnerError as exc:
        out.error(exc)
        return out.finish(EXIT_FAILURE, exc)
    except Exception as exc:
        # Unexpected failures print only the exception type: messages may carry secrets or server text.
        internal = RunnerError("internal", type(exc).__name__)
        out.error(internal)
        return out.finish(EXIT_FAILURE, internal)
