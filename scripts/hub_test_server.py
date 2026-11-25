"""Hub test server of the web Playwright suite (WEB-01): the real `python -m agent_hub` on its own database.

Creates a throwaway database with fresh owner and writer roles on the superuser connection of
`AGENT_HUB_TEST_POSTGRES` (pattern of `tests/platform/hub/conftest.py`), applies the migrations with
`python -m agent_hub.db migrate`, runs `python -m agent_hub serve` on `--port` with the compiled test platform root key,
and drops database and roles again when it is stopped (SIGTERM/SIGINT) or the hub exits. The hub serves the web build
from its static directory under the production CSP. With `--director-port`, the device double of the suite
(`scripts/hub_test_director.py`, decision WEB-04) runs next to it on the same database.

Every Playwright test reaches the hub from the same loopback address, so the anonymous rate limit of 03-api.md
section 1 (30 per minute per source address, `agent_hub.http.limits`) would refuse the suite's sign-in ceremonies
after a few tests. The test hub therefore runs with the anonymous limit raised to `ANONYMOUS_PER_MINUTE` (only in
this process; the limit itself is tested by the hub's own suite). Decision WEB-03. For the same reason the limit of
`POST /v1/recovery/begin` per source address (5 per hour) is raised to `RECOVERY_PER_SOURCE`; the limit per recovery
ID (20 per day, counted in the database) stays, so the suite still sees a real `429` (decision WEB-14). The limit per
browser session (600 per minute) is raised to `SESSION_PER_MINUTE` as well: the volume gate (KOR-35) signs one user in
for many views in a row, and the views it measures read every page of large lists (the limit is tested by the hub's
own suite).

With `--volume-cache DIR` (a lane of the volume gate, decisions/HOT-21.md) the volume data set comes from the cache
of `scripts/hub_test_volume_cache.py`: a usable entry is restored into the migrated database before the hub starts
(with the entry's hub signing key, its state handed to the device double); without one, the device double seeds on
request and stores a new entry.

    python scripts/hub_test_server.py --port 20262 --root-key <b64u> [--director-port 20264] [--volume-cache DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_hub.db.migrate import OWNER_VARIABLE, WRITER_VARIABLE

TEST_POSTGRES = "AGENT_HUB_TEST_POSTGRES"
ROLE_ATTRIBUTES = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
MODEL_CATALOG = Path(__file__).resolve().parents[1] / "contracts" / "model-catalog.example.json"
DIRECTOR = Path(__file__).resolve().parent / "hub_test_director.py"
VOLUME_CACHE = Path(__file__).resolve().parent / "hub_test_volume_cache.py"
ANONYMOUS_PER_MINUTE = 100_000
RECOVERY_PER_SOURCE = 100_000
SESSION_PER_MINUTE = 100_000
# `python -m agent_hub serve` with the anonymous limit of the test hub (see the module docstring)
LAUNCH = (
    "import runpy, sys\n"
    "import agent_hub.http.limits as limits\n"
    "limits.PER_MINUTE['anonymous'] = int(sys.argv[1])\n"
    "limits.PER_MINUTE['session'] = int(sys.argv[3])\n"
    "import functools\n"
    "import agent_hub.auth.recovery as recovery\n"
    "recovery.SourceLimit = functools.partial(recovery.SourceLimit, limit=int(sys.argv[2]))\n"
    "sys.argv = ['agent_hub', 'serve']\n"
    "runpy.run_module('agent_hub', run_name='__main__', alter_sys=True)\n"
)


def admin_params(environ: dict[str, str]) -> dict[str, Any]:
    value = environ.get(TEST_POSTGRES)
    if not value:
        raise SystemExit(f"{TEST_POSTGRES} is not set (tools/wp/testpg.sh)")
    params = conninfo_to_dict(value)
    if not params.get("host") or str(params["host"]).startswith("/"):
        raise SystemExit(f"{TEST_POSTGRES} must name a TCP host")
    return params


def url(params: dict[str, Any], role: str, password: str, database: str) -> str:
    host = str(params["host"])
    sslmode = "disable" if host in {"localhost", "::1"} or host.startswith("127.") else "require"
    return f"postgresql://{role}:{password}@{host}:{params.get('port') or 5432}/{database}?sslmode={sslmode}"


def volume_cache() -> Any:
    """`scripts/hub_test_volume_cache.py` (scripts/ is no package)."""
    spec = importlib.util.spec_from_file_location("hub_test_volume_cache", VOLUME_CACHE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def volume_data(cache: Path, admin_url: str, signing_key: str) -> tuple[str, dict[str, str]]:
    """The cached volume data set restored into the database at `admin_url`, or how the device double stores a new
    one: the hub's signing key and the device double's environment."""
    module = volume_cache()
    name = module.key()
    entry = module.usable(cache, name)
    if entry is None:
        print(f"volume data set {name}: not cached, seeded on request", flush=True)
        snapshot = {"WEB_E2E_VOLUME_SNAPSHOT": str(cache / name), "WEB_E2E_VOLUME_ADMIN_URL": admin_url}
        return signing_key, {**snapshot, "WEB_E2E_HUB_SIGNING_KEY": signing_key}
    module.restore(admin_url, entry)
    print(f"volume data set {name}: restored from the cache", flush=True)
    state = (entry / "state.json").read_text()
    return (entry / "signing.key").read_text().strip(), {"WEB_E2E_VOLUME_STATE": state}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root-key", required=True)
    parser.add_argument("--director-port", type=int, default=None)
    parser.add_argument("--volume-cache", type=Path, default=None)
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    params = admin_params(environ)
    suffix = secrets.token_hex(6)
    name, owner, writer = f"t_web_{suffix}", f"t_web_owner_{suffix}", f"t_web_writer_{suffix}"
    owner_password, writer_password = secrets.token_hex(32), secrets.token_hex(32)
    with psycopg.connect(**params, autocommit=True) as admin:
        for role, password in ((owner, owner_password), (writer, writer_password)):
            admin.execute(
                sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                    sql.Identifier(role), ROLE_ATTRIBUTES, sql.Literal(password)
                )
            )
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner)))
    hub: subprocess.Popen[bytes] | None = None
    director: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: FrameType | None) -> None:
        if hub is not None:
            hub.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        base = {key: value for key, value in environ.items() if not key.startswith("AGENT_HUB_")}
        writer_url = url(params, writer, writer_password, name)
        migrate_env = {**base, OWNER_VARIABLE: url(params, owner, owner_password, name), WRITER_VARIABLE: writer_url}
        subprocess.run([sys.executable, "-m", "agent_hub.db", "migrate"], env=migrate_env, check=True)
        signing_key, volume_env = secrets.token_hex(32), dict[str, str]()
        if args.volume_cache is not None:
            admin_url = make_conninfo(**{**params, "dbname": name})
            signing_key, volume_env = volume_data(args.volume_cache, admin_url, signing_key)
        hub_env = {
            **base,
            "AGENT_HUB_PUBLIC_URL": f"http://localhost:{args.port}",
            "AGENT_HUB_DATABASE": writer_url,
            "AGENT_HUB_SIGNING_KEY": signing_key,
            "AGENT_HUB_HUB_KEY_ID": "hub-web-test",
            "AGENT_HUB_PLATFORM_ROOT_KEY": args.root_key,
            "AGENT_HUB_HOST": "127.0.0.1",
            "AGENT_HUB_PORT": str(args.port),
            "AGENT_HUB_MODEL_CATALOG": str(MODEL_CATALOG),
        }
        hub = subprocess.Popen(
            [
                sys.executable,
                "-c",
                LAUNCH,
                str(ANONYMOUS_PER_MINUTE),
                str(RECOVERY_PER_SOURCE),
                str(SESSION_PER_MINUTE),
            ],
            env=hub_env,
        )
        if args.director_port is not None:
            command = [
                sys.executable,
                str(DIRECTOR),
                "--port",
                str(args.director_port),
                "--hub",
                hub_env["AGENT_HUB_PUBLIC_URL"],
            ]
            director = subprocess.Popen(command, env={**base, **volume_env, "AGENT_HUB_DATABASE": writer_url})
        return hub.wait()
    finally:
        if director is not None:
            director.terminate()
            director.wait()
        with psycopg.connect(**params, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in (writer, owner):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    sys.exit(main())
