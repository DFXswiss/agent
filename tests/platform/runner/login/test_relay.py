"""U-RUN-login (relay): `claude.login` mode `manual` (05-runner.md section 11.2) through the real request service, the
hub kit, the login-CLI double in a PTY and the auth-page and mail doubles; PTY time limits, wrong follow-ups, the
temporary directory and the secrecy of token and code."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.cli import authorize_url
from agent_runner.credentials import cache
from agent_runner.login import pty
from agent_runner.login import requests as login_requests
from agent_runner.login.relay import LoginSession
from agent_runner.sync.http import HubRefusal, HubTransportError

from ..credentials.kit import ALICE, WALL, seal_request
from .conftest import FAST, Login, Run, cli_argv, code_for, run_request, script_argv

MANUAL = {"mode": "manual"}


def test_relay_stores_the_subscription_and_removes_the_temporary_directory(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        first = await run.wait_bodies(1)
        assert first == [{"step": "authorize", "url": authorize_url(login.auth.page_url, codes.DEFAULT_SEED)}]
        assert run.finals(run.root) == [False]
        assert [p.name for p in run.tmp_dir.iterdir()] == [f"login-{run.root}"]
        workdir = run.tmp_dir / f"login-{run.root}"
        assert workdir.stat().st_mode & 0o777 == 0o700
        assert (workdir / "config" / ".claude.json").is_file()  # the CLI's configuration went to the temp dir
        pasted = await asyncio.to_thread(code_for, login, first[0]["url"])
        follow = await run.follow({"code": pasted})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        assert run.finals(follow) == [True]
        record = run.env.hub.records[(ALICE, "claude_subscription")]
        assert record["header"]["author"] == run.env.context.identity.device.device_id
        async with cache.opened(run.env.context, ALICE, "claude_subscription") as body:
            stored = dict(body)
        token = codes.access_token(codes.DEFAULT_SEED, 1)
        assert stored == {"token": token, "email": None, "kind": "setup_token"}
        assert not workdir.exists()
        run.secrets_absent(token, pasted, pasted.split("#")[0])
        assert f"runner.login done request_id={run.root}" in run.env.log()
        assert login.auth.state()["failures"] == []

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login))


def test_a_token_wrapped_in_a_narrow_window_is_joined(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The double lays out for the PTY's width like the real UI; in 64 columns (the setup-token label row, 62
    characters, must fit one row) the 108-character token spans two rows and the link several, and both are joined."""
    monkeypatch.setattr(pty, "COLUMNS", 64)
    login = doubles()

    async def check(run: Run) -> None:
        first = await run.wait_bodies(1)
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, first[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        async with cache.opened(run.env.context, ALICE, "claude_subscription") as body:
            assert body["token"] == codes.access_token(codes.DEFAULT_SEED, 1)

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login))


def test_a_refused_code_ends_code_invalid_at_once_while_the_cli_stays_open(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    """Like the real program the double shows "OAuth error … Press Enter to retry" and does not end: the error on the
    screen ends the login, long before `token_wait`, and the program is killed."""
    login = doubles()

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        started = time.monotonic()
        follow = await run.follow({"code": "wrong#state"})
        await run.settle()
        assert time.monotonic() - started < 30  # token_wait is 120 s
        assert run.bodies(follow) == [{"step": "failed", "code": "code_invalid"}]
        assert run.finals(follow) == [True]
        assert (ALICE, "claude_subscription") not in run.env.hub.records
        assert "runner.login screen rows=" in run.env.log()
        assert " prompt=no error=yes " in run.env.log()
        run.secrets_absent("wrong#state", "Invalid state")

    opts = dataclasses.replace(FAST, token_wait=120.0)
    run_request(
        store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login, "--prompt-delay", "0.2"), opts=opts
    )


def test_the_link_is_relayed_only_once_the_input_field_shows(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    """The double draws the link and the input field in one frame after `--prompt-delay`; the relay answers the link
    only then, because the real program drops what is typed before its input field shows (KOR-21), so the code is
    typed into a shown field."""
    login = doubles()

    async def check(run: Run) -> None:
        started = time.monotonic()
        first = await run.wait_bodies(1)
        assert time.monotonic() - started >= 8  # link and field are one frame, after the delay
        pasted = await asyncio.to_thread(code_for, login, first[0]["url"])
        follow = await run.follow({"code": pasted})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]

    argv = cli_argv(login, "--prompt-delay", "8")
    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=argv)


SILENT_CLI = (
    "import os, subprocess, sys, time\n"
    "subprocess.run([os.environ['BROWSER'], 'http://localhost:45454/callback-redirect?x=1'])\n"
    'print("Browser didn\'t open? Use the URL below to sign in (c to copy)", flush=True)\n'
    "print(flush=True)\n"
    "print('https://auth.example/oauth/authorize?x=1', flush=True)\n"
    "print(flush=True)\n"
    "print('Paste code here if prompted', flush=True)\n"
    "gate = sys.argv[1]\n"
    "while not os.path.exists(os.path.join(gate, 'go')):\n"
    "    time.sleep(0.05)\n"
    "sys.stdout.write('\\x1b[2J\\x1b[H')\n"
    "sys.stdout.flush()\n"
    "open(os.path.join(gate, 'erased'), 'w').close()\n"
)


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        ("print('Opening browser', flush=True); time.sleep(30)", "timeout"),
        ("print('bye'); sys.exit(0)", "login_cli_failed"),
    ],
)
def test_an_input_field_erased_before_the_code_is_typed_ends_the_login(
    store_dsn: str, tmp_path: Path, tail: str, code: str
) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()

    async def check(run: Run) -> None:
        await run.wait_bodies(1)  # the link and the field are on the screen
        (gate / "go").touch()
        deadline = time.monotonic() + 20
        while not (gate / "erased").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert (gate / "erased").exists()  # the stand-in then erases the field before the code is sent
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": code}]
        assert "runner.login screen rows=1 marker=no token_blocks=0 prompt=no error=" in run.env.log()

    opts = dataclasses.replace(FAST, prompt_wait=0.5)
    run_request(
        store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(SILENT_CLI + tail, str(gate)), opts=opts
    )


@pytest.mark.parametrize("body", [{"code": "two words"}, {"code": 7}, {"mode": "manual"}, {"code": "x", "more": 1}])
def test_a_malformed_follow_up_ends_code_invalid(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login], body: dict[str, object]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        follow = await run.follow(body)
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "code_invalid"}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login))


def test_a_follow_up_of_another_user_is_refused_and_the_flow_goes_on(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        first = await run.wait_bodies(1)
        pasted = await asyncio.to_thread(code_for, login, first[0]["url"])
        forged = await run.follow({"code": pasted}, user="bob")
        assert run.env.hub.stored_requests[forged].responses == []
        assert run.service.flows[run.root].chain == []
        audits = await run.env.audits()
        assert [a["target_id"] for a in audits][-1:] == [forged]
        follow = await run.follow({"code": pasted})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login))


@pytest.mark.parametrize("body", [{}, {"mode": "other"}, {"mode": "manual", "x": 1}, {"code": "abc"}])
def test_a_malformed_root_is_answered_upstream_error_without_starting_the_cli(
    store_dsn: str, tmp_path: Path, body: dict[str, object]
) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "upstream_error"}]
        assert not run.tmp_dir.exists()

    run_request(store_dsn, tmp_path, "claude.login", body, check)


def test_no_authorize_url_in_time_ends_timeout(store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "timeout"}]

    opts = dataclasses.replace(FAST, url_wait=0.5)
    argv = cli_argv(login, "--variant", "no-url", "--code-timeout", "30")
    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=argv, opts=opts)


def test_a_cli_that_never_ends_after_the_code_ends_timeout(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        follow = await run.follow({"code": "any#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "timeout"}]

    opts = dataclasses.replace(FAST, token_wait=0.5)
    argv = cli_argv(login, "--variant", "timeout", "--code-timeout", "30")
    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=argv, opts=opts)


def test_no_code_before_the_request_expires_ends_timeout(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [
            {"step": "authorize", "url": run.bodies()[0]["url"]},
            {"step": "failed", "code": "timeout"},
        ]

    opts = dataclasses.replace(FAST, code_margin=899.5)
    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login), opts=opts)


def test_a_newer_login_replaces_the_running_one_and_its_directory_is_removed(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        newer = run.env.hub.add_request(seal_request("claude.login", MANUAL, ts=WALL + 1))
        await run.service.process(newer)
        await run.wait_bodies(1, newer)
        assert sorted(p.name for p in run.tmp_dir.iterdir()) == [f"login-{newer}"]
        assert f"abandoned request_id={run.root} reason=replaced" in run.env.log()
        follow = run.env.hub.add_request(seal_request("claude.login", {"code": "late#code"}, follows=run.root))
        await run.service.process(follow)
        assert run.env.hub.stored_requests[follow].responses == []
        await run.service.shutdown()
        assert run.bodies(newer)[-1] == {"step": "failed", "code": "device_shutdown"}

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=cli_argv(login))


def test_the_cli_runs_with_an_allow_list_environment_inside_the_temporary_directory(
    store_dsn: str, tmp_path: Path
) -> None:
    out = tmp_path / "seen.json"
    source = (
        "import json, os, sys; "
        "json.dump({'env': dict(os.environ), 'cwd': os.getcwd(), 'tty': os.isatty(0)}, open(sys.argv[1], 'w')); "
        "sys.exit(3)"
    )

    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "login_cli_failed"}]
        seen = json.loads(out.read_text())
        workdir = str(run.tmp_dir / f"login-{run.root}")
        env = seen["env"]
        assert set(env) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {
            "PATH",
            "HOME",
            "CLAUDE_CONFIG_DIR",
            "TMPDIR",
            "BROWSER",
            "TERM",
            "LANG",
            "COLUMNS",
            "NO_COLOR",
        }
        for name in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "BROWSER"):
            assert Path(env[name]).resolve().is_relative_to(Path(workdir).resolve()), name
        assert Path(seen["cwd"]).resolve() == Path(workdir).resolve()
        paths = env["PATH"].split(":")
        assert paths[1:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        assert seen["tty"] is True
        for value in env.values():
            assert ".claude" not in value.replace(workdir, "")
            assert "/.local/share/agent" not in value

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(source, str(out)))


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (("no-such-login-cli-run13",), "login_cli_failed"),
        (script_argv("import sys; sys.exit(0)"), "login_cli_failed"),
        (
            script_argv(
                "import os, subprocess, time; "
                "subprocess.run([os.environ['BROWSER'], 'ftp://x']); "
                'print("Browser didn\'t open? Use the URL below to sign in (c to copy)", flush=True); '
                "print('ftp://x', flush=True); "
                "print('Paste code here if prompted', flush=True); "
                "time.sleep(30)"
            ),
            "login_cli_failed",
        ),
    ],
)
def test_a_cli_that_cannot_start_or_opens_no_valid_url_fails(
    store_dsn: str, tmp_path: Path, argv: tuple[str, ...], code: str
) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": code}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=argv, env_path=str(tmp_path))


URL_THEN = (
    "import os, subprocess, sys; "
    "subprocess.run([os.environ['BROWSER'], 'http://localhost:45454/callback-redirect?x=1']); "
    'print("Browser didn\'t open? Use the URL below to sign in (c to copy)", flush=True); '
    "print(flush=True); "
    "print('https://auth.example/oauth/authorize?x=1', flush=True); "
    "print(flush=True); "
    "print('Paste code here if prompted', flush=True); "
    "line = sys.stdin.readline(); "
)


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        ("print('Something else went wrong'); sys.exit(1)", "login_cli_failed"),
        ("print('OAuth error: Invalid code'); sys.exit(1)", "code_invalid"),
        ("print('Your OAuth token (valid for 1 year):'); print(); print('not-a-token'); sys.exit(0)", "token_invalid"),
        ("print('done without a token'); sys.exit(0)", "token_invalid"),
    ],
)
def test_the_end_of_the_cli_is_mapped_to_a_failure_code(store_dsn: str, tmp_path: Path, tail: str, code: str) -> None:
    async def check(run: Run) -> None:
        assert (await run.wait_bodies(1))[0]["url"] == "https://auth.example/oauth/authorize?x=1"
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": code}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(URL_THEN + tail))


def test_an_unreadable_token_screen_logs_its_shape_only(store_dsn: str, tmp_path: Path) -> None:
    broken = "sk-" + "ant-oat01-" + "B" * 40 + " (copy)"
    tail = f"print('Your OAuth token (valid for 1 year):'); print(); print({broken!r}); sys.exit(0)"

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "token_invalid"}]
        log = run.env.log()
        assert "runner.login screen rows=6 marker=yes token_blocks=0 prompt=yes error=no request_id=" in log
        for text in ("B" * 40, "auth.example", "some#code", "OAuth token"):
            assert text not in log  # the form of the screen only, never its text, the URL or the code

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(URL_THEN + tail))


TOKEN_CLI = URL_THEN + (
    "print('Your OAuth token (valid for 1 year):'); print(); "
    "print('sk-' + 'ant-oat01-' + 'A' * 40); print(); sys.exit(0)"
)


def test_without_the_owners_credential_key_the_login_ends_credential_missing(store_dsn: str, tmp_path: Path) -> None:
    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "credential_missing"}]
        assert "store_failed reason=key_missing" in run.env.log()

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(TOKEN_CLI), ck=False)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (cache.CredentialUnavailable("refused", "forbidden"), "upstream_error"),
        (HubRefusal(500, "internal"), "upstream_error"),
        (HubTransportError("hub_unreachable"), "upstream_unreachable"),
    ],
)
def test_a_failed_credential_write_is_answered(
    store_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    async def refuse(*_args: object, **_options: object) -> int:
        raise error

    monkeypatch.setattr(login_requests, "put", refuse)

    async def check(run: Run) -> None:
        await run.wait_bodies(1)
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": code}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(TOKEN_CLI))


@pytest.mark.parametrize("lost", [["before"], ["after"]])
def test_a_credential_write_lost_once_is_sent_again(store_dsn: str, tmp_path: Path, lost: list[str]) -> None:
    """KOR-18: one write lost in transport (before or after the hub stored it) does not discard the completed login;
    the repeat sends the same record, so the hub keeps exactly one version."""

    async def check(run: Run) -> None:
        run.env.hub.put_lost.extend(lost)
        await run.wait_bodies(1)
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        record = run.env.hub.records[(ALICE, "claude_subscription")]
        assert record["header"]["version"] == 1
        puts = [r for r in run.env.hub.requests if r.method == "PUT"]
        assert len(puts) == 2
        assert puts[0].raw == puts[1].raw
        log = run.env.log()
        assert "runner.credentials store_retry step=write attempt=1 reason=hub_unreachable cause=ReadError\n" in log

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(TOKEN_CLI))


def test_a_credential_write_that_stays_lost_ends_upstream_unreachable(store_dsn: str, tmp_path: Path) -> None:
    async def check(run: Run) -> None:
        run.env.hub.put_lost.extend(["before"] * 4)
        await run.wait_bodies(1)
        follow = await run.follow({"code": "some#code"})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "upstream_unreachable"}]
        assert len([r for r in run.env.hub.requests if r.method == "PUT"]) == 4
        assert "runner.login store_failed reason=hub_unreachable cause=ReadError" in run.env.log()

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv(TOKEN_CLI))


def test_a_program_the_system_cannot_execute_fails(store_dsn: str, tmp_path: Path) -> None:
    program = tmp_path / "broken-cli"
    program.write_bytes(b"\x00\x01not a program")
    program.chmod(0o700)

    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "login_cli_failed"}]

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=(str(program),))


def test_an_incomplete_cleanup_is_logged(store_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = LoginSession.close

    def close(session: LoginSession) -> None:
        original(session)
        session.left = True

    monkeypatch.setattr(LoginSession, "close", close)

    async def check(run: Run) -> None:
        await run.settle()
        assert f"runner.login cleanup_incomplete request_id={run.root}" in run.env.log()

    run_request(store_dsn, tmp_path, "claude.login", MANUAL, check, argv=script_argv("import sys; sys.exit(1)"))
