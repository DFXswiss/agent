"""U-MON patch: rendering into an infrastructure checkout (new files, absence rule, inventory tile, host README,
idempotence), the refusals for a checkout of another shape, and the patch itself: diffed from a scratch clone,
`git apply --check` on a fresh clone, the source checkout untouched, the rendered alert file passing the
infrastructure-side regression test of `patch/tree/`."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from agent_monitoring import assets, patch

from .conftest import STACK, git, write

RUNNER_DIR = "/var/lib/agent-runner/textfile"
ALERTING = f"{STACK}/grafana/provisioning/alerting"


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_render_for_a_known_device_adds_only_new_files_and_the_readme(infra: Path) -> None:
    memory, inventory = (
        read(infra, f"{ALERTING}/fleet-host-memory.yaml"),
        read(infra, f"{STACK}/dashboard-inventory.toml"),
    )
    assert patch.render(infra, ["runner-a"], RUNNER_DIR) == []
    assert read(infra, f"{ALERTING}/fleet-host-memory.yaml") == memory
    assert read(infra, f"{STACK}/dashboard-inventory.toml") == inventory
    alerts = read(infra, f"{ALERTING}/agent-platform.yaml")
    assert 'server=~"runner-a"' in alerts
    assert assets.PLACEHOLDER not in alerts
    folder = infra / STACK / "grafana/provisioning/dashboards/json/Agent platform"
    assert sorted(path.name for path in folder.iterdir()) == sorted(f"{uid}.json" for uid in assets.DASHBOARD_UIDS)
    readme = read(infra, "infrastructure/runner-a/README.md")
    assert readme.startswith("# runner-a\n\nHost notes.\n\n## Agent runner textfile\n")
    assert f"sudo ln -sfn {RUNNER_DIR}/agent_runner.prom /opt/runner-a/textfile/agent_runner.prom" in readme
    assert 'server="runner-a"' in readme
    assert "<" + "textfile-dir>" not in readme
    assert (infra / "infrastructure/tests/monitoring-stack/test_agent_platform_alerts.py").is_file()

    patch.render(infra, ["runner-a"], RUNNER_DIR)  # idempotent
    assert read(infra, "infrastructure/runner-a/README.md") == readme


def test_render_for_a_new_device_adds_absence_fallback_and_tile(infra: Path) -> None:
    notes = patch.render(infra, ["runner-a", "runner-b"], RUNNER_DIR)
    assert notes == [patch.ABSENT_NOTE.format(device="runner-b"), patch.TILE_NOTE.format(device="runner-b")]
    assert "test_fleet_host_memory_alerts.py" in notes[0]
    memory = read(infra, f"{ALERTING}/fleet-host-memory.yaml")
    assert 'node_memory_total_bytes{server=~"host-a|runner-a|runner-b"}' in memory
    assert memory.count('or label_replace(vector(0), "server", "runner-b", "", "")\n') == 1
    fallback = '                or label_replace(vector(0), "server", "runner-a", "", "")\n'
    assert fallback + fallback.replace("runner-a", "runner-b") in memory
    assert memory.index("runner-b") < memory.index("fleet-host-memory-other")
    inventory = read(infra, f"{STACK}/dashboard-inventory.toml")
    assert 'server_display_order = ["host-a", "runner-a", "runner-b"]' in inventory
    assert inventory.count('[[servers]]\nkey = "runner-b"\n') == 1
    assert 'prom_server = "runner-b"' in inventory
    readme = read(infra, "infrastructure/runner-b/README.md")
    assert readme.startswith("## Agent runner textfile\n")
    alerts = read(infra, f"{ALERTING}/agent-platform.yaml")
    assert 'server=~"runner-a|runner-b"' in alerts

    assert patch.render(infra, ["runner-a", "runner-b"], RUNNER_DIR) == []  # idempotent
    assert read(infra, f"{ALERTING}/fleet-host-memory.yaml") == memory
    assert read(infra, f"{STACK}/dashboard-inventory.toml") == inventory


def test_rendered_tree_passes_the_infrastructure_regression_test(infra: Path) -> None:
    patch.render(infra, ["runner-a", "runner-b"], RUNNER_DIR)
    test = infra / "infrastructure/tests/monitoring-stack/test_agent_platform_alerts.py"
    done = subprocess.run([sys.executable, str(test)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "Ran 4 tests" in done.stderr


def test_absent_fallback_is_added_when_only_the_selector_names_the_device() -> None:
    text = (
        "- uid: fleet-host-memory-absent\n"
        '  expr: node_memory_total_bytes{server=~"a|b"}\n'
        '    or label_replace(vector(0), "server", "a", "", "")\n'
    )
    changed = patch.add_absent_device(text, "b")
    assert changed.endswith('    or label_replace(vector(0), "server", "b", "", "")\n')
    assert 'server=~"a|b"' in changed


def test_inventory_with_an_empty_display_order() -> None:
    changed = patch.add_inventory_tile("server_display_order = []\n", "runner-a")
    assert changed.startswith('server_display_order = ["runner-a"]\n\n[[servers]]\nkey = "runner-a"\n')


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- uid: other\n", "rule fleet-host-memory-absent not found"),
        ("- uid: fleet-host-memory-absent\n  expr: x\n", "selector or zero fallback not found"),
        ('- uid: fleet-host-memory-absent\n  expr: node_memory_total_bytes{server=~"a"}\n', "zero fallback"),
    ],
)
def test_absent_rule_of_another_shape_is_refused(text: str, message: str) -> None:
    with pytest.raises(patch.PatchError, match=message):
        patch.add_absent_device(text, "b")


def test_inventory_tile_is_found_by_its_key() -> None:
    text = 'server_display_order = ["runner-a"]\n\n[[servers]]\n# comment\nname = "x"\nkey = "runner-a"\n'
    assert patch.add_inventory_tile(text, "runner-a") == text
    with pytest.raises(patch.PatchError, match=r"dashboard-inventory\.toml: "):
        patch.add_inventory_tile("[[servers]\n", "runner-a")


def test_absent_rule_whose_fallbacks_differ_from_the_selector_is_refused() -> None:
    text = (
        "- uid: fleet-host-memory-absent\n"
        '  expr: node_memory_total_bytes{server=~"a|c"}\n'
        '    or label_replace(vector(0), "server", "a", "", "")\n'
    )
    with pytest.raises(patch.PatchError, match="different hosts"):
        patch.add_absent_device(text, "b")


def test_inventory_without_display_order_is_refused() -> None:
    with pytest.raises(patch.PatchError, match="server_display_order"):
        patch.add_inventory_tile('[[servers]]\nkey = "x"\n', "runner-a")


def test_checkout_of_another_shape_is_refused(infra: Path) -> None:
    with pytest.raises(patch.PatchError, match="host baseline"):
        patch.render(infra, ["runner-c"], RUNNER_DIR)
    write(infra, "infrastructure/runner-c/node_exporter.args", "--web.listen-address=:9100\n")
    with pytest.raises(patch.PatchError, match=re.escape("has no --collector.textfile.directory=")):
        patch.render(infra, ["runner-c"], RUNNER_DIR)
    for bad in ("relative/dir", "/with blank"):
        with pytest.raises(patch.PatchError, match="absolute path"):
            patch.render(infra, ["runner-a"], bad)
    with pytest.raises(ValueError, match="invalid device"):
        patch.render(infra, ["../x"], RUNNER_DIR)
    write(infra, "infrastructure/hub-b/monitoring-stack/dashboard-inventory.toml", "")
    with pytest.raises(patch.PatchError, match="found 2"):
        patch.render(infra, ["runner-a"], RUNNER_DIR)


def test_make_patch_applies_to_a_fresh_clone_and_leaves_the_source_alone(infra: Path, tmp_path: Path) -> None:
    head = git("rev-parse", "HEAD", cwd=infra).strip()
    text, notes = patch.make_patch(infra, ["runner-a", "runner-b"], RUNNER_DIR)
    assert len(notes) == 2
    assert git("status", "--porcelain", cwd=infra) == ""
    assert git("rev-parse", "HEAD", cwd=infra).strip() == head
    assert text.startswith(f"Subject: [PATCH] {patch.SUBJECT}\n\nBase: {head}\nHand edit: runner-b added to ")
    assert f"Hand edit: {notes[1]}\n---\n" in text
    assert "Agent platform/agent-platform-devices.json" in text
    assert "infrastructure/runner-b/README.md" in text

    target = tmp_path / "target"
    git("clone", "-q", "--no-local", str(infra), str(target), cwd=tmp_path)
    patch_file = tmp_path / "agent-platform.patch"
    patch_file.write_text(text, encoding="utf-8")
    git("apply", str(patch_file), cwd=target)
    assert 'server=~"runner-a|runner-b"' in read(target, f"{ALERTING}/agent-platform.yaml")


def test_make_patch_reports_git_failures(tmp_path: Path) -> None:
    with pytest.raises(patch.PatchError, match="git clone failed"):
        patch.make_patch(tmp_path / "missing", ["runner-a"], RUNNER_DIR)


def test_command_line(infra: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out.patch"
    argv = ["--infra", str(infra), "--device", "runner-a", "--runner-textfile-dir", RUNNER_DIR, "--out", str(out)]
    assert patch.main(argv) == 0
    assert out.read_text(encoding="utf-8").startswith("Subject: [PATCH]")
    output = capsys.readouterr()
    assert "git apply --check passed" in output.out
    assert output.err == ""
    assert patch.main([*argv[:2], "--device", "runner-b", *argv[4:]]) == 0
    assert "patch: hand edit needed: runner-b added to fleet-host-memory-absent" in capsys.readouterr().err
    assert patch.main([*argv[:2], "--device", "runner-c", *argv[4:]]) == 1
    assert "host baseline" in capsys.readouterr().err
