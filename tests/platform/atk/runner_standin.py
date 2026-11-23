"""The runner stand-in of ATK-33, ATK-38 and ATK-40 (``agent_doubles.atk.extended.Runner``; decisions ATS-02).

The attack suite drives the guest without hub and runner daemon, so nobody would answer a session's
``request_operation``. This stand-in answers with the runner's **real** code where the attack is aimed at it:

* ``check`` - ``agent_runner.operations.checks.check`` (schema, branch, path, allow-list, protection, connection,
  model rules) with workgroup settings that allow the suite's repositories, the default protected branches, a
  connected GitHub owner and the packaged model catalog; ``None`` when every formal check passes
* ``tarball`` - ``atk/links`` from the GitHub double (seed ``fixtures/github-seed.json``: symbolic links out of the
  workspace) through its REST tarball route; the ``atk/evil-*`` archives from ``fixtures/tarballs.py``
* ``repack`` - ``agent_runner.github.tarsafe.repack`` with the checkout limits, i.e. what ``github.checkout`` imports
  into the guest, or the refusal code (``archive_invalid`` becomes ``upstream_error`` like in the executor)
* ``raw`` - the archive as a plain tar without its top directory, links and devices unchanged, for the direct guest
  import (the guest must refuse it alone)
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_doubles.atk import extended
from agent_doubles.github.server import GitHubDouble
from agent_runner.github import checkout, tarsafe
from agent_runner.model.catalog import load_catalog
from agent_runner.operations.checks import CheckRequest, check
from agent_runner.operations.extensions import CheckFailed
from agent_runner.operations.rules import DEFAULT_PROTECTED
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.sessions.records import LocalSession
from tests.platform.atk.fixtures import tarballs

FIXTURES = Path(__file__).with_name("fixtures")
TOKEN = "atk-suite-github-double-token-0001"
REPOSITORIES = frozenset({*extended.SUITE_REPOSITORIES, *extended.EVIL_REPOSITORIES})
SESSION = LocalSession(
    id="01a20000-0000-7000-8000-000000000000",
    task_id="01a20000-0000-7000-8000-000000000001",
    org_id="01a20000-0000-7000-8000-000000000002",
    wg_id="01a20000-0000-7000-8000-000000000003",
    vm_id=None,
    parent_id=None,
    depth=0,
    state="working",
    claude_session_id=None,
    model_access="platform",
    model_id=extended.MODEL_ID,
    owner_id=None,
    created_at=datetime(2026, 11, 22, tzinfo=UTC),
    ready_at=None,
    ended_at=None,
)


class RunnerStandIn:
    def __init__(self) -> None:
        self.settings = WorkgroupSettings(
            repositories=REPOSITORIES, protected_branches=DEFAULT_PROTECTED, pr_draft_default=True
        )
        self.catalog = load_catalog(None)
        self.github = GitHubDouble().start()
        self.github.world.seed(json.loads((FIXTURES / "github-seed.json").read_text(encoding="utf-8")))

    def close(self) -> None:
        self.github.stop()

    def check(self, op_type: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        request = CheckRequest(
            op_type=op_type,
            params=params,
            session=SESSION,
            settings=self.settings,
            catalog=self.catalog,
            connected=True,
            task_commits=frozenset(),
        )
        try:
            check(request)
        except CheckFailed as failed:
            return failed.error()
        return None

    def _get(self, path: str) -> bytes:
        request = urllib.request.Request(
            self.github.url + path,
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data: bytes = response.read()
            return data

    def tarball(self, repo: str) -> tuple[str, bytes]:
        if repo in extended.EVIL_REPOSITORIES:
            return tarballs.SHA, tarballs.tarball(repo)
        sha = str(json.loads(self._get(f"/repos/{repo}/commits/main"))["sha"])
        return sha, self._get(f"/repos/{repo}/tarball/{sha}")

    def repack(self, data: bytes) -> tuple[bytes | None, str | None]:
        target = io.BytesIO()
        try:
            tarsafe.repack(io.BytesIO(data), target, limits=checkout.CONTENT_LIMITS)
        except tarsafe.UnsafeArchive as error:
            return None, "upstream_error" if error.code == "archive_invalid" else error.code
        return target.getvalue(), None

    def raw(self, repo: str) -> bytes:
        if repo in extended.EVIL_REPOSITORIES:
            return tarballs.raw(repo)
        _sha, data = self.tarball(repo)
        out = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as source,
            tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as target,
        ):
            for member in source.getmembers():
                _top, _, rest = member.name.partition("/")
                if not rest:
                    continue
                member.name = rest
                target.addfile(member, source.extractfile(member) if member.isreg() else None)
        return out.getvalue()
