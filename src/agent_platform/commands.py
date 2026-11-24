"""The building blocks of the command line (11-agent-access.md section 8.4): the parser whose errors and help are
exceptions, the context of a command, the wrapper that opens the paired account, and the tree that adds commands.

A command that writes into an org or stores a credential is registered with `writes=True`: it runs inside
`upkeep.around` (the owed rotations and the credential-key check before it, catch-up wrapping after it), which
`cli.build_parser` hands to the tree.

Command modules register themselves through a function `register(tree: Tree) -> None` listed in
`agent_platform.registry.MODULES`; `Tree.group` returns the same group for the same name, so a module may add commands
to a group another module created (for example `org leave` next to `org list`).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, TextIO

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError

MAX_STDIN = 1024 * 1024


class Help(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__("help")


class Parser(argparse.ArgumentParser):
    """Argument errors and help become exceptions (printed as one JSON object)."""

    def error(self, message: str) -> NoReturn:
        raise PlatformError("invalid_argument")

    def print_help(self, file: Any = None) -> None:
        raise Help(self.format_help())


Handler = Callable[[argparse.Namespace, "Context"], dict[str, Any]]
Work = Callable[[Account, argparse.Namespace, "Context"], dict[str, Any]]
Around = Callable[[Account, Callable[[], dict[str, Any]]], dict[str, Any]]
"""Runs a command on the opened account with what has to happen before and after it (`upkeep.around`)."""


class Context:
    """What a command needs besides its arguments."""

    def __init__(self, environ: Mapping[str, str], stdin: TextIO) -> None:
        self.environ = environ
        self.stdin = stdin
        self.home = Home.from_env(environ)

    def read_stdin(self) -> str:
        return self.stdin.read(MAX_STDIN)

    def account(self, args: argparse.Namespace) -> Account:
        root = getattr(args, "platform_root_key", None)
        if root is not None:
            try:
                b64u_decode(root, 32)
            except CryptoError:
                raise PlatformError("invalid_argument") from None
        return Account.open(self.home, root)


def with_account(work: Work, around: Around | None = None) -> Handler:
    def handler(args: argparse.Namespace, context: Context) -> dict[str, Any]:
        account = context.account(args)
        try:
            if around is None:
                return work(account, args, context)
            return around(account, lambda: work(account, args, context))
        finally:
            account.close()

    return handler


def text_argument(args: argparse.Namespace, context: Context, name: str = "text") -> str:
    """A text option whose value `-` means: read it from stdin."""
    value = str(getattr(args, name))
    return context.read_stdin() if value == "-" else value


def _root_option() -> Parser:
    common = Parser(add_help=False)
    common.add_argument("--platform-root-key", help="platform root key (b64u) instead of the one from the pairing")
    return common


class Tree:
    """Adds commands to the parser tree; every command gets the common options unless told otherwise."""

    def __init__(self, commands: Any, around: Around | None = None) -> None:
        self.commands = commands
        self.around = around
        self.common = _root_option()
        self._groups: dict[tuple[int, str], Any] = {}

    def add(self, group: Any, name: str, handler: Handler, help_text: str, common: bool = True) -> Parser:
        sub: Parser = group.add_parser(name, help=help_text, parents=[self.common] if common else [])
        sub.set_defaults(handler=handler)
        return sub

    def command(self, group: Any, name: str, work: Work, help_text: str, *, writes: bool = False) -> Parser:
        """A command that runs on the paired account; one that `writes` (into an org, or a credential) runs inside
        the tree's `around` (the browser's jobs before and after it, `upkeep`)."""
        return self.add(group, name, with_account(work, self.around if writes else None), help_text)

    def group(self, name: str, help_text: str, parent: Any = None) -> Any:
        """The group `name` under `parent` (the top level by default), created on first use."""
        holder = parent or self.commands
        key = (id(holder), name)
        if key not in self._groups:
            sub: Parser = holder.add_parser(name, help=help_text)
            self._groups[key] = sub.add_subparsers(dest=f"{name}_command", parser_class=Parser, required=True)
        return self._groups[key]
