"""Operator CLI `agent-hub platform …` end to end against the test hub (ADR-023, 04-crypto.md section 5.9): root key
file, every command, the local checks (root key of the hub, verified log, response signatures), retries and errors."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg_pool import ConnectionPool

from agent_core.signing import format_time, response_text, sha256_hex
from agent_hub import cli as main_cli
from agent_hub.config import HubConfig, load_config
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import ed25519_public_raw, ed25519_seed
from agent_hub.platform import cli
from tests.platform.hub.conftest import (
    FixedClock,
    HubDatabase,
    HubHarness,
    hub_environ,
    root_private_key,
    start_hub,
    uuid7,
)
from tests.platform.hub.platform.conftest import Platform, new_org, new_user


def write_key(path: Path, key: Ed25519PrivateKey, mode: int = 0o600) -> Path:
    path.write_text(json.dumps({"v": 1, "root": b64u_encode(ed25519_seed(key))}), encoding="ascii")
    path.chmod(mode)
    return path


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """The test root key (the key of the `hub` fixture) in a 0600 key file."""
    return write_key(tmp_path / "root.json", root_private_key())


def parse(*argv: str) -> argparse.Namespace:
    return main_cli.build_parser().parse_args(["platform", *argv])


@pytest.fixture
def run(hub: HubHarness, key_file: Path) -> Callable[..., tuple[int, str, str]]:
    """Run a command with the hub's test client and clock; `(exit code, stdout, stderr)`."""

    def call(*argv: str, http: httpx.Client | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = parse(*argv[:1], "--key-file", str(key_file), "--hub", "http://localhost:8788", *argv[1:])
        code = cli.run(args, http=http or hub.base, clock=hub.clock, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    return call


def test_init_creates_an_offline_key_file(tmp_path: Path) -> None:
    path = tmp_path / "root.json"
    out = io.StringIO()
    assert cli.run(parse("init", "--key-file", str(path)), out=out) == cli.EXIT_OK
    key = cli.load_key_file(path)
    lines = out.getvalue().splitlines()
    assert lines[0] == f"root public key {b64u_encode(ed25519_public_raw(key))}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b64u_encode(ed25519_seed(key)) not in out.getvalue()
    err = io.StringIO()
    assert cli.run(parse("init", "--key-file", str(path)), err=err) == cli.EXIT_USAGE
    assert "refusing to overwrite" in err.getvalue()
    assert cli.load_key_file(path).public_key().public_bytes_raw() == key.public_key().public_bytes_raw()


def test_main_dispatches_the_platform_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main_cli.main(["platform", "init", "--key-file", str(tmp_path / "k.json")]) == 0
    assert capsys.readouterr().out.startswith("root public key ")


@pytest.mark.parametrize(
    ("content", "mode", "message"),
    [
        (None, 0o600, "cannot be read"),
        ('{"v":1,"root":"AAAA"}', 0o644, "group or others"),
        ("not json", 0o600, "not a platform root key"),
        ('{"v":2,"root":"AAAA"}', 0o600, "not a platform root key"),
        ('{"v":1,"root":"AAAA"}', 0o600, "not a platform root key"),
        ("[1]", 0o600, "not a platform root key"),
    ],
)
def test_bad_key_files_are_usage_errors(tmp_path: Path, content: str | None, mode: int, message: str) -> None:
    path = tmp_path / "root.json"
    if content is not None:
        path.write_text(content, encoding="ascii")
        path.chmod(mode)
    err = io.StringIO()
    args = parse("decertify", "--key-file", str(path), "--hub", "http://localhost:8788", "--device", uuid_v7())
    assert cli.run(args, err=err) == cli.EXIT_USAGE
    assert message in err.getvalue()


def uuid_v7() -> str:
    return str(uuid7())


def test_a_key_file_that_cannot_be_created_is_a_usage_error(tmp_path: Path) -> None:
    err = io.StringIO()
    assert cli.run(parse("init", "--key-file", str(tmp_path / "missing" / "k.json")), err=err) == cli.EXIT_USAGE
    assert "cannot be created" in err.getvalue()


@pytest.fixture
def cli_hub(
    migrated_db: HubDatabase, hub_pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock, tmp_path: Path
) -> Iterator[tuple[HubHarness, Path]]:
    """A hub configured with a root key that `platform init` created."""
    path = tmp_path / "operator.json"
    out = io.StringIO()
    assert cli.run(parse("init", "--key-file", str(path)), out=out) == 0
    public = out.getvalue().split()[3]
    config: HubConfig = load_config(hub_environ(migrated_db, AGENT_HUB_PLATFORM_ROOT_KEY=public))
    for harness in start_hub(config, hub_pool, clock):
        yield harness, path


def test_operator_flow_end_to_end(cli_hub: tuple[HubHarness, Path]) -> None:
    hub, path = cli_hub

    def call(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = parse(argv[0], "--key-file", str(path), "--hub", "http://localhost:8788", *argv[1:])
        return cli.run(args, http=hub.base, clock=hub.clock, out=out, err=err), out.getvalue(), err.getvalue()

    init = ("init",)
    assert call(*init) == (0, "root entry appended (seq 0)\n", "")
    assert call(*init) == (0, "root entry present\n", "")
    code, out, _ = call("invite-user")
    lines = out.splitlines()
    assert code == 0
    assert lines[0].startswith("invitation code ")
    secrets_of_code = invite_codes.invite_secrets(invite_codes.parse_code(lines[0].split()[2]))
    assert lines[1] == f"invite_id {secrets_of_code.invite_id} expires 2026-11-24T12:00:00.123Z (seq 1)"
    lookup = hub.anon.post("/v1/invites/lookup", json={"invite_id": secrets_of_code.invite_id}).json()
    assert (lookup["scope"], lookup["kind"]) == ("platform", "user")
    code, out, _ = call("invite-device", "--expires-hours", "2")
    device_code = invite_codes.invite_secrets(invite_codes.parse_code(out.splitlines()[0].split()[2]))
    assert out.splitlines()[1].endswith("expires 2026-11-21T14:00:00.123Z (seq 2)")
    plat = Platform(hub, root=cli.load_key_file(path))
    device = plat.enroll(device_code)
    assert call("certify-pending") == (0, f"certified {device.device_id} (seq 3)\n", "")
    assert call("certify-pending") == (0, "no pending enrollments\n", "")
    with hub.pool.connection() as connection:
        assert connection.execute("SELECT state, certified_seq FROM hub.device").fetchall() == [("active", 3)]
    user, org = str(new_user(hub)), new_org(hub)
    assert call("grant-role", "inviter", "--user", user) == (0, "appended role_grant (seq 4)\n", "")
    assert call("revoke-role", "inviter", "--user", user) == (0, "appended role_revoke (seq 5)\n", "")
    assert call("grant-org-access", "--org", org) == (0, "appended org_platform_access (seq 6)\n", "")
    assert call("grant-org-access", "--org", org, "--revoke")[0] == 0
    assert call("decertify", "--device", device.device_id) == (0, "appended device_decertify (seq 8)\n", "")
    with hub.pool.connection() as connection:
        assert connection.execute("SELECT state FROM hub.device").fetchall() == [("decertified",)]
        rows = connection.execute("SELECT type FROM hub.platform_entry ORDER BY seq").fetchall()
    assert [row[0] for row in rows] == [
        "root",
        "user_invite",
        "device_invite",
        "device_certify",
        "role_grant",
        "role_revoke",
        "org_platform_access",
        "org_platform_access",
        "device_decertify",
    ]
    key_text = path.read_text(encoding="ascii")
    assert json.loads(key_text)["root"] not in out


def test_the_first_command_writes_the_root_entry(run: Callable[..., tuple[int, str, str]], hub: HubHarness) -> None:
    user = str(new_user(hub))
    assert run("grant-role", "inviter", "--user", user) == (0, "appended role_grant (seq 1)\n", "")


def test_certify_pending_selects_devices(run: Callable[..., tuple[int, str, str]], plat: Platform) -> None:
    first, second = plat.enroll(), plat.enroll()
    assert run("certify-pending", "--device", uuid_v7())[0:3:2] == (1, "agent-hub platform: refused: not_found\n")
    assert run("certify-pending", "--device", second.device_id)[1] == f"certified {second.device_id} (seq 3)\n"
    code, out, _ = run("certify-pending", "--device", first.device_id, "--device", first.device_id)
    assert (code, out) == (0, f"certified {first.device_id} (seq 4)\n")


def test_local_checks_refuse_before_sending(run: Callable[..., tuple[int, str, str]], plat: Platform) -> None:
    plat.init()
    assert run("decertify", "--device", uuid_v7()) == (1, "", "agent-hub platform: refused: log_rights\n")
    with plat.hub.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM hub.platform_entry").fetchone() == (1,)


def test_the_hub_refusal_is_reported(run: Callable[..., tuple[int, str, str]], plat: Platform) -> None:
    plat.init()
    assert run("grant-org-access", "--org", uuid_v7()) == (1, "", "agent-hub platform: refused: not_found\n")


def test_root_mismatch(hub: HubHarness, tmp_path: Path) -> None:
    other = write_key(tmp_path / "other.json", Ed25519PrivateKey.generate())
    err = io.StringIO()
    args = parse("decertify", "--key-file", str(other), "--hub", "http://localhost:8788", "--device", uuid_v7())
    assert cli.run(args, http=hub.base, clock=hub.clock, err=err) == cli.EXIT_REFUSED
    assert err.getvalue() == "agent-hub platform: refused: platform_root_mismatch\n"


def test_a_log_the_root_did_not_sign_is_refused(run: Callable[..., tuple[int, str, str]], hub: HubHarness) -> None:
    """A hub that answers with a forged log cannot make the CLI extend it."""
    plat = Platform(hub)
    plat.init()
    forged = Platform(hub, root=Ed25519PrivateKey.generate())
    fake_log = forged.chain(forged.body("root", pub=b64u_encode(ed25519_public_raw(forged.root))))

    def handler(request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode("ascii")
        response = hub.base.request(request.method, target, content=request.content, headers=request.headers)
        if request.url.path == "/v1/platform/enrollments":
            return resign(hub, request, response.status_code, {"enrollments": [], "log": fake_log})
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    fake = httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(handler))
    assert run("decertify", "--device", uuid_v7(), http=fake) == (
        1,
        "",
        "agent-hub platform: refused: platform_root_mismatch\n",
    )


def resign(hub: HubHarness, request: httpx.Request, status: int, body: Any) -> httpx.Response:
    """A response signed with the hub key, as a malicious or buggy hub could send it."""
    content = json.dumps(body).encode("ascii")
    signer = hub.config.signer()
    hub_time = format_time(int(hub.clock().timestamp()))
    text = response_text(
        "platform-root",
        request.headers["agent-nonce"],
        request.headers["agent-time"],
        status,
        hub_time,
        sha256_hex(content),
    )
    headers = {"agent-hub-key": signer.key_id, "agent-hub-time": hub_time, "agent-hub-signature": signer.sign(text)}
    return httpx.Response(status, headers=headers, content=content)


def test_unsigned_or_wrongly_signed_answers_are_refused(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness
) -> None:
    def unsigned(request: httpx.Request) -> httpx.Response:
        response = hub.base.request(request.method, request.url.path, content=request.content, headers=request.headers)
        if request.url.path == "/v1/config":
            return httpx.Response(200, content=response.content)
        return httpx.Response(200, json={"enrollments": [], "log": []})

    fake = httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(unsigned))
    assert run("decertify", "--device", uuid_v7(), http=fake)[2] == "agent-hub platform: refused: signature_invalid\n"


def test_a_signed_non_object_error_is_unavailable(run: Callable[..., tuple[int, str, str]], hub: HubHarness) -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/config":
            response = hub.base.get("/v1/config")
            return httpx.Response(200, content=response.content)
        return resign(hub, request, 503, ["x"])

    fake = httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(odd))
    assert run("decertify", "--device", uuid_v7(), http=fake)[2] == "agent-hub platform: refused: unavailable\n"


def test_an_unreachable_or_failing_hub(run: Callable[..., tuple[int, str, str]]) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake = httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(down))
    assert run("decertify", "--device", uuid_v7(), http=fake) == (
        1,
        "",
        "agent-hub platform: the hub cannot be reached\n",
    )
    failing = httpx.Client(
        base_url="http://localhost:8788", transport=httpx.MockTransport(lambda _r: httpx.Response(503, json={}))
    )
    assert run("decertify", "--device", uuid_v7(), http=failing)[2] == "agent-hub platform: refused: unavailable\n"


def test_a_concurrent_append_is_retried(hub: HubHarness, key_file: Path, plat: Platform) -> None:
    plat.init()
    operator = cli.Operator(http=hub.base, key=cli.load_key_file(key_file), clock=hub.clock)
    operator.connect()
    calls: list[int] = []

    def build(log: Any, _pending: Any) -> list[dict[str, Any]]:
        calls.append(1)
        plat.entries = list(log.entries)
        if len(calls) == 1:
            plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
        return [{"type": "role_grant", "subject": str(new_user(hub)), "role": "inviter"}]

    assert operator.append(build) == cli.Appended(root=False, seqs=[2])
    assert len(calls) == 2

    def always(log: Any, _pending: Any) -> list[dict[str, Any]]:
        plat.entries = list(log.entries)
        plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
        return [{"type": "role_grant", "subject": str(new_user(hub)), "role": "inviter"}]

    with pytest.raises(cli.Refused) as refused:
        operator.append(always)
    assert refused.value.code == "seq_conflict"


@pytest.mark.parametrize(
    "argv",
    [
        ("decertify", "--device", "not-a-uuid"),
        ("invite-user", "--expires-hours", "0"),
        ("invite-user", "--expires-hours", "721"),
        ("invite-device", "--expires-hours", "x"),
        ("grant-role", "admin", "--user", "01a1ee56-5e00-73dd-ae4f-2dc5614b7565"),
        ("decertify", "--device", "01a1ee56-5e00-73dd-ae4f-2dc5614b7565"),
    ],
)
def test_bad_arguments_are_usage_errors(argv: tuple[str, ...], key_file: Path) -> None:
    rest = ["--key-file", str(key_file)] if argv[-1] != "01a1ee56-5e00-73dd-ae4f-2dc5614b7565" else []
    with pytest.raises(SystemExit) as raised:
        parse(argv[0], *rest, *argv[1:])
    assert raised.value.code == 2


def test_the_default_client_targets_the_hub_url(key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Recorder(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            seen.append(str(kwargs["base_url"]))
            super().__init__(transport=httpx.MockTransport(lambda _r: httpx.Response(503, json={})), **kwargs)

    monkeypatch.setattr(httpx, "Client", Recorder)
    args = parse("decertify", "--key-file", str(key_file), "--hub", "https://hub.example", "--device", uuid_v7())
    err = io.StringIO()
    assert cli.run(args, err=err) == cli.EXIT_REFUSED
    assert seen == ["https://hub.example"]
    assert os.environ.get("AGENT_HUB_PLATFORM_ROOT_KEY_FILE") is None


def test_the_record_pins_the_hub_key_and_the_head(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness, key_file: Path
) -> None:
    assert run("grant-role", "inviter", "--user", str(new_user(hub)))[0] == 0
    record = json.loads(cli.state_path(key_file).read_text(encoding="ascii"))
    entry = record["hubs"]["http://localhost:8788"]
    assert entry["head"]["seq"] == 1
    assert entry["hub_key"]["id"] == hub.config.signer().key_id
    assert stat.S_IMODE(cli.state_path(key_file).stat().st_mode) == 0o600
    entry["hub_key"]["public_key"] = b64u_encode(bytes(32))
    cli.state_path(key_file).write_text(json.dumps(record), encoding="ascii")
    assert run("grant-role", "inviter", "--user", str(new_user(hub)))[2] == (
        "agent-hub platform: refused: hub_key_changed\n"
    )


def test_a_shortened_log_is_refused_as_rollback(run: Callable[..., tuple[int, str, str]], hub: HubHarness) -> None:
    assert run("grant-role", "inviter", "--user", str(new_user(hub)))[0] == 0
    with hub.pool.connection() as connection:
        rows = connection.execute("SELECT entry_text FROM hub.platform_entry WHERE seq = 0").fetchall()
    root_entry = json.loads(rows[0][0])

    def shortened(request: httpx.Request) -> httpx.Response:
        response = hub.base.request(request.method, request.url.path, content=request.content, headers=request.headers)
        if request.url.path == "/v1/platform/enrollments":
            return resign(hub, request, 200, {"enrollments": [], "log": [root_entry]})
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    fake = httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(shortened))
    assert run("decertify", "--device", uuid_v7(), http=fake)[2] == "agent-hub platform: refused: log_rollback\n"


@pytest.mark.parametrize("content", ["not json", '{"v":2,"hubs":{}}', '{"v":1,"hubs":[]}'])
def test_a_damaged_record_is_a_usage_error(
    run: Callable[..., tuple[int, str, str]], key_file: Path, content: str
) -> None:
    cli.state_path(key_file).write_text(content, encoding="ascii")
    code, _, err = run("decertify", "--device", uuid_v7())
    assert code == cli.EXIT_USAGE
    assert "is not an operator record" in err


@pytest.mark.parametrize("url", ["http://hub.example", "ftp://localhost"])
def test_the_hub_url_must_be_https_outside_localhost(key_file: Path, url: str) -> None:
    err = io.StringIO()
    args = parse("decertify", "--key-file", str(key_file), "--hub", url, "--device", uuid_v7())
    assert cli.run(args, err=err) == cli.EXIT_USAGE
    assert "https" in err.getvalue()


def test_a_malformed_answer_is_reported_without_a_traceback(run: Callable[..., tuple[int, str, str]]) -> None:
    html = httpx.Client(
        base_url="http://localhost:8788",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"<html>bad gateway</html>")),
    )
    code, _, err = run("decertify", "--device", uuid_v7(), http=html)
    assert code == cli.EXIT_REFUSED
    assert err.startswith("agent-hub platform: refused: unavailable")


def forwarding(hub: HubHarness, refuse: Callable[[httpx.Request], bool]) -> httpx.Client:
    """A client to the hub that answers the requests `refuse` picks with a signed `400 request_invalid`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if refuse(request):
            return resign(hub, request, 400, {"error": "request_invalid"})
        target = request.url.raw_path.decode("ascii")
        response = hub.base.request(request.method, target, content=request.content, headers=request.headers)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    return httpx.Client(base_url="http://localhost:8788", transport=httpx.MockTransport(handler))


def test_the_log_is_read_page_by_page(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-063 section 2 (KOR-35): the CLI reads the platform log of the enrollments page by page."""
    monkeypatch.setattr(cli, "LOG_PAGE", 2)
    targets: list[str] = []

    def note(request: httpx.Request) -> bool:
        targets.append(request.url.raw_path.decode("ascii"))
        return False

    client = forwarding(hub, note)
    for seq in (1, 2, 3):
        assert run("grant-role", "inviter", "--user", str(new_user(hub)), http=client)[1] == (
            f"appended role_grant (seq {seq})\n"
        )
    assert run("grant-role", "inviter", "--user", str(new_user(hub)), http=client)[1] == "appended role_grant (seq 4)\n"
    reads = [target for target in targets if target.startswith("/v1/platform/enrollments")]
    assert reads[-2:] == ["/v1/platform/enrollments?limit=2", "/v1/platform/enrollments?after=1&limit=2"]


def test_a_hub_without_paging_answers_the_whole_log(run: Callable[..., tuple[int, str, str]], hub: HubHarness) -> None:
    assert run("grant-role", "inviter", "--user", str(new_user(hub)))[0] == 0
    old_hub = forwarding(hub, lambda request: request.url.query != b"" and "enrollments" in request.url.path)
    assert run("grant-role", "inviter", "--user", str(new_user(hub)), http=old_hub) == (
        0,
        "appended role_grant (seq 2)\n",
        "",
    )


def test_a_refused_later_page_is_reported(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run("grant-role", "inviter", "--user", str(new_user(hub)))[0] == 0
    monkeypatch.setattr(cli, "LOG_PAGE", 1)
    refusing = forwarding(hub, lambda request: b"after=" in request.url.query)
    assert run("decertify", "--device", uuid_v7(), http=refusing) == (
        1,
        "",
        "agent-hub platform: refused: request_invalid\n",
    )
