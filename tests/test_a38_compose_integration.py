from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_cli.a38_job_adapters.compose import run_compose


pytestmark = [
    pytest.mark.no_pg,
    pytest.mark.skipif(
        os.environ.get("A38_TEST_DOCKER") != "1",
        reason="set A38_TEST_DOCKER=1 to run the real Docker Compose integration test",
    ),
]

_BRANCH = "a38-integration"
_PROOF = "a38-real-compose-proof"
_RUNTIME_LINE_RE = re.compile(
    r"^A38_RUNTIME_PROOF project=(a38-[a-f0-9]{24}) "
    r"image=(a38-integration:\1) token=([a-f0-9]{32}) "
    rf"value={_PROOF}$",
    flags=re.MULTILINE,
)


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "A38 Integration Fixture",
            "GIT_AUTHOR_EMAIL": "a38-integration@example.invalid",
            "GIT_COMMITTER_NAME": "A38 Integration Fixture",
            "GIT_COMMITTER_EMAIL": "a38-integration@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return env


def _git(repo: Path, hooks: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            "git",
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=_git_env(),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout.strip()


def _commit(repo: Path, hooks: Path, message: str) -> str:
    _git(repo, hooks, "add", "--all")
    _git(repo, hooks, "commit", "-m", message)
    return _git(repo, hooks, "rev-parse", "HEAD")


def _init_repo(path: Path, hooks: Path, origin: str) -> None:
    path.mkdir()
    _git(path, hooks, "init", "--initial-branch", _BRANCH)
    _git(path, hooks, "remote", "add", "origin", origin)


@pytest.fixture
def compose_repositories(tmp_path: Path) -> dict[str, object]:
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()

    consumer = tmp_path / "consumer"
    _init_repo(consumer, hooks, "https://github.com/example/consumer.git")
    (consumer / "README.md").write_text("A38 Compose integration fixture\n", encoding="utf-8")
    base = _commit(consumer, hooks, "Add fixture readme")
    (consumer / "Dockerfile").write_text(
        "FROM busybox:1.36.1\n"
        "LABEL org.opencontainers.image.title=\"A38 Compose integration fixture\"\n",
        encoding="utf-8",
    )
    head = _commit(consumer, hooks, "Add fixture image")

    companion = tmp_path / "companion"
    _init_repo(companion, hooks, "https://github.com/example/services.git")
    (companion / "compose.yml").write_text(
        "services:\n"
        "  tests:\n"
        "    image: \"${A38_INTEGRATION_IMAGE}\"\n"
        "    command:\n"
        "      - sh\n"
        "      - -ec\n"
        "      - |\n"
        "        mkdir -p /proof\n"
        f"        printf '%s\\n' '{_PROOF}' > /proof/result.txt\n"
        "        printf 'A38_RUNTIME_PROOF project=%s image=%s token=%s value=%s\\n' \"$$A38_PROJECT\" \"$$A38_IMAGE\" \"$$A38_TOKEN\" \"$$(cat /proof/result.txt)\"\n"
        "    environment:\n"
        "      A38_IMAGE: \"${A38_INTEGRATION_IMAGE}\"\n"
        "      A38_PROJECT: \"${A38_INTEGRATION_PROJECT}\"\n"
        "      A38_TOKEN: \"${A38_INTEGRATION_TOKEN}\"\n"
        "    labels:\n"
        "      a38.integration.token: \"${A38_INTEGRATION_TOKEN}\"\n"
        "    networks:\n"
        "      - default\n"
        "    volumes:\n"
        "      - proof:/proof\n"
        "networks:\n"
        "  default:\n"
        "    labels:\n"
        "      a38.integration.token: \"${A38_INTEGRATION_TOKEN}\"\n"
        "volumes:\n"
        "  proof:\n"
        "    labels:\n"
        "      a38.integration.token: \"${A38_INTEGRATION_TOKEN}\"\n",
        encoding="utf-8",
    )
    companion_head = _commit(companion, hooks, "Add Compose fixture")

    assert _git(consumer, hooks, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(companion, hooks, "status", "--porcelain", "--untracked-files=all") == ""
    return {
        "base": base,
        "companion": companion,
        "companion_head": companion_head,
        "consumer": consumer,
        "head": head,
    }


def _docker(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _docker_lines(env: dict[str, str], *args: str) -> list[str]:
    completed = _docker(env, *args)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _remove_exact(env: dict[str, str], kind: str, values: list[str]) -> None:
    if not values:
        return
    command = {
        "container": ("rm", "--force"),
        "image": ("image", "rm", "--force"),
        "network": ("network", "rm"),
        "volume": ("volume", "rm", "--force"),
    }[kind]
    _docker(env, *command, *values)


def _fallback_owned(
    env: dict[str, str], kind: str, token: str, project: str | None
) -> list[str]:
    commands = {
        "container": ["container", "ls", "--all", "--quiet"],
        "image": ["image", "ls", "--all", "--quiet"],
        "network": ["network", "ls", "--quiet"],
        "volume": ["volume", "ls", "--quiet"],
    }
    labels = [f"label=a38.integration.token={token}"]
    if project is not None and kind != "image":
        labels.append(f"label=com.docker.compose.project={project}")
    found: list[str] = []
    for label in labels:
        completed = _docker(env, *commands[kind], "--filter", label)
        if completed.returncode == 0:
            found.extend(completed.stdout.splitlines())
    return list(dict.fromkeys(value.strip() for value in found if value.strip()))


def test_real_compose_run_copies_proof_and_removes_owned_resources(
    compose_repositories: dict[str, object],
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    consumer = compose_repositories["consumer"]
    companion = compose_repositories["companion"]
    assert isinstance(consumer, Path)
    assert isinstance(companion, Path)

    token = uuid.uuid4().hex
    companion_ref = f"refs/heads/{_BRANCH}"
    env = os.environ.copy()
    env.update(
        {
            "A38_BASE_SHA": str(compose_repositories["base"]),
            "A38_HEAD_SHA": str(compose_repositories["head"]),
            "A38_INTEGRATION_COMPANION_DIR": str(companion),
            "A38_INTEGRATION_COMPANION_REF": companion_ref,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    config = {
        "env": {
            "A38_INTEGRATION_IMAGE": "{image:integration}",
            "A38_INTEGRATION_PROJECT": "{project}",
            "A38_INTEGRATION_TOKEN": token,
        },
        "companion": {
            "directory_env": "A38_INTEGRATION_COMPANION_DIR",
            "ref_env": "A38_INTEGRATION_COMPANION_REF",
            "ref": companion_ref,
            "repository": "example/services",
        },
        "files": ["compose.yml"],
        "builds": [
            {
                "argv": [
                    "docker",
                    "build",
                    "--label",
                    f"a38.integration.token={token}",
                    "--tag",
                    "{image:integration}",
                    "{repo}",
                ],
                "image": "{image:integration}",
            }
        ],
        "test_service": "tests",
        "test_image": "{image:integration}",
        "artifacts": [
            {"source": "/proof/result.txt", "destination": "proof/result.txt"}
        ],
    }

    artifacts: Path | None = None
    owned: dict[str, list[str]] = {
        "container": [],
        "image": [],
        "network": [],
        "volume": [],
    }
    image_tag: str | None = None
    project: str | None = None
    try:
        status = run_compose(
            json.dumps(config),
            cwd=consumer,
            lock_root=tmp_path / "locks",
            environ=env,
        )
        captured = capfd.readouterr()
        artifact_matches = re.findall(
            r"^a38: artifacts: (.+)$", captured.out, flags=re.MULTILINE
        )
        if artifact_matches:
            artifacts = Path(artifact_matches[-1])
        assert len(artifact_matches) == 1, captured.out
        assert status == 0, captured.err

        runtime_matches = list(_RUNTIME_LINE_RE.finditer(captured.out))
        assert len(runtime_matches) == 1, captured.out
        project, image_tag, runtime_token = runtime_matches[0].groups()
        assert runtime_token == token
        assert (artifacts / "proof" / "result.txt").read_text(
            encoding="utf-8"
        ) == f"{_PROOF}\n"

        project_label = f"label=com.docker.compose.project={project}"
        token_label = f"label=a38.integration.token={token}"
        owned = {
            "container": _docker_lines(
                env, "container", "ls", "--all", "--quiet", "--filter", project_label
            ),
            "image": _docker_lines(
                env, "image", "ls", "--all", "--quiet", "--filter", token_label
            ),
            "network": _docker_lines(
                env, "network", "ls", "--quiet", "--filter", project_label
            ),
            "volume": _docker_lines(
                env, "volume", "ls", "--quiet", "--filter", project_label
            ),
        }
        image_inspect = _docker(env, "image", "inspect", image_tag)
        assert image_inspect.returncode != 0, image_inspect.stdout
        assert owned == {"container": [], "image": [], "network": [], "volume": []}
    finally:
        # Assertions above observe adapter leaks before this failure-only safety net.
        try:
            fallback = {
                kind: list(
                    dict.fromkeys(
                        [*owned[kind], *_fallback_owned(env, kind, token, project)]
                    )
                )
                for kind in ("container", "image", "network", "volume")
            }
            if image_tag is not None:
                fallback["image"].append(image_tag)
            _remove_exact(env, "container", fallback["container"])
            _remove_exact(env, "network", fallback["network"])
            _remove_exact(env, "volume", fallback["volume"])
            _remove_exact(env, "image", fallback["image"])
        except (OSError, subprocess.TimeoutExpired):
            # Preserve the assertion that led here if Docker itself disappeared.
            pass
        if artifacts is not None:
            shutil.rmtree(artifacts, ignore_errors=True)
