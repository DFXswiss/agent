"""Control routes of the full live tests (LVT-01; 07-testing.md sections 2.4 and 6; live runs only, `run.sh --live`).

- `POST /live/github` (`{}`; LIVE-02): the runners talk to the real GitHub through the live bridge on the runner
  device (`orchestrator/live_bridge.py`): API URL `<bridge>/live/github_<secret>` (`quota.github_name`: the bridge's
  port is tunnelled into the VMs, the guests must not reach GitHub through it), web URL the bridge's origin (the tarball
  redirect to codeload comes back as `<bridge>/codeload/...`). The runners read the URLs at start and are restarted
  with the same home. Answers `{api_url, web_url, restarted}`.
- `POST /live/catalog` (`{"providers_allowed": ["<label>", ...]}`; LIVE-01, B-13): a copy of the runner's packaged
  model catalog (`agent_runner.model.catalog.PACKAGED`) whose platform access allows only the given OpenRouter
  providers, written to the run's scratch on the runner device and configured for every runner
  (`AGENT_RUNNER_MODEL_CATALOG`, restart with the same home). A provider OpenRouter routes to outside that list is
  flagged by the gateway and audited by the runner (`model.provider_outside_allowlist`). Answers `{path, restarted}`.
- `POST /live/model-double` (`{}`; LIVE-02 after LIVE-01/LIVE-03 in the same run): the runners' model upstreams point
  at the run's model double again (as at the start of the run; restart with the same home), for a live test whose
  session is scripted. Answers `{url, restarted}`.
- `GET /live/journal`: the live bridge's journal so far (`{entries: [{method, upstream, path, status}, ...]}`, in
  order; paths without query): the REST sequence LIVE-02 asserts, like E2E-21 does at the GitHub double.
"""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.node import NodeError
from orchestrator.routes import quota
from orchestrator.run import LIVE_BRIDGE_JOURNAL

API_VARIABLE = "AGENT_RUNNER_GITHUB_API_URL"
WEB_VARIABLE = "AGENT_RUNNER_GITHUB_WEB_URL"
CATALOG_VARIABLE = "AGENT_RUNNER_MODEL_CATALOG"
MODEL_VARIABLES = ("AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC", "AGENT_RUNNER_MODEL_UPSTREAM_OPENROUTER")
CATALOG_FILE = "live-model-catalog.json"
MAX_PROVIDERS = 64
_LABEL_RE = re.compile(r"^[ -~]{1,64}$")
PACKAGED_SCRIPT = "from agent_runner.model.catalog import PACKAGED\nprint(PACKAGED.read_text(encoding='utf-8'))\n"
WRITE_SCRIPT = 'umask 077 && mkdir -p "$(dirname "$1")" && printf %s "$E2E_CATALOG" > "$1"'


def _live(ctx: Context) -> None:
    if not ctx.config.live:
        raise RouteError(409, "invalid", "the live routes exist only in a live run (run.sh --live)")


def post_github(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if body not in (None, {}):
        raise RouteError(400, "invalid", "body must be {}")
    _live(ctx)
    double = quota.usage_double(ctx)
    settings = {API_VARIABLE: f"{double.raw_url}/live/{quota.github_name(ctx)}", WEB_VARIABLE: double.raw_url}
    restarted = quota.point_runners(ctx, settings)
    return 200, {"api_url": settings[API_VARIABLE], "web_url": settings[WEB_VARIABLE], "restarted": restarted}


def post_catalog(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    providers = body.get("providers_allowed") if isinstance(body, dict) and len(body) == 1 else None
    if (
        not isinstance(providers, list)
        or len(providers) > MAX_PROVIDERS
        or not all(isinstance(item, str) and _LABEL_RE.fullmatch(item) for item in providers)
    ):
        raise RouteError(400, "invalid", 'body must be {"providers_allowed": ["<label>", ...]} (at most 64)')
    _live(ctx)
    node = ctx.runner_node
    done = node.run([ctx.agent_python, "-I", "-c", PACKAGED_SCRIPT])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"packaged model catalog not readable: {done['stderr'].strip()[-300:]}")
    try:
        catalog = json.loads(done["stdout"])
        catalog["access"]["platform"]["providers_allowed"] = providers
    except (ValueError, KeyError, TypeError):
        raise RouteError(500, "failed", "the packaged model catalog has no platform access") from None
    path = f"{ctx.runner_scratch}/{CATALOG_FILE}"
    text = json.dumps(catalog, indent=1, sort_keys=True)
    written = node.run(["/bin/sh", "-c", WRITE_SCRIPT, "sh", path], env={"E2E_CATALOG": text, "PATH": "/usr/bin:/bin"})
    if written["rc"] != 0:
        raise RouteError(500, "failed", f"model catalog not written: {written['stderr'].strip()[-300:]}")
    return 200, {"path": path, "restarted": quota.point_runners(ctx, {CATALOG_VARIABLE: path})}


def post_model_double(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if body not in (None, {}):
        raise RouteError(400, "invalid", "body must be {}")
    _live(ctx)
    url = ctx.doubles["model"].url
    settings = dict.fromkeys(MODEL_VARIABLES, url)
    return 200, {"url": url, "restarted": quota.point_runners(ctx, settings)}


def get_journal(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    _live(ctx)
    try:
        text = ctx.runner_node.read_text(f"{ctx.runner_run_dir}/{LIVE_BRIDGE_JOURNAL}")
    except NodeError:
        return 200, {"entries": []}
    entries = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a line the bridge is still writing
        upstream = entry.get("upstream")
        if isinstance(upstream, str) and upstream.startswith("github_"):
            entry["upstream"] = "github"  # never hand out the secret name
        entries.append({name: entry.get(name) for name in ("method", "upstream", "path", "status")})
    return 200, {"entries": entries}


ROUTES = [
    Route("POST", "/live/github", post_github),
    Route("POST", "/live/catalog", post_catalog),
    Route("POST", "/live/model-double", post_model_double),
    Route("GET", "/live/journal", get_journal),
]
