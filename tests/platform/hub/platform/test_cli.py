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
from agent_hub.crypto import identity as identity_codes
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import Identity, ed25519_public_raw, ed25519_seed
from agent_hub.platform import cli
from agent_hub.users import identity as users_identity
from tests.platform.hub.conftest import (
    FixedClock,
    HubDatabase,
    HubHarness,
    hub_environ,
    root_private_key,
    start_hub,
    uuid7,
)
from tests.platform.hub.orgs.support import insert_device, insert_identity
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
        response = hub.base.request(request.method, request.url.path, content=request.content, headers=request.headers)
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


def _account(hub: HubHarness) -> tuple[str, dict[str, str]]:
    """A registered user (identity only) and its public keys."""
    user, keys = str(uuid7(hub.clock())), Identity.generate()
    insert_identity(hub.pool, user, keys, hub.clock())
    return user, keys.public()


def test_certify_user_prints_the_account_code_and_appends(
    run: Callable[..., tuple[int, str, str]], plat: Platform, hub: HubHarness
) -> None:
    invite_id = plat.invite_user()
    first, first_pub = _account(hub)
    second, second_pub = _account(hub)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "UPDATE hub.invite SET registered_user = %s, registered_at = %s WHERE id = %s",
            (first, hub.clock(), invite_id),
        )
    first_code = identity_codes.format_account_code(identity_codes.account_code(first, first_pub))
    second_code = identity_codes.format_account_code(identity_codes.account_code(second, second_pub))
    code, out, err = run("certify-user", "--user", first, "--user", second, "--code", first_code.lower())
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"user {first} account code {first_code} registered through {invite_id}",
        f"user {second} account code {second_code} registered through an org invitation",
        f"certified user {first} (seq 2)",
        f"certified user {second} (seq 3)",
    ]
    assert plat.query("SELECT type FROM hub.platform_entry WHERE seq >= 2 ORDER BY seq") == [
        ("user_certify",),
        ("user_certify",),
    ]
    code, out, _ = run("certify-user", "--user", second)
    assert (code, out.splitlines()[1:]) == (0, [f"user {second} is certified already (seq 3)"])
    assert run("decertify-user", "--user", second) == (0, "appended user_decertify (seq 4)\n", "")
    code, out, _ = run("certify-user", "--user", second, "--code", second_code.replace("-", " "))
    assert (code, out.splitlines()[-1]) == (0, f"certified user {second} (seq 5)")


def test_certify_user_refusals(run: Callable[..., tuple[int, str, str]], plat: Platform, hub: HubHarness) -> None:
    plat.init()
    user, _ = _account(hub)
    assert run("certify-user", "--user", user, "--code", "AAAA-AAAA-AAAA") == (
        1,
        "",
        "agent-hub platform: refused: account_code_mismatch\n",
    )
    assert run("certify-user", "--user", user, "--code", "AAAA-AAAA-AAA9") == (
        1,
        "",
        "agent-hub platform: refused: account_code_invalid\n",
    )
    assert run("certify-user", "--user", user, "--code", "A", "--code", "B") == (
        2,
        "",
        "agent-hub platform: more --code than --user\n",
    )
    assert run("certify-user", "--user", uuid_v7())[0:3:2] == (1, "agent-hub platform: refused: not_found\n")
    assert run("decertify-user", "--user", user) == (1, "", "agent-hub platform: refused: log_rights\n")
    assert plat.query("SELECT count(*) FROM hub.platform_entry") == [(1,)]


def test_certify_user_checks_the_code_against_the_keys_it_signs(
    run: Callable[..., tuple[int, str, str]], plat: Platform, hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub that pairs the account's correct code with keys of its own is refused; so is an answer for another
    user (review finding H1 of KOR-37)."""
    plat.init()
    user, pub = _account(hub)
    real = identity_codes.format_account_code(identity_codes.account_code(user, pub))
    swapped = Identity.generate().public()
    honest = users_identity.registered_identity

    def lying(connection: Any, platform: Any, target: Any) -> dict[str, Any] | None:
        found = honest(connection, platform, target)
        return None if found is None else {**found, "pub": swapped}

    monkeypatch.setattr(users_identity, "registered_identity", lying)
    assert run("certify-user", "--user", user, "--code", real) == (
        1,
        "",
        "agent-hub platform: refused: account_code_mismatch\n",
    )

    def other(connection: Any, platform: Any, target: Any) -> dict[str, Any] | None:
        found = honest(connection, platform, target)
        return None if found is None else {**found, "user_id": uuid_v7()}

    monkeypatch.setattr(users_identity, "registered_identity", other)
    assert run("certify-user", "--user", user) == (1, "", "agent-hub platform: refused: response_invalid\n")
    with pytest.raises(cli.Refused) as refused:
        cli._code_of(user, {})  # keys that are no keys (the contract keeps them out of a valid answer)
    assert refused.value.code == "header_invalid"
    assert plat.query("SELECT count(*) FROM hub.platform_entry") == [(1,)]


def test_user_certifications_wait_for_every_active_platform_device(
    run: Callable[..., tuple[int, str, str]], plat: Platform, hub: HubHarness
) -> None:
    """ADR-060: an older runner would stop at an unknown platform-log type, so the hub refuses `user_certify` while
    an active platform device has not announced `log.admit` (`409 peer_outdated` with the devices)."""
    plat.init()
    user, _ = _account(hub)
    device = str(uuid7(hub.clock()))
    insert_device(hub.pool, device, Identity.generate(), hub.clock(), "platform")
    code, _, err = run("certify-user", "--user", user)
    assert (code, err) == (1, "agent-hub platform: refused: peer_outdated\n")
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.device SET features = '{log.admit}' WHERE id = %s", (device,))
    assert run("certify-user", "--user", user)[0] == 0


def _waiting_task(hub: HubHarness, org: str, reason: str) -> str:
    """A workgroup of the org with one task waiting for a device (rows as the tasks area writes them)."""
    wg, task = str(uuid_v7()), str(uuid_v7())
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, false, 1)", (wg, org)
        )
        connection.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, state_reason, created_at, state_at,"
            " last_activity_at) VALUES (%s, %s, %s, %s, 'scheduling', %s, %s, %s, %s)",
            (task, org, wg, new_user(hub), reason, hub.clock(), hub.clock(), hub.clock()),
        )
    return wg


def test_waiting_lists_the_tasks_the_operator_must_serve(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness, plat: Platform
) -> None:
    """ADR-075 section 7: `platform_access_required`, and `no_device` while nobody could assign a device."""
    assert run("waiting") == (0, "no tasks wait for a device\n", "")
    closed = new_org(hub)
    first = _waiting_task(hub, closed, "platform_access_required")
    hub.clock.advance(1)
    granted = new_org(hub, private=True)
    second = _waiting_task(hub, granted, "no_device")
    # an org with a device of its own, or with a platform device it could get, is not the operator's task
    own = new_org(hub)
    _waiting_task(hub, own, "no_device")
    device = str(uuid_v7())
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', now())", (device,))
        connection.execute(
            "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os,"
            " arch, capacity, updated_at) VALUES (%s, 'org', %s, %s, %s, 'active', now(), '0.1.0', 'macos', 'arm64',"
            " '{}', now())",
            (device, b"k" * 32, b"s" * 32, own),
        )
        connection.execute(
            "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq)"
            " VALUES (%s, %s, 'device', 'device', 1)",
            (own, device),
        )
    hub.clock.advance(1)
    credit = _waiting_task(hub, own, "credit_exhausted")  # ADR-079: credit is the operator's as well
    code, out, _ = run("waiting")
    assert code == 0
    assert out.splitlines() == [
        f"org {closed} wg {first} platform_access_required tasks 1 since 2026-11-21T12:00:00.123Z platform access no",
        f"org {granted} wg {second} no_device tasks 1 since 2026-11-21T12:00:01.123Z platform access yes",
        f"org {own} wg {credit} credit_exhausted tasks 1 since 2026-11-21T12:00:02.123Z platform access no",
    ]
    certified = plat.enroll()
    plat.certify(certified)
    assert [line.split()[3] for line in run("waiting")[1].splitlines()] == [first, credit]
    assert hub.browser().get("/v1/platform/waiting").status_code == 403


def test_switches_lists_and_the_operator_role(
    run: Callable[..., tuple[int, str, str]], hub: HubHarness, plat: Platform
) -> None:
    """ADR-079 section 2: `set-policy`, `grant-operator-subscriptions [--revoke]`, `grant-role operator` and
    `settings`, which prints the switches and lists of the log verified with the operator's own key."""
    assert run("settings") == (
        0,
        "policy open_platform_access off\npolicy open_platform_invites off\n",
        "",
    )
    org, other, user = new_org(hub), new_org(hub), str(new_user(hub))
    assert run("set-policy", "open_platform_access", "on")[0] == 0
    assert run("grant-org-access", "--org", other, "--revoke")[0] == 0
    assert run("grant-operator-subscriptions", "--org", org)[0] == 0
    assert run("grant-operator-subscriptions", "--org", other, "--revoke")[0] == 0
    assert run("grant-role", "operator", "--user", user)[1].startswith("appended role_grant")
    code, out, _ = run("settings")
    assert code == 0
    expected = [
        "policy open_platform_access on",
        "policy open_platform_invites off",
        f"org {other} platform access revoked",
        *sorted(
            [f"org {org} operator subscriptions granted", f"org {other} operator subscriptions revoked"],
            key=lambda line: line.split()[1],
        ),
    ]
    assert out.splitlines() == expected
    assert run("set-policy", "open_platform_invites", "on")[1].startswith("appended platform_policy")
    assert plat.query('SELECT name, "on" FROM hub.platform_policy ORDER BY name') == [
        ("open_platform_access", True),
        ("open_platform_invites", True),
    ]
    assert run("set-policy", "open_platform_access", "off")[0] == 0
    assert "policy open_platform_access off" in run("settings")[1]
