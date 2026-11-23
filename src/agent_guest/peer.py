"""The UID of the process on the other end of a loopback TCP connection to the API port (ATK-36, decisions ATS-02).

A session token belongs to exactly one session (05-runner.md section 4.4), and every legitimate caller of
``127.0.0.1:7700`` is the Claude Code process of that session, running as the session's own user. The API process
therefore looks up which UID owns the client end of the connection and the control process accepts a token only
from that UID: a token another session got hold of is useless there, and guessing tokens from one session can never
reach another session's token.

Linux lists every TCP socket of the network namespace with its owner in ``/proc/net/tcp`` (``AGENT_GUEST_PEER_TABLE``,
set by ``agent-guest-api.service``). The client end of a connection is the entry whose local address is the peer
address the server sees and whose remote address is the server's own address::

      sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
       1: 0100007F:D2A4 0100007F:1E14 01 00000000:00000000 00:00000000 00000000 20001        0 41234 ...

Addresses are the IPv4 address as a little-endian hex word and the port as big-endian hex (the API binds 127.0.0.1
only, so IPv6 never occurs). ``peer_uid`` returns:

* ``None`` - binding is off (no table configured; tests and other operating systems): the caller checks tokens only
* the UID of the client socket
* ``UNKNOWN`` (-1, no user has it) - binding is on but no entry was found or the table could not be read: the control
  process then refuses every token, fail-closed
"""

from __future__ import annotations

import socket
import struct

UNKNOWN = -1
# The client end of a live connection: established, or half-closed by the client after it sent its request
# (FIN_WAIT1, FIN_WAIT2). TIME_WAIT entries carry no owner and never match.
CONNECTED = frozenset({"01", "04", "05"})


def _address(value: str) -> tuple[str, int] | None:
    host, sep, port = value.partition(":")
    if not sep or len(host) != 8 or len(port) != 4:
        return None
    try:
        packed = struct.pack("<I", int(host, 16))
        return socket.inet_ntoa(packed), int(port, 16)
    except ValueError:
        return None


def parse_table(text: str) -> dict[tuple[tuple[str, int], tuple[str, int]], int]:
    """``{(local, remote): uid}`` of every live client connection in a ``/proc/net/tcp`` listing."""
    found: dict[tuple[tuple[str, int], tuple[str, int]], int] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[3] not in CONNECTED or not fields[7].isdigit():
            continue
        local, remote = _address(fields[1]), _address(fields[2])
        if local is not None and remote is not None:
            found[(local, remote)] = int(fields[7])
    return found


def peer_uid(table: str, server: tuple[str, int] | None, peer: tuple[str, int] | None) -> int | None:
    """The UID owning the client end of the connection ``peer`` -> ``server`` (see the module docstring)."""
    if not table:
        return None
    if server is None or peer is None:
        return UNKNOWN
    try:
        with open(table, encoding="ascii", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return UNKNOWN
    return parse_table(text).get((peer, server), UNKNOWN)
