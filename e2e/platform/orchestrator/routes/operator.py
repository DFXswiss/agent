"""`POST /operator` (`{"args": ["<command>", ...]}`): the hub operator CLI `agent-hub platform <command> --key-file
<run key file> --hub <public origin> ...` on the hub device, for invitations, certifications and role grants. The key
file holds the public test root key (`services.write_operator_key`); the CLI reaches the hub under the public origin
(T1: the runner relay, T2: the operator relay of the hub device). Answers `{rc, stdout, stderr}`; until the CLI
exists (HUB-04) `501 not_available`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError, not_available
from orchestrator.services import write_operator_key

KEY_FILE = "platform-root.json"
OPERATOR_RELAY = "operator-hub"


def post_operator(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    args = body.get("args") if isinstance(body, dict) else None
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise RouteError(400, "invalid", 'body must be {"args": ["<subcommand>", ...]}')
    hub = ctx.hub
    if hub is None or not hub.has_command("platform"):
        raise not_available("the operator CLI agent-hub platform", "HUB-04")
    if ctx.runner_node is not ctx.hub_node and OPERATOR_RELAY not in ctx.hub_node.relays():
        # T2: the public origin's port on the hub device leads to the hub too (opened on first use).
        public, target = ("127.0.0.1", ctx.config.port("public")), (ctx.bind, ctx.config.port("hub"))
        ctx.hub_node.relay_start(OPERATOR_RELAY, public, target)
    key = write_operator_key(Path(ctx.scratch) / KEY_FILE)
    argv = [hub.python, "-m", "agent_hub", "platform", args[0], "--key-file", str(key)]
    argv += ["--hub", ctx.config.public_origin, *args[1:]]
    done = hub.node.run(argv, env=hub.env)
    return 200, {"rc": done["rc"], "stdout": done["stdout"], "stderr": done["stderr"]}


ROUTES = [Route("POST", "/operator", post_operator)]
