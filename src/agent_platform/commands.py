"""The building blocks of the command line (11-agent-access.md section 8.4): the parser whose errors and help are
exceptions, the context of a command, the wrapper that opens the paired account (an agent that still holds its
user's identity converts itself first when the hub asks for it, `convert.ensure`, ADR-082 section 5), and the tree
that adds commands.

A command that writes into an org or stores a credential is registered with `writes=True`: it runs inside
`upkeep.around` (the owed rotations and the credential-key check before it, catch-up wrapping after it), which
`cli.build_parser` hands to the tree.

Command modules register themselves through a function `register(tree: Tree) -> None` listed in
`agent_platform.registry.MODULES`; `Tree.group` returns the same group for the same name, so a module may add commands
to a group another module created (for example `org leave` next to `org list`).
"""

from __future__ import annotations

import argparse
import difflib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, TextIO

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError

MAX_STDIN = 1024 * 1024


@dataclass(frozen=True)
class Doc:
    """What `agent-platform search` and `--help` say about a command (KOR-63): the one source of the lookup.

    `summary` is the purpose in one sentence, `example` one complete call, `words` further search words (German and
    English, the words an agent or its user would use), `then` the usual next step, `trim` how `--limit` cuts the
    result (`first` items, or `last` items and text lines), None for a command whose output does not grow."""

    summary: str
    example: str
    words: str
    then: str = ""
    trim: str | None = None


class Help(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__("help")


class Parser(argparse.ArgumentParser):
    """Argument errors and help become exceptions (printed as one JSON object)."""

    doc: Doc | None = None

    def error(self, message: str) -> NoReturn:
        """`invalid_argument` with the usage of this (sub)command and, for a mistyped command, the closest names
        (both from the tree, never free text)."""
        usage = " ".join(self.format_usage().split()).removeprefix("usage: ")
        wrong = re.search(r"invalid choice: '([^']*)'", message)
        names = [
            name
            for action in self._actions
            if isinstance(action, argparse._SubParsersAction)
            for name in action.choices
        ]
        close = difflib.get_close_matches(wrong.group(1), names, n=3, cutoff=0.5) if wrong else []
        raise PlatformError("invalid_argument", usage=usage, **({"did_you_mean": close} if close else {}))

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
        from agent_platform import convert  # noqa: PLC0415 - the conversion builds on the command modules

        return convert.ensure(Account.open(self.home, root))


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

    def add(
        self,
        group: Any,
        name: str,
        handler: Handler,
        help_text: str,
        common: bool = True,
        *,
        example: str,
        words: str,
        then: str = "",
        trim: str | None = None,
    ) -> Parser:
        """A command with its `Doc`; a command with `trim` gets `--limit` and `--fields` (applied by `cli`)."""
        doc = Doc(help_text, example, words, then, trim)
        epilog = "\n".join(f"{label}: {text}" for label, text in (("example", example), ("next", then)) if text)
        sub: Parser = group.add_parser(
            name,
            help=help_text,
            description=help_text,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[self.common] if common else [],
        )
        sub.doc = doc
        sub.set_defaults(handler=handler, trim=trim)
        if trim is not None:
            sub.add_argument("--limit", type=int, help="at most N items (the newest for a chat or terminal)")
            sub.add_argument("--fields", help="only these keys of each item, comma-separated (for example id,state)")
        return sub

    def command(
        self,
        group: Any,
        name: str,
        work: Work,
        help_text: str,
        *,
        writes: bool = False,
        example: str,
        words: str,
        then: str = "",
        trim: str | None = None,
    ) -> Parser:
        """A command that runs on the paired account; one that `writes` (into an org, or a credential) runs inside
        the tree's `around` (the browser's jobs before and after it, `upkeep`)."""
        handler = with_account(work, self.around if writes else None)
        return self.add(group, name, handler, help_text, example=example, words=words, then=then, trim=trim)

    def group(self, name: str, help_text: str, parent: Any = None) -> Any:
        """The group `name` under `parent` (the top level by default), created on first use."""
        holder = parent or self.commands
        key = (id(holder), name)
        if key not in self._groups:
            sub: Parser = holder.add_parser(name, help=help_text)
            self._groups[key] = sub.add_subparsers(dest=f"{name}_command", parser_class=Parser, required=True)
        return self._groups[key]


def trimmed(result: dict[str, Any], limit: int | None, fields: str | None, trim: str) -> dict[str, Any]:
    """`--limit N` and `--fields a,b` on a result (KOR-63): every list of the result keeps at most N items (the
    first, or with `trim="last"` the last ones, whose multi-line texts also keep their last N lines) and every
    object in those lists keeps only the named keys. A cut list is named with its full length under `limited`;
    `next_after` then points behind the last item kept, so the next call goes on from there. Without the options
    the result is unchanged. A page cut this way has no `next_before`: the hub's cursor points behind the whole
    page, so following it would skip the items left out (page on without `--limit`)."""
    if limit is not None and limit < 1:
        raise PlatformError("invalid_argument")
    names = [name for name in (fields or "").split(",") if name.strip()]
    keys = [name.strip() for name in names]
    if fields is not None and not keys:
        raise PlatformError("invalid_argument")
    out: dict[str, Any] = {}
    limited: dict[str, int] = {}
    for key, value in result.items():
        kept = value
        if isinstance(value, list):
            if limit is not None and len(value) > limit:
                limited[key] = len(value)
                kept = value[-limit:] if trim == "last" else value[:limit]
                seq = kept[-1].get("seq") if isinstance(kept[-1], dict) else None
                if trim != "last" and isinstance(seq, int) and isinstance(result.get("next_after"), int):
                    out["next_after"] = seq
                if "next_before" in result:
                    out["next_before"] = None
            if keys:
                kept = [{k: item[k] for k in keys if k in item} if isinstance(item, dict) else item for item in kept]
        elif isinstance(value, str) and limit is not None and trim == "last":
            lines = value.splitlines(keepends=True)
            if len(lines) > limit:
                limited[key] = len(lines)
                kept = "".join(lines[-limit:])
        out.setdefault(key, kept)
    return {**out, "limited": limited} if limited else out
