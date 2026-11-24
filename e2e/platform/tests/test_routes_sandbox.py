"""Routes of the kernel-refusal case (KOR-60): a root-owned file in a session's workspace and the guest's session
metrics, against a scripted node."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.routes import sandbox_denials
from test_routes_guest import OTHER, SESSION, VM1, Result, call, failed, ok, scripted, session_handler


def test_locked_file(tmp_path: Path) -> None:
    def extra(_vm: str, argv: list[str]) -> Result | None:
        return ok() if argv[:4] == ["python3", "-I", "-c", sandbox_denials.LOCK_SCRIPT] else None

    ctx, node = scripted(tmp_path, session_handler(extra))
    route = f"/sessions/{SESSION}/locked-file"
    for body in ({"name": ".hidden", "text": "x"}, {"name": "a/b", "text": "x"}, {"name": "x"}, None, [1]):
        assert call(ctx, "POST", route, body)[0] == 400
    assert call(ctx, "POST", route, {"name": "x", "text": "y" * (sandbox_denials.MAX_TEXT + 1)})[0] == 400
    path = f"/srv/sessions/{SESSION}/work/locked.txt"
    assert call(ctx, "POST", route, {"name": "locked.txt", "text": "kz"}) == (200, {"vm": VM1, "path": path})
    assert node.runs[-1][-2:] == [path, "kz"]
    assert call(ctx, "POST", "/sessions/not-an-id/locked-file", {"name": "x", "text": ""})[0] == 404
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: failed(1, "FileExistsError")))
    found = call(ctx, "POST", route, {"name": "locked.txt", "text": "kz"})
    assert found[0] == 500
    assert found[1]["detail"].endswith("FileExistsError")


def test_guest_metrics(tmp_path: Path) -> None:
    entry = {"session_id": SESSION, "sandbox_denials": 1}
    answer = {"sessions": [{"session_id": OTHER, "sandbox_denials": 0}, entry]}

    def extra(_vm: str, argv: list[str]) -> Result | None:
        return ok(json.dumps(answer)) if argv[:4] == ["python3", "-I", "-c", sandbox_denials.METRICS_SCRIPT] else None

    ctx, _ = scripted(tmp_path, session_handler(extra))
    assert call(ctx, "GET", f"/sessions/{SESSION}/guest-metrics") == (200, {"vm": VM1, "session": entry})
    answer["sessions"] = []
    assert call(ctx, "GET", f"/sessions/{SESSION}/guest-metrics") == (200, {"vm": VM1, "session": None})
    for broken in ("not json", "{}", '{"sessions": [1]}', '{"sessions": [{}]}'):

        def answering(_vm: str, _argv: list[str], text: str = broken) -> Result | None:
            return ok(text)

        ctx, _ = scripted(tmp_path, session_handler(answering))
        found = call(ctx, "GET", f"/sessions/{SESSION}/guest-metrics")
        assert found[0] == 500
        assert "unexpected answer" in found[1]["detail"]
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: failed(1, "GET /v1/metrics: 503")))
    assert call(ctx, "GET", f"/sessions/{SESSION}/guest-metrics")[0] == 500
