"""The Claude subscription login against the login doubles (E2E-28; 05-runner.md sections 11.2, 11.3; ADR-011).

- `POST /login/doubles` (`{"seed": "<seed>", "junk": bool, "fault": "<auth-page fault>" | null, "account": {"user",
  "password"}?}`): on first use starts the mail double (SMTP, IMAP, IMAPS with a runtime CA in the runner device's
  scratch) and the auth-page double (HTTPS pages with the same CA, sending its magic links through the mail double from
  an `anthropic.com` address, the sender the runner's IMAP search filters on) on the runner device, registered as
  doubles `mail` and `authpage` (the run's final check asserts their state), and points every runner at them:
  `AGENT_RUNNER_LOGIN_ARGV` = the login-CLI double (`setup-token` against the auth page, trusting the CA),
  `AGENT_RUNNER_LOGIN_AUTOMATION=on`, `AGENT_RUNNER_LOGIN_SANDBOX=process` and `SSL_CERT_FILE` = the CA (IMAPS and
  the page pin of the automation); running runners are restarted with the same home. Then it adds the mailbox account
  (if given) and loads the auth page's scenario (`seed`: the tokens it issues; `junk`: the magic-link mail lands in `Junk`; `fault`: a page or mail
  failure). Answers `{pages, imaps: {host, port}, restarted}`.
- `POST /login/magic-link` (`{"user", "password", "folder": "INBOX" | "Junk"}`): the newest magic link in a folder of
  the mail double, read over IMAP like a person reading their mail (the password reaches the reader through its
  environment). Answers `{messages, link}` (`link` null when there is none). For the manual relay of a spec: the
  person signs in at the auth page with that link, authorizes and copies the code.
- `GET /runners/{name}/login-dirs`: the runner's temporary login directories (`$AGENT_RUNNER_HOME/tmp/login-*`), which
  must be gone after every login (05-runner.md section 11.2 step 5). Answers `{dirs: [...]}`.

Ports (ADR-027 section 7): auth pages `+12`, auth-page control `+15`, mail control `+16`, SMTP `+17`, IMAPS `+18`,
IMAP `+19`.
"""

from __future__ import annotations

import json
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes.quota import point_runners
from orchestrator.services import READY_TIMEOUT_S, Double

OFFSETS = {"pages": 12, "authpage": 15, "mail": 16, "smtp": 17, "imaps": 18, "imap": 19}
MAIL_FROM = "Claude <no-reply@mail.anthropic.com>"
HOST = "127.0.0.1"
FAULTS = ("email_page", "no_mail", "authorize_page", "code_page", "token")
SCENARIO_KEYS = {"seed", "junk", "fault", "account"}
FOLDERS = ("INBOX", "Junk")
MAGIC_SCRIPT = """import email, imaplib, json, os, re, sys
imap = imaplib.IMAP4(sys.argv[1], int(sys.argv[2]))
imap.login(sys.argv[3], os.environ["E2E_MAILBOX_PASSWORD"])
imap.select(sys.argv[4], readonly=True)
numbers = imap.search(None, "ALL")[1][0].split()
links = []
for number in numbers:
    message = email.message_from_bytes(imap.fetch(number, "(RFC822)")[1][0][1])
    for part in message.walk():
        if part.get_content_maintype() == "text":
            text = part.get_payload(decode=True).decode("utf-8", "replace")
            links += re.findall(r"https://[A-Za-z0-9.:-]+/magic-link#[A-Za-z0-9_-]+", text)
imap.logout()
print(json.dumps({"messages": len(numbers), "link": links[-1] if links else None}))
"""


def _port(ctx: Context, name: str) -> int:
    return ctx.config.port_base + OFFSETS[name]


def _start(ctx: Context, name: str, module: str, args: list[str]) -> Double:
    node = ctx.runner_node
    argv = [ctx.agent_python, "-m", module, "--host", HOST, "--port", str(_port(ctx, name))]
    argv += ["--journal", f"{ctx.runner_run_dir}/{name}-journal.jsonl", *args]
    node.spawn(f"double-{name}", argv, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    node.wait_line(f"double-{name}", r"^http://", READY_TIMEOUT_S)
    double = Double(node, name, _port(ctx, name), _port(ctx, name))
    ctx.doubles[name] = double
    return double


def ca_file(ctx: Context) -> str:
    return f"{ctx.runner_scratch}/login-ca/ca.pem"


def login_doubles(ctx: Context) -> tuple[Double, Double]:
    """The mail and auth-page doubles of the run; started on first use."""
    mail, authpage = ctx.doubles.get("mail"), ctx.doubles.get("authpage")
    if mail is None or authpage is None:
        ca_dir = f"{ctx.runner_scratch}/login-ca"
        ports = ["--smtp-port", str(_port(ctx, "smtp")), "--imaps-port", str(_port(ctx, "imaps"))]
        ports += ["--imap-port", str(_port(ctx, "imap"))]
        mail = _start(ctx, "mail", "agent_doubles.mail", [*ports, "--ca-dir", ca_dir])
        pages = ["--page-port", str(_port(ctx, "pages")), "--ca-dir", ca_dir, "--mail-from", MAIL_FROM]
        authpage = _start(ctx, "authpage", "agent_doubles.login_cli.authpage", [*pages, "--smtp", smtp(ctx)])
    return mail, authpage


def smtp(ctx: Context) -> str:
    return f"{HOST}:{_port(ctx, 'smtp')}"


def pages_url(ctx: Context) -> str:
    return f"https://{HOST}:{_port(ctx, 'pages')}"


def login_argv(ctx: Context) -> list[str]:
    """The login-CLI double as `claude setup-token` (absolute interpreter: the login runs with a bare environment)."""
    return [
        ctx.agent_python,
        "-m",
        "agent_doubles.login_cli",
        "setup-token",
        "--auth-url",
        pages_url(ctx),
        "--ca-file",
        ca_file(ctx),
    ]


def _control(double: Double, path: str, body: Any) -> Any:
    answer = double.node.http("POST", f"{double.raw_url}{path}", body)
    if not 200 <= answer["status"] < 300:  # accounts answer 201
        raise RouteError(400, "invalid", f"{double.name} double refused {path}: {answer['body']}")
    return answer["body"]


def post_doubles(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict) or set(body) - SCENARIO_KEYS:
        raise RouteError(400, "invalid", 'body must be {"seed", "junk", "fault", "account"?}')
    fault, account = body.get("fault"), body.get("account")
    if fault is not None and fault not in FAULTS:
        raise RouteError(400, "invalid", f"fault must be null or one of {', '.join(FAULTS)}")
    if account is not None and not (
        isinstance(account, dict)
        and set(account) == {"user", "password"}
        and all(isinstance(value, str) and value for value in account.values())
    ):
        raise RouteError(400, "invalid", 'account must be {"user": "<address>", "password": "<password>"}')
    mail, authpage = login_doubles(ctx)
    settings = {
        "AGENT_RUNNER_LOGIN_ARGV": json.dumps(login_argv(ctx)),
        "AGENT_RUNNER_LOGIN_AUTOMATION": "on",
        # the doubles run on the test host, which a login VM's boundary does not reach (ADR-098 section 3)
        "AGENT_RUNNER_LOGIN_SANDBOX": "process",
        "SSL_CERT_FILE": ca_file(ctx),
    }
    restarted = point_runners(ctx, settings)
    if account is not None:
        _control(mail, "/_double/accounts", account)
    scenario = {"seed": body.get("seed"), "junk": body.get("junk", False), "fault": fault}
    _control(authpage, "/_double/scenario", scenario)
    imaps = {"host": HOST, "port": _port(ctx, "imaps")}
    return 200, {"pages": pages_url(ctx), "imaps": imaps, "restarted": restarted}


def post_magic_link(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    user = body.get("user") if isinstance(body, dict) else None
    password = body.get("password") if isinstance(body, dict) else None
    folder = body.get("folder") if isinstance(body, dict) else None
    if not isinstance(user, str) or not user or not isinstance(password, str) or not password or folder not in FOLDERS:
        raise RouteError(400, "invalid", 'body must be {"user", "password", "folder": "INBOX" | "Junk"}')
    if "mail" not in ctx.doubles:
        raise RouteError(409, "invalid", "the mail double is not running (POST /login/doubles first)")
    argv = [ctx.agent_python, "-I", "-c", MAGIC_SCRIPT, HOST, str(_port(ctx, "imap")), user, str(folder)]
    done = ctx.runner_node.run(argv, env={"PATH": "/usr/bin:/bin", "E2E_MAILBOX_PASSWORD": password})
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"reading the mailbox failed: {done['stderr'].strip()[-300:]}")
    return 200, json.loads(done["stdout"])


def get_login_dirs(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    script = 'for d in "$1"/tmp/login-*; do [ -e "$d" ] && echo "${d##*/}"; done; true'
    done = runner.node.run(["/bin/sh", "-c", script, "sh", runner.env["AGENT_RUNNER_HOME"]])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"listing the login directories failed: {done['stderr'].strip()[-300:]}")
    return 200, {"dirs": [line for line in str(done["stdout"]).splitlines() if line]}


ROUTES = [
    Route("POST", "/login/doubles", post_doubles),
    Route("POST", "/login/magic-link", post_magic_link),
    Route("GET", "/runners/{name}/login-dirs", get_login_dirs),
]
