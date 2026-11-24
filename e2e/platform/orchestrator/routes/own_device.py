"""E2E-56: the user's agent connects its own Mac with `agent-platform device add --this-computer` (ADR-074).

`POST /own-device/prepare` (`{"home": "<agent-tool computer>", "runner": "<runner instance>"}`):

- publishes a test release in the hub's download area (`<scratch>/downloads/runner/`): a small stand-in package and
  its `latest.json` (notarised, 90 days), signed with a fresh release key; the private key stays in the run's scratch;
- stops the runner instance (it is enrolled by the stand-in below and started by the spec);
- gives the agent-tool computer `home` the stand-in of the one administrator step (`AGENT_PLATFORM_DEVICE_ADMIN`), a
  layout root for the installation's public state (`AGENT_PLATFORM_DEVICE_ROOT`) and the test release key
  (`AGENT_PLATFORM_DEVICE_KEYS`), through `own-device.json` that `agent_tool._env` reads.

The test devices give no root, so only the administrator step is replaced: the stand-in gets the very arguments the
root script gets (package path, digest, request file), checks the package digest, and does what `postinstall` does
for the enrollment - it enrolls the runner instance with the code of the request file through `agent-runner enroll
--code-file` (the code never appears in an argv) - and writes `state.json` as the root helper does. For `remove` it
prints the helper's answer with empty leftovers and removes the state (the spec removes the runner instance). Answers
`{"key", "version", "package"}`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes import agent_tool

RELEASE = r"""
import hashlib, json, os, sys
from datetime import UTC, datetime, timedelta
from agent_runner.install import release
folder, version = sys.argv[1], sys.argv[2]
seed = os.urandom(32)
package = f"DFX-Agent-Runner-{version}.pkg"
data = b"E2E-56 stand-in package " + version.encode()
with open(os.path.join(folder, package), "wb") as handle:
    handle.write(data)
now = datetime.now(UTC).replace(microsecond=0)
feed = release.Feed(version, package, hashlib.sha256(data).hexdigest(), "0" * 64, "14.0", True,
                    now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"))
text = release.canonical(feed.document())
with open(os.path.join(folder, "latest.json"), "wb") as handle:
    handle.write(text)
with open(os.path.join(folder, "latest.json.sig"), "w") as handle:
    handle.write(release.sign(text, seed))
print(json.dumps({"key": release.public_key(seed), "package": package}))
"""
SEAM = r"""#!{python}
# Stand-in of the administrator step of `agent-platform device add|remove` (E2E-56, orchestrator/routes/own_device.py).
import hashlib, json, os, subprocess, sys, tempfile
from datetime import UTC, datetime
config = json.load(open({config!r}))
state = os.path.join(config["root"], "Library/Application Support/DFXAgentRunner/state.json")
mode = sys.argv[1]
if mode == "uninstall":
    if os.path.exists(state):
        os.unlink(state)
    print(json.dumps({{"ok": True, "removed": ["stand-in"], "leftovers": [], "notes": []}}))
    sys.exit(0)
package, digest, request = sys.argv[2:5]
with open(package, "rb") as handle:
    if hashlib.sha256(handle.read()).hexdigest() != digest:
        sys.exit("digest mismatch")
with open(request) as handle:
    code = handle.read().strip()
folder = tempfile.mkdtemp()
code_file = os.path.join(folder, "code")
with open(os.open(code_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
    handle.write(code)
done = subprocess.run(
    [config["python"], "-m", "agent_runner", "--json", "enroll", "--hub", config["hub"], "--code-file", code_file],
    env=config["env"], capture_output=True, text=True, timeout=120,
)
os.unlink(code_file)
os.rmdir(folder)
answer = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {{}}
enroll = next((fact for fact in answer.get("facts", []) if fact.get("fact") == "runner.enroll"), {{}})
fields = {{"v": 1, "version": config["version"], "owner": os.environ.get("USER", ""),
          "enrolled": done.returncode == 0, "device_id": enroll.get("device_id", ""),
          "enrollment": "enrolled" if done.returncode == 0 else "failed", "daemon": "stopped",
          "golden": "unknown", "disabled_in_login_items": False,
          "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}}
if done.returncode != 0:
    fields["enrollment_error"] = (answer.get("error") or {{}}).get("code") or "enroll"
os.makedirs(os.path.dirname(state), exist_ok=True)
with open(state, "w") as handle:
    json.dump(fields, handle)
"""
VERSION = "2026.11.24.0000"


def post_prepare(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    home = body.get("home") if isinstance(body, dict) else None
    name = body.get("runner") if isinstance(body, dict) else None
    runner = ctx.runners.get(name) if isinstance(name, str) else None
    if runner is None:
        raise RouteError(400, "invalid", 'body must be {"home": "<computer>", "runner": "<runner instance>"}')
    root = agent_tool._home(ctx, home)
    downloads = Path(ctx.scratch) / "downloads" / "runner"
    downloads.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(
        [ctx.agent_python, "-c", RELEASE, str(downloads), VERSION],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(ctx.agent_src) / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if made.returncode != 0:
        raise RouteError(500, "failed", f"release: {made.stderr.strip()[-300:]}")
    published = json.loads(made.stdout)
    runner.stop()
    device_root = root / "device-root"
    device_root.mkdir(exist_ok=True)
    config = root / "device-seam.json"
    config.write_text(
        json.dumps(
            {
                "python": runner.python,
                "env": runner.env,
                "hub": ctx.config.public_origin,
                "root": str(device_root),
                "version": VERSION,
            }
        )
    )
    config.chmod(0o600)
    seam = root / "tools" / "device-admin"
    seam.write_text(SEAM.format(python=ctx.agent_python, config=str(config)))
    seam.chmod(0o755)
    (root / "own-device.json").write_text(
        json.dumps(
            {
                "AGENT_PLATFORM_DEVICE_ADMIN": str(seam),
                "AGENT_PLATFORM_DEVICE_ROOT": str(device_root),
                "AGENT_PLATFORM_DEVICE_KEYS": published["key"],
            }
        )
    )
    return 200, {"key": published["key"], "version": VERSION, "package": published["package"]}


ROUTES = [Route("POST", "/own-device/prepare", post_prepare)]
