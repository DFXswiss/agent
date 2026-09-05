"""Exercise actual composite shell steps against a hostile consumer workspace."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.no_pg
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("step_index", [1, 2], ids=["installer", "guard"])
def test_composite_cannot_import_consumer_modules(tmp_path: Path, step_index: int) -> None:
    action = yaml.safe_load((ROOT / ".github/actions/a38-guard/action.yml").read_text())
    step = action["runs"]["steps"][step_index]
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    action_path = tmp_path / "trusted/.github/actions/a38-guard"
    action_path.mkdir(parents=True)
    trusted_src = tmp_path / "trusted/src"
    trusted_src.mkdir()
    marker = tmp_path / "untrusted-imported"
    malicious = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('consumer code imported')\n"
    for name in ("pip.py", "yaml.py"):
        (consumer / name).write_text(malicious)
    (consumer / "agent_cli").mkdir()
    (consumer / "agent_cli/__init__.py").write_text(malicious)
    (consumer / "agent_cli/a38_guard.py").write_text(malicious)

    # Trusted stand-ins avoid network/package writes while running the exact
    # checked-in shell command. The guard stand-in also imports its YAML module.
    (trusted_src / "pip.py").write_text("print('trusted installer')\n")
    (trusted_src / "yaml.py").write_text("TRUSTED = True\n")
    (trusted_src / "agent_cli").mkdir()
    (trusted_src / "agent_cli/__init__.py").write_text("")
    (trusted_src / "agent_cli/a38_guard.py").write_text(
        "import yaml\nassert yaml.TRUSTED\nprint('trusted guard')\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    # A caller may already have checked out untrusted code and exported this.
    env["PYTHONPATH"] = str(consumer)
    inputs = {key: str(value.get("default", "")) for key, value in action["inputs"].items()}
    inputs["token"] = "test-only"
    for key, value in step.get("env", {}).items():
        value = str(value).replace("${{ github.action_path }}", str(action_path))
        for name, replacement in inputs.items():
            value = value.replace("${{ inputs." + name + " }}", replacement)
        assert "${{" not in value
        env[key] = value
    cwd = str(step.get("working-directory", consumer)).replace("${{ github.action_path }}", str(action_path))
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = "trusted installer" if step_index == 1 else "trusted guard"
    assert expected in result.stdout
    assert not marker.exists(), "the composite executed consumer-controlled Python"
