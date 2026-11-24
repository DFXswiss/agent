"""U-DOC-prerequisites (KOR-49, ADR-079 section 1, rules E-05, E-11, E-19): the register of prerequisites is complete.

`docs/platform/spec/15-prerequisites.md` has one row for every code with which a task, a session, an operation or a
request can wait or be refused. The codes come from the closed contract lists - scheduling reasons, session failure
reasons, operation error codes, hub error codes - from the reasons a runner holds an operation for
(`agent_runner.operations.pipeline.HOLD_CODES`), and from a scan of the hub and the runner for every code raised by one
of `RAISERS` (a string literal,
or a module constant the scan resolves; codes passed through from a variable are re-raises of codes found
elsewhere). Each row names the class of its justification and the step the platform starts (E-05); a class
"approval" does not exist (E-11). A new waiting or refusal code is therefore red here until it is registered.
"""

from __future__ import annotations

import ast
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from agent_runner.operations.pipeline import HOLD_CODES

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER = REPO_ROOT / "docs" / "platform" / "spec" / "15-prerequisites.md"
ENUMS = REPO_ROOT / "contracts" / "enums"
SCANNED = (REPO_ROOT / "src" / "agent_hub", REPO_ROOT / "src" / "agent_runner")
SKIPPED = (
    REPO_ROOT / "src" / "agent_runner" / "crypto",  # copy of the reference; its codes are the log codes of the enum
)
OPERATOR_CLI = "cli.py"
"""The operator's own command lines of the hub (`agent-hub platform|billing …`) refuse with messages, not codes."""
RAISERS = frozenset(
    {
        "HubError",
        "CheckFailed",
        "StartFailed",
        "NoPlacement",
        "AccessRefused",
        "Refused",
        "Rejected",
        "OperationFailed",
        "RequestClosed",
        "PromptRefused",
        "QuotaRefused",
    }
)
CLASSES = frozenset({"technical", "third_party", "option", "rights", "credit", "input"})
CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REGISTER_HEADING = "## 5. Register"
ROW_RE = re.compile(r"^\| `([a-z][a-z0-9_]*)` \|(.*)\|\s*$")


def _json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ENUMS / name).read_text(encoding="utf-8"))
    return data


def contract_codes() -> set[str]:
    """Every code of the closed contract lists and every held reason."""
    tasks = _json("task-states.json")
    codes = {str(code) for code in tasks["scheduling_reasons"]}
    codes |= {str(code) for code in _json("session-failure-reasons.json")["reasons"]}
    codes |= {str(code) for code in _json("operation-error-codes.json")["codes"]}
    codes |= {str(item["code"]) for item in _json("error-codes.json")["codes"]}
    codes |= set(HOLD_CODES.values())
    return codes


def _skipped(path: Path) -> bool:
    return path.name == OPERATOR_CLI or any(skip in path.parents for skip in SKIPPED)


def _module_path(module: str) -> Path | None:
    base = REPO_ROOT / "src" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


@cache
def _constants(path: Path) -> dict[str, str]:
    """Module-level `NAME = "text"` and `NAME: T = "text"` of a module, and names imported `from` other modules of
    this repository (resolved there)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.update({target.id: value.value for target in targets if isinstance(target, ast.Name)})
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            source = _module_path(node.module)
            if source is not None:
                imported = _constants(source)
                found.update({(a.asname or a.name): imported[a.name] for a in node.names if a.name in imported})
    return found


def _imported_modules(path: Path) -> dict[str, Path]:
    """`alias -> module file` of `import x.y as z` and `from x import y` (y a module) of a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: dict[str, Path] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            for alias in node.names:
                found = _module_path(f"{node.module}.{alias.name}")
                if found is not None:
                    modules[alias.asname or alias.name] = found
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found = _module_path(alias.name)
                if found is not None and alias.asname is not None:
                    modules[alias.asname] = found
    return modules


def _code(node: ast.expr, path: Path) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return _constants(path).get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        module = _imported_modules(path).get(node.value.id)
        return None if module is None else _constants(module).get(node.attr)
    return None


def raised_codes() -> dict[str, list[str]]:
    """`code -> [file:line, …]` of every code raised in the hub and the runner (module docstring)."""
    found: dict[str, list[str]] = {}
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if _skipped(path):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name not in RAISERS:
                    continue
                code = _code(node.args[0], path)
                if code is not None and CODE_RE.match(code):
                    where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    found.setdefault(code, []).append(where)
    return found


def register_rows() -> dict[str, list[str]]:
    """`code -> cells` of the register table (Where, Class, Why, Handling, Started by)."""
    rows: dict[str, list[str]] = {}
    text = REGISTER.read_text(encoding="utf-8")
    for line in text[text.index(REGISTER_HEADING) :].splitlines():
        match = ROW_RE.match(line)
        if match is None:
            continue
        code = match.group(1)
        assert code not in rows, f"{code}: two rows in the register"
        rows[code] = [cell.strip() for cell in match.group(2).split(" | ")]
    return rows


def test_every_contract_code_has_a_row() -> None:
    missing = sorted(contract_codes() - set(register_rows()))
    assert not missing, f"codes without a row in {REGISTER.name}: {missing}"


def test_every_raised_code_has_a_row() -> None:
    rows = register_rows()
    missing = {code: places for code, places in raised_codes().items() if code not in rows}
    assert not missing, f"raised codes without a row in {REGISTER.name}: {missing}"


def test_the_scan_finds_the_codes_of_every_raiser() -> None:
    """The scan itself works: literals, module constants and constants of imported modules are found."""
    raised = raised_codes()
    assert "platform_access_denied" in raised  # HubError("…") in the hub
    assert "branch_protected" in raised  # CheckFailed("…") in the runner
    assert "model_access_unavailable" in raised  # AccessRefused(UNAVAILABLE), a module constant
    assert "platform_access_required" in raised  # NoPlacement(PLATFORM_ACCESS_REQUIRED), imported from another module


def test_every_row_names_a_known_code() -> None:
    known = contract_codes() | set(raised_codes())
    unknown = sorted(set(register_rows()) - known)
    assert not unknown, f"rows for codes no source has: {unknown}"


@pytest.mark.parametrize("code", sorted(register_rows()))
def test_every_row_is_justified_and_starts_a_step(code: str) -> None:
    cells = register_rows()[code]
    assert len(cells) == 5, f"{code}: Where | Class | Why | Handling | Started by"
    where, kind, why, handling, started = cells
    assert where, f"{code}: where it occurs"
    assert kind.strip("`") in CLASSES, f"{code}: class {kind!r} is none of {sorted(CLASSES)} (no approval, E-11)"
    assert len(why) >= 10, f"{code}: the justification"
    assert len(handling) >= 10, f"{code}: the step the platform starts (E-05)"
    assert started, f"{code}: who starts it"


def test_codes_are_code_shaped() -> None:
    assert all(CODE_RE.match(code) for code in contract_codes())
