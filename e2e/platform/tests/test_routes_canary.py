"""routes/canary.py (E2E-09): canaries per run, the hub-side scan (database dump, logs, both links in both directions,
exports), the positive control on the runner device and the column report. Real devices, the real scanner command
line (run on the "runner device" with its own checkout), fake PostgreSQL tools and a fake hub Python."""

from __future__ import annotations

import json
import stat
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from checks import hub_columns
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.device import Device
from orchestrator.node import LocalNode, Node
from orchestrator.relay import BROWSER_TAP, RUNNER_TAP
from orchestrator.routes import canary
from orchestrator.services import Cluster, Hub, Runner
from support import CORE, free_base

ROUTES = discover()

PG_DUMP = """#!/bin/sh
out=""; db=""
while [ $# -gt 0 ]; do
  case "$1" in -f) out="$2"; shift 2;; -d) db="$2"; shift 2;; -*) shift 2;; *) db="$1"; shift;; esac
done
[ -e "$(dirname "$0")/fail-$db" ] && { echo "dump broken" >&2; exit 3; }
cp "$(dirname "$0")/$db.sql" "$out"
"""
PSQL = """#!/bin/sh
for last; do :; done
case "$last" in
  *"INSERT INTO e2e_probe.marker"*) echo "$last" >> "$(dirname "$0")/agent_hub.sql";;
  *'SELECT 1 FROM "zk-probe-'*) echo "ERROR: $last" >> "$FAKE_LOGS/postgres.log"; exit 1;;
  *information_schema.columns*) cat "$FAKE_DIR/columns.json";;
  *"FROM hub.org"*) cat "$FAKE_DIR/orgs.json";;
  *"UNION ALL"*|*" AS bad"*) cat "$FAKE_DIR/bad.json";;
  *) echo "unexpected $last" >&2; exit 1;;
esac
"""
HUB_PYTHON = """#!/bin/sh
# python -m agent_hub export (--help | --org <id> --out <path>)
[ "$3" = "export" ] || exit 1
[ -n "$FAKE_EXPORT" ] || exit 2
[ "$4" = "--help" ] && exit 0
[ "$FAKE_EXPORT" = "fail" ] && exit 4
printf '{"org":"%s","text":"%s"}\\n' "$5" "$FAKE_EXPORT" > "$7"
"""


def script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Fake:
    """A T1 run (one local device) with a fake cluster, a fake hub and one runner home."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "pgbin"
        self.bin.mkdir()
        script(self.bin / "pg_dump", PG_DUMP)
        script(self.bin / "psql", PSQL)
        (self.bin / "agent_hub.sql").write_text("COPY hub.event (ct) FROM stdin;\n\\\\xdeadbeef\n", encoding="utf-8")
        (self.bin / "agent_runner.sql").write_text("", encoding="utf-8")
        self.data = tmp_path / "fake"
        self.data.mkdir()
        self.write("columns.json", [{"table": "event", "column": "kind", "type": "text"}])
        self.write("orgs.json", ["01a2038c-dc32-7fa4-8d67-de6e8ec39d0d"])
        self.write("bad.json", [{"table": "event", "column": "kind", "bad": 0, "sample": None}])
        self.run_dir = tmp_path / "hub-device"
        self.node = LocalNode("hub-device", Device(self.run_dir))
        config = RunConfig(
            topology="t1",
            core_src=CORE,
            agent_src=tmp_path,
            artifacts=tmp_path / "a",
            port_base=free_base(),
            vm_prefix="w-",
        )
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        self.ctx = Context(config, self.node, self.node, sys.executable, scratch=str(self.scratch))
        self.ctx.core_on_runner = str(CORE)
        self.ctx.runner_run_dir = str(self.run_dir)
        self.ctx.runner_scratch = str(self.scratch)
        self.hub_env = {"PATH": "/usr/bin:/bin"}
        self.ctx.hub = Hub(self.node, str(script(tmp_path / "hubpy", HUB_PYTHON)), self.hub_env, "127.0.0.1", 1)
        logs = str(self.run_dir / "logs")
        self.ctx.cluster = Cluster(
            self.node, str(self.bin), "/data", 1, {"FAKE_DIR": str(self.data), "FAKE_LOGS": logs}
        )
        home = tmp_path / "runner-1"
        (home / "pg").mkdir(parents=True)
        (home / "pg" / "port").write_text("21099\n", encoding="utf-8")
        self.servers: list[ThreadingHTTPServer] = []
        self.ctx.runners["runner-1"] = Runner(self.node, "runner-1", sys.executable, {"AGENT_RUNNER_HOME": str(home)})

    def write(self, name: str, value: Any) -> None:
        (self.data / name).write_text(json.dumps(value), encoding="utf-8")

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return dispatch(ROUTES, self.ctx, method, path, body)


def stand_in(port: int, log: Path, tap: Path) -> ThreadingHTTPServer:
    """The hub behind a relay: answers 404, logs the request line like uvicorn and records the request in the tap."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f'INFO:     127.0.0.1:1 - "GET {self.path} HTTP/1.1" 404 Not Found\n')
            tap.mkdir(exist_ok=True)
            (tap / "00099.up").write_bytes(f"GET {self.path} HTTP/1.1\r\nHost: hub\r\n\r\n".encode())
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    made = Fake(tmp_path)
    monkeypatch.setattr(canary, "find_pg_bin", lambda node: str(made.bin))
    config = made.ctx.config
    log = made.run_dir / "logs" / "hub.log"
    servers = [
        stand_in(config.port("browser"), log, made.scratch / BROWSER_TAP),
        stand_in(config.port("public"), log, made.scratch / RUNNER_TAP),
    ]
    made.servers = servers
    yield made
    for server in servers:
        server.shutdown()
        server.server_close()


def test_new_canaries_are_random_pairs_and_given_values(fake: Fake) -> None:
    status, made = fake.call("POST", "/canary/new", {"labels": ["prompt", "wg-name"]})
    assert status == 200
    assert made["prompt"]["ascii"].startswith("zk-prompt-")
    unicode = made["prompt"]["unicode"]
    assert unicode.startswith("Zκ-Ärger-日本語-prompt-")
    assert unicode.endswith("-😀")
    assert made["prompt"]["ascii"][-12:] == unicode.split("-")[-2]
    assert made["prompt"] != fake.call("POST", "/canary/new", {"labels": ["prompt2"]})[1]["prompt2"]
    given = {"invite-code": "ABCD-EFGH-IJKL"}
    assert fake.call("POST", "/canary/new", {"values": given}) == (200, {"invite-code": {"value": "ABCD-EFGH-IJKL"}})
    stored = json.loads((fake.scratch / canary.STORE).read_text(encoding="utf-8"))
    assert set(stored) == {"prompt", "wg-name", "prompt2", "invite-code"}
    assert fake.call("POST", "/canary/new", {"labels": ["prompt"]})[0] == 409
    refused: list[Any] = [None, {}, {"labels": "x"}, {"labels": ["Bad Label"]}, {"labels": ["a", "a"]}, {"values": []}]
    for bad in refused:
        assert fake.call("POST", "/canary/new", bad)[0] == 400, bad
    assert fake.call("POST", "/canary/new", {"values": {"short": "abc"}})[0] == 400


def test_scan_needs_canaries_and_the_hub(fake: Fake) -> None:
    assert fake.call("POST", "/canary/scan")[0] == 409
    assert fake.call("POST", "/canary/runner")[0] == 409
    fake.call("POST", "/canary/new", {"labels": ["prompt"]})
    fake.ctx.cluster = None
    assert fake.call("POST", "/canary/scan")[0] == 409
    assert fake.call("GET", "/canary/columns")[0] == 409


def tap(directory: Path, name: str, data: bytes) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(data)


def test_scan_finds_canaries_in_every_hub_source(fake: Fake) -> None:
    made = fake.call("POST", "/canary/new", {"labels": ["prompt", "answer"], "values": {"code": "-INVITE-CODE-1"}})[1]
    prompt, answer = made["prompt"], made["answer"]
    # nothing leaked: every source is scanned and counted, no export yet
    logs = fake.run_dir / "logs"
    (logs / "hub.log").write_text("GET /v1/health 200\n", encoding="utf-8")
    (logs / "hub.1.log").write_text(f"earlier hub process: {prompt['unicode']}\n", encoding="utf-8")
    (logs / "postgres.log").write_text("ready\n", encoding="utf-8")
    (logs / "playwright.log").write_text(f"the browser may show {answer['ascii']}\n", encoding="utf-8")
    browser = fake.scratch / BROWSER_TAP
    tap(browser, "00001.down", b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\nc\r\nevent: hello\n\r\n")
    tap(browser, "00001.up", b"POST /v1/events HTTP/1.1\r\ncontent-length: 14\r\n\r\n-INVITE-CODE-1")
    runner = fake.scratch / RUNNER_TAP
    switched = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
    text = json.dumps({"hint": answer["unicode"]}).encode()
    tap(runner, "00001.down", switched + bytes([0x81, len(text)]) + text)
    status, report = fake.call("POST", "/canary/scan")
    assert status == 200
    assert report["findings"] == [
        {"source": "logs/hub.1.log", "label": "prompt/unicode", "encoding": "raw"},
        {"source": "logs/hub.1.log", "label": "prompt/token", "encoding": "raw"},
        {"source": "browser-link/00001.up", "label": "code", "encoding": "raw"},
        {"source": "runner-link/00001.down", "label": "answer/unicode", "encoding": "json"},
        {"source": "runner-link/00001.down", "label": "answer/token", "encoding": "raw"},
    ]
    # the probe went through the dump, both logs and both links' requests; it is no finding
    assert report["probe"] == [
        "browser-link/00099.up",
        "hub-db-dump",
        "logs/hub.log",
        "logs/postgres.log",
        "runner-link/00099.up",
    ]
    assert report["ws_failures"] == 0
    assert set(report["sources"]) == {
        "hub-db-dump",
        "logs/hub.1.log",
        "logs/hub.log",
        "logs/postgres.log",
        "browser-link/00001.down",
        "browser-link/00001.up",
        "browser-link/00099.up",
        "runner-link/00001.down",
        "runner-link/00099.up",
    }
    assert (report["sse_frames"], report["ws_messages"]) == (1, 1)
    assert report["notes"] == ["exports: agent-hub export is not available yet (DR-01); nothing to scan"]
    assert not (fake.scratch / canary.DUMP_NAME).exists()
    assert prompt["ascii"] not in json.dumps(report)


def test_scan_without_taps_and_with_exports(fake: Fake) -> None:
    made = fake.call("POST", "/canary/new", {"labels": ["org-name"]})[1]
    fake.hub_env["FAKE_EXPORT"] = made["org-name"]["ascii"]
    status, report = fake.call("POST", "/canary/scan")
    assert status == 200
    org = "01a2038c-dc32-7fa4-8d67-de6e8ec39d0d"
    assert report["findings"] == [
        {"source": f"export/{org}", "label": "org-name/ascii", "encoding": "raw"},
        {"source": f"export/{org}", "label": "org-name/token", "encoding": "raw"},
    ]
    assert report["notes"] == ["exports: 1 organization(s)"]
    assert set(report["sources"]) == {
        "hub-db-dump",
        "logs/hub.log",
        "logs/postgres.log",
        f"export/{org}",
        "browser-link/00099.up",
        "runner-link/00099.up",
    }
    assert list(fake.scratch.glob(f"{canary.EXPORT_PREFIX}*")) == []
    fake.hub_env["FAKE_EXPORT"] = "fail"
    status, answer = fake.call("POST", "/canary/scan")
    assert (status, answer["error"]) == (500, "failed")


def test_scan_fails_when_the_runner_side_scan_fails(fake: Fake) -> None:
    fake.call("POST", "/canary/new", {"labels": ["prompt"]})
    fake.ctx.core_on_runner = str(fake.tmp)  # no checks package there
    status, answer = fake.call("POST", "/canary/scan")
    assert (status, answer["error"]) == (500, "failed")
    assert "scan on hub-device exited" in answer["detail"]


def test_scan_refuses_a_hub_on_a_remote_device(fake: Fake) -> None:
    fake.call("POST", "/canary/new", {"labels": ["prompt"]})

    class Remote(Node):
        name = "remote"

        def call(self, op: str, /, **args: Any) -> Any:
            raise AssertionError(op)

    fake.ctx.hub_node = Remote()
    assert fake.call("POST", "/canary/scan")[0] == 409


def test_runner_control_finds_canaries_on_the_runner_device(fake: Fake) -> None:
    made = fake.call("POST", "/canary/new", {"labels": ["prompt", "file", "secret"]})[1]
    # the same canary twice in one source (JSON-escaped and raw): one source entry
    escaped = json.dumps({"text": made["prompt"]["unicode"]})
    (fake.run_dir / "model-journal.jsonl").write_text(f"{escaped}\n{made['prompt']['unicode']}\n", encoding="utf-8")
    (fake.run_dir / "github-journal.jsonl").write_text(made["file"]["ascii"] + "\n" + made["prompt"]["ascii"])
    logs = fake.run_dir / "logs"
    (logs / "runner-1.log").write_text(f"runner {made['file']['ascii']}\n", encoding="utf-8")
    (logs / "hub.log").write_text(f"hub {made['secret']['ascii']}\n", encoding="utf-8")  # not the runner side
    (fake.bin / "agent_runner.sql").write_text(f"store {made['prompt']['ascii']}\n", encoding="utf-8")
    status, report = fake.call("POST", "/canary/runner")
    assert status == 200
    assert report["present"] == {
        "prompt/unicode": ["journal/model"],
        "prompt/token": ["journal/model", "journal/github", "store/runner-1"],
        "file/ascii": ["journal/github", "logs/runner-1.log"],
        "file/token": ["journal/github", "logs/runner-1.log"],
        "prompt/ascii": ["journal/github", "store/runner-1"],
    }
    assert set(report["sources"]) == {"journal/model", "journal/github", "logs/runner-1.log", "store/runner-1"}
    assert not (fake.scratch / "runner-1-store.sql").exists()


def test_runner_control_without_runners_and_with_a_broken_store(fake: Fake) -> None:
    fake.call("POST", "/canary/new", {"labels": ["prompt"]})
    (fake.run_dir / "model-journal.jsonl").write_text("{}\n")
    (fake.run_dir / "github-journal.jsonl").write_text("{}\n")
    (fake.bin / "fail-agent_runner").write_text("")
    status, answer = fake.call("POST", "/canary/runner")
    assert (status, answer["error"]) == (500, "failed")
    assert "runner-1 store dump exited with 3" in answer["detail"]
    fake.ctx.runners.clear()
    assert fake.call("POST", "/canary/runner") == (
        200,
        {"present": {}, "sources": {"journal/model": 3, "journal/github": 3}},
    )


def test_columns_report(fake: Fake) -> None:
    status, report = fake.call("GET", "/canary/columns")
    assert status == 200
    assert report["problems"][0] == "agent.bundle_ct: mapped but not in the schema"  # sorted
    assert report["columns"] == [
        {
            "table": "event",
            "column": "kind",
            "type": "text",
            "category": "events",
            "section7": hub_columns.CATEGORIES["events"],
            "content": "code",
            "values_checked": True,
        }
    ]
    fake.write("columns.json", [{"table": "event", "column": "id", "type": "uuid"}])
    assert fake.call("GET", "/canary/columns")[1]["columns"][0]["values_checked"] is False


def test_a_truncated_or_recased_copy_is_found_by_the_random_part(fake: Fake) -> None:
    made = fake.call("POST", "/canary/new", {"labels": ["title"]})[1]["title"]
    preview = made["unicode"][:-2]  # the emoji and its dash cut off
    (fake.run_dir / "logs" / "hub.log").write_text(f"{preview.upper()}\n", encoding="utf-8")
    findings = fake.call("POST", "/canary/scan")[1]["findings"]
    assert findings == [{"source": "logs/hub.log", "label": "title/token", "encoding": "raw"}]


def test_the_probe_fails_loudly_when_the_hub_is_unreachable(fake: Fake) -> None:
    fake.call("POST", "/canary/new", {"labels": ["prompt"]})
    for server in fake.servers:
        server.shutdown()
        server.server_close()
    status, answer = fake.call("POST", "/canary/scan")
    assert (status, answer["error"]) == (500, "failed")
