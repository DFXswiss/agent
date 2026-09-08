"""Native adapter probes against a fake provider, with no real model or credentials.

The static validation script must explicitly supply AGENT_TEST_NATIVE_LANES as
a JSON file mapping grok/codex to binary and sha256. Without that test-only
manifest these optional installed-CLI probes skip; ordinary unit tests remain
mandatory. CI does not install a vendor CLI or select an account implicitly.
"""
import json
import shlex
import http.server
import os
import threading
from pathlib import Path

import pytest

from agent_cli.ai_accounts import AIAccount, AIRole
from agent_cli.lane_protocol import ProtocolError
from agent_cli.lane_text import TextCLI

pytestmark = pytest.mark.no_pg


def reply(handler, request, tool_name, marker, state):
    # Grok's automatic title request is separate from its work request.
    choice = request.get("tool_choice")
    title = isinstance(choice, dict) and choice.get("name") == "session_title"
    if title:
        item = {"type": "function_call", "id": "fc_title", "call_id": "call_title",
                "name": "session_title", "arguments": '{"session_title":"Static boundary probe"}', "status": "completed"}
    elif not state.get("injected"):
        state["injected"] = True
        command = "printf probe > " + shlex.quote(str(marker))
        args = {"command": command, "description": "Static probe sentinel only", "timeout": 1000}
        if tool_name == "exec_command":
            args = {"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 100}
        item = {"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                "name": tool_name, "arguments": json.dumps(args), "status": "completed"}
        if tool_name.startswith("custom:"):
            name = tool_name.split(":", 1)[1]
            code = ("*** Begin Patch\n*** Add File: " + str(marker) + "\n+probe\n*** End Patch"
                    if name == "apply_patch" else
                    'const result = await tools.exec_command(' + json.dumps({"cmd": command}) + '); text(result);')
            item = {"type": "custom_tool_call", "id": "ct_probe", "call_id": "call_probe",
                    "name": name, "input": code, "status": "completed"}
    else:
        # Capture only the deterministic fake call's result, never prompt/credentials.
        state["returned_items"] = [x for x in request.get("input", [])
                                   if isinstance(x, dict) and x.get("type") in ("function_call_output", "custom_tool_call_output")]
        item = {"type": "message", "id": "msg_probe", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps({"request": {"action": "finish", "text": "STATUS: complete\nVERDICT: approved"}}), "annotations": []}]}
    response = {"id": "resp_probe", "object": "response", "created_at": 1,
                "model": "static-probe", "status": "completed", "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "status": "in_progress", **({"arguments": ""} if item["type"] == "function_call" else {})}},
    ]
    if item["type"] == "function_call":
        events.append({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                       "output_index": 0, "delta": item["arguments"]})
        events.append({"type": "response.function_call_arguments.done", "item_id": item["id"],
                       "output_index": 0, "arguments": item["arguments"]})
    elif item["type"] == "message":
        events.append({"type": "response.output_text.delta", "item_id": item["id"],
                       "output_index": 0, "content_index": 0, "delta": item["content"][0]["text"]})
    events += [{"type": "response.output_item.done", "output_index": 0, "item": item},
               {"type": "response.completed", "response": response}]
    body = "".join("event: " + e["type"] + "\ndata: " + json.dumps({**e, "sequence_number": n}) + "\n\n"
                   for n, e in enumerate(events)).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.mark.parametrize("vendor,tool,positive", [
    ("codex", "exec_command", False), ("codex", "spawn_agent", False),
    ("codex", "write_stdin", False), ("codex", "wait_agent", False),
    ("codex", "view_image", False), ("codex", "sleep", False),
    ("codex", "custom:exec", False), ("codex", "custom:apply_patch", False),
    ("codex", "exec_command", True),
    ("grok", "run_terminal_command", False), ("grok", "read_file", False),
    ("grok", "write", False), ("grok", "monitor", False),
    ("grok", "scheduler_create", False), ("grok", "workflow", False),
    ("grok", "use_tool", False), ("grok", "task", False),
    ("grok", "run_terminal_command", True),
])
def test_native_model_tool_boundary(tmp_path, vendor, tool, positive):
    manifest = os.environ.get("AGENT_TEST_NATIVE_LANES")
    if not manifest:
        pytest.skip("installed native CLI probes require an explicitly supplied test manifest")
    config = json.loads(Path(manifest).read_text())[vendor]
    requests, state, active = [], {}, {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 1_000_000:
                self.send_error(400)
                return
            data = json.loads(self.rfile.read(length))
            requests.append({"tools": data.get("tools") or [],
                             "long_marker": "PROBE_LONG_END" in json.dumps(data.get("input")),
                             "secret_leak": "FORBIDDEN_PROBE_SECRET_CONTENT" in json.dumps(data.get("input"))})
            reply(self, data, tool, active["marker"], state)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        auth = tmp_path / "auth.json"
        auth.write_text("{}")
        auth.chmod(0o600)
        outside = tmp_path / "outside-source.txt"
        outside.write_text("FORBIDDEN_PROBE_SECRET_CONTENT")
        model = "gpt-5.6-sol" if vendor == "codex" else "lane-probe"
        selected = AIRole("probe", AIAccount("fake-account", vendor, str(tmp_path)), model, "read-only")
        with TextCLI(selected, binary=config["binary"], sha256=config["sha256"], timeout=35) as cli:
            cli.env["LANE_PROBE_DUMMY_KEY"] = "not-a-real-credential"
            active["marker"] = cli.cwd / "static-probe-sentinel"
            if vendor == "codex":
                for key, value in {
                    "model_provider": "lane_probe",
                    "model_providers.lane_probe.name": "Static local fake provider",
                    "model_providers.lane_probe.base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model_providers.lane_probe.env_key": "LANE_PROBE_DUMMY_KEY",
                    "model_providers.lane_probe.wire_api": "responses",
                    "model_providers.lane_probe.request_max_retries": 0,
                    "model_providers.lane_probe.stream_max_retries": 0,
                }.items():
                    cli.args += ["-c", key + "=" + json.dumps(value)]
            else:
                with cli.config.open("a") as stream:
                    stream.write(f'\n[model.lane-probe]\nmodel = "lane-probe"\nbase_url = "http://127.0.0.1:{server.server_port}/v1"\nenv_key = "LANE_PROBE_DUMMY_KEY"\napi_backend = "responses"\nmax_retries = 0\nsupports_backend_search = false\n')
                # One extra fake response exposes the rejected tool's result.
                cli.args[cli.args.index("--max-turns") + 1] = "2"
            if positive:
                args = []
                iterator = iter(cli.args)
                for value in iterator:
                    if value in ("--tools", "--disallowed-tools"):
                        next(iterator)
                    elif value == "--disable":
                        feature = next(iterator)
                        if feature not in ("shell_tool", "unified_exec"):
                            args += [value, feature]
                    elif value == "--sandbox":
                        next(iterator)
                        args += [value, "workspace-write"]
                    else:
                        args.append(value)
                cli.args = args
                cli.args += (["--tools", "Bash", "--always-approve"] if vendor == "grok" else
                             ["--enable", "shell_tool", "--enable", "unified_exec", "-c", 'approval_policy="never"'])
            try:
                cli.complete("Synthetic context data\n" * 10_000 + "PROBE_LONG_END\n@" + str(outside))
            except ProtocolError:
                # Rejected native calls may also exhaust the deliberately
                # narrow structured-response turn contract; that is not a pass.
                pass
            outputs = [str(item.get("output", "")) for item in state.get("returned_items", [])]
            assert state.get("injected") is True
            assert outputs, "the rejection must be observed, not inferred from missing side effects"
            assert all(not r["secret_leak"] for r in requests), "host file mentions must remain text"
            assert active["marker"].exists() is positive
            if not positive:
                if tool == "custom:exec":
                    assert any("code-mode host is disabled" in output for output in outputs)
                elif tool == "custom:apply_patch":
                    assert any("read-only" in output and "patch rejected" in output for output in outputs)
                else:
                    assert any("unsupported call: " + tool in output or "Tool not found: " + tool in output for output in outputs)
                # The title metadata request is separate from Grok's work.
                work = [r for r in requests if not any(t.get("name") == "session_title" for t in r["tools"])]
                assert work and all(r["tools"] == [] and r["long_marker"] for r in work)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
