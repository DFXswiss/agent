"""Parity web client ↔ agent tool (docs/platform/agent-parity.md, 11-agent-access.md section 8.4, ADR-056 §4; owner
rules E-04 and E-19 of KOR-42): an AI agent with the tool `agent-platform` alone can do everything a person can do in
the web client with a passkey, and the gap cannot come back unnoticed.

The tables of `agent-parity.md` are checked against the three sources of this checkout:

- **The web client** (`web/src`, tests excluded, comments stripped). Screens: every directory under
  `web/src/ui/screens/` appears as `screens/<directory>` and every settings section under
  `web/src/ui/settings/sections/` as `settings/<directory>` in the Screen column of the Actions table (the screen
  `settings` only hosts the sections), and the table names no directory that is gone. Routes: the method and path of
  every `request('METHOD', path)` and `anon('METHOD', path)` call whose path is a literal, and every string or template
  literal that starts with `/v1/` (paths built by helpers, the live stream); template parts become `{}`, query strings
  are dropped. Every route the client uses must be in the Routes table.
- **The hub** (`agent_hub`: the routes of every area's router as the app registers them, with their authentication
  class). Every route of the table is a route the web client calls or a route the hub registers (a table route that
  neither has is stale), every route the client calls is registered at the hub (a concrete segment such as
  `/v1/me/bundles/recovery` matches the hub's `{slot}`), and every route of the table that a person's browser may call
  at the hub (`browser`, `anon`, `anon_or_browser`) maps to a command: one of its action rows is `done`, or all of
  them are `n/a` or `pending` with their reason.
- **The tool** (the argparse tree of `agent_platform.cli.build_parser`). Every row has a known state; every command
  of a `done` row exists with the options it names; a `pending` row names the package that delivers it; an `n/a` row
  gives its reason. The guide (`docs/platform/agents.md`) has an example of every command, and every example names an
  existing command.

A new screen, settings section or route of the web client, a new browser route of the hub that the client calls, or
a new command without an example therefore fails here until the table, the tool and the guide follow. A new button
inside an existing screen that calls no new route needs its row by review (the table's intro says so).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from agent_hub.app import discovered_routers
from agent_hub.http.routing import route_spec
from agent_platform.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "docs" / "platform" / "agent-parity.md"
GUIDE = REPO_ROOT / "docs" / "platform" / "agents.md"
WEB = REPO_ROOT / "web" / "src"
SCREENS = WEB / "ui" / "screens"
SECTIONS = WEB / "ui" / "settings" / "sections"
HOST_SCREEN = "settings"
STATES = {"done", "pending", "n/a"}
PENDING = re.compile(r"package KOR-\d+ delivers\b")
# Authentication classes of hub routes a person's browser calls (03-api.md section 3); `agent`, `device` and `root`
# routes belong to the agent's own pairing, the execution devices and the operator.
BROWSER_AUTH = {"browser", "anon", "anon_or_browser"}
# A bar inside a cell (command alternatives `a\|b`) is escaped; only unescaped bars separate cells.
CELL_BORDER = re.compile(r"(?<!\\)\|")
ESCAPED_BAR = "\\|"
# Comments and string literals of TypeScript (templates with one level of nested braces in `${...}`).
TOKEN = re.compile(
    r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|\$\{(?:[^{}]|\{[^{}]*\})*\}|[^`\\])*`",
    re.S,
)
CALL = re.compile(r"\b(?:request|anon)\(\s*'(GET|POST|PUT|PATCH|DELETE)'\s*,\s*")
PLACEHOLDER = re.compile(r"\$\{(?:[^{}]|\{[^{}]*\})*\}")
PARAM = re.compile(r"\{[^}]*\}")
ROUTE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[a-z0-9/{}_-]*)`")
GUIDE_COMMAND = re.compile(r"^(?:printf [^|]*\| )?agent-platform ([a-z][^#<\n]*)", re.MULTILINE)

Route = tuple[str, str]


# --- the table ---


def table(heading: str) -> list[list[str]]:
    """The body rows of the table under `## <heading>` as cells."""
    found: list[list[str]] = []
    inside = False
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line == f"## {heading}"
            continue
        if not inside or not line.startswith("|"):
            continue
        body = line.strip().removeprefix("|").removesuffix("|")
        cells = [cell.strip().replace(ESCAPED_BAR, "|") for cell in CELL_BORDER.split(body)]
        if cells[0] in ("Action", "Route") or set(cells[0]) == {"-"}:
            continue
        found.append(cells)
    return found


@cache
def rows() -> list[dict[str, str]]:
    """The rows of the Actions table."""
    found = []
    for cells in table("Actions"):
        assert len(cells) == 4, cells
        found.append({"action": cells[0], "screen": cells[1], "command": cells[2], "state": cells[3]})
    return found


def table_routes() -> list[tuple[str, str, list[str]]]:
    """`(method, path with {} for parameters, actions)` of the Routes table."""
    found = []
    for cells in table("Routes"):
        assert len(cells) == 2, cells
        match = ROUTE.fullmatch(cells[0])
        assert match is not None, cells
        method, path = match.groups()
        found.append((method, PARAM.sub("{}", path), [name.strip() for name in cells[1].split(",")]))
    return found


def named(prefix: str) -> set[str]:
    """The directories named as `<prefix>/<directory>` in the Screen column."""
    pattern = re.compile(rf"\b{prefix}/([a-z0-9-]+)")
    return {name for row in rows() for name in pattern.findall(row["screen"])}


def directories(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_dir()}


# --- the web client ---


def normalize(path: str) -> str:
    """Template parts as `{}`, without the query string."""
    return PLACEHOLDER.sub("{}", path).split("?", 1)[0]


def sources() -> list[Path]:
    return [
        path
        for path in sorted(WEB.rglob("*"))
        if path.suffix in (".ts", ".tsx") and ".test." not in path.name and "__tests__" not in path.parts
    ]


@cache
def client_routes() -> tuple[set[str], set[Route], set[str]]:
    """`(paths, (method, path) of calls with a literal path, methods of calls with a built path)` of the web client."""
    paths: set[str] = set()
    calls: set[Route] = set()
    built: set[str] = set()
    for source in sources():
        code = TOKEN.sub(lambda m: "" if m.group(0).startswith("/") else m.group(0), source.read_text(encoding="utf-8"))
        for token in TOKEN.finditer(code):
            text = token.group(0)[1:-1]
            if text.startswith("/v1/"):
                paths.add(normalize(text))
        for call in CALL.finditer(code):
            literal = TOKEN.match(code, call.end())
            if literal is not None and literal.group(0)[1:].startswith("/v1/"):
                calls.add((call.group(1), normalize(literal.group(0)[1:-1])))
            else:
                built.add(call.group(1))
    return paths, calls, built


def client_uses(method: str, path: str) -> bool:
    """The client calls `method path`: a call with this literal path, or a path it builds with a built call."""
    paths, calls, built = client_routes()
    return (method, path) in calls or (path in paths and method in built)


# --- the hub ---


@cache
def hub_routes() -> dict[Route, str]:
    """`(method, path with {} for parameters)` of every route the hub registers, with its authentication class."""
    found: dict[Route, str] = {}
    for _area, router in discovered_routers():
        for route in router.routes:
            spec = route_spec(route)
            assert spec is not None, route
            for method in getattr(route, "methods", None) or {"WEBSOCKET"}:
                found[(method, PARAM.sub("{}", str(getattr(route, "path", ""))))] = spec.auth
    return found


def segments_match(path: str, pattern: str) -> bool:
    """`path` (concrete segments or `{}`) is served by the hub route `pattern` (`{}` matches one segment)."""
    have, want = path.split("/"), pattern.split("/")
    return len(have) == len(want) and all(b in (a, "{}") for a, b in zip(have, want, strict=True))


def hub_auth(method: str, path: str) -> str | None:
    """The authentication class of the hub route serving `method path` (an exact route before a pattern), or None."""
    routes = hub_routes()
    if (method, path) in routes:
        return routes[(method, path)]
    return next((auth for (m, p), auth in routes.items() if m == method and segments_match(path, p)), None)


# --- the tool ---


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings}


def command_exists(text: str) -> bool:
    """The words of `text` walk down the parser tree (`a|b` alternatives each); options must exist at the end (an
    option `--a|--b` names alternatives, each must exist)."""
    node: argparse.ArgumentParser = build_parser()
    words = text.split()
    walked = 0
    for word in words:
        if word.startswith("-"):
            break
        choices = _subcommands(node)
        alternatives = word.split("|")
        if not choices:
            break
        if not all(choice in choices for choice in alternatives):
            return False
        node = choices[alternatives[0]]
        walked += 1
    if walked == 0:
        return False
    options = {option for word in words[walked:] if word.startswith("--") for option in word.split("=")[0].split("|")}
    return options <= _options(node)


def leaf_commands(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> Iterator[str]:
    """Every command of the tool (`org admit`, `quotas`, …)."""
    found = _subcommands(parser)
    if not found:
        yield " ".join(prefix)
    for name, sub in found.items():
        yield from leaf_commands(sub, (*prefix, name))


def commands(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def guide_commands() -> list[str]:
    """The commands of the guide's examples (`agent-platform …` at the start of a line, up to a comment or `<`)."""
    found = []
    for match in GUIDE_COMMAND.finditer(GUIDE.read_text(encoding="utf-8")):
        words = [word for word in match.group(1).split() if not word.startswith(('"', "'", "$", "["))]
        found.append(" ".join(words))
    return found


# --- the Actions table and the screens ---


def test_rows_are_well_formed() -> None:
    found = rows()
    assert len(found) > 60
    assert {row["state"] for row in found} <= STATES
    assert len({row["action"] for row in found}) == len(found)


@pytest.mark.parametrize("row", [row for row in rows() if row["state"] == "done"], ids=lambda row: row["action"])
def test_done_rows_name_existing_commands(row: dict[str, str]) -> None:
    assert commands(row["command"]), row
    for command in commands(row["command"]):
        assert command_exists(command), command


def test_pending_rows_name_a_package_and_na_rows_a_reason() -> None:
    for row in rows():
        if row["state"] == "pending":
            assert PENDING.search(row["command"]), row
        if row["state"] == "n/a":
            assert re.fullmatch(r"\(.{20,}\)", row["command"]), row


def test_every_screen_has_a_row() -> None:
    screens = directories(SCREENS)
    assert HOST_SCREEN in screens
    assert screens - {HOST_SCREEN} - named("screens") == set()


def test_every_settings_section_has_a_row() -> None:
    sections = directories(SECTIONS)
    assert sections
    assert sections - named("settings") == set()


def test_the_matrix_names_no_missing_directory() -> None:
    assert named("screens") - directories(SCREENS) == set()
    assert named("settings") - directories(SECTIONS) == set()


# --- the Routes table, the web client and the hub ---


def test_the_extraction_finds_the_client_routes() -> None:
    paths, calls, built = client_routes()
    assert {"/v1/stream", "/v1/scopes/{}/events", "/v1/tasks/{}/close", "/v1/config", "/v1/me/invitees"} <= paths
    assert {
        ("POST", "/v1/sessions/{}/control"),
        ("DELETE", "/v1/me/agents/{}"),
        ("POST", "/v1/passkeys/login/begin"),
        ("GET", "/v1/users/{}/identity"),
    } <= calls
    assert "GET" in built
    assert normalize("/v1/orgs/${encodeURIComponent(org)}/audit?${query.toString()}") == "/v1/orgs/{}/audit"
    assert client_uses("GET", "/v1/stream")
    assert not client_uses("DELETE", "/v1/stream")


def test_the_hub_routes_are_read_from_the_app() -> None:
    routes = hub_routes()
    assert routes[("GET", "/v1/me/invitees")] == "browser"
    assert routes[("POST", "/v1/agent/claim")] == "agent"
    assert routes[("POST", "/v1/passkeys/login/begin")] == "anon"
    assert hub_auth("PUT", "/v1/me/bundles/recovery") == "browser"
    assert hub_auth("GET", "/v1/nosuch") is None
    assert segments_match("/v1/me/bundles/{}", "/v1/me/bundles/{}")
    assert not segments_match("/v1/me/bundles", "/v1/me/bundles/{}")


def test_every_route_of_the_client_is_in_the_table() -> None:
    paths, calls, _built = client_routes()
    listed = {(method, path) for method, path, _actions in table_routes()}
    assert calls - listed == set()
    assert paths - {path for _method, path in listed} == set()


def test_every_route_of_the_client_is_a_hub_route() -> None:
    _paths, calls, _built = client_routes()
    assert [route for route in sorted(calls) if hub_auth(*route) is None] == []
    assert [(m, p) for m, p, _actions in table_routes() if client_uses(m, p) and hub_auth(m, p) is None] == []


def test_the_table_names_no_route_that_neither_the_client_nor_the_hub_has() -> None:
    routes = table_routes()
    assert len(routes) > 50
    assert len({(method, path) for method, path, _actions in routes}) == len(routes)
    stale = [(m, p) for m, p, _actions in routes if not client_uses(m, p) and hub_auth(m, p) is None]
    assert stale == []


def test_every_route_names_existing_actions() -> None:
    actions = {row["action"] for row in rows()}
    for method, path, named_actions in table_routes():
        assert named_actions, (method, path)
        assert set(named_actions) <= actions, (method, path)


def test_every_browser_route_maps_to_a_command() -> None:
    """A route a person's browser calls has a `done` action (a command of its own), or only `n/a`/`pending` ones."""
    state = {row["action"]: row["state"] for row in rows()}
    browser = [(m, p, names) for m, p, names in table_routes() if hub_auth(m, p) in BROWSER_AUTH]
    assert len(browser) > 50
    covered = [{state[name] for name in names} for _m, _p, names in browser]
    missing = [
        (m, p)
        for (m, p, _names), states in zip(browser, covered, strict=True)
        if "done" not in states and not states <= {"n/a", "pending"}
    ]
    assert missing == []


# --- the guide ---


def test_generic_call_exists() -> None:
    assert command_exists("api GET /v1/me --body -")


def test_the_walker_refuses_unknown_commands() -> None:
    assert not command_exists("nosuch")
    assert not command_exists("org frobnicate")
    assert not command_exists("org list --nosuch")
    assert command_exists("claude service install|remove")
    assert command_exists("device assign --wg|--org")
    assert not command_exists("device assign --wg|--nosuch")
    assert not command_exists("--hub")
    assert {"org admit", "session workvm", "quotas", "claude service install"} <= set(leaf_commands(build_parser()))


def test_every_example_of_the_guide_names_an_existing_command() -> None:
    found = guide_commands()
    assert len(found) > 60
    assert [command for command in found if not command_exists(command)] == []


def test_every_command_has_an_example_in_the_guide() -> None:
    examples = guide_commands()
    missing = [
        command
        for command in leaf_commands(build_parser())
        if not any(example == command or example.startswith(f"{command} ") for example in examples)
    ]
    assert missing == []
