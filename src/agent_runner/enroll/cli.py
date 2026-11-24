"""`agent-runner enroll --hub URL --code CODE [--platform]` (05-runner.md sections 1.2 and 2.1).

`--platform` enrolls a platform device (`enroll.platform`). Without it the org-device enrollment of
`agent_runner.enroll.org` runs (RUN-06: `enroll(config, code, out) -> int`).

`--code-file PATH` reads the code from a file instead, `--code-file -` from standard input (the installation helper of
an own device hands it over there, so it never appears in an argv or a file, ADR-074); exactly one of the two is
required.

`--hub` sets the hub for this enrollment (it is validated like `AGENT_RUNNER_HUB_URL`: `https://`, loopback `http://`
only in test mode) and is recorded in `device.json`; `AGENT_RUNNER_PLATFORM_ROOT_KEY` is required. The code is never
printed. Output: `runner.enroll state=pending_certification kind=platform device_id=<id>`.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any, TextIO

from agent_runner.config import PREFIX, load_config
from agent_runner.enroll import platform
from agent_runner.errors import RunnerError

ORG_MODULE = "agent_runner.enroll.org"
REQUIRED = frozenset({"HUB_URL", "PLATFORM_ROOT_KEY"})
CODE_FILE_MAX = 4096


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("enroll", help="enroll this device with an invitation code")
    parser.add_argument("--hub", required=True, help="hub URL (https://host)")
    codes = parser.add_mutually_exclusive_group(required=True)
    codes.add_argument("--code", help="invitation code")
    codes.add_argument("--code-file", help="file with the invitation code, `-` for standard input (at most 4 KiB)")
    parser.add_argument("--platform", action="store_true", help="enroll as a platform device (operator invitation)")
    parser.set_defaults(handler=enroll)


def read_code(path: str, stdin: TextIO | None = None) -> str:
    """The code in `path`, or on standard input for `-` (whitespace stripped); `RunnerError("invite_code_invalid")`
    when it cannot be read."""
    try:
        if path == "-":
            text = (stdin or sys.stdin).read(CODE_FILE_MAX + 1)
        else:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(CODE_FILE_MAX + 1)
    except (OSError, UnicodeDecodeError):
        raise RunnerError("invite_code_invalid") from None
    if len(text) > CODE_FILE_MAX or not text.strip():
        raise RunnerError("invite_code_invalid")
    return text.strip()


def enroll(args: argparse.Namespace, ctx: Any) -> int:
    env = {**ctx.host.env, PREFIX + "HUB_URL": args.hub}
    config = load_config(env, platform=ctx.host.platform, require=REQUIRED)
    code = args.code if args.code is not None else read_code(args.code_file)
    if not args.platform:
        org: Any = importlib.import_module(ORG_MODULE)
        return int(org.enroll(config, code, ctx.out))
    device = platform.run_enroll(config, code, platform.default_options())
    ctx.out.fact("enroll", {"state": platform.PENDING, "kind": device.kind, "device_id": device.device_id})
    return 0
