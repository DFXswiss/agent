"""PostToolUse of ``Write``/``Edit`` -> ``session.file`` at once (05-runner.md section 5.5; GST-05).

The hook decision and ``session.tool`` belong to GST-03 (``hooks.py``). This addon hands the written path to the
control process (api.sock op ``capture.file_written``, ``control_routes/capture.py``), which reads the new content as
root and journals ``session.file {path, op, sha256, size, patch?}`` unless the scanner reported that content already.
It never adds context to the answer; a failing op is counted by the hook handler (``hook_handler_failed``).
"""

from __future__ import annotations

from agent_guest.hooks import HookAddon, HookCall
from agent_guest.server import ApiContext

OPS = {"Write": "write", "Edit": "edit"}
MAX_PATH = 4096


def handle(context: ApiContext, call: HookCall) -> None:
    body = call.body
    if body is None or body.get("tool_name") not in OPS:
        return None
    path = body["tool_input"].get("file_path")
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > MAX_PATH:
        return None
    context.control.call(
        "capture.file_written", {"session_id": call.session_id, "path": path, "op": OPS[body["tool_name"]]}
    )
    return None


ADDON = HookAddon(events=frozenset({"PostToolUse"}), handle=handle)
