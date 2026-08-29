"""Start or connect to the local PostgreSQL cluster under AGENT_HOME."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

from psycopg import sql


class PgError(SystemExit):
    pass


_DBNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def require_loopback_dsn(dsn: str) -> None:
    """Reject a DSN that is not bound to loopback or a local unix socket."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        info = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        raise PgError(f"invalid postgres DSN: {exc}") from exc
    if info.get("service") or info.get("servicefile"):
        raise PgError("postgres DSN must not use a service file")
    hosts: list[str] = []
    for key in ("host", "hostaddr"):
        raw = info.get(key)
        if isinstance(raw, str) and raw:
            hosts.extend(part.strip() for part in raw.split(",") if part.strip())
    if not hosts:
        raise PgError("postgres DSN must set host, hostaddr, or a unix socket path")
    for host in hosts:
        if host.startswith("/"):
            continue
        if host not in _LOOPBACK:
            raise PgError("postgres DSN must use 127.0.0.1, ::1, localhost, or a unix socket")


def _bin(name: str) -> str:
    override = os.environ.get("AGENT_PG_BIN")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override) / name)
    found = os.environ.get("PATH", "")
    for folder in found.split(os.pathsep):
        if folder:
            candidates.append(Path(folder) / name)
    for extra in (
        Path("/opt/homebrew/opt/postgresql@16/bin"),
        Path("/opt/homebrew/opt/postgresql@15/bin"),
        Path("/usr/lib/postgresql/16/bin"),
        Path("/usr/lib/postgresql/15/bin"),
    ):
        candidates.append(extra / name)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise PgError(f"{name} is not installed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def dsn_with_db(dsn: str, dbname: str) -> str:
    if not _DBNAME_RE.match(dbname):
        raise PgError(f"invalid database name: {dbname}")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        info = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        raise PgError(f"invalid postgres DSN: {exc}") from exc
    info["dbname"] = dbname
    return make_conninfo(**info)


def create_database(admin_dsn: str, name: str) -> str:
    import psycopg

    if not _DBNAME_RE.match(name):
        raise PgError(f"invalid database name: {name}")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return dsn_with_db(admin_dsn, name)


def drop_database(admin_dsn: str, name: str) -> None:
    import psycopg

    if not _DBNAME_RE.match(name):
        raise PgError(f"invalid database name: {name}")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def cluster_dsn(data_dir: Path) -> str | None:
    port_file = data_dir / "port"
    if not port_file.is_file():
        return None
    port = port_file.read_text(encoding="utf-8").strip()
    if not port.isdigit():
        return None
    return f"host=127.0.0.1 port={port} user=agent dbname=postgres"


def _running(pgdata: Path) -> bool:
    status = subprocess.run(  # noqa: S603
        [_bin("pg_ctl"), "-D", str(pgdata), "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    return status.returncode == 0


def start_cluster(data_dir: Path) -> str:
    """Return a libpq DSN to a postgres database on this cluster."""
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    pgdata = data_dir / "data"
    port_file = data_dir / "port"
    if (pgdata / "PG_VERSION").is_file():
        existing = cluster_dsn(data_dir)
        if existing and _running(pgdata):
            return existing
        if existing:
            log = data_dir / "pg.log"
            started = subprocess.run(  # noqa: S603
                [_bin("pg_ctl"), "-D", str(pgdata), "-l", str(log), "-w", "start"],
                check=False,
                capture_output=True,
                text=True,
            )
            if started.returncode != 0:
                raise PgError((started.stderr or started.stdout or log.read_text(encoding="utf-8")).strip())
            return existing
        raise PgError("postgres data directory exists but port file is missing")
    completed = subprocess.run(  # noqa: S603
        [_bin("initdb"), "-D", str(pgdata), "--auth=trust", "-U", "agent", "--no-instructions"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise PgError((completed.stderr or completed.stdout or "initdb failed").strip())
    port = _free_port()
    conf = pgdata / "postgresql.conf"
    extra = (
        f"\nlisten_addresses = '127.0.0.1'\n"
        f"port = {port}\n"
        "unix_socket_directories = ''\n"
        "logging_collector = off\n"
    )
    conf.write_text(conf.read_text(encoding="utf-8") + extra, encoding="utf-8")
    port_file.write_text(str(port), encoding="utf-8")
    log = data_dir / "pg.log"
    started = subprocess.run(  # noqa: S603
        [_bin("pg_ctl"), "-D", str(pgdata), "-l", str(log), "-w", "start"],
        check=False,
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        raise PgError((started.stderr or started.stdout or log.read_text(encoding="utf-8")).strip())
    return f"host=127.0.0.1 port={port} user=agent dbname=postgres"


def stop_cluster(data_dir: Path) -> None:
    pgdata = data_dir / "data"
    if not pgdata.is_dir():
        return
    subprocess.run(  # noqa: S603
        [_bin("pg_ctl"), "-D", str(pgdata), "-m", "fast", "stop"],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_ready(dsn: str, timeout: float = 10.0) -> None:
    import psycopg

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=1) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(0.1)
    raise PgError(f"postgres did not become ready: {last}")


def cluster_exists(data_dir: Path) -> bool:
    """True when data_dir/data/PG_VERSION is a file."""
    return (data_dir / "data" / "PG_VERSION").is_file()


def cluster_running(data_dir: Path) -> bool:
    """False when the cluster does not exist; otherwise pg_ctl status == 0 (reuse _running)."""
    if not cluster_exists(data_dir):
        return False
    return _running(data_dir / "data")


def ensure_cluster(data_dir: Path, *, create: bool = True) -> str:
    """Start the local cluster, creating it only when create is True.

    When create is False and the cluster does not exist, raise before calling
    start_cluster so nothing is created and no directory is made.
    """
    if not create and not cluster_exists(data_dir):
        raise PgError(f"no local postgres cluster under {data_dir}; run agent init")
    dsn = start_cluster(data_dir)
    wait_ready(dsn)
    return dsn
