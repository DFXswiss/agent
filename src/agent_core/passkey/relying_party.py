"""The origin and the relying-party id of PASSKEY-PROTOCOL.md section 3.

Both are derived from the hub's public URL, as `Config.from_env` holds it. The refusal of a host
that ends with a dot or is an IP address runs in `Config.from_env` (section 3) and not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import public_url_host


@dataclass(frozen=True)
class RelyingParty:
    """`origin` as a browser writes `location.origin`; `rp_id` the host without port."""

    origin: str
    rp_id: str


def relying_party(public_url: str) -> RelyingParty:
    """The origin and the rp id of a public URL.

    The origin is the lowercase scheme, `://` and the host line of KEY-PROTOCOL.md section 5
    (`public_url_host`, which also refuses every URL form that `Config.from_env` refuses). The rp
    id is the host name without port, lowercase, in ASCII form.
    """
    host_line = public_url_host(public_url)
    parts = urlsplit(public_url)
    hostname = str(parts.hostname)
    rp_id = hostname.encode("idna").decode("ascii").lower()
    return RelyingParty(origin=f"{parts.scheme.lower()}://{host_line}", rp_id=rp_id)
