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
from typing import Any, TextIO

from agent_platform import (
    __version__,
    alone,
    attention,
    claude,
    diagnose,
    lookup,
    membership,
    modelaccess,
    names,
    orgs,
    registry,
    remote,
    tasks,
    upkeep,
)
from agent_platform.account import Account
from agent_platform.commands import (
    Context,
    Handler,
    Help,
    Parser,
    Tree,
    Work,
    text_argument,
    trimmed,
    with_account,
)
from agent_platform.credentials import put_record, read_record
from agent_platform.errors import PlatformError
from agent_platform.pairing import pair, register
from agent_runner.crypto.identity import account_code, format_account_code

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# `claude connect` and `claude sync`: summary, example, search words, next step.
CLAUDE_DOCS = {
    "connect": (
        "store this computer's Claude subscription login for the user's tasks",
        "agent-platform claude connect",
        "abo abonnement anmeldung hinterlegen speichern subscription login store",
        "agent-platform claude service install",
    ),
    "sync": (
        "store the local Claude login again if it changed",
        "agent-platform claude sync",
        "abgleichen aktualisieren erneuern abo refresh update resync subscription",
        "",
    ),
}


# --- commands ---


def _message(args: argparse.Namespace, context: Context, *, stdin_taken: bool = False) -> str | None:
    """`--message <text>` or `--message -` (stdin): the requester's personal message (rule E-28), trimmed; stdin only
    when nothing else is read from it (`invalid_argument`)."""
    if args.message is None:
        return None
    if args.message != "-":
        return str(args.message).strip()
    if stdin_taken:
        raise PlatformError("invalid_argument")
    return context.read_stdin().strip()


def cmd_register(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    from_env = context.environ.get("AGENT_PLATFORM_INVITE", "")
    message = _message(args, context, stdin_taken=not from_env)
    link = from_env or context.read_stdin()
    if not link.strip():
        raise PlatformError("invalid_argument")
    if args.own_account:
        if args.remote or args.replace_pending or message is not None:
            raise PlatformError("invalid_argument")
        return alone.register_own(
            context.home,
            invite=link,
            display_name=args.display_name,
            name=args.name,
            host=args.host,
            root_key=args.platform_root_key,
        )
    if args.platform_root_key is not None:
        raise PlatformError("invalid_argument")
    if args.remote:
        result = remote.start_register(
            context.home,
            invite=link,
            display_name=args.display_name,
            name=args.name,
            host=args.host,
            replace=args.replace_pending,
            message=message,
        )
    elif args.replace_pending:
        raise PlatformError("invalid_argument")
    else:
        result = register(
            context.home,
            context.environ,
            invite=link,
            display_name=args.display_name,
            name=args.name,
            host=args.host,
            message=message,
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
        if args.hub is not None or args.remote or args.replace_pending or args.message is not None:
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
            context.home,
            hub=args.hub,
            name=args.name,
            host=args.host,
            replace=args.replace_pending,
            message=_message(args, context),
        )
    if args.replace_pending:
        raise PlatformError("invalid_argument")
    result = pair(
        context.home,
        context.environ,
        hub=args.hub,
        name=args.name,
        host=args.host,
        browser=not args.no_browser,
        message=_message(args, context),
    )
    return _with_display_name(result, context)


def cmd_status(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.attention:
        return attention.status(account, args.org)
    if args.org is not None:
        raise PlatformError("invalid_argument")
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
    return lookup.guide(args.topic)


def cmd_search(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return lookup.search(build_parser(), " ".join(args.query))


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
    body: dict[str, Any] = {
        "model_access": args.model_access or settings.get("model_access") or "auto",
        "model_id": settings.get("model_id") if args.model is None else (args.model or None),
        "locale": args.locale or settings.get("locale") or "en",
    }
    fallback = modelaccess.fallback_body(settings, args.fallback, args.fallback_platform)
    if fallback is not None:
        body["fallback"] = fallback
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
    # opening an org writes the user's own name into it if it has none there (ADR-073); the view never fails on it
    try:
        own_name = names.ensure_own_name(account, args.org)
    except PlatformError:
        own_name = "failed"
    return {**orgs.org_show(account, args.org), "own_name": own_name}


def cmd_org_invite(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.name is not None:
        orgs.display_name(args.name)
    wgs = orgs.parse_wgs(args.wg)
    created = orgs.org_invite(account, args.org, args.role, wgs, args.expires_hours, message=_message(args, context))
    return names.invite_named(account, args.org, created, args.name)


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
    return names.admit_named(account, args.org, args.user, role=args.role, wgs=orgs.parse_wgs(args.wg), name=args.name)


def cmd_org_member_name(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return names.member_name(account, args.org, args.user, args.name)


def cmd_org_invitees(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.org_invitees(account, args.org)


def cmd_wg_grant(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return membership.wg_grant(account, args.wg, args.user, args.access, args.history)


def cmd_wg_placement(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return orgs.wg_placement(account, args.wg, args.device, args.vm, args.auto)


def cmd_device_assign(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.org is not None:
        return orgs.device_assign_org(account, args.org, args.device)
    if args.device is not None:
        raise PlatformError("invalid_argument")
    return orgs.device_assign(account, args.wg)


def cmd_task_list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.org is not None:
        if args.state is None:
            raise PlatformError("invalid_argument")
        limit = attention.LIST_DEFAULT if args.limit is None else args.limit
        return attention.task_list(account, args.org, args.state, mine=args.mine, limit=limit)
    if args.state not in (None, *tasks.STATES) or args.mine:
        raise PlatformError("invalid_argument")
    return tasks.task_list(account, args.wg, args.state)


def cmd_task_create(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return tasks.task_create(
        account, args.wg, text_argument(args, context), args.device, args.vm, github_identity=args.github_identity
    )


def cmd_task_prompt(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    decision = "approve" if args.approve else "decline" if args.decline else None
    text = None if args.text is None else text_argument(args, context)
    return tasks.task_prompt(account, args.task, tasks.answer_text(decision, text))


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


MESSAGE_HELP = "a personal message shown where the request is approved (one line, up to 500 characters), or - for stdin"


def _account_commands(tree: Tree) -> None:
    sub = tree.add(
        tree.commands,
        "pair",
        cmd_pair,
        "connect this agent to the user's account (--remote: from another computer, e.g. over SSH)",
        common=False,
        example='agent-platform pair --hub https://agent.dfx.swiss --name "Claude Code"',
        words=(
            "koppeln verbinden anmelden konto login connect link account remote ssh server anderer computer fern "
            "another"
        ),
        then="agent-platform status",
    )
    sub.add_argument("--hub")
    sub.add_argument("--name", default="Claude Code")
    sub.add_argument("--host", default=(socket.gethostname().split(".")[0] or "computer")[:64])
    sub.add_argument("--no-browser", action="store_true")
    sub.add_argument("--remote", action="store_true", help="the agent runs on another computer than the browser")
    sub.add_argument("--replace-pending", action="store_true", help="delete a pending remote pairing first")
    sub.add_argument("--wait", action="store_true", help="finish the pending remote pairing")
    sub.add_argument("--return-code", help="with --wait: the code the page showed the user")
    sub.add_argument("--timeout", type=int, default=remote.WAIT_DEFAULT_S, help="with --wait: seconds (0-600)")
    sub.add_argument("--message", help=MESSAGE_HELP)
    sub = tree.add(
        tree.commands,
        "register",
        cmd_register,
        "a new account through an invitation (link on stdin)",
        common=False,
        example='printf \'%s\' "<invitation link>" | agent-platform register --display-name "<your user\'s name>" '
        '--name "Claude Code"',
        words=(
            "registrieren konto einladungslink allein ohne mensch eigenes signup sign up new account invitation own "
            "alone"
        ),
        then="agent-platform status",
    )
    sub.add_argument("--display-name", required=True)
    sub.add_argument("--name", default="Claude Code")
    sub.add_argument("--host", default=(socket.gethostname().split(".")[0] or "computer")[:64])
    sub.add_argument("--remote", action="store_true", help="the agent runs on another computer than the browser")
    sub.add_argument("--replace-pending", action="store_true", help="delete a pending remote pairing first")
    sub.add_argument("--message", help=MESSAGE_HELP)
    sub.add_argument("--own-account", action="store_true", help="an account of this agent alone: no user, no passkey")
    sub.add_argument("--platform-root-key", help="with --own-account: platform root key (b64u) instead of the hub's")
    sub = tree.add(
        tree.commands,
        "status",
        writing(cmd_status),
        "the account as the agent sees it",
        example="agent-platform status",
        words="zustand konto überblick lage entscheidung account overview whoami state situation attention decision",
        then="agent-platform org list",
    )
    sub.add_argument("--attention", action="store_true", help="the situation: counters, decisions and stuck tasks")
    sub.add_argument("--org", help="with --attention: only this organization")
    sub = tree.add(
        tree.commands,
        "api",
        with_account(cmd_api),
        "a signed request to any route",
        example="agent-platform api GET /v1/me",
        words="schnittstelle anfrage route request endpoint http raw call",
    )
    sub.add_argument("method")
    sub.add_argument("path")
    sub.add_argument("--body", help="JSON body, or - to read it from stdin")
    sub = tree.add(
        tree.commands,
        "guide",
        cmd_guide,
        "the short start for AI agents, or one topic of the full guide",
        common=False,
        example="agent-platform guide --topic connect",
        words="anleitung hilfe einstieg handbuch thema help manual topic start docs documentation",
        then="agent-platform search <word>",
    )
    sub.add_argument("--topic", help="a topic of the full guide (see topics), or all")
    sub = tree.add(
        tree.commands,
        "search",
        cmd_search,
        "find the command for a task: purpose, call, example and next step of the best matches",
        common=False,
        example='agent-platform search "invite a person"',
        words="suchen finden nachschlagen befehl wie find lookup command how which",
    )
    sub.add_argument("query", nargs="+", help="words in English or German")
    settings = tree.group("settings", "user settings")
    sub = tree.add(
        settings,
        "set",
        with_account(cmd_settings_set),
        "PUT /v1/me/settings",
        example="agent-platform settings set --model-access subscription",
        words=(
            "einstellungen modell sprache abo ausweichen erschöpft dfx-zugang immer so preferences model language "
            "locale configure fallback exhausted"
        ),
    )
    sub.add_argument("--model-access", choices=["auto", "subscription", "api_key", "platform"])
    sub.add_argument("--model", help="model ID ('' clears it)")
    sub.add_argument("--locale", choices=["de", "en"])
    sub.add_argument(
        "--fallback",
        choices=["on", "off"],
        help="when the own subscription is used up, continue on DFX access (charged; default off, ADR-080)",
    )
    sub.add_argument(
        "--fallback-platform",
        choices=["on", "off"],
        help="when the operator's subscriptions are used up, continue on DFX access over OpenRouter (default on)",
    )
    credentials = tree.group("credentials", "credential records")
    tree.add(
        credentials,
        "set-api-key",
        writing(cmd_set_api_key),
        "store an Anthropic API key (stdin)",
        example="printf '%s' \"$KEY\" | agent-platform credentials set-api-key",
        words="anthropic-schlüssel schlüssel hinterlegen speichern store",
    )
    sub = tree.add(
        tree.commands,
        "diagnose",
        with_account(cmd_diagnose),
        "why a task does not start",
        example="agent-platform diagnose --task <task_id>",
        words="fehlersuche warum startet nicht problem hängt debug why stuck troubleshoot",
    )
    sub.add_argument("--task")


def _task_commands(tree: Tree) -> None:
    group = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.add(
        group,
        "list",
        with_account(cmd_task_list),
        "tasks of a workgroup, or of an org by attention",
        example="agent-platform task list --wg <wg_id> [--state open|closed]",
        words="aufgabenliste aufgaben liste arbeitsgruppe titel hängt läuft tasks all stuck running needs-decision",
        then="agent-platform task show --task <task_id>",
        trim="first",
    )
    where = sub.add_mutually_exclusive_group(required=True)
    where.add_argument("--wg", help="the tasks of a workgroup (--state open|closed)")
    where.add_argument("--org", help="one category of the org's situation view (--state needs-decision|...)")
    sub.add_argument("--state", choices=[*tasks.STATES, *attention.CATEGORIES])
    sub.add_argument("--mine", action="store_true", help="with --org: only tasks the user owns")
    sub = tree.add(
        group,
        "create",
        writing(cmd_task_create),
        "a new task with its first prompt",
        example="agent-platform task create --wg <wg_id> --text -  < prompt.txt",
        words="beauftragen aufgabe auftrag starten anlegen neu start job new order",
        then="agent-platform task follow --task <task_id>",
    )
    sub.add_argument("--wg", required=True, help="the workgroup ID (`wg list --org <org_id>`, `status`)")
    sub.add_argument("--text", required=True, help="the prompt, or - to read it from stdin")
    sub.add_argument("--device", help="run it on this device (a device assigned to the workgroup)")
    sub.add_argument("--vm", help="and on this VM of the workgroup on that device (needs --device)")
    sub.add_argument("--github-identity", help="the task's GitHub identity (from `github accounts`; default automatic)")
    sub = tree.add(
        group,
        "prompt",
        writing(cmd_task_prompt),
        "a further prompt, an answer or an approval",
        example='agent-platform task prompt --task <task_id> --text "…"',
        words="nachfragen anweisung nachricht antworten freigeben ablehnen schreiben message reply send follow-up"
        " approve decline",
        then="agent-platform task follow --task <task_id>",
    )
    sub.add_argument("--task", required=True)
    sub.add_argument("--text", help="the prompt (with --approve/--decline the comment), or - to read it from stdin")
    decision = sub.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="answer an approval: Approved.")
    decision.add_argument("--decline", action="store_true", help="answer an approval: Declined.")
    sub = tree.add(
        group,
        "show",
        with_account(cmd_task_show),
        "a task and its chat, opened and verified",
        example="agent-platform task show --task <task_id>",
        words="gespräch chat verlauf ergebnis antwort ansehen lesen conversation result answer output read view",
        trim="last",
    )
    sub.add_argument("--task", required=True)
    sub = tree.add(
        group,
        "follow",
        with_account(cmd_task_follow),
        "new chat events after a position (waits)",
        example="agent-platform task follow --task <task_id> [--after <next_after>] [--wait 60]",
        words="mitverfolgen neue antworten warten live beobachten watch wait stream updates",
        then="agent-platform task follow --task <task_id> --after <next_after>",
        trim="first",
    )
    sub.add_argument("--task", required=True)
    sub.add_argument("--after", type=int, default=0, help="next_after of the previous call (0: from the start)")
    sub.add_argument("--wait", type=int, default=60, help="seconds to wait for the first new event (0-600)")


def _claude_commands(tree: Tree) -> None:
    group = tree.group("claude", "the Claude subscription")
    for name, handler in (("connect", cmd_claude_connect), ("sync", cmd_claude_sync)):
        summary, example, words, then = CLAUDE_DOCS[name]
        sub = tree.add(group, name, writing(handler), summary, example=example, words=words, then=then)
        sub.add_argument("--replace", action="store_true", help="also replace a setup-token record")
    sub = tree.add(
        group,
        "watch",
        cmd_claude_watch,
        "sync in a loop",
        example="agent-platform claude watch [--interval 300]",
        words="schleife dauernd überwachen abo loop repeat periodic subscription",
    )
    sub.add_argument("--interval", type=int, default=claude.WATCH_INTERVAL_S)
    sub.add_argument("--iterations", type=int, help=argparse.SUPPRESS)
    service = tree.group("service", "the background sync service", group)
    sub = tree.add(
        service,
        "install",
        cmd_service_install,
        "install the user-level service",
        example="agent-platform claude service install",
        words="hintergrunddienst dienst automatisch einrichten hintergrund background daemon launchd systemd autostart",
    )
    sub.add_argument("--interval", type=int, default=claude.WATCH_INTERVAL_S)
    sub.add_argument("--no-load", action="store_true")
    sub = tree.add(
        service,
        "remove",
        cmd_service_remove,
        "remove the user-level service",
        common=False,
        example="agent-platform claude service remove",
        words="deinstallieren hintergrunddienst dienst entfernen uninstall background daemon stop",
    )
    sub.add_argument("--no-load", action="store_true")
    tree.add(
        group,
        "setup-token",
        writing(cmd_setup_token),
        "run `claude setup-token` and store the token",
        example="agent-platform claude setup-token",
        words="langzeittoken abo hinterlegen jahr long lived subscription year",
    )


def _org_commands(tree: Tree) -> None:
    group = tree.group("org", "organizations")
    tree.add(
        group,
        "list",
        with_account(cmd_org_list),
        "organizations of the user",
        example="agent-platform org list",
        words="organisationen firmen teams meine organizations companies mine",
        then="agent-platform org show --org <org_id>",
        trim="first",
    )
    sub = tree.add(
        group,
        "create",
        writing(cmd_org_create),
        "create a team organization",
        example='agent-platform org create --name "Acme"',
        words="gründen organisation firma anlegen neu found company new",
        then='agent-platform wg create --org <org_id> --name "Research"',
    )
    sub.add_argument("--name", required=True)
    sub.add_argument("--display-name")
    sub = tree.add(
        group,
        "show",
        with_account(cmd_org_show),
        "members, workgroups, invitations and their state",
        example="agent-platform org show --org <org_id>",
        words="einladungen mitglieder arbeitsgruppen details status organisation members invitations",
        trim="first",
    )
    sub.add_argument("--org", required=True)
    sub = tree.add(
        group,
        "invite",
        writing(cmd_org_invite),
        "invite a user (prints invite_link once)",
        example="agent-platform org invite --org <org_id>",
        words="einladen einladung person kollege hinzufügen add colleague link",
        then="agent-platform org show --org <org_id>",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--role", choices=["admin", "member"], default="member")
    sub.add_argument("--wg", action="append", default=[], help="<wg_id>[:read|write], repeatable")
    sub.add_argument("--expires-hours", type=int, default=72)
    sub.add_argument("--message", help=MESSAGE_HELP)
    sub.add_argument("--name", help="the invited person's name, shown in the invitation list (ADR-073)")
    sub = tree.add(
        group,
        "admit",
        writing(cmd_org_admit),
        "admit an existing account directly (no step on its side)",
        example='agent-platform org admit --org <org_id> --user <account_code> --name "<name>" [--role admin]',
        words="aufnehmen direkt hinzufügen kontocode person add directly account code",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--user", required=True, help="user ID or account code (XXXX-XXXX-XXXX)")
    sub.add_argument("--role", choices=["admin", "member"], default="member")
    sub.add_argument("--wg", action="append", default=[], help="<wg_id>[:read|write], repeatable")
    sub.add_argument("--name", required=True, help="the person's name: every member sees it until their own one")
    sub = tree.add(
        group,
        "member-name",
        writing(cmd_org_member_name),
        "give or change the provisional name of a member or an invitation (admins)",
        example="agent-platform org member-name --org <org_id> --user <user_id|account code|invite_id>",
        words="umbenennen vorläufiger name mitglied rename member provisional name",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--user", required=True, help="user ID, account code (XXXX-XXXX-XXXX) or invitation ID")
    sub.add_argument("--name", required=True)
    sub = tree.add(
        group,
        "invitees",
        with_account(cmd_org_invitees),
        "accounts registered through your platform invitations",
        example="agent-platform org invitees [--org <org_id>]",
        words="eingeladene registriert angemeldet wer invited registered signed up who",
        then="agent-platform org admit --org <org_id> --user <account_code>",
        trim="first",
    )
    sub.add_argument("--org", help="only those that can be admitted to this organization")
    sub = tree.add(
        group,
        "join",
        writing(cmd_org_join),
        "accept an org invitation (link on stdin)",
        example="printf '%s' \"<invitation link>\" | agent-platform org join",
        words="beitreten annehmen einladung akzeptieren organisation accept invitation enter",
    )
    sub.add_argument("--display-name")
    group = tree.group("wg", "workgroups")
    sub = tree.add(
        group,
        "list",
        with_account(cmd_wg_list),
        "workgroups of an organization",
        example="agent-platform wg list --org <org_id>",
        words="arbeitsgruppen gruppen arbeitsgruppe id zugriff projekte workgroups workgroup groups access projects",
        then="agent-platform task list --wg <wg_id>",
        trim="first",
    )
    sub.add_argument("--org", required=True)
    sub = tree.add(
        group,
        "create",
        writing(cmd_wg_create),
        "create a workgroup",
        example='agent-platform wg create --org <org_id> --name "Research"',
        words="projektgruppe arbeitsgruppe gruppe anlegen neu projekt group new project",
        then="agent-platform device assign --wg <wg_id>",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--name", required=True)
    sub = tree.add(
        group,
        "grant",
        writing(cmd_wg_grant),
        "give a member access to a workgroup",
        example="agent-platform wg grant --wg <wg_id> --user <user_id> [--access read] [--history]",
        words="freigeben zugriff berechtigung mitglied gewähren arbeitsgruppe permission share give",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--user", required=True)
    sub.add_argument("--access", choices=["read", "write"], default="write")
    sub.add_argument("--history", action="store_true")
    _placement_commands(tree, group)


def _placement_commands(tree: Tree, wg_group: Any) -> None:
    """`wg placement` and `device assign` (ADR-075 sections 5 and 6)."""
    group = wg_group
    sub = tree.add(
        group,
        "placement",
        writing(cmd_wg_placement),
        "the workgroup's default device and VM",
        example="agent-platform wg placement --wg <wg_id>",
        words="standard gerät vm vorgabe wählen automatisch default device choose pin placement",
        then="agent-platform task create --wg <wg_id> --text -",
    )
    sub.add_argument("--wg", required=True)
    choice = sub.add_mutually_exclusive_group()
    choice.add_argument("--device", help="the default device for new tasks")
    choice.add_argument("--auto", action="store_true", help="choose automatically (clears the default)")
    sub.add_argument("--vm", help="the default VM on that device (needs --device)")
    group = tree.group("device", "execution devices")
    sub = tree.add(
        group,
        "assign",
        writing(cmd_device_assign),
        "assign a device to a workgroup or the whole organization",
        example="agent-platform device assign --wg <wg_id>",
        words="zuweisen gerät rechner ausführung arbeitsgruppe organisation computer execution attach organization",
        then="agent-platform task create --wg <wg_id> --text -",
    )
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--wg", help="a platform device for this workgroup")
    target.add_argument("--org", help="a device for every workgroup of this organization (it holds all their keys)")
    sub.add_argument("--device", help="the device (with --org; default: the first candidate)")


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
    result = handler(args, Context(environ, stdin))
    if args.trim is not None:
        result = trimmed(result, args.limit, args.fields, args.trim)
    return {"ok": True, **result}


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
