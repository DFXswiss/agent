"""Shared values of the CI tests (spec 16 section 17): IDs, a configuration, jobs (no real names, no secrets)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_runner.ci.attribution import Owner
from agent_runner.ci.repos import CiConfig, parse

ORG = "0193000c-0000-7000-8000-0000000000c1"
WG = "0193000c-0000-7000-8000-0000000000c2"
FIXED_TASK = "0193000c-0000-7000-8000-0000000000c3"
FIXED_SESSION = "0193000c-0000-7000-8000-0000000000c4"
PR_TASK = "0193000c-0000-7000-8000-0000000000d3"
PR_SESSION = "0193000c-0000-7000-8000-0000000000d4"
OTHER_TASK = "0193000c-0000-7000-8000-0000000000e3"
OTHER_SESSION = "0193000c-0000-7000-8000-0000000000e4"
REPO = "example-org/example-repo"
SHA = "a" * 40
FIXED = Owner(ORG, WG, FIXED_TASK, FIXED_SESSION)
PR_OWNER = Owner(ORG, WG, PR_TASK, PR_SESSION)
OTHER_OWNER = Owner(ORG, WG, OTHER_TASK, OTHER_SESSION)


def config_data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "api_url": "https://api.github.test",
        "app": {"client_id": "Iv23-test", "key_file": "/nonexistent/app.pem"},
        "listener": "/nonexistent/agent-ci-listener",
        "socket": "/nonexistent/ci.sock",
        "push_attribution": True,
        "repositories": [
            {
                "repo": REPO,
                "installation_id": 11,
                "scale_set": "agent-ci-example-repo",
                "org_id": ORG,
                "wg_id": WG,
                "task_id": FIXED_TASK,
                "session_id": FIXED_SESSION,
                "size": "standard",
            }
        ],
    }
    data.update(changes)
    return data


def config(**changes: Any) -> CiConfig:
    return parse(config_data(**changes))


def write_config(path: Path, **changes: Any) -> Path:
    path.write_text(json.dumps(config_data(**changes)), encoding="utf-8")
    return path


def job_message(
    request_id: int = 42, event: str = "pull_request", ref: str = "refs/pull/7/merge", **changes: Any
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "request_id": request_id,
        "owner": "example-org",
        "repo": "example-repo",
        "job_id": f"job-{request_id}",
        "run_id": 900 + request_id,
        "workflow_ref": f"example-org/example-repo/.github/workflows/ci.yml@{ref}",
        "event": event,
        "labels": ["agent-ci-example-repo"],
    }
    message.update(changes)
    return message
