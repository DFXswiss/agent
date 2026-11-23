"""The prepared patch for the infrastructure repository (08-monitoring.md section 5, ADR-018).

This repository names no machine, so the patch is rendered per installation from a checkout of the infrastructure
repository and the runner device names:

    python -m agent_monitoring.patch --infra CHECKOUT --device NAME [--device NAME…] \\
        --runner-textfile-dir DIR --out FILE

The checkout is only read: it is cloned twice into a temporary directory (`git clone --no-local`), the first clone is
rendered and diffed into FILE (`git diff --binary`, applied with `git apply`), the second, fresh clone proves
`git apply --check FILE`. Rendering (`render`) changes, below the one monitoring stack of the repository
(`infrastructure/*/monitoring-stack`, the directory with `dashboard-inventory.toml`):

- `grafana/provisioning/dashboards/json/Agent platform/agent-platform-{devices,vms,sessions}.json` (new),
- `grafana/provisioning/alerting/agent-platform.yaml` (new, device placeholder rendered),
- `grafana/provisioning/alerting/fleet-host-memory.yaml`: a device missing in the selector of
  `fleet-host-memory-absent` is added to the selector and gets its own zero fallback,
- `dashboard-inventory.toml`: a device without a `[[servers]]` tile gets `inventory-tile.toml` and an entry in
  `server_display_order`,

and per device `infrastructure/<device>/README.md` (section `textfile-dir.md`, with the textfile directory of
`infrastructure/<device>/node_exporter.args`), plus the files under `patch/tree/` (the infrastructure-side
regression test of the alert file).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

from agent_monitoring.assets import (
    ALERT_FILE,
    ASSETS,
    DASHBOARD_DIR,
    DASHBOARD_FOLDER,
    DASHBOARD_UIDS,
    PLACEHOLDER,
    check_devices,
    render_devices,
)

TREE = ASSETS / "patch" / "tree"
SECTION = ASSETS / "textfile-dir.md"
TILE = ASSETS / "inventory-tile.toml"
SECTION_HEADING = "## Agent runner textfile"
ABSENT_UID = "fleet-host-memory-absent"
TEXTFILE_FLAG = "--collector.textfile.directory="
SUBJECT = "Add the agent platform dashboards, alert rules and runner textfile step."
FALLBACK_RE = re.compile(r'^( *)or label_replace\(vector\(0\), "server", "([^"]+)", "", ""\)\n', re.MULTILINE)
SELECTOR_RE = re.compile(r'node_memory_total_bytes\{server=~"([^"]*)"\}')
ABSENT_NOTE = (
    "{device} added to fleet-host-memory-absent: add it to the watched hosts and the expected expression of "
    "infrastructure/tests/monitoring-stack/test_fleet_host_memory_alerts.py and to the host list of the rule "
    "description"
)
TILE_NOTE = (
    "tile for {device} added to dashboard-inventory.toml: complete its description and the display-order comment"
)


class PatchError(Exception):
    """The checkout does not have the shape the patch expects; the message says what is missing."""


def find_stack(infra: Path) -> Path:
    stacks = sorted(path.parent for path in infra.glob("infrastructure/*/monitoring-stack/dashboard-inventory.toml"))
    if len(stacks) != 1:
        raise PatchError(f"expected exactly one infrastructure/*/monitoring-stack, found {len(stacks)}")
    return stacks[0]


def _rule_block(text: str, uid: str) -> tuple[int, int]:
    """Start and end offset of the rule `uid` in an alert file (up to the next rule or the end)."""
    start = text.find(f"- uid: {uid}\n")
    if start < 0:
        raise PatchError(f"rule {uid} not found")
    following = re.compile(r"^\s*- uid: ", re.MULTILINE).search(text, start + 1)
    return start, following.start() if following else len(text)


def add_absent_device(text: str, device: str) -> str:
    """`fleet-host-memory.yaml` with `device` in the selector and the zero fallback of `fleet-host-memory-absent`.

    The result is checked: the selector and the zero fallbacks of the rule name the same hosts (the regression test
    of the infrastructure repository requires it), otherwise the file is refused rather than edited loosely."""
    start, end = _rule_block(text, ABSENT_UID)
    block = text[start:end]
    selector = SELECTOR_RE.search(block)
    fallbacks = list(FALLBACK_RE.finditer(block))
    if selector is None or not fallbacks:
        raise PatchError(f"{ABSENT_UID}: selector or zero fallback not found")
    names = selector.group(1).split("|")
    if device not in names:
        block = block[: selector.start(1)] + "|".join([*names, device]) + block[selector.end(1) :]
        fallbacks = list(FALLBACK_RE.finditer(block))
    if device not in {match.group(2) for match in fallbacks}:
        last = fallbacks[-1]
        line = f'{last.group(1)}or label_replace(vector(0), "server", "{device}", "", "")\n'
        block = block[: last.end()] + line + block[last.end() :]
    selected = SELECTOR_RE.search(block)
    if selected is None or set(selected.group(1).split("|")) != {m.group(2) for m in FALLBACK_RE.finditer(block)}:
        raise PatchError(f"{ABSENT_UID}: selector and zero fallbacks name different hosts")
    return text[:start] + block + text[end:]


def add_inventory_tile(text: str, device: str) -> str:
    """`dashboard-inventory.toml` with a tile and a display position for `device`, unless it has a tile already."""
    try:
        servers = tomllib.loads(text).get("servers", [])
    except tomllib.TOMLDecodeError as exc:
        raise PatchError(f"dashboard-inventory.toml: {exc}") from exc
    if any(server.get("key") == device for server in servers):
        return text
    order = re.search(r"^server_display_order = \[(.*)\]$", text, re.MULTILINE)
    if order is None:
        raise PatchError("server_display_order not found (one line expected)")
    names = [*order.group(1).split(", "), f'"{device}"'] if order.group(1) else [f'"{device}"']
    text = text[: order.start(1)] + ", ".join(names) + text[order.end(1) :]
    tile = TILE.read_text(encoding="utf-8").replace(PLACEHOLDER, device)
    return text.rstrip("\n") + "\n\n" + tile


def textfile_dir(infra: Path, device: str) -> str:
    args = infra / "infrastructure" / device / "node_exporter.args"
    if not args.is_file():
        raise PatchError(
            f"infrastructure/{device}/node_exporter.args not found: onboard the host (host baseline) first"
        )
    for item in args.read_text(encoding="utf-8").split():
        if item.startswith(TEXTFILE_FLAG):
            return item[len(TEXTFILE_FLAG) :]
    raise PatchError(f"infrastructure/{device}/node_exporter.args has no {TEXTFILE_FLAG}")


def add_readme_section(text: str, device: str, directory: str, runner_dir: str) -> str:
    if SECTION_HEADING in text:
        return text
    section = SECTION.read_text(encoding="utf-8")
    section = section.replace(PLACEHOLDER, device).replace("<textfile-dir>", directory)
    section = section.replace("<runner-textfile-dir>", runner_dir)
    return text.rstrip("\n") + "\n\n" + section if text.strip() else section


def _edit(path: Path, change: str) -> None:
    path.write_text(change, encoding="utf-8")


def render(infra: Path, devices: Sequence[str], runner_dir: str) -> list[str]:
    """Write the monitoring assets for `devices` into the infrastructure checkout `infra` (a scratch clone); the
    hand edits the infrastructure repository needs besides (texts and pinned lists the patch does not rewrite)."""
    check_devices(list(devices))
    if not runner_dir.startswith("/") or any(char.isspace() for char in runner_dir):
        raise PatchError("the runner textfile directory must be an absolute path without blanks")
    stack = find_stack(infra)
    provisioning = stack / "grafana" / "provisioning"
    folder = provisioning / "dashboards" / "json" / DASHBOARD_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for uid in DASHBOARD_UIDS:
        shutil.copyfile(DASHBOARD_DIR / f"{uid}.json", folder / f"{uid}.json")
    alerting = provisioning / "alerting"
    _edit(alerting / ALERT_FILE.name, render_devices(ALERT_FILE.read_text(encoding="utf-8"), list(devices)))
    memory = alerting / "fleet-host-memory.yaml"
    inventory = stack / "dashboard-inventory.toml"
    notes = []
    for device in devices:
        directory = textfile_dir(infra, device)
        before, after = (
            memory.read_text(encoding="utf-8"),
            add_absent_device(memory.read_text(encoding="utf-8"), device),
        )
        if after != before:
            _edit(memory, after)
            notes.append(ABSENT_NOTE.format(device=device))
        before = inventory.read_text(encoding="utf-8")
        after = add_inventory_tile(before, device)
        if after != before:
            _edit(inventory, after)
            notes.append(TILE_NOTE.format(device=device))
        readme = infra / "infrastructure" / device / "README.md"
        current = readme.read_text(encoding="utf-8") if readme.exists() else ""
        _edit(readme, add_readme_section(current, device, directory, runner_dir))
    for source in sorted(TREE.rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            target = infra / source.relative_to(TREE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return notes


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], check=False, cwd=cwd, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise PatchError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def make_patch(source: Path, devices: Sequence[str], runner_dir: str) -> tuple[str, list[str]]:
    """The patch text for `devices` against the checkout `source`, verified with `git apply --check`, and the hand
    edits it needs (also listed in the patch header)."""
    with tempfile.TemporaryDirectory(prefix="agent-monitoring-patch.") as scratch:
        work, fresh = Path(scratch, "work"), Path(scratch, "fresh")
        _git("clone", "-q", "--no-local", str(source), str(work))
        head = _git("rev-parse", "HEAD", cwd=work).strip()
        notes = render(work, devices, runner_dir)
        _git("add", "-A", cwd=work)
        diff = _git("diff", "--cached", "--binary", cwd=work)
        stat = _git("diff", "--cached", "--stat", cwd=work)
        hand = "".join(f"Hand edit: {note}\n" for note in notes)
        text = f"Subject: [PATCH] {SUBJECT}\n\nBase: {head}\n{hand}---\n{stat}\n{diff}"
        patch_file = Path(scratch, "agent-platform.patch")
        patch_file.write_text(text, encoding="utf-8")
        _git("clone", "-q", "--no-local", str(source), str(fresh))
        _git("apply", "--check", str(patch_file), cwd=fresh)
        return text, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_monitoring.patch", description=__doc__.splitlines()[0])
    parser.add_argument("--infra", type=Path, required=True, help="checkout of the infrastructure repository (read)")
    parser.add_argument("--device", action="append", required=True, help="runner device (host folder); repeatable")
    parser.add_argument("--runner-textfile-dir", required=True, help="the runner's own TEXTFILE_DIR on the device")
    parser.add_argument("--out", type=Path, required=True, help="patch file to write")
    args = parser.parse_args(argv)
    try:
        text, notes = make_patch(args.infra.resolve(), args.device, args.runner_textfile_dir)
    except (PatchError, ValueError) as exc:
        print(f"patch: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8")
    print(f"patch: {args.out} ({text.count(chr(10))} lines), git apply --check passed")
    for note in notes:
        print(f"patch: hand edit needed: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
