"""Parity matrix web client ↔ agent tool (docs/platform/agent-parity.md, 11-agent-access.md section 8.4, ADR-056 §4).

Two checks keep the gap between the web client and the agent tool from coming back:

- Screens and sections: every screen directory under `web/src/ui/screens/` must appear as `screens/<directory>` and
  every settings section under `web/src/ui/settings/sections/` as `settings/<directory>` in the Screen column of the
  Actions table (the screen `settings` only hosts the sections), and the table names no directory that is gone.
- Routes: the Routes table lists exactly the hub routes the web client calls. The routes are read from the client's
  source (tests excluded, comments stripped): the method and path of every `request('METHOD', path)` and
  `anon('METHOD', path)` call whose path is a literal, and every string or template literal that starts with `/v1/`
  (paths built by helpers, the live stream). Template parts become `{}`, query strings are dropped; the table's
  `{param}` names compare as `{}`. Every route used must be in the table, the table names no path the client does not
  use, a table route's method is one the client uses with that path or with a path it builds, and every route names
  action rows of the Actions table.

The states of the rows and the commands of `done` rows are checked by the parity test of the `agent` repository
(`tests/platform/tool/test_parity.py`), which has the tool but not the web source.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "docs" / "platform" / "agent-parity.md"
WEB = REPO_ROOT / "web" / "src"
SCREENS = WEB / "ui" / "screens"
SECTIONS = WEB / "ui" / "settings" / "sections"
HOST_SCREEN = "settings"
STATES = {"done", "pending", "n/a"}
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
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
TABLE_PARAM = re.compile(r"\{[a-z_]+\}")
ROUTE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[a-z0-9/{}_-]*|/v1/config)`")


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


def matrix_rows() -> list[list[str]]:
    """The rows of the Actions table as cells (action, screen, command, state)."""
    return table("Actions")


def named(prefix: str) -> set[str]:
    """The directories named as `<prefix>/<directory>` in the Screen column."""
    pattern = re.compile(rf"\b{prefix}/([a-z0-9-]+)")
    return {name for row in matrix_rows() for name in pattern.findall(row[1])}


def directories(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_dir()}


def normalize(path: str) -> str:
    """Template parts as `{}`, without the query string."""
    return PLACEHOLDER.sub("{}", path).split("?", 1)[0]


def sources() -> list[Path]:
    return [
        path
        for path in sorted(WEB.rglob("*"))
        if path.suffix in (".ts", ".tsx") and ".test." not in path.name and "__tests__" not in path.parts
    ]


def client_routes() -> tuple[set[str], set[tuple[str, str]], set[str]]:
    """`(paths, (method, path) of calls with a literal path, methods of calls with a built path)` of the web client."""
    paths: set[str] = set()
    calls: set[tuple[str, str]] = set()
    built: set[str] = set()
    for source in sources():
        code = TOKEN.sub(lambda m: "" if m.group(0).startswith("/") else m.group(0), source.read_text(encoding="utf-8"))
        for token in TOKEN.finditer(code):
            text = token.group(0)[1:-1]
            if text.startswith("/v1/") or text == "/v1/config":
                paths.add(normalize(text))
        for call in CALL.finditer(code):
            literal = TOKEN.match(code, call.end())
            if literal is not None and literal.group(0)[1:].startswith("/v1/"):
                calls.add((call.group(1), normalize(literal.group(0)[1:-1])))
            else:
                built.add(call.group(1))
    return paths, calls, built


def table_routes() -> list[tuple[str, str, list[str]]]:
    """`(method, normalized path, actions)` of the Routes table."""
    found = []
    for cells in table("Routes"):
        assert len(cells) == 2, cells
        match = ROUTE.fullmatch(cells[0])
        assert match is not None, cells
        method, path = match.groups()
        found.append((method, TABLE_PARAM.sub("{}", path), [name.strip() for name in cells[1].split(",")]))
    return found


def test_rows_are_well_formed() -> None:
    rows = matrix_rows()
    assert len(rows) > 60
    assert all(len(row) == 4 for row in rows)
    assert {row[3] for row in rows} <= STATES
    assert len({row[0] for row in rows}) == len(rows)


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


def test_the_extraction_finds_the_client_routes() -> None:
    paths, calls, built = client_routes()
    assert {"/v1/stream", "/v1/scopes/{}/events", "/v1/tasks/{}/close", "/v1/config"} <= paths
    assert {
        ("POST", "/v1/sessions/{}/control"),
        ("DELETE", "/v1/me/agents/{}"),
        ("POST", "/v1/passkeys/login/begin"),
    } <= calls
    assert "GET" in built
    assert normalize("/v1/orgs/${encodeURIComponent(org)}/audit?${query.toString()}") == "/v1/orgs/{}/audit"


def test_every_route_of_the_client_is_in_the_table() -> None:
    paths, calls, _built = client_routes()
    listed = {(method, path) for method, path, _actions in table_routes()}
    assert calls - listed == set()
    assert paths - {path for _method, path in listed} == set()


def test_the_table_names_no_route_the_client_does_not_use() -> None:
    paths, calls, built = client_routes()
    routes = table_routes()
    assert len({(method, path) for method, path, _actions in routes}) == len(routes)
    for method, path, _actions in routes:
        assert path in paths, path
        assert (method, path) in calls or method in built, (method, path)


def test_every_route_names_existing_actions() -> None:
    actions = {row[0] for row in matrix_rows()}
    for method, path, named_actions in table_routes():
        assert named_actions, (method, path)
        assert set(named_actions) <= actions, (method, path)
