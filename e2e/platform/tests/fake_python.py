#!python
"""Stand-in for the Python of the hub and of the agent checkout in the orchestrator tests.

Switches come from `$HOME/fake.env` (`FAKE_<NAME>=value`); every call is logged to `$HOME/fake.log`.
`-m orchestrator.<module>` runs the real interpreter (the golden module of the suite).
"""

import hashlib
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOME = Path(os.environ["HOME"])
SWITCHES: dict[str, str] = {}
if (HOME / "fake.env").exists():
    for line in (HOME / "fake.env").read_text().splitlines():
        key, _, value = line.partition("=")
        SWITCHES[key] = value
ARGS = sys.argv[1:]


def switch(name: str, default: str = "") -> str:
    return SWITCHES.get(f"FAKE_{name}", default)


def note(text: str) -> None:
    with open(HOME / "fake.log", "a") as fh:
        fh.write(text + "\n")


def option(name: str) -> str:
    return ARGS[ARGS.index(name) + 1]


def serve_forever(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.05)


class Json(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def reply(self, status: int, payload: Any, text: bool = False) -> None:
        data = payload.encode() if text else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


note("python " + " ".join(ARGS))
module = ARGS[1] if ARGS[:1] == ["-m"] else ""

if module.startswith("orchestrator."):
    real = "@REAL@"
    os.execv(real, [real, *ARGS])

if module == "agent_hub.db" and ARGS[2:] == ["migrate"]:
    if switch("MIGRATE_FAIL"):
        print("migration refused", file=sys.stderr)
        sys.exit(1)
    assert os.environ["AGENT_HUB_OWNER_DATABASE"]
    assert os.environ["AGENT_HUB_DATABASE"]
    print("applied 1")
    sys.exit(0)

if module == "agent_hub" and ARGS[-1:] == ["--help"]:
    words = " ".join(ARGS[2:-1])
    sys.exit(0 if words in switch("HUB_COMMANDS").split(",") else 2)

if module == "agent_hub" and ARGS[2:] == ["serve"]:
    if switch("HUB_EXIT"):
        print("hub refuses to start")
        sys.exit(3)
    if switch("HUB_LEAK"):
        print("signing key " + os.environ["AGENT_HUB_SIGNING_KEY"], flush=True)
    downloads = os.environ.get("AGENT_HUB_DOWNLOADS_DIR", "")
    note(f"hub downloads={downloads} runner-area={Path(downloads, 'runner').is_dir()}")
    health = int(switch("HUB_HEALTH", "200"))
    healthy_calls = int(switch("HUB_HEALTHY_CALLS", "1000000"))
    calls = [0]

    class Hub(Json):
        def do_GET(self) -> None:
            calls[0] += 1
            status = health if calls[0] <= healthy_calls else 503
            self.reply(status if self.path == "/v1/health" else 404, {"ok": True})

    serve_forever(os.environ["AGENT_HUB_HOST"], int(os.environ["AGENT_HUB_PORT"]), Hub)

if module == "agent_hub" and ARGS[2:] == ["audit", "verify"]:
    print("audit chain " + ("broken" if switch("AUDIT_RC", "0") != "0" else "ok"))
    sys.exit(int(switch("AUDIT_RC", "0")))

if module == "agent_hub" and ARGS[2:3] == ["platform"]:
    print("platform " + " ".join(ARGS[3:]))
    sys.exit(0)

if module == "agent_doubles.model":
    if switch("DOUBLE_EXIT"):
        sys.exit(1)
    key = os.environ.get(option("--key-token-env"), "")
    if not key:
        sys.exit(2)  # the real double refuses an empty platform key (agent_doubles.model --key-token-env)
    note(f"model-key sha256={hashlib.sha256(key.encode()).hexdigest()} in-argv={any(key in a for a in ARGS)}")
    scenario = json.loads(Path(option("--scenario")).read_text())
    state: dict[str, Any] = {"scenario": scenario["name"], "requests": 0, "failures": [], "done": False, "ok": False}

    class Double(Json):
        def do_GET(self) -> None:
            if self.path == "/_double/state":
                self.reply(int(switch("DOUBLE_STATE_STATUS", "200")), {"double": "model", **state})
            else:
                self.reply(404, "no such route", text=True)

        def do_POST(self) -> None:
            body = self.body()
            if self.path == "/_double/scenario":
                state.update(scenario=body["scenario"]["name"], requests=0, failures=[], done=False, ok=False)
                self.reply(200, {"name": state["scenario"]})
            elif self.path == "/_double/reset":
                state.update(requests=0, failures=[], done=False, ok=False)
                self.reply(200, {})
            else:
                state["requests"] += 1
                if state["scenario"] == "idle":
                    state["failures"].append("unexpected request")
                else:
                    state.update(done=True, ok=not switch("DOUBLE_NOT_OK"))
                self.reply(200, {"content": [{"type": "text", "text": "smoke ok"}]})

    port = int(option("--port"))
    print(f"http://127.0.0.1:{port}", flush=True)
    serve_forever("127.0.0.1", port, Double)

if module == "agent_doubles.github":
    assert option("--public-url").startswith("http://127.0.0.1:")
    assert option("--html-url") == "https://github.example"

    class GitHub(Json):
        def do_GET(self) -> None:
            ok = not switch("GITHUB_NOT_OK")
            failures = [] if ok else ["unhandled route GET /nope"]
            self.reply(200, {"double": "github", "ok": ok, "done": True, "failures": failures, "requests": 0})

    port = int(option("--port"))
    print(f"http://127.0.0.1:{port}", flush=True)
    serve_forever("127.0.0.1", port, GitHub)

if module == "agent_runner" and ARGS[2:] == ["run"]:
    key_file = os.environ.get("AGENT_RUNNER_OPENROUTER_KEY_FILE")
    if key_file:
        note(f"runner-key sha256={hashlib.sha256(Path(key_file).read_text().strip().encode()).hexdigest()}")
    print('runner.migrate result="applied 1"')
    print("runner.run tasks=2 enrolled=no", flush=True)
    if switch("RUNNER_DIE"):
        time.sleep(0.3)
        sys.exit(1)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.05)

if module == "agent_runner" and ARGS[2:] == ["pool", "status"]:
    if switch("POOL_STATUS_FAIL"):
        print("runner.error code=store_unavailable", file=sys.stderr)
        sys.exit(1)
    print("runner.pool.vm vm_id=vm-00000001 lima_name=x golden_version=g created_at=t instance=Running")
    print(f"runner.pool size=2 ready={switch('POOL_READY', '2')} golden=g refill=full")
    sys.exit(0)

if module == "agent_runner" and ARGS[2:4] == ["image", "adopt"]:
    if switch("ADOPT_FAIL"):
        print("runner.error code=golden_missing name=x", file=sys.stderr)
        sys.exit(1)
    print(f"runner.image action=adopt version=v lima_name={ARGS[4]}")
    sys.exit(0)

if module == "agent_runner" and ARGS[2:3] == ["enroll"]:
    if switch("ENROLL_FAIL"):
        print("runner.error code=invite_unknown", file=sys.stderr)
        sys.exit(1)
    assert option("--code")
    assert option("--hub")
    print("runner.enroll state=pending_certification kind=platform device_id=0190c0de-0000-7000-8000-000000000001")
    sys.exit(0)

if module == "agent_runner" and ARGS[2:] == ["image", "--help"]:
    sys.exit(0 if switch("IMAGE") else 2)

if module == "agent_runner" and ARGS[2:4] == ["image", "build"]:
    if not switch("IMAGE_BUILD_FAIL"):
        with open(HOME / "lima.jsonl", "a") as fh:
            tag = ARGS[ARGS.index("--tag") + 1]
            fh.write(json.dumps({"name": f"cache-golden-{tag}", "status": "Stopped", "memory": 0}) + "\n")
    sys.exit(1 if switch("IMAGE_BUILD_FAIL") else 0)

if module == "agent_runner" and ARGS[2:5] == ["workvm", "image", "build"]:
    if not switch("WORKVM_BUILD_FAIL"):
        with open(HOME / "lima.jsonl", "a") as fh:
            fh.write(json.dumps({"name": f"cache-wimg-{option('--name')}", "status": "Stopped", "memory": 0}) + "\n")
    sys.exit(1 if switch("WORKVM_BUILD_FAIL") else 0)

sys.exit(9)
