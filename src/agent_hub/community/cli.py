"""Operator CLI of the community: `agent-hub community moderator grant|revoke|list` (12-community.md section 7).

The moderator role is kept at the hub (ADR-061 decision 5) and changed only by the operator with root-signed requests,
exactly like `agent-hub platform` (`agent_hub.platform.cli.Operator`: the hub's root key must be the operator's, the hub
key is pinned on first use and verifies every answer). Commands:

- `grant --user ID`, `revoke --user ID`: `POST /v1/community/moderators`; prints `moderator <ID> granted|revoked`;
- `list`: `GET /v1/community/moderators`; prints `moderator <ID> since <time>` per moderator (or `no moderators`).

The user ID is shown to every user in the web client's settings (section "Account"). Exit codes as `agent-hub
platform`: 0 done, 1 refused (the line names the code), 2 usage or key-file error.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TextIO

import httpx

from agent_hub.clock import Clock, system_clock
from agent_hub.platform import cli as platform_cli


def dispatch(args: argparse.Namespace, http: httpx.Client | None, clock: Clock, out: TextIO) -> int:
    """Run one command (the parsed arguments of `register`)."""
    operator = platform_cli.open_operator(args, http, clock)
    if args.moderator_command == "list":
        moderators = operator.request("GET", "/v1/community/moderators")["moderators"]
        for item in moderators:
            print(f"moderator {item['user']} since {item['granted_at']}", file=out)
        if not moderators:
            print("no moderators", file=out)
        return platform_cli.EXIT_OK
    granted = args.moderator_command == "grant"
    operator.request("POST", "/v1/community/moderators", {"user": args.user, "granted": granted})
    print(f"moderator {args.user} {'granted' if granted else 'revoked'}", file=out)
    return platform_cli.EXIT_OK


def run(
    args: argparse.Namespace,
    *,
    http: httpx.Client | None = None,
    clock: Clock = system_clock,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """The handler of `agent-hub community moderator …`; `http` and `clock` are injected by tests."""
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    try:
        return dispatch(args, http, clock, stdout)
    except platform_cli.UsageError as error:
        print(f"agent-hub community: {error}", file=stderr)
        return platform_cli.EXIT_USAGE
    except platform_cli.Refused as refusal:
        print(f"agent-hub community: refused: {refusal.code}", file=stderr)
        return platform_cli.EXIT_REFUSED
    except httpx.HTTPError:
        print("agent-hub community: the hub cannot be reached", file=stderr)
        return platform_cli.EXIT_REFUSED
    except (ValueError, KeyError, TypeError):
        print("agent-hub community: refused: unavailable (the hub's answer is malformed)", file=stderr)
        return platform_cli.EXIT_REFUSED


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `agent-hub community moderator <command>`."""
    parser = subparsers.add_parser("community", help="operator commands of the community (root key)")
    areas = parser.add_subparsers(dest="community_command", metavar="command", required=True)
    moderator = areas.add_parser("moderator", help="grant, revoke or list community moderators")
    commands = moderator.add_subparsers(dest="moderator_command", metavar="command", required=True)
    for name, help_text in (
        ("grant", "make a user a community moderator"),
        ("revoke", "take the moderator role back"),
        ("list", "list the moderators"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--key-file", required=True, help="the platform root key file (0600)")
        sub.add_argument("--hub", required=True, help="hub URL, e.g. https://agent.example")
        if name != "list":
            sub.add_argument("--user", type=platform_cli.uuid_text, required=True, help="the user ID")
    parser.set_defaults(handler=run)
