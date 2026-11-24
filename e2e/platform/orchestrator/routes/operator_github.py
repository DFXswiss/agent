"""`POST /runners/{name}/operator-github`: the operator's GitHub account on a platform runner (ADR-053, ADR-076
section 7; 05-runner.md section 1.3), for the identities scenario (E2E-58).

`{"token": "<token>", "org": "<organization ID>"}` writes the token as one line into `token` of the directory
`<runner home>-operator-github` on the runner device - the directory with mode 0700 and the file with mode 0600, both
owned by the runner's account, as `agent_runner.github.operator.read_token` requires; the file is replaced atomically -
and sets `AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE` and `AGENT_RUNNER_OPERATOR_GITHUB_ORG`. `{"token": null}` deletes
the directory and unsets both. The runner reads its settings at start, so a running runner is restarted (same home:
identity, store and VMs stay). The token is never answered. Answers `{name, token_file, org, restarted}`."""

from __future__ import annotations

import re
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

TOKEN_FILE = "AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"
ORG = "AGENT_RUNNER_OPERATOR_GITHUB_ORG"
SUFFIX = "-operator-github"
# The runner's token format: printable ASCII without spaces, up to 4096 characters.
TOKEN_RE = re.compile(r"[\x21-\x7e]{1,4096}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
WRITE = (
    'umask 077 && mkdir -p "$1" && chmod 700 "$1" && printf "%s\\n" "$E2E_TOKEN" > "$1/token.tmp"'
    ' && mv -f "$1/token.tmp" "$1/token"'
)
REMOVE = 'rm -rf "$1"'
PATH = "/usr/bin:/bin"


def post_operator_github(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    if not isinstance(body, dict) or "token" not in body:
        raise RouteError(400, "invalid", 'body must be {"token": "<token>", "org": "<uuid>"} or {"token": null}')
    token, org = body.get("token"), body.get("org")
    directory = runner.env["AGENT_RUNNER_HOME"] + SUFFIX
    if token is None:
        done = runner.node.run(["/bin/sh", "-c", REMOVE, "sh", directory], env={"PATH": PATH})
        settings: dict[str, str] = {}
    else:
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise RouteError(400, "invalid", "token must be printable ASCII without spaces")
        if not isinstance(org, str) or not UUID_RE.fullmatch(org):
            raise RouteError(400, "invalid", "org must be an organization ID")
        done = runner.node.run(["/bin/sh", "-c", WRITE, "sh", directory], env={"PATH": PATH, "E2E_TOKEN": token})
        settings = {TOKEN_FILE: f"{directory}/token", ORG: org}
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"operator token file: {str(done['stderr']).strip()[-300:]}")
    runner.env.pop(TOKEN_FILE, None)
    runner.env.pop(ORG, None)
    runner.env.update(settings)
    restart = runner.state() != "stopped"
    if restart:
        runner.stop()
        runner.start()
    return 200, {
        "name": runner.name,
        "token_file": runner.env.get(TOKEN_FILE),
        "org": runner.env.get(ORG),
        "restarted": restart,
    }


ROUTES = [Route("POST", "/runners/{name}/operator-github", post_operator_github)]
