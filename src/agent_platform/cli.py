"""The command line of `agent-platform` (11-agent-access.md sections 8.1 and 8.4).

Every command prints exactly one JSON object on stdout at its end (`{"ok": true, ...}` or
`{"ok": false, "error", "next", ...}`), exit code 0 or 1; progress goes to stderr without secrets. Argument errors
and `--help` are JSON as well. A top-level handler turns every unexpected failure into `{"ok": false, "error":
"internal"}` without a traceback. Secret inputs (an invitation link, an API key, a request body) come from stdin or
the environment, never from the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any, TextIO

from agent_platform import __version__, claude, diagnose, membership, orgs, registry, remote, tasks, upkeep
from agent_platform.account import Account
from agent_platform.commands import Context, Handler, Help, Parser, Tree, Work, text_argument, with_account
from agent_platform.credentials import put_record, read_record
from agent_platform.errors import PlatformError
from agent_platform.pairing import pair, register
from agent_runner.crypto.identity import account_code, format_account_code

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


# --- commands ---


def cmd_register(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    link = context.environ.get("AGENT_PLATFORM_INVITE", "") or context.read_stdin()
    if not link.strip():
        raise PlatformError("invalid_argument")
    if args.remote:
        result = remote.start_register(
            context.home,
            invite=link,
            display_name=args.display_name,
            name=args.name,
            host=args.host,
            replace=args.replace_pending,
        )
    elif args.replace_pending:
        raise PlatformError("invalid_argument")
    else:
        result = register(
            context.home, context.environ, invite=link, display_name=args.display_name, name=args.name, host=args.host
        )
    return {**result, "display_name": args.display_name.strip()}


def _with_display_name(result: dict[str, Any], context: Context) -> dict[str, Any]:
    account = Account.open(context.home)
    try:
        result["display_name"] = account.display_name()
    except PlatformError:
        result["display_name"] = None
    finally:
        account.close()
    return result


def cmd_pair(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.wait:
        if args.hub is not None or args.remote or args.replace_pending:
            raise PlatformError("invalid_argument")
        result = remote.wait(context.home, return_code=args.return_code, timeout=args.timeout)
        connected = result["state"] == "connected" and result["kind"] == "pair"
        return _with_display_name(result, context) if connected else result
    if args.hub is None or args.return_code is not None or args.timeout != remote.WAIT_DEFAULT_S:
        raise PlatformError("invalid_argument")
    if args.remote:
        if args.no_browser:
            raise PlatformError("invalid_argument")
        return remote.start_pair(
            context.home, hub=args.hub, name=args.name, host=args.host, replace=args.replace_pending
        )
    if args.replace_pending:
        raise PlatformError("invalid_argument")
    result = pair(
        context.home, context.environ, hub=args.hub, name=args.name, host=args.host, browser=not args.no_browser
    )
    return _with_display_name(result, context)


def cmd_status(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    me = account.me()
    credentials = []
    for item in account.call("GET", "/v1/me/credentials").get("credentials", []):
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in ("kind", "version", "epoch", "updated_at")}
        if item.get("kind") == claude.KIND:
            _, body = read_record(account, claude.KIND)
            row["token_kind"] = None if body is None else body.get("kind")
            row["expires_at"] = None if body is None else body.get("expires_at")
        credentials.append(row)
    devices = account.call("GET", "/v1/me/devices").get("devices", [])
    return {
        "hub": account.origin,
        "agent_id": account.agent_id,
        "user_id": account.user_id,
        "account_code": format_account_code(account_code(account.user_id, account.identity.public())),
        "display_name": account.display_name(),
        "settings": me.get("settings"),
        "orgs": orgs.org_list(account)["orgs"],
        "credentials": credentials,
        "devices": devices,
    }


def cmd_api(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    method = args.method.upper()
    if method not in METHODS or not args.path.startswith("/"):
        raise PlatformError("invalid_argument")
    body: object | None = None
    if args.body is not None:
        text = context.read_stdin() if args.body == "-" else args.body
        try:
            body = json.loads(text)
        except ValueError:
            raise PlatformError("invalid_argument") from None
    return {"answer": account.call(method, args.path, body)}


def cmd_guide(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return {"guide": resources.files("agent_platform").joinpath("guide.md").read_text(encoding="utf-8")}


def cmd_claude_connect(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return claude.connect(account, context.environ, replace=args.replace, only_changed=False)


def cmd_claude_sync(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return claude.connect(account, context.environ, replace=args.replace, only_changed=True)


def cmd_claude_watch(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.interval < 1 or (args.iterations is not None and args.iterations < 1):
        raise PlatformError("invalid_argument")
    return claude.watch(
        lambda: context.account(args),
        context.environ,
        interval=args.interval,
        iterations=args.iterations,
        upkeep=upkeep.sync,
    )


def cmd_service_install(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    context.account(args).close()
    return claude.install_service(context.environ, args.interval, not args.no_load)


def cmd_service_remove(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return claude.remove_service(context.environ, not args.no_load)


def cmd_setup_token(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return claude.setup_token(account, context.environ, context.home)


def cmd_settings_set(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    current = account.me().get("settings")
    settings = dict(current) if isinstance(current, dict) else {}
    body = {
        "model_access": args.model_access or settings.get("model_access") or "auto",
        "model_id": settings.get("model_id") if args.model is None else (args.model or None),
        "locale": args.locale or settings.get("locale") or "en",
    }
    return {"settings": account.call("PUT", "/v1/me/settings", body)}


def cmd_set_api_key(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    key = context.read_stdin().strip()
    if not key or not all("!" <= char <= "~" for char in key) or len(key) > 4096:
        raise PlatformError("invalid_argument")
    return {**put_record(account, "anthropic_api_key", {"key": key}), "kind": "anthropic_api_key"}


def cmd_org_list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_list(account)


def cmd_org_create(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_create(account, args.name, args.display_name)


def cmd_org_show(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_show(account, args.org)


def cmd_org_invite(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_invite(account, args.org, args.role, orgs.parse_wgs(args.wg), args.expires_hours)


def cmd_org_join(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    link = context.environ.get("AGENT_PLATFORM_INVITE", "") or context.read_stdin()
    if not link.strip():
        raise PlatformError("invalid_argument")
    return orgs.org_join(account, link, args.display_name)


def cmd_wg_list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.wg_list(account, args.org)


def cmd_wg_create(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.wg_create(account, args.org, args.name)


def cmd_org_admit(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_admit(account, args.org, args.user, args.role, orgs.parse_wgs(args.wg))


def cmd_org_invitees(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_invitees(account, args.org)


def cmd_wg_grant(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return membership.wg_grant(account, args.wg, args.user, args.access, args.history)


def cmd_device_assign(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.device_assign(account, args.wg)


def cmd_task_list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_list(account, args.wg, args.state)


def cmd_task_create(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_create(account, args.wg, text_argument(args, context))


def cmd_task_prompt(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_prompt(account, args.task, text_argument(args, context))


def cmd_task_show(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_show(account, args.task)


def cmd_task_follow(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_follow(account, args.task, args.after, args.wait)


def cmd_diagnose(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return diagnose.diagnose(account, args.task)


# --- parser ---


def writing(work: Work) -> Handler:
    """A command that writes into an org or stores a credential (and `status`): inside `upkeep.around`."""
    return with_account(work, upkeep.around)


def _account_commands(tree: Tree) -> None:
    sub = tree.add(tree.commands, "pair", cmd_pair, "connect this agent to the user's account", common=False)
    sub.add_argument("--hub")
    sub.add_argument("--name", default="Claude Code")
    sub.add_argument("--host", default=(socket.gethostname().split(".")[0] or "computer")[:64])
    sub.add_argument("--no-browser", action="store_true")
    sub.add_argument("--remote", action="store_true", help="the agent runs on another computer than the browser")
    sub.add_argument("--replace-pending", action="store_true", help="delete a pending remote pairing first")
    sub.add_argument("--wait", action="store_true", help="finish the pending remote pairing")
    sub.add_argument("--return-code", help="with --wait: the code the page showed the user")
    sub.add_argument("--timeout", type=int, default=remote.WAIT_DEFAULT_S, help="with --wait: seconds (0-600)")
    sub = tree.add(
        tree.commands, "register", cmd_register, "a new account through an invitation (link on stdin)", common=False
    )
    sub.add_argument("--display-name", required=True)
    sub.add_argument("--name", default="Claude Code")
    sub.add_argument("--host", default=(socket.gethostname().split(".")[0] or "computer")[:64])
    sub.add_argument("--remote", action="store_true", help="the agent runs on another computer than the browser")
    sub.add_argument("--replace-pending", action="store_true", help="delete a pending remote pairing first")
    tree.add(tree.commands, "status", writing(cmd_status), "the account as the agent sees it")
    sub = tree.add(tree.commands, "api", with_account(cmd_api), "a signed request to any route")
    sub.add_argument("method")
    sub.add_argument("path")
    sub.add_argument("--body", help="JSON body, or - to read it from stdin")
    tree.add(tree.commands, "guide", cmd_guide, "the guide for AI agents", common=False)
    settings = tree.group("settings", "user settings")
    sub = tree.add(settings, "set", with_account(cmd_settings_set), "PUT /v1/me/settings")
    sub.add_argument("--model-access", choices=["auto", "subscription", "api_key", "platform"])
    sub.add_argument("--model", help="model ID ('' clears it)")
    sub.add_argument("--locale", choices=["de", "en"])
    credentials = tree.group("credentials", "credential records")
    tree.add(credentials, "set-api-key", writing(cmd_set_api_key), "store an Anthropic API key (stdin)")
    sub = tree.add(tree.commands, "diagnose", with_account(cmd_diagnose), "why a task does not start")
    sub.add_argument("--task")


def _task_commands(tree: Tree) -> None:
    group = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.add(group, "list", with_account(cmd_task_list), "tasks of a workgroup with their titles")
    sub.add_argument("--wg", required=True)
    sub.add_argument("--state", choices=list(tasks.STATES))
    sub = tree.add(group, "create", writing(cmd_task_create), "a new task with its first prompt")
    sub.add_argument("--wg", required=True)
    sub.add_argument("--text", required=True, help="the prompt, or - to read it from stdin")
    sub = tree.add(group, "prompt", writing(cmd_task_prompt), "a further prompt to a task")
    sub.add_argument("--task", required=True)
    sub.add_argument("--text", required=True, help="the prompt, or - to read it from stdin")
    sub = tree.add(group, "show", with_account(cmd_task_show), "a task and its chat, opened and verified")
    sub.add_argument("--task", required=True)
    sub = tree.add(group, "follow", with_account(cmd_task_follow), "new chat events after a position (waits)")
    sub.add_argument("--task", required=True)
    sub.add_argument("--after", type=int, default=0, help="next_after of the previous call (0: from the start)")
    sub.add_argument("--wait", type=int, default=60, help="seconds to wait for the first new event (0-600)")


def _claude_commands(tree: Tree) -> None:
    group = tree.group("claude", "the Claude subscription")
    for name, handler in (("connect", cmd_claude_connect), ("sync", cmd_claude_sync)):
        sub = tree.add(group, name, writing(handler), f"claude {name}")
        sub.add_argument("--replace", action="store_true", help="also replace a setup-token record")
    sub = tree.add(group, "watch", cmd_claude_watch, "sync in a loop")
    sub.add_argument("--interval", type=int, default=claude.WATCH_INTERVAL_S)
    sub.add_argument("--iterations", type=int, help=argparse.SUPPRESS)
    service = tree.group("service", "the background sync service", group)
    sub = tree.add(service, "install", cmd_service_install, "install the user-level service")
    sub.add_argument("--interval", type=int, default=claude.WATCH_INTERVAL_S)
    sub.add_argument("--no-load", action="store_true")
    sub = tree.add(service, "remove", cmd_service_remove, "remove the user-level service", common=False)
    sub.add_argument("--no-load", action="store_true")
    tree.add(group, "setup-token", writing(cmd_setup_token), "run `claude setup-token` and store the token")


def _org_commands(tree: Tree) -> None:
    group = tree.group("org", "organizations")
    tree.add(group, "list", with_account(cmd_org_list), "organizations of the user")
    sub = tree.add(group, "create", writing(cmd_org_create), "create a team organization")
    sub.add_argument("--name", required=True)
    sub.add_argument("--display-name")
    sub = tree.add(group, "show", with_account(cmd_org_show), "members, workgroups, invitations and their state")
    sub.add_argument("--org", required=True)
    sub = tree.add(group, "invite", writing(cmd_org_invite), "invite a user (prints invite_link once)")
    sub.add_argument("--org", required=True)
    sub.add_argument("--role", choices=["admin", "member"], default="member")
    sub.add_argument("--wg", action="append", default=[], help="<wg_id>[:read|write], repeatable")
    sub.add_argument("--expires-hours", type=int, default=72)
    sub = tree.add(group, "admit", writing(cmd_org_admit), "admit an existing account directly (no step on its side)")
    sub.add_argument("--org", required=True)
    sub.add_argument("--user", required=True, help="user ID or account code (XXXX-XXXX-XXXX)")
    sub.add_argument("--role", choices=["admin", "member"], default="member")
    sub.add_argument("--wg", action="append", default=[], help="<wg_id>[:read|write], repeatable")
    sub = tree.add(
        group, "invitees", with_account(cmd_org_invitees), "accounts registered through your platform invitations"
    )
    sub.add_argument("--org", help="only those that can be admitted to this organization")
    sub = tree.add(group, "join", writing(cmd_org_join), "accept an org invitation (link on stdin)")
    sub.add_argument("--display-name")
    group = tree.group("wg", "workgroups")
    sub = tree.add(group, "list", with_account(cmd_wg_list), "workgroups of an organization")
    sub.add_argument("--org", required=True)
    sub = tree.add(group, "create", writing(cmd_wg_create), "create a workgroup")
    sub.add_argument("--org", required=True)
    sub.add_argument("--name", required=True)
    sub = tree.add(group, "grant", writing(cmd_wg_grant), "give a member access to a workgroup")
    sub.add_argument("--wg", required=True)
    sub.add_argument("--user", required=True)
    sub.add_argument("--access", choices=["read", "write"], default="write")
    sub.add_argument("--history", action="store_true")
    group = tree.group("device", "execution devices")
    sub = tree.add(group, "assign", writing(cmd_device_assign), "assign a platform device to a workgroup")
    sub.add_argument("--wg", required=True)


def build_parser() -> Parser:
    parser = Parser(prog="agent-platform", description="The agent tool of agent.dfx.swiss (one JSON object per run).")
    parser.add_argument("--version", action="store_true", help="print the version")
    tree = Tree(parser.add_subparsers(dest="command", parser_class=Parser), upkeep.around)
    _account_commands(tree)
    _claude_commands(tree)
    _org_commands(tree)
    _task_commands(tree)
    for module in registry.MODULES:
        module(tree)
    return parser


# --- running ---


def emit(stream: TextIO, document: Mapping[str, Any]) -> None:
    stream.write(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()


def execute(argv: Sequence[str], environ: Mapping[str, str], stdin: TextIO) -> dict[str, Any]:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv))
    except Help as shown:
        return {"ok": True, "help": shown.text}
    if args.version:
        return {"ok": True, "version": __version__}
    handler = getattr(args, "handler", None)
    if handler is None:
        return {"ok": True, "help": parser.format_help()}
    return {"ok": True, **handler(args, Context(environ, stdin))}


def run(
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run one command; prints one JSON object and returns the exit code."""
    out = sys.stdout if stdout is None else stdout
    try:
        document = execute(argv, os.environ if environ is None else environ, sys.stdin if stdin is None else stdin)
    except PlatformError as error:
        document = error.to_json()
    except KeyboardInterrupt:
        document = PlatformError("internal", reason="interrupted").to_json()
    except Exception:
        document = PlatformError("internal").to_json()
    emit(out, document)
    return 0 if document["ok"] else 1
