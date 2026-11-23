"""The TypeScript harness `contracts/tools/validate.mjs` (Ajv, strict mode) agrees with the Python harness."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.platform.contracts.harness import CONTRACTS, DRAFT, ID_BASE

SCRIPT = CONTRACTS / "tools" / "validate.mjs"


def run(root: Path | None = None) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for the contract harness (07-testing.md section 7 prerequisites)")
    if not (CONTRACTS / "node_modules" / "ajv").is_dir():
        pytest.fail("run `npm ci` in contracts/ first (tools/wp/start.sh does it)")
    args = [node, str(SCRIPT)] + ([str(root)] if root else [])
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=120)


def test_validate_mjs_is_green() -> None:
    result = run()
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.endswith(" 0 problems\n")


def write(root: Path, rel: str, document: object) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def test_validate_mjs_reports_problems(tmp_path: Path) -> None:
    write(tmp_path, "lib.json", {"$schema": DRAFT, "$id": f"{ID_BASE}lib.json", "title": "l", "$defs": {}})
    write(tmp_path, "data.json", {"codes": []})
    write(tmp_path, "a.json", {"$schema": DRAFT, "$id": f"{ID_BASE}a.json", "type": "integer"})
    write(tmp_path, "fixtures/a/valid/one.json", "text")
    write(tmp_path, "fixtures/a/invalid/one.json", 1)
    write(tmp_path, "b.json", {"$schema": DRAFT, "$id": f"{ID_BASE}wrong.json", "type": "string", "format": "uuid"})
    write(tmp_path, "c.json", {"$schema": DRAFT, "$id": f"{ID_BASE}c.json", "type": "strin"})
    result = run(tmp_path)
    assert result.returncode == 1
    lines = result.stderr.splitlines()
    assert "fixtures/a/valid/one.json: " in lines[0]
    assert "fixtures/a/invalid/one.json: accepted" in lines
    assert "b.json: $id https://agent.dfx.swiss/contracts/wrong.json" in lines
    assert "b.json: keyword /format not allowed" in lines
    assert any(line.startswith("b.json: unknown schema") for line in lines)
    assert any(line.startswith("c.json: schema is invalid") for line in lines)
    assert result.stdout.strip() == f"contracts: 4 schemas, 2 fixtures, {len(lines)} problems"


def test_validate_mjs_reports_missing_fixtures_and_unloadable_files(tmp_path: Path) -> None:
    write(tmp_path, "a.json", {"$schema": DRAFT, "$id": f"{ID_BASE}a.json", "type": "integer"})
    result = run(tmp_path)
    assert result.stderr.splitlines() == ["a.json: no valid fixture", "a.json: no invalid fixture"]
    (tmp_path / "broken.json").write_text("{")
    result = run(tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("loading: ")


def test_load_contracts_is_strict_by_default(tmp_path: Path) -> None:
    write(tmp_path, "c.json", {"$schema": DRAFT, "$id": f"{ID_BASE}c.json", "type": "strin"})
    node = shutil.which("node")
    assert node is not None
    code = f"import {{ loadContracts }} from {json.dumps(SCRIPT.as_uri())}; loadContracts({json.dumps(str(tmp_path))});"
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "c.json: schema is invalid" in result.stderr
