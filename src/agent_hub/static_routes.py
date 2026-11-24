"""Delivery of the web build (06-web.md sections 2.4, 10.1 and 10.2).

`npm run build` writes `index.html` and the content-hashed `assets/` into the static directory
(`AGENT_HUB_STATIC_DIR`, default the package's `static/`). `/assets/<file>` is served with
`Cache-Control: public, max-age=31536000, immutable`; `index.html` with `Cache-Control: no-cache` for every SPA route
of 06-web.md section 2.4 (status 200) and, for any other path outside `/v1/` and `/assets/`, with status 404 so the
client shows its not-found screen. Unknown API and asset paths get the JSON `404 not_found`; without a build the page
is `503 unavailable`. `GET /build-manifest.json` delivers the build manifest unchanged and revalidated (`no-cache`):
the security page hashes its exact bytes into the build hash (06-web.md sections 10.3 and 10.5); without a build it is
the JSON `404 not_found`. `GET /agents.md` delivers the guide for AI agents of the build (`docs/platform/agents.md`,
copied into the build by the web client; ADR-056) as `text/markdown`, revalidated; without it the JSON
`404 not_found`. The security headers (CSP) are set by `agent_hub.http.headers` on every response.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, Response

from agent_hub.errors import HubError
from agent_hub.http.context import Context, HubContext
from agent_hub.http.routing import API_PREFIX, HubRouter

MANIFEST_FILE = "build-manifest.json"
GUIDE_FILE = "agents.md"
ASSET_CACHE = "public, max-age=31536000, immutable"
PAGE_CACHE = "no-cache"
# 06-web.md section 2.4, path parameters as single segments.
SPA_ROUTES = (
    "/",
    "/invite",
    "/connect",
    "/login",
    "/recover",
    "/o/{org}",
    "/o/{org}/w/{wg}",
    "/o/{org}/t/{task}",
    "/o/{org}/s/{session}",
    "/o/{org}/whiteboard",
    "/o/{org}/w/{wg}/whiteboard",
    "/o/{org}/members",
    "/o/{org}/workgroups",
    "/o/{org}/w/{wg}/settings",
    "/o/{org}/devices",
    "/o/{org}/monitoring",
    "/o/{org}/audit",
    "/notifications",
    "/settings",
    "/platform/invites",
    "/security",
)

router = HubRouter()


def page(context: HubContext, status: int) -> Response:
    """`index.html` of the build, never cached without revalidation."""
    index = context.config.static_dir / "index.html"
    if not index.is_file():
        raise HubError("unavailable")
    return Response(
        index.read_bytes(),
        status_code=status,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": PAGE_CACHE},
    )


def asset_path(static_dir: Path, name: str) -> Path | None:
    """The file `assets/<name>` inside the static directory; None for anything that leaves it or is no file."""
    root = (static_dir / "assets").resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


@router.get("/assets/{name:path}", auth="anon", right="anon", include_in_schema=False)
async def asset(name: str, context: Context) -> Response:
    path = asset_path(context.config.static_dir, name)
    if path is None:
        raise HubError("not_found")
    return FileResponse(path, headers={"Cache-Control": ASSET_CACHE})


@router.get(f"/{MANIFEST_FILE}", auth="anon", right="anon", include_in_schema=False)
async def build_manifest(context: Context) -> Response:
    path = context.config.static_dir / MANIFEST_FILE
    if not path.is_file():
        raise HubError("not_found")
    return Response(path.read_bytes(), media_type="application/json", headers={"Cache-Control": PAGE_CACHE})


@router.get(f"/{GUIDE_FILE}", auth="anon", right="anon", include_in_schema=False)
async def agent_guide(context: Context) -> Response:
    path = context.config.static_dir / GUIDE_FILE
    if not path.is_file():
        raise HubError("not_found")
    return Response(path.read_bytes(), media_type="text/markdown; charset=utf-8", headers={"Cache-Control": PAGE_CACHE})


async def spa_page(context: Context) -> Response:
    return page(context, 200)


for _path in SPA_ROUTES:
    router.get(_path, auth="anon", right="anon", include_in_schema=False)(spa_page)


@router.get("/{rest:path}", auth="anon", right="anon", include_in_schema=False)
async def not_found_page(rest: str, context: Context) -> Response:
    """The catch-all: JSON 404 below `/v1/` and `/assets/`, else the page with 404. Registered last by the app."""
    if f"/{rest}".startswith((API_PREFIX, "/assets/")):
        raise HubError("not_found")
    return page(context, 404)
