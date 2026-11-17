"""Connection strings of the record database (RECORD-PROTOCOL.md section 15).

A connection string is a secret: no message of this module contains it or a part of it.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Mapping

import psycopg
from psycopg import sql

WRITER_VARIABLE = "AGENT_CORE_RECORD_DATABASE"
OWNER_VARIABLE = "AGENT_CORE_RECORD_OWNER_DATABASE"
DEFAULT_PORT = 5432
MAX_PORT = 65535
MAX_HOST_NAME = 253
MIN_LIBPQ_VERSION = 160000
CONNECT_TIMEOUT_SECONDS = 10

_NAME = r"[a-z_][a-z0-9_]{0,62}"
_NAME_RE = re.compile(_NAME)
_STRING_RE = re.compile(
    r"postgresql://"
    rf"(?P<role>{_NAME})"
    r":(?P<password>[0-9a-f]{64})"
    r"@(?P<host>\[[0-9a-f:.]+\]|[a-z0-9.-]+)"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
    rf"/(?P<database>{_NAME})"
    r"\?sslmode=(?P<sslmode>disable|require|verify-full)"
)
# Names PostgreSQL treats specially or reserves. In a role position `public` is the pseudo-role
# PUBLIC even in double quotes, `none` is refused, and the keywords stand for a role of the
# session; the prefix `pg_` is reserved for the system.
RESERVED_NAMES = frozenset({"public", "none", "user", "current_user", "current_role", "session_user"})
RESERVED_PREFIX = "pg_"
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DOTTED_DIGITS_RE = re.compile(r"[0-9.]+")
_IPV6_LOOPBACK = ipaddress.IPv6Address("::1")


class ConnectionStringError(ValueError):
    """A connection string is missing or breaks its form. The message names no value."""


@dataclass(frozen=True)
class ConnectionString:
    """A parsed connection string. `host` is as the driver takes it (IPv6 without brackets)."""

    role: str
    password: str = dataclass_field(repr=False)
    host: str
    port: int
    database: str
    sslmode: str

    def connect_kwargs(self) -> dict[str, Any]:
        """The parameters for the driver. `verify-full` checks against the system trust store."""
        kwargs: dict[str, Any] = {
            "host": self.host,
            # Given explicitly as empty, so the variable PGHOSTADDR cannot send the connection to
            # another address than the checked host.
            "hostaddr": "",
            "port": self.port,
            "dbname": self.database,
            "user": self.role,
            "password": self.password,
            "sslmode": self.sslmode,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        }
        if self.sslmode == "verify-full":
            # Given explicitly, so it takes precedence over the variable PGSSLROOTCERT.
            kwargs["sslrootcert"] = "system"
        return kwargs

    def connect(self, **options: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(**self.connect_kwargs(), **options)


def _host(text: str) -> tuple[str, bool] | None:
    """The driver's host and whether it is a loopback host; None when the host breaks its form."""
    if text.startswith("["):
        inner = text[1:-1]
        try:
            address6 = ipaddress.IPv6Address(inner)
        except ValueError:
            return None
        return inner, address6 == _IPV6_LOOPBACK
    if _DOTTED_DIGITS_RE.fullmatch(text) is not None:
        try:
            address4 = ipaddress.IPv4Address(text)
        except ValueError:
            return None
        return text, address4.is_loopback
    if len(text) > MAX_HOST_NAME:
        return None
    if not all(_LABEL_RE.fullmatch(label) is not None for label in text.split(".")):
        return None
    return text, text == "localhost"


def reserved_name(name: str) -> bool:
    """A role or database name that PostgreSQL treats specially or reserves, in any case."""
    folded = name.lower()
    return folded in RESERVED_NAMES or folded.startswith(RESERVED_PREFIX)


def identifier(name: object) -> sql.Identifier:
    """A role or database name of a connection string as an SQL identifier composed by the driver.

    The only way a name of a connection string reaches SQL: it must have the form of section 15
    and must not be a reserved name.
    """
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise ConnectionStringError("a name breaks the form of section 15")
    if reserved_name(name):
        raise ConnectionStringError("a name is reserved by PostgreSQL")
    return sql.Identifier(name)


def parse_connection_string(text: object) -> ConnectionString:
    """Section 15: `postgresql://<role>:<password>@<host>[:<port>]/<database>?sslmode=<mode>`."""
    if not isinstance(text, str):
        raise ConnectionStringError("a connection string is text")
    match = _STRING_RE.fullmatch(text)
    if match is None:
        raise ConnectionStringError("the connection string breaks its form")
    if reserved_name(match["role"]) or reserved_name(match["database"]):
        raise ConnectionStringError("the connection string names a role or database reserved by PostgreSQL")
    host = _host(match["host"])
    if host is None:
        raise ConnectionStringError("the host of the connection string breaks its form")
    port = DEFAULT_PORT if match["port"] is None else int(match["port"])
    if port > MAX_PORT:
        raise ConnectionStringError("the port of the connection string is above 65535")
    driver_host, loopback = host
    if match["sslmode"] == "disable" and not loopback:
        raise ConnectionStringError("sslmode=disable is allowed only for a loopback host or localhost")
    return ConnectionString(
        role=match["role"],
        password=match["password"],
        host=driver_host,
        port=port,
        database=match["database"],
        sslmode=match["sslmode"],
    )


def read_connection_string(environ: Mapping[str, str], name: str) -> ConnectionString:
    """The connection string of a variable; missing, empty or malformed stops the program."""
    value = environ.get(name)
    if value is None or value == "":
        raise ConnectionStringError(f"{name} is not set")
    try:
        return parse_connection_string(value)
    except ConnectionStringError:
        raise ConnectionStringError(f"{name} does not have the form of section 15") from None


def migrate_config(environ: Mapping[str, str]) -> tuple[ConnectionString, ConnectionString]:
    """Step `config` of the `migrate` command (section 11.4): the owner's and the writer's string."""
    owner = read_connection_string(environ, OWNER_VARIABLE)
    writer = read_connection_string(environ, WRITER_VARIABLE)
    if owner.role == writer.role:
        raise ConnectionStringError("the two connection strings name the same role")
    if (owner.host, owner.port, owner.database, owner.sslmode) != (
        writer.host,
        writer.port,
        writer.database,
        writer.sslmode,
    ):
        raise ConnectionStringError(
            "the two connection strings differ in host, port, database or sslmode"
        )
    return owner, writer


def libpq_version() -> int:
    """The version of the libpq that psycopg uses, such as 160002."""
    return psycopg.pq.version()


def libpq_supported() -> bool:
    """Section 15: libpq 16 or later, because `sslrootcert=system` needs it."""
    return libpq_version() >= MIN_LIBPQ_VERSION
