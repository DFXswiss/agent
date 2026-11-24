#!python
"""Stand-in for `web/node_modules/.bin/playwright` in the orchestrator tests: drives the control API like a spec.

Switches from `$HOME/fake.env`: `FAKE_PW_RC` (exit code), `FAKE_PW_MODEL=1` (load a scenario into the model double
and send it one request, so its state becomes ok), `FAKE_PW_IDLE_REQUEST=1` (a request without a scenario),
`FAKE_PW_SLEEP` (seconds before exiting).
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path(os.environ["HOME"])
SWITCHES: dict[str, str] = {}
if (HOME / "fake.env").exists():
    for line in (HOME / "fake.env").read_text().splitlines():
        key, _, value = line.partition("=")
        SWITCHES[key] = value
CONTROL = os.environ["E2E_CONTROL_URL"]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, path: str, body: object = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": "Bearer " + os.environ["E2E_CONTROL_TOKEN"]}
    request = urllib.request.Request(CONTROL + path, data=data, method=method, headers=headers)
    with OPENER.open(request, timeout=10) as response:
        return json.loads(response.read())


with open(HOME / "fake.log", "a") as fh:
    fh.write("playwright " + " ".join(sys.argv[1:]) + f" cwd={Path.cwd().name}\n")
state = call("GET", "/state")
output = Path(os.environ["E2E_ITERATION_DIR"]) / "playwright"
output.mkdir(parents=True, exist_ok=True)
(output / "results.json").write_text(json.dumps({"state": state}))
if SWITCHES.get("FAKE_PW_MODEL"):
    call("POST", "/doubles/model/scenario", {"scenario": {"name": "e2e-00-smoke"}})
    call("POST", "/doubles/model/request", {"method": "POST", "path": "/v1/messages", "body": {"messages": []}})
if SWITCHES.get("FAKE_PW_IDLE_REQUEST"):
    call("POST", "/doubles/model/request", {"method": "POST", "path": "/v1/messages", "body": {"messages": []}})
time.sleep(float(SWITCHES.get("FAKE_PW_SLEEP", "0")))
sys.exit(int(SWITCHES.get("FAKE_PW_RC", "0")))
