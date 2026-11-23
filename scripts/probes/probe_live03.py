"""Probe LIVE-03 (first run in M0): a Claude subscription access token through the guest model gateway, and one GET
on the usage endpoint (PRB-01; 07-testing.md sections 2.4 and 6; 05-runner.md sections 10.2 and 10.6; ADR-010).

Never part of `make platform-ci`. The subscription login is only ever read:

* the token is the **current access token** of an operator-chosen profile, read with
  `security find-generic-password -s <service> -a <user> -w` (no refresh, login or logout; no keychain or file
  change; the refresh token is never extracted). A token that expires within `--min-valid` seconds is refused;
* it travels only in memory: printed by `token` to a pipe (refused on a terminal), read by `run` from standard
  input, sent to the guest with `PUT .../credential` and to the usage endpoint; it is never written to a file,
  an argument or the environment;
* `snapshot` before and after (content digests of every profile file, keychain attributes without the secret) and
  `compare` prove that nothing changed; `scan` checks every artefact for the token afterwards;
* the snapshot sees the keychain item's attributes, not its access list: the `-w` read must run without an access
  prompt (as the legacy usage polling does); if macOS ever asks, deny it and stop - "Always Allow" would change the
  item unseen;
* profiles named with `--exclude` or in `AGENT_LIVE_EXCLUDED_PROFILES` (comma list) are refused by every subcommand
  (compared after resolving the path).

`run` exits 0 when the turn passed (prompt accepted, a gateway request of the model with status 200), 2 when it did
not, 1 when an artefact holds the token; the usage endpoint's answer is reported, not judged (05 §10.6).

Subcommands:

    probe_live03.py check    --profile DIR [--min-valid S]           # operator machine: expiry, no token output
    probe_live03.py snapshot --profile DIR --out FILE                # operator machine
    probe_live03.py token    --profile DIR [--min-valid S] | ...     # operator machine, pipe only
    ... | probe_live03.py run --golden G --prefix P --out DIR        # runner device, token on standard input
    probe_live03.py compare  BEFORE AFTER                            # exit 1 on any difference
    probe_live03.py scan     --profile DIR PATH...                   # exit 1 when an artefact holds the token

`run` clones a VM from the golden (clone path of the runner, without model double), creates a session with access
`subscription` and model `claude-haiku-5-5` (Haiku class), provisions the token as credential (upstream
`https://api.anthropic.com`, `Authorization: Bearer`, the gateway adds `anthropic-beta: oauth-2025-04-20`) and
waits for the gateway's usage records of the mini turn; then it sends one `GET` to the usage endpoint
(`Authorization: Bearer`, `anthropic-beta: oauth-2025-04-20`, 8 s, no redirects) and keeps only the status and the
key names of the answer. The session is stopped and the VM deleted before the script ends.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from probehost import ORG, Watcher, credential, emit, runner_vms, session_request, uuid7
from probelib.live03 import (
    AccessToken,
    TokenError,
    access_token,
    compare_snapshots,
    is_excluded,
    keychain_service,
    redact,
    scan_files,
    snapshot,
    token_findings,
    turn_problems,
    usage_shape,
)

SECURITY = "/usr/bin/security"
MISSING_ITEM = 44
UPSTREAM = "https://api.anthropic.com"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
MODEL = "claude-haiku-5-5"
# model-catalog.example.json access.subscription.allowed with the catalog context windows.
SUBSCRIPTION_MODELS = (
    ("claude-opus-5-5", 1_000_000),
    ("claude-sonnet-5-5", 1_000_000),
    ("claude-haiku-5-5", 1_000_000),
    ("claude-fable-5-2", 1_000_000),
)
PROMPT = "This is a connectivity check. Reply with exactly one word: pong"
TURN_TIMEOUT_S = 120.0
TAIL_S = 5.0


EXCLUDED_ENV = "AGENT_LIVE_EXCLUDED_PROFILES"
# The scan must find the token that was sent even after it expired (never refreshed in between: the snapshots prove it).
ANY_EXPIRY_S = -(10**9)


def _profile(args: argparse.Namespace) -> Path:
    """The profile directory, refused when excluded (`--exclude` or the comma list in AGENT_LIVE_EXCLUDED_PROFILES)."""
    excluded = [*args.exclude, *os.environ.get(EXCLUDED_ENV, "").split(",")]
    if is_excluded(args.profile, excluded):
        raise SystemExit("this profile is excluded from live tests")
    path: Path = args.profile.resolve()
    return path


def _user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def _keychain(profile: Path, *, secret: bool) -> str:
    argv = [SECURITY, "find-generic-password", "-s", keychain_service(str(profile)), "-a", _user()]
    done = subprocess.run([*argv, "-w"] if secret else argv, capture_output=True, text=True, timeout=15, check=False)
    if done.returncode == MISSING_ITEM:
        raise SystemExit("no keychain entry for this profile")
    if done.returncode != 0:
        raise SystemExit(f"keychain read failed (exit {done.returncode})")
    # `security` prints the attributes on standard error unless -w is given.
    return done.stdout if secret else done.stdout + done.stderr


def _token(profile: Path, min_valid: int) -> AccessToken:
    try:
        return access_token(_keychain(profile, secret=True), now_ms=int(time.time() * 1000), min_valid_s=min_valid)
    except TokenError as exc:
        raise SystemExit(str(exc)) from None


def cmd_check(args: argparse.Namespace) -> int:
    found = _token(_profile(args), args.min_valid)
    left = (found.expires_at_ms - int(time.time() * 1000)) // 1000
    print(json.dumps({"expires_in_s": left, "subscription": found.subscription}))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    profile = _profile(args)
    taken = snapshot(profile, _keychain(profile, secret=False))
    taken["taken_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.write_text(json.dumps(taken, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    files = taken["files"]
    print(
        json.dumps({"files": len(files) if isinstance(files, dict) else 0, "keychain_sha256": taken["keychain_sha256"]})
    )
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    if sys.stdout.isatty():
        raise SystemExit("refusing to print the token on a terminal; pipe it into `run`")
    sys.stdout.write(_token(_profile(args), args.min_valid).token + "\n")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    problems = compare_snapshots(before, after)
    print(json.dumps({"unchanged": not problems, "differences": problems}, indent=1))
    return 1 if problems else 0


def cmd_scan(args: argparse.Namespace) -> int:
    token = _token(_profile(args), ANY_EXPIRY_S).token
    paths = [p for root in args.paths for p in ([root] if root.is_file() else sorted(root.rglob("*")))]
    problems = scan_files(paths, token)
    print(json.dumps({"files": len([p for p in paths if p.is_file()]), "findings": problems}, indent=1))
    return 1 if problems else 0


async def live_run(args: argparse.Namespace, token: str) -> dict[str, Any]:
    out: dict[str, Any] = {"model": MODEL, "upstream": UPSTREAM}
    async with runner_vms(args.golden, args.prefix, pool_size=0) as runner:
        wg = uuid7()
        row = await runner.manager.ensure_vm(ORG, wg)
        sid = uuid7()
        try:
            async with await runner.manager.client(row) as client, await runner.manager.client(row) as follower:
                head = (await client.journal(0, limit=1, wait=0)).head
                watcher = Watcher(follower, sid, runner.marks, head, need_answer=False)
                runner.marks.begin()
                task = asyncio.create_task(watcher.run())
                await client.create_session(session_request(sid, PROMPT, access="subscription", model_id=MODEL, env={}))
                body = credential(UPSTREAM, "Bearer " + token, access="subscription", models=SUBSCRIPTION_MODELS)
                await client.put_credential(sid, body)
                await wait_turn(watcher, runner.marks.values)
                await asyncio.sleep(TAIL_S)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.control(sid, "stop")
        finally:
            await runner.manager.delete_workgroup(wg)
        out["prompt_accepted"] = "prompt_accepted" in runner.marks.values
        out["answered_s"] = runner.marks.values.get("answered")
        out["failure"] = watcher.failure
        out["hooks"] = list(dict.fromkeys(watcher.hooks))
        out["gateway_requests"] = [
            {key: item.get(key) for key in ("model", "status", "input_tokens", "output_tokens", "cache_read_tokens")}
            for item in watcher.usage
        ]
    out["usage_endpoint"] = usage_endpoint(token)
    return out


async def wait_turn(watcher: Watcher, marks: dict[str, float]) -> None:
    """Until the prompt was accepted and the gateway recorded a request of the session's model (any status)."""
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        if watcher.failure is not None:
            return
        if "prompt_accepted" in marks and any(item.get("model") == MODEL for item in watcher.usage):
            return
        await asyncio.sleep(0.2)
    watcher.failure = "timeout"


def usage_endpoint(token: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False) as client:
            answer = client.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": type(exc).__name__}
    return usage_shape(answer.status_code, answer.content)


def cmd_run(args: argparse.Namespace) -> int:
    token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("no token on standard input")
    try:
        result = asyncio.run(live_run(args, token))
    except BaseException as exc:
        # Never let a token reach the terminal through an error text.
        raise SystemExit(redact(f"run failed: {type(exc).__name__}: {exc}", token)) from None
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / "live03.json"
    target.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    findings = scan_files([target], token)
    text = json.dumps(result, sort_keys=True)
    problems = turn_problems(result, MODEL)
    emit(
        "live03", **result, passed=not problems, problems=problems,
        scan=findings + [f"stdout: {item}" for item in token_findings(text, token)],
    )  # fmt: skip
    return 1 if findings else (2 if problems else 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    profile_commands = []
    for name in ("check", "token"):
        p = sub.add_parser(name)
        p.add_argument("--min-valid", type=int, default=1800)
        profile_commands.append(p)
    p = sub.add_parser("snapshot")
    p.add_argument("--out", type=Path, required=True)
    profile_commands.append(p)
    p = sub.add_parser("compare")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    p = sub.add_parser("scan")
    p.add_argument("paths", type=Path, nargs="+")
    profile_commands.append(p)
    for p in profile_commands:
        p.add_argument("--profile", type=Path, required=True)
        p.add_argument("--exclude", action="append", default=[], help="profile directory name never to use")
    p = sub.add_parser("run")
    p.add_argument("--golden", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--out", type=Path, default=Path("probe-out"))
    args = parser.parse_args()
    handlers = {
        "check": cmd_check,
        "snapshot": cmd_snapshot,
        "token": cmd_token,
        "compare": cmd_compare,
        "scan": cmd_scan,
        "run": cmd_run,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
