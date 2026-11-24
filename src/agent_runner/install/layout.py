"""Paths of an installed runner package (ADR-074 sections 1 and 2).

Every path hangs below `root` (`/` on a Mac; a temporary directory in tests), so the helper never writes outside the
layout it was given. The payload is root-owned; the owner of the Mac owns only the runner's home below its own home.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_ID = "swiss.dfx.agent-runner"
RUNNER_LABEL = "swiss.dfx.agent-runner"
MAINTENANCE_LABEL = "swiss.dfx.agent-runner.maintenance"
PACKAGE_PREFIX = "DFX-Agent-Runner-"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RUNNER_HOME_RELATIVE = Path(".local/share/dfx-agent-runner")
VM_PREFIX = "dfx-"


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/")

    @property
    def payload(self) -> Path:
        return self.root / "Library/Application Support/DFXAgentRunner"

    @property
    def spool(self) -> Path:
        """Root-only (0700) inbox of the enrollment request: the one channel for a code (ADR-074 section 3)."""
        return self.payload / "spool"

    @property
    def request(self) -> Path:
        return self.spool / "enroll.request"

    @property
    def host_tools(self) -> Path:
        """Claude Code for the host, downloaded at installation (not part of the signed payload)."""
        return self.payload / "host"

    @property
    def versions(self) -> Path:
        return self.payload / "versions"

    @property
    def current(self) -> Path:
        return self.payload / "current"

    @property
    def state_file(self) -> Path:
        """Public state (0644): what `agent-platform device add` and the device's owner may read."""
        return self.payload / "state.json"

    @property
    def maintenance_file(self) -> Path:
        """Private state of the maintenance daemon (0600 root): update window, pending update, bad versions."""
        return self.payload / "maintenance.json"

    @property
    def log_dir(self) -> Path:
        return self.payload / "log"

    @property
    def downloads(self) -> Path:
        """Root-only scratch for updates (0700)."""
        return self.payload / "downloads"

    @property
    def launch_daemons(self) -> Path:
        return self.root / "Library/LaunchDaemons"

    @property
    def runner_plist(self) -> Path:
        return self.launch_daemons / f"{RUNNER_LABEL}.plist"

    @property
    def maintenance_plist(self) -> Path:
        return self.launch_daemons / f"{MAINTENANCE_LABEL}.plist"

    @staticmethod
    def runner_home(owner_home: Path) -> Path:
        """The runner's home below the owner's home (0700, created and removed by the owner, never by root)."""
        return owner_home / RUNNER_HOME_RELATIVE

    def version_dir(self, version: str) -> Path:
        return self.versions / version

    def python(self, base: Path | None = None) -> Path:
        """The payload's interpreter, through `current` unless a version directory is given."""
        return (self.current if base is None else base) / "python/bin/python3"

    def launcher(self) -> Path:
        """The program of both LaunchDaemons (a recognisable name in Login Items): runs `python -I -m <module>`."""
        return self.current / "libexec/dfx-agent-runner"

    def claude_bin(self) -> Path:
        return self.host_tools / "bin"

    def runner_path(self) -> str:
        """`PATH` of the runner daemon: the payload's Lima and PostgreSQL, Claude Code for the host, then the system."""
        bins = [self.current / "lima/bin", self.current / "postgresql/bin", self.claude_bin()]
        return ":".join([*(str(path) for path in bins), SYSTEM_PATH])

    def golden_template(self) -> Path:
        return self.current / "golden-src/images/golden.yaml"
