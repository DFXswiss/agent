"""Test kit of the RUN-10 suites: the operation pipeline of RUN-07 with this area's executors and pre-checks, the
GitHub double (DBL-02) seeded with a repository that has an executable, a binary file and a symbolic link, and a guest
whose file endpoints are the real ``agent_guest.files`` code on a temporary workspace per session (the test user is
the session user). The pipeline is installed in the daemon context, as `pipeline_for` does in production, so the
commit pre-check reaches the guest."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import os
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from agent_guest import files
from agent_guest.state import Session
from agent_runner.credentials.cache import _store
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.operations import records
from agent_runner.operations.pipeline import install
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.kit import ALICE, CLIENT_ID, ck, github_body, make_record
from ..operations.kit import OKit, OpGuest, setup
from ..reconcile.rig import Rig, standard_world
from .conftest import PUBLIC, REPO, TOKEN, seed

BINARY = bytes(range(256))
STATUS = {"path_invalid": 400, "request_invalid": 400, "not_found": 404, "state_conflict": 409, "size_exceeded": 413}


def world() -> dict[str, Any]:
    """The RUN-09 world; `main` of `octo-org/hello` has three more files: `run.sh` (100755), `logo.bin` (binary) and
    `link` (a symbolic link to `README.md`)."""
    data = copy.deepcopy(seed())
    main = data["repositories"][0]["branches"][0]
    main["commits"].append(
        {
            "message": "Add more files.",
            "files": {
                "run.sh": {"content": "echo run\n", "mode": "100755"},
                "logo.bin": {"base64": base64.b64encode(BINARY).decode()},
                "link": {"target": "README.md"},
            },
        }
    )
    return data


@dataclass
class FilesGuest(OpGuest):
    """A guest whose `files/import` and `files/export` run `agent_guest.files` on `root/<session>/work`."""

    root: Path = Path("/nonexistent")
    imports: list[tuple[str, str, bool]] = field(default_factory=list)
    exports: list[tuple[str, str, list[str] | None]] = field(default_factory=list)
    export_override: Callable[[Callable[[bytes], object]], str] | None = None
    import_answer: dict[str, Any] | None = None
    # Sizes of the pieces of every streamed import (a list per import; bytes bodies are not streamed).
    import_chunks: list[list[int]] = field(default_factory=list)

    def session(self, sid: str) -> Session:
        work = self.root / sid / "work"
        work.mkdir(parents=True, exist_ok=True)
        return Session(sid, os.getuid(), "0" * 64, "working", str(work), None, "2026-11-21T10:00:00.000Z")

    def work(self, sid: str) -> Path:
        return Path(self.session(sid).workspace)

    async def import_files(
        self,
        sid: str,
        directory: str,
        data: bytes | AsyncIterable[bytes],
        *,
        replace: bool = False,
        size: int | None = None,
    ) -> dict[str, Any]:
        self._call("import_files", (sid, directory, replace))
        self.imports.append((sid, directory, replace))
        if self.import_answer is not None:
            return self.import_answer
        body = io.BytesIO()
        if isinstance(data, bytes):
            body.write(data)
        else:
            pieces: list[int] = []
            async for chunk in data:
                pieces.append(len(chunk))
                body.write(chunk)
            self.import_chunks.append(pieces)
            # like the guest's Content-Length check
            assert size == body.tell(), (size, body.tell())
        body.seek(0)
        try:
            return files.import_tar(self.session(sid), directory, body, replace=replace)
        except files.FilesError as error:
            raise GuestRefused("files_import", STATUS[error.code], error.code) from None

    async def export_files(
        self, sid: str, directory: str, write: Callable[[bytes], object], *, paths: Sequence[str] | None = None
    ) -> str:
        self._call("export_files", (sid, directory, paths))
        self.exports.append((sid, directory, None if paths is None else list(paths)))
        if self.export_override is not None:
            return self.export_override(write)
        try:
            exported = files.export_tar(self.session(sid), directory, paths)
        except files.FilesError as error:
            raise GuestRefused("files_export", STATUS[error.code], error.code) from None
        for chunk in exported.stream:
            write(chunk)
        return exported.digest


class PublicHost(httpx.AsyncBaseTransport):
    """Sends requests for the double's public https host to its loopback address (what DNS and TLS would do), so the
    tarball redirect to `<public>/codeload/...` reaches the double."""

    def __init__(self, local: str) -> None:
        self.loopback = httpx.URL(local)
        self.public = httpx.URL(PUBLIC)
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == self.public.host:
            request.url = request.url.copy_with(scheme="http", host=self.loopback.host, port=self.loopback.port)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class R10:
    ok: OKit
    guest: FilesGuest
    github: Any
    tmp: Path

    async def session(self, n: int = 1) -> str:
        return await self.ok.session(n)

    async def run(self, sid: str, op_type: str, params: Mapping[str, Any], **kw: Any) -> records.Operation:
        return await self.ok.run(sid, op_type, dict(params), **kw)

    def scratch(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir()) if self.tmp.is_dir() else []

    def journal(self) -> list[str]:
        paths: list[str] = self.github.paths()
        return paths


class Double(Protocol):
    """The GitHub double, or `MockDouble` when a mock transport answers instead."""

    @property
    def url(self) -> str: ...

    def failures(self) -> list[str]: ...

    def clock(self) -> float: ...


class MockDouble:
    """Stands in for the GitHub double when a test installs a mock transport."""

    url = "https://api.mock"

    @staticmethod
    def failures() -> list[str]:
        return []

    @staticmethod
    def clock() -> float:
        return 1_800_000_000.0


def run10(
    dsn: str,
    github: Double,
    root: Path,
    body: Callable[[R10], Awaitable[None]],
    *,
    allowed: Sequence[str] = (REPO,),
    guest: FilesGuest | None = None,
    token: str = TOKEN,
) -> None:
    guest = guest or FilesGuest(root=root / "guest")

    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            config = env.context.config
            config.github_client_id = CLIENT_ID
            config.github_api_url = github.url
            config.github_web_url = PUBLIC
            config.tmp_dir = root / "tmp"
            env.context.github_options = GitHubOptions(wall=github.clock, transport=PublicHost(github.url))
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS, guest=guest)
            install(env.context, found.pipeline)
            await found.settings(list(allowed))
            async with session(env.pool) as conn:
                await _store(conn, ALICE, "github", 1, make_record(github_body(token=token)))
                await store_scope_key(conn, env.sync.storage_key, ("cred", ALICE, 1), ck(), source="wrap")
            await body(R10(found, guest, github, root / "tmp"))

    asyncio.run(main())
    assert github.failures() == []


def refused(op: records.Operation) -> str:
    """The code of an operation the runner refused before execution (registered `rejected`; the kit's hub accepts
    everything, so the pipeline ends it `failed` with the local code and answers the session `rejected`)."""
    assert op.answer is not None
    assert op.answer["status"] == "rejected", op.answer
    code: str = op.answer["error"]["code"]
    assert op.error_code == code
    return code
