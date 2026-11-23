"""The source address of a request behind trusted reverse proxies (03-api.md section 1 "per source address"; A-11).

Without `AGENT_HUB_TRUSTED_PROXIES` (the default) the source is the peer address of the connection and no forwarding
header is read. With it, and only when the peer is one of the trusted proxies, the one configured header
(`AGENT_HUB_FORWARDED_HEADER`: `X-Forwarded-For`, the default, or RFC 7239 `Forwarded` with its `for=` parameter;
never both, so a header the proxies do not write cannot be smuggled through them) is read from right to left: each
address that is itself a trusted proxy is a hop and skipped, the first other address is the source. A client can put
anything in front, but it cannot change what the trusted proxies appended. A malformed or obfuscated entry
(`unknown`, `_hidden`, no `for=`) ends the walk and the last trusted hop is the source; so is the leftmost hop when
every entry is a trusted proxy. Several header lines count as one list in their order.
"""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

Address = IPv4Address | IPv6Address
Network = IPv4Network | IPv6Network
FORWARDED = "forwarded"
X_FORWARDED_FOR = "x-forwarded-for"
HEADERS = (X_FORWARDED_FOR, FORWARDED)


def parse_address(text: str) -> Address | None:
    """An address of a forwarding entry (`198.51.100.1`, `198.51.100.1:80`, `[2001:db8::1]:80`, quoted or not;
    IPv4-mapped IPv6 as IPv4), None when it is no address."""
    value = text.strip().strip('"')
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        value = value[1:end]
    elif value.count(":") == 1:
        value = value.partition(":")[0]
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _for_value(element: str) -> str:
    """The `for=` value of one RFC 7239 element ("" without one)."""
    for pair in element.split(";"):
        name, _, value = pair.partition("=")
        if name.strip().lower() == "for":
            return value
    return ""


def _entries(headers: Sequence[tuple[bytes, bytes]], header: str) -> list[str]:
    name = header.encode("ascii")
    entries = [item for key, value in headers if key == name for item in value.decode("latin-1").split(",")]
    return [_for_value(item) for item in entries] if header == FORWARDED else entries


def client_address(peer: str, headers: Sequence[tuple[bytes, bytes]], trusted: Sequence[Network], header: str) -> str:
    """The source address of a request from `peer` with the request `headers` (module docstring)."""
    hop = parse_address(peer)
    if hop is None or not any(hop in network for network in trusted):
        return peer
    for entry in reversed(_entries(headers, header)):
        address = parse_address(entry)
        if address is None:
            break
        if not any(address in network for network in trusted):
            return str(address)
        hop = address
    return str(hop)
