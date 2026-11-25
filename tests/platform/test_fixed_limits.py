"""No fixed limit without a reason (KONZEPT E-19, A5, A14; ADR-088 section 4).

Every place that bounds something is found and must be listed in `fixed_limits.json` with one of the reasons of that
file; a count of things a person or session creates is no reason - it must be a setting (A5) and the test refuses a
new one until it is listed as such:

- in `src/agent_hub`, `src/agent_runner` and `src/agent_guest`: every module-level or class-level name containing
  `MAX` or `LIMIT` (any case, any position: `MAX_SESSIONS`, `RUNTIME_MAX_S`, `max_running`, `LIMITS`) whose value is
  not a string (an environment variable's name is no bound), every slice with a literal upper bound of at least
  `SLICE_MIN` (`items[:64]`; shorter slices cut identifiers and digests), and every SQL `LIMIT` with a literal number
  above 1;
- in `contracts/` (without fixtures, vectors and node modules): every `maxItems`, `maximum` and `maxProperties`.

The inventory must be exact: a listed place that no longer exists fails too, so the list always tells the truth.
`python tests/platform/test_fixed_limits.py --write` adds new places with the reason `unreasoned` (which fails until a
person chooses a reason) and drops places that are gone.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("fixed_limits.json")
PACKAGES = ("agent_hub", "agent_runner", "agent_guest")
NAME_RE = re.compile(r"max|limit", re.IGNORECASE)
SQL_LIMIT_RE = re.compile(r"\bLIMIT\s+([2-9]|[1-9][0-9]+)\b")
SLICE_MIN = 20
CONTRACT_KEYWORDS = ("maxItems", "maximum", "maxProperties")
SKIPPED_CONTRACT_PARTS = ("node_modules", "fixtures", "vectors")
SKIPPED_CONTRACT_FILES = ("package.json", "package-lock.json")


def _names(target: ast.expr) -> Iterator[str]:
    """The plain names a target binds (`A = …`, `A, B = …`, `(A, (B, C)) = …`)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _names(element)


def _named(tree: ast.Module) -> Iterator[str]:
    """Module- and class-level names like a limit whose value is not a string."""
    nodes = list(tree.body) + [item for node in tree.body if isinstance(node, ast.ClassDef) for item in node.body]
    for node in nodes:
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            continue
        yield from (name for target in targets for name in _names(target) if NAME_RE.search(name))


def _sliced(tree: ast.Module) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            upper = node.slice.upper
            if (
                node.slice.lower is None
                and isinstance(upper, ast.Constant)
                and type(upper.value) is int
                and upper.value >= SLICE_MIN
            ):
                yield ast.unparse(node)


def _sql(tree: ast.Module) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield from (match.group(0) for match in SQL_LIMIT_RE.finditer(node.value))


def code_places(root: Path = ROOT) -> dict[str, dict[str, set[str]]]:
    """`{"constants"|"slices"|"sql": {path: {place}}}` of the three packages."""
    found: dict[str, dict[str, set[str]]] = {"constants": {}, "slices": {}, "sql": {}}
    for package in PACKAGES:
        for path in sorted((root / "src" / package).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for kind, places in (("constants", _named(tree)), ("slices", _sliced(tree)), ("sql", _sql(tree))):
                for place in places:
                    found[kind].setdefault(relative, set()).add(place)
    return found


def _bounds(value: Any, pointer: str) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in CONTRACT_KEYWORDS and isinstance(item, int | float) and not isinstance(item, bool):
                yield f"{pointer}/{key}"
            yield from _bounds(item, f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _bounds(item, f"{pointer}/{index}")


def contract_places(root: Path = ROOT) -> dict[str, set[str]]:
    """`{path: {json pointer of a maxItems/maximum}}` of `contracts/`."""
    found: dict[str, set[str]] = {}
    for path in sorted((root / "contracts").rglob("*.json")):
        relative = path.relative_to(root)
        if path.name in SKIPPED_CONTRACT_FILES or any(part in SKIPPED_CONTRACT_PARTS for part in relative.parts):
            continue
        places = set(_bounds(json.loads(path.read_text(encoding="utf-8")), ""))
        if places:
            found[relative.as_posix()] = places
    return found


def found_places(root: Path = ROOT) -> dict[str, dict[str, set[str]]]:
    return {**code_places(root), "contracts": contract_places(root)}


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return data


def _flat(places: dict[str, dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {(kind, path, place) for kind, paths in places.items() for path, names in paths.items() for place in names}


def test_every_bound_is_listed_with_a_reason() -> None:
    inventory = load()
    found = _flat(found_places())
    listed = _flat({kind: inventory[kind] for kind in ("constants", "slices", "sql", "contracts")})
    assert sorted(found - listed) == [], "a new bound: give it a reason in fixed_limits.json or make it a setting"
    assert sorted(listed - found) == [], "a listed bound is gone: drop it from fixed_limits.json"


def test_every_reason_is_an_allowed_one() -> None:
    inventory = load()
    reasons = set(inventory["reasons"])
    assert "quantity" not in reasons
    used = {
        reason
        for kind in ("constants", "slices", "sql", "contracts")
        for names in inventory[kind].values()
        for reason in names.values()
    }
    assert sorted(used - reasons) == []


def test_the_subagent_limits_are_settings_not_constants() -> None:
    """ADR-088 section 2: the numbers of subagents are hub settings; no copy of them in code or contracts."""
    inventory = load()
    constants = inventory["constants"]
    for path, names in constants.items():
        for name, reason in names.items():
            if re.search(r"subagent|depth|running", name, re.IGNORECASE) and "subagent" in path:
                assert reason in {"setting", "compat", "size", "structure", "time"}, (path, name)
    rules = json.loads((ROOT / "contracts" / "ops" / "rules.json").read_text(encoding="utf-8"))
    assert set(rules["subagent_limits"]) == {"purpose_max_length", "prompt_max_length"}


def test_the_scan_sees_every_kind_of_bound(tmp_path: Path) -> None:
    """The scanner itself: names in any position, class fields, literal slices, SQL limits, contract keywords."""
    package = tmp_path / "src" / "agent_hub"
    package.mkdir(parents=True)
    (package / "sample.py").write_text(
        "RUNTIME_MAX_S = 1800\nMAX_NAME = 'AGENT_HUB_MAX'\nMAX_A, (MAX_B, other) = 1, (2, 3)\nclass Limits:\n"
        "    max_running: int = 3\n"
        'def cut(items):\n    return items[:64], items[:8], items[1:64], "SELECT 1 LIMIT 500", "LIMIT 1"\n',
        encoding="utf-8",
    )
    for other in ("agent_runner", "agent_guest"):
        (tmp_path / "src" / other).mkdir(parents=True)
    contracts = tmp_path / "contracts" / "api"
    contracts.mkdir(parents=True)
    bounds = {"a/b": {"maxItems": 10, "maximum": True, "maxProperties": 4}}
    (contracts / "x.json").write_text(json.dumps(bounds), encoding="utf-8")
    (tmp_path / "contracts" / "fixtures").mkdir()
    (tmp_path / "contracts" / "fixtures" / "y.json").write_text(json.dumps({"maxItems": 1}), encoding="utf-8")
    (tmp_path / "contracts" / "package.json").write_text(json.dumps({"maximum": 1}), encoding="utf-8")
    found = found_places(tmp_path)
    assert found["constants"] == {"src/agent_hub/sample.py": {"RUNTIME_MAX_S", "MAX_A", "MAX_B", "max_running"}}
    assert found["slices"] == {"src/agent_hub/sample.py": {"items[:64]"}}
    assert found["sql"] == {"src/agent_hub/sample.py": {"LIMIT 500"}}
    assert found["contracts"] == {"contracts/api/x.json": {"/a~1b/maxItems", "/a~1b/maxProperties"}}


def write(root: Path = ROOT) -> None:
    """Bring the inventory up to date: keep reasons, add new places as `unreasoned`, drop gone ones."""
    inventory = load()
    found = found_places(root)
    for kind, paths in found.items():
        old = inventory.get(kind, {})
        inventory[kind] = {
            path: {place: old.get(path, {}).get(place, "unreasoned") for place in sorted(places)}
            for path, places in sorted(paths.items())
        }
    INVENTORY.write_text(json.dumps(inventory, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":  # the maintenance command of the module docstring
    if sys.argv[1:] == ["--write"]:
        write()
