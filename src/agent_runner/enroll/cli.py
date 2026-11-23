"""`agent-runner enroll --hub URL --code CODE [--platform]` (05-runner.md sections 1.2 and 2.1).

`--platform` enrolls a platform device (`enroll.platform`). Without it the org-device enrollment of
`agent_runner.enroll.org` runs (RUN-06: `enroll(config, code, out) -> int`).

`--hub` sets the hub for this enrollment (it is validated like `AGENT_RUNNER_HUB_URL`: `https://`, loopback `http://`
only in test mode) and is recorded in `device.json`; `AGENT_RUNNER_PLATFORM_ROOT_KEY` is required. The code is never
printed. Output: `runner.enroll state=pending_certification kind=platform device_id=<id>`.
"""

from __future__ import annotations

import argparse
import importlib
from typing import Any

from agent_runner.config import PREFIX, load_config
from agent_runner.enroll import platform

ORG_MODULE = "agent_runner.enroll.org"
REQUIRED = frozenset({"HUB_URL", "PLATFORM_ROOT_KEY"})


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("enroll", help="enroll this device with an invitation code")
    parser.add_argument("--hub", required=True, help="hub URL (https://host)")
    parser.add_argument("--code", required=True, help="invitation code")
    parser.add_argument("--platform", action="store_true", help="enroll as a platform device (operator invitation)")
    parser.set_defaults(handler=enroll)


def enroll(args: argparse.Namespace, ctx: Any) -> int:
    env = {**ctx.host.env, PREFIX + "HUB_URL": args.hub}
    config = load_config(env, platform=ctx.host.platform, require=REQUIRED)
    if not args.platform:
        org: Any = importlib.import_module(ORG_MODULE)
        return int(org.enroll(config, args.code, ctx.out))
    device = platform.run_enroll(config, args.code, platform.default_options())
    ctx.out.fact("enroll", {"state": platform.PENDING, "kind": device.kind, "device_id": device.device_id})
    return 0
