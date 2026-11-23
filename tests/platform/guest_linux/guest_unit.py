"""`agent-guest-control` of the Linux unit tests as a real systemd unit (KOR-09, AUD-02 NB-14).

Usage (as root, from ``systemd-run``): ``python3 guest_unit.py <spec.json>``. The spec names the checkout's source
directory and the session machinery's test paths (those of ``make_rig`` in ``test_sessions_linux.py``); the
configuration comes from ``AGENT_GUEST_*`` like in production. Like ``python3 -m agent_guest.control`` it serves until
SIGTERM; its first start-up hook installs the session manager with the test paths, then the discovered hooks run
(``reattach`` and the others), so a ``systemctl restart`` of the unit is the real restart of the control process.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any


def main(spec_file: str) -> int:
    spec: dict[str, Any] = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    sys.path.insert(0, spec["source"])
    from agent_guest import control, sessions, tmux  # noqa: PLC0415 - after the checkout is on the path
    from agent_guest.config import load_config  # noqa: PLC0415
    from agent_guest.driver import ClaudeCodeDriver  # noqa: PLC0415

    def install(context: control.ControlContext) -> None:
        manager = sessions.SessionManager(
            context=context,
            system=sessions.LinuxSystem(),
            tmux=tmux.Tmux(socket=spec["tmux_socket"]),
            driver=ClaudeCodeDriver(spec["claude_version"], binary=spec["claude"], mcp_config=spec["mcp_config"]),
            paths=sessions.Paths(**spec["paths"]),
            interpreter=(spec["interpreter"][0], spec["interpreter"][1]),
            exec_paths=tuple(spec["exec_paths"]),
        )
        sessions.install(context, manager)

    stop = threading.Event()
    service = control.ControlService(load_config(), startup=[("units_test_manager", install), *control.startup_hooks()])
    service.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    stop.wait()
    service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
