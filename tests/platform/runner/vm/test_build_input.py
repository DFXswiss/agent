"""U-RUN-vm build input of an installed runner (KOR-03, AUD-01 B-25; decisions VM-01 item 7, KOR-03).

The golden build needs one tree with `images/`, `src/agent_guest/` and `contracts/` (`golden.load_source`). An
installed package (a wheel: `agent_runner` alone below `site-packages`) has none of it: the build is refused with
`golden_source_missing` instead of building from whatever lies next to the package, and works once
`AGENT_RUNNER_GOLDEN_TEMPLATE` names the checkout's `images/golden.yaml` (the installation profile of the runner
device, runbook runner-installation)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_runner.vm import golden

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from agent_runner.errors import RunnerError
from agent_runner.vm import golden
assert golden.__file__.startswith(sys.argv[1]), golden.__file__
out = {"root": str(golden.default_root())}
try:
    golden.load_source()
except RunnerError as error:
    out["default"] = error.code
template = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if template is not None:
    out["template"] = str(golden.load_source(template).root)
print(json.dumps(out))
"""


def _installed(tmp_path: Path) -> Path:
    """`agent_runner` and `agent_guest` copied like a wheel installs them (packages only, no checkout around)."""
    site = tmp_path / "lib" / "python3.12" / "site-packages"
    source = Path(golden.__file__).resolve().parents[2]
    for package in ("agent_runner", "agent_guest"):
        shutil.copytree(source / package, site / package, ignore=shutil.ignore_patterns("__pycache__"))
    return site


def _probe(site: Path, *args: str) -> dict[str, str]:
    # The copy is a throw-away tree: coverage's subprocess measurement must not count it as source.
    env = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE")}
    done = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(site), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    value: dict[str, str] = json.loads(done.stdout)
    return value


def test_an_installed_runner_without_a_checkout_refuses_to_build(tmp_path: Path) -> None:
    site = _installed(tmp_path)
    found = _probe(site)
    assert found["default"] == "golden_source_missing"
    assert not (Path(found["root"]) / "images").exists()


def test_the_configured_template_names_the_build_input(tmp_path: Path) -> None:
    site = _installed(tmp_path)
    checkout = golden.default_root()
    found = _probe(site, str(checkout / "images" / "golden.yaml"))
    assert Path(found["template"]) == checkout
