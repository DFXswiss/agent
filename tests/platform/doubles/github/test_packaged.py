"""U-DBL-github: the GitHub double imports only the standard library (07-testing.md §1)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[4] / "src" / "agent_doubles"


def test_github_modules_import_only_the_standard_library() -> None:
    allowed = set(sys.stdlib_module_names) | {"agent_doubles"}
    files = sorted((SRC / "github").rglob("*.py"))
    assert len(files) >= 8
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path}: relative import"
                roots = [str(node.module).split(".")[0]]
            else:
                continue
            assert set(roots) <= allowed, f"{path}: imports {roots}"
