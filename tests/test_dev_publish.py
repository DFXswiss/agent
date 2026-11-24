from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEV_WORKFLOW = ROOT / ".github" / "workflows" / "agent-core-dev.yaml"
AUDIENCE = "https://image-published.dfxserve.com/v1/image-published"

BUILD_STEP = "Build and push Docker image"
PUBLISH_STEP = "Notify image publish endpoint"
PULL_STEP = "Notify infrastructure to pull :beta"


def _dev_workflow() -> dict[str, object]:
    loaded = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps() -> list[dict[str, object]]:
    jobs = _dev_workflow()["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["build-and-push"]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _step(name: str) -> dict[str, object]:
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def test_permissions_are_contents_read_and_id_token_write() -> None:
    assert _dev_workflow()["permissions"] == {"contents": "read", "id-token": "write"}


def test_build_step_id_is_push() -> None:
    assert _step(BUILD_STEP)["id"] == "push"


def test_push_notifies_image_publish_endpoint() -> None:
    step = _step(PUBLISH_STEP)
    assert step["if"] == "github.event_name == 'push'"
    env = step["env"]
    assert isinstance(env, dict)
    assert env["IMAGE_DIGEST"] == "${{ steps.push.outputs.digest }}"
    script = step["run"]
    assert isinstance(script, str)
    assert AUDIENCE in script
    assert "jq -nc" in script
    assert "DISPATCH_TOKEN" not in script
    assert "gh api" not in script


def test_manual_dispatch_still_pulls_beta() -> None:
    step = _step(PULL_STEP)
    assert step["if"] == "github.event_name != 'push'"
    script = step["run"]
    assert isinstance(script, str)
    assert "gh api" in script
    assert "DISPATCH_TOKEN" in script


def test_publish_step_sits_between_build_and_pull() -> None:
    names = [step["name"] for step in _steps()]
    build_at = names.index(BUILD_STEP)
    publish_at = names.index(PUBLISH_STEP)
    pull_at = names.index(PULL_STEP)
    assert publish_at == build_at + 1
    assert pull_at == publish_at + 1
