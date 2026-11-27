"""The configuration of the CI controller (spec 16 section 17.1): one JSON file, readable by the controller's account
only (it names the key file of the GitHub App, never the key itself).

```json
{
  "api_url": "https://api.github.com",
  "app": {"client_id": "Iv23...", "key_file": "/var/agent-ci/app.pem"},
  "listener": "/usr/local/libexec/agent-ci-listener",
  "socket": "/var/run/agent-ci/ci.sock",
  "push_attribution": false,
  "repositories": [
    {"repo": "example-org/example-repo", "installation_id": 1, "scale_set": "agent-ci-example-repo",
     "org_id": "<uuid>", "wg_id": "<uuid>", "task_id": "<uuid>", "session_id": "<uuid>", "size": "standard"}
  ]
}
```

- One entry per repository, one scale set per repository. The scale set is registered at the repository's
  organisation (the app holds only the organisation permission "Self-hosted runners", spec 16 section 17.9) in its
  default runner group; `scale_set` is its name and the label its workflows use (`runs-on: agent-ci-example-repo`).
  Jobs of other repositories that name the label are not taken (`repository_not_served`).
- `app.client_id` and `installation_id` are written by `agent-ci setup` (`setup.update_config`); until then they
  may be absent and the controller does not start (`ready`).
- `org_id`, `wg_id`, `task_id` and `session_id` name the **fixed task of the repository** in the repository's
  workgroup (decision D-12, e.g. a task titled "CI example-org/example-repo"; `session_id` is its main session): it
  carries scheduled runs and deploys, and the audit rows of jobs that are not taken.
- `push_attribution` says whether the app may read workflow runs and pull requests (`Actions: read`,
  `Pull requests: read`). Without it a `push` job cannot be traced to its merged pull request and is not taken
  (`push_unattributable`); see spec 16 section 13, D-4.
- `size` is the work VM size of the repository's jobs (`standard`, `large`, `xlarge`).

`load` refuses a file that does not fit (`ConfigError` with the JSON pointer of the first problem), a repository named
twice (GitHub names compare case-insensitively) and a scale set named twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.ci.jobs import NAME, OWNER

SIZES = ("standard", "large", "xlarge")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
SCALE_SET = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
DEFAULT_API = "https://api.github.com"


class ConfigError(Exception):
    def __init__(self, pointer: str) -> None:
        super().__init__(f"ci configuration invalid at {pointer}")
        self.pointer = pointer


@dataclass(frozen=True)
class Repository:
    repo: str
    installation_id: int | None
    scale_set: str
    org_id: str
    wg_id: str
    task_id: str
    session_id: str
    size: str = "standard"

    @property
    def key(self) -> str:
        return self.repo.lower()

    @property
    def owner(self) -> str:
        """The GitHub organisation of the repository."""
        return self.repo.split("/")[0]


@dataclass(frozen=True)
class CiConfig:
    api_url: str
    client_id: str | None
    key_file: Path
    listener: Path
    socket: Path
    push_attribution: bool
    repositories: tuple[Repository, ...]

    @property
    def ready(self) -> bool:
        """The app exists and is installed for every repository (`agent-ci setup` ran)."""
        return self.client_id is not None and all(entry.installation_id for entry in self.repositories)

    @property
    def organisations(self) -> list[str]:
        """The organisations of the repositories, in order, each once (the app is installed in each)."""
        found: list[str] = []
        for entry in self.repositories:
            if entry.owner.lower() not in [org.lower() for org in found]:
                found.append(entry.owner)
        return found

    def repository(self, full_name: str) -> Repository | None:
        """The entry of `<owner>/<repo>` (case-insensitive), None when the repository is not served."""
        return next((entry for entry in self.repositories if entry.key == full_name.lower()), None)


def _text(data: Mapping[str, Any], key: str, pointer: str, pattern: re.Pattern[str] | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise ConfigError(f"{pointer}/{key}")
    return value


def _repo_name(value: str, pointer: str) -> str:
    owner, slash, name = value.partition("/")
    if not slash or OWNER.fullmatch(owner) is None or NAME.fullmatch(name) is None or name in (".", ".."):
        raise ConfigError(pointer)
    return value


def _repository(data: object, pointer: str) -> Repository:
    if not isinstance(data, dict):
        raise ConfigError(pointer)
    installation = data.get("installation_id")
    if installation is not None and (
        not isinstance(installation, int) or isinstance(installation, bool) or installation < 1
    ):
        raise ConfigError(f"{pointer}/installation_id")
    size = data.get("size", "standard")
    if size not in SIZES:
        raise ConfigError(f"{pointer}/size")
    return Repository(
        repo=_repo_name(_text(data, "repo", pointer), f"{pointer}/repo"),
        installation_id=installation,
        scale_set=_text(data, "scale_set", pointer, SCALE_SET),
        org_id=_text(data, "org_id", pointer, UUID),
        wg_id=_text(data, "wg_id", pointer, UUID),
        task_id=_text(data, "task_id", pointer, UUID),
        session_id=_text(data, "session_id", pointer, UUID),
        size=size,
    )


def parse(data: object) -> CiConfig:
    """The configuration of a parsed JSON document (module docstring)."""
    if not isinstance(data, dict):
        raise ConfigError("")
    app = data.get("app")
    if not isinstance(app, dict):
        raise ConfigError("/app")
    api_url = data.get("api_url", DEFAULT_API)
    if not isinstance(api_url, str) or not api_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise ConfigError("/api_url")
    push = data.get("push_attribution", False)
    if not isinstance(push, bool):
        raise ConfigError("/push_attribution")
    entries = data.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("/repositories")
    repositories = tuple(_repository(entry, f"/repositories/{index}") for index, entry in enumerate(entries))
    for index, entry in enumerate(repositories):
        before = repositories[:index]
        if any(other.key == entry.key for other in before):
            raise ConfigError(f"/repositories/{index}/repo")
        if any(other.scale_set == entry.scale_set for other in before):
            raise ConfigError(f"/repositories/{index}/scale_set")
    client_id = app.get("client_id")
    if client_id is not None:
        client_id = _text(app, "client_id", "/app", CLIENT_ID)
    return CiConfig(
        api_url=api_url.rstrip("/"),
        client_id=client_id,
        key_file=Path(_text(app, "key_file", "/app")),
        listener=Path(_text(data, "listener", "")),
        socket=Path(_text(data, "socket", "")),
        push_attribution=push,
        repositories=repositories,
    )


def load(path: Path) -> CiConfig:
    """The configuration in `path` (module docstring)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigError("") from None
    return parse(data)
