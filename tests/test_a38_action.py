"""Exercise actual composite shell steps against a hostile consumer workspace."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.no_pg
ROOT = Path(__file__).resolve().parents[1]

# Reviewed guard revision installed by this repository. It declares the
# actor_app_slug input; moving the pin is a reviewed change that updates this.
GUARD_ACTION_SHA = "63bd283dc9a3b6eb29827af9f690d2ba79ed1f14"
GUARD_ACTION_USES = f"DFXswiss/agent/.github/actions/a38-guard@{GUARD_ACTION_SHA}"


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
    # checked-in shell command. YAML is asserted at import; the trusted marker
    # prints only from main() so python -m cannot satisfy the stdout check.
    (trusted_src / "pip.py").write_text("print('trusted installer')\n")
    (trusted_src / "yaml.py").write_text("TRUSTED = True\n")
    (trusted_src / "agent_cli").mkdir()
    (trusted_src / "agent_cli/__init__.py").write_text("")
    (trusted_src / "agent_cli/a38_guard.py").write_text(
        "import yaml\n"
        "assert yaml.TRUSTED\n"
        "def main(*args, **kwargs):\n"
        "    print('trusted guard')\n"
        "    return 0\n"
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
        value = value.replace("${{ github.action_ref }}", "a" * 40)
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


def test_composite_passes_immutable_action_revision_via_environment() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/a38-guard/action.yml").read_text())
    guard_step = action["runs"]["steps"][2]
    assert guard_step["env"]["A38_RUNTIME_REVISION"] == "${{ github.action_ref }}"
    assert "github.sha" not in str(guard_step)


def test_repository_guard_workflow_uses_pinned_app_token_for_the_guard_step() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/a38-guard.yml").read_text(encoding="utf-8")
    )
    example = yaml.safe_load(
        (ROOT / "examples/a38-guard.yml").read_text(encoding="utf-8")
    )
    assert "concurrency" not in workflow

    guard_job = workflow["jobs"]["guard"]
    example_guard_job = example["jobs"]["guard"]
    assert guard_job["if"] == example_guard_job["if"]
    assert guard_job["concurrency"] == {
        "group": example_guard_job["concurrency"]["group"],
        "cancel-in-progress": False,
    }

    steps = workflow["jobs"]["guard"]["steps"]

    app_token_steps = [step for step in steps if step.get("id") == "app-token"]
    assert len(app_token_steps) == 1
    app_token = app_token_steps[0]
    assert re.fullmatch(
        r"actions/create-github-app-token@[0-9a-fA-F]{40}", app_token["uses"]
    )
    assert app_token["with"]["client-id"] == "${{ vars.A38_GUARD_APP_CLIENT_ID }}"
    assert (
        app_token["with"]["private-key"]
        == "${{ secrets.A38_GUARD_APP_PRIVATE_KEY }}"
    )

    guard_steps = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith(
            "DFXswiss/agent/.github/actions/a38-guard@"
        )
    ]
    assert len(guard_steps) == 1
    guard_step = guard_steps[0]
    assert guard_step["uses"] == GUARD_ACTION_USES
    assert steps.index(app_token) < steps.index(guard_step)

    guard_with = guard_step["with"]
    assert guard_with["token"] == "${{ steps.app-token.outputs.token }}"
    assert guard_with["actor_app_slug"] == "${{ steps.app-token.outputs.app-slug }}"
    assert all(
        (step.get("with") or {}).get("token") != "${{ secrets.GITHUB_TOKEN }}"
        for step in steps
    )

    action = yaml.safe_load(
        (ROOT / ".github/actions/a38-guard/action.yml").read_text(encoding="utf-8")
    )
    # Valid: pinned action.yml declared these same inputs when the pin was set.
    assert set(guard_with) <= set(action["inputs"])
