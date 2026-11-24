"""The download area of the hub (ADR-074 section 7, 03-api.md section 2).

`GET /downloads/runner/{name}` serves, anonymously, the files of `<AGENT_HUB_DOWNLOADS_DIR>/runner/` that a release
operator placed there: `latest.json` (`application/json`), its signature `latest.json.sig`
(`text/plain; charset=us-ascii`), both revalidated (`no-cache`), and the versioned package
`DFX-Agent-Runner-<YYYY.MM.DD.HHMM>.pkg` (`application/octet-stream`, streamed, `immutable`). Any other name, a missing
file, a symbolic link, a path that resolves outside `runner/` or an unset setting is the JSON `404 not_found`. The hub
never signs or changes these files.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.responses import FileResponse, Response

from agent_hub.errors import HubError
from agent_hub.http.context import Context
from agent_hub.http.routing import HubRouter

RUNNER_DIR = "runner"
LATEST = "latest.json"
LATEST_SIGNATURE = "latest.json.sig"
PACKAGE_RE = re.compile(r"DFX-Agent-Runner-\d{4}\.\d{2}\.\d{2}\.\d{4}\.pkg")
REVALIDATE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"
# The small, revalidated files and their content types; the package is the only other name.
SMALL_FILES = {LATEST: "application/json", LATEST_SIGNATURE: "text/plain; charset=us-ascii"}

router = HubRouter()


def runner_file(downloads_dir: Path | None, name: str) -> Path | None:
    """The regular file `runner/<name>` of the download directory; None for a name outside the allow-list, an unset
    directory, a missing file, a symbolic link or a path that resolves outside `runner/`."""
    if downloads_dir is None or (name not in SMALL_FILES and PACKAGE_RE.fullmatch(name) is None):
        return None
    root = downloads_dir / RUNNER_DIR
    candidate = root / name
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved


@router.get("/downloads/runner/{name}", auth="anon", right="anon", include_in_schema=False)
async def runner_download(name: str, context: Context) -> Response:
    path = runner_file(context.config.downloads_dir, name)
    if path is None:
        raise HubError("not_found")
    media_type = SMALL_FILES.get(name)
    if media_type is not None:
        return Response(path.read_bytes(), media_type=media_type, headers={"Cache-Control": REVALIDATE})
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": IMMUTABLE})
