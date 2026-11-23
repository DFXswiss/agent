"""Field types of the authentication and identity request bodies (`contracts/common.json`, `contracts/api/`).

The request models are strict pydantic models that forbid unknown fields; a body that breaks them is answered with
`400 request_invalid` and the JSON pointers of the offending fields (`agent_hub.http.handlers`). The patterns are the
ones of `contracts/common.json#/$defs`, so the hub accepts exactly what the contract allows.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from agent_core.signing import b64url_decode
from agent_hub.errors import HubError

_B64U = r"^(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048])?$"

Uuid7 = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
]
B64u16Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{21}[AQgw]$")]
B64u32 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")]
B64u64 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{85}[AQgw]$")]
B64u80 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{106}[AEIMQUYcgkosw048]$")]
BundleCt = Annotated[str, StringConstraints(pattern=_B64U, min_length=38, max_length=1366)]
CredentialId = Annotated[str, StringConstraints(pattern=_B64U, min_length=2, max_length=1364)]
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")]
Transport = Literal["usb", "nfc", "ble", "smart-card", "hybrid", "internal"]


def b64u_text(max_length: int) -> object:
    """Canonical b64u text of at most `max_length` characters."""
    return StringConstraints(pattern=_B64U, max_length=max_length)


class Strict(BaseModel):
    """Base of every request model: strict types, unknown fields refused (`additionalProperties: false`)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Empty(Strict):
    """The empty request body `{}`."""


def decode(text: str) -> bytes:
    """The bytes of canonical b64u text (`request_invalid` for anything else)."""
    raw = b64url_decode(text)
    if raw is None:
        raise HubError("request_invalid")
    return raw
