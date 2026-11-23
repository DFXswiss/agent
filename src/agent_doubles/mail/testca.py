"""A certificate authority for the TLS endpoints of the doubles, created at run time (no key ever lives in the repo).

`DoubleCA.create` writes into a directory (created 0700): ``ca.pem`` (the CA certificate clients trust), ``server.pem``
and ``server.key`` (one P-256 server certificate for every name of ``names``: DNS names and IP addresses as subject
alternative names) and ``names.txt``; `DoubleCA.open_or_create` reuses such a directory, so the mail and auth-page
doubles of one test run can share a CA. Both certificates are valid for two days, signed with SHA-256 and carry the
extensions strict verifiers require (CA basic constraints and key usage, subject and authority key identifiers,
``serverAuth``).

The keys are made by the ``openssl`` command (OpenSSL or LibreSSL; the standard library cannot create keys), the
private keys are 0600. Clients trust the CA through `client_context` or by pointing ``SSL_CERT_FILE`` at ``ca.pem``
(which `ssl.create_default_context` honours, so unchanged code such as the legacy IMAP poll verifies against it);
browsers can pin the server key with `spki_sha256` (Chromium's ``--ignore-certificate-errors-spki-list``).
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import os
import re
import ssl
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_NAMES = ("localhost", "127.0.0.1", "::1")
VALID_DAYS = "2"
NAMES_FILE = "names.txt"
LOCK_FILE = ".lock"
# A shared CA is renewed when its server certificate would expire within this time.
RENEW_SECONDS = 3600
# Digest, validity and (completed by the caller) the serial number of a signing step.
SIGN = ("-sha256", "-days", VALID_DAYS, "-set_serial")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DNS_NAME = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*")
_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = {cn}
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = {san}
"""


class DoubleCAError(Exception):
    """The CA could not be created (no ``openssl``, a failing command or an invalid name)."""


def subject_alt_names(names: Sequence[str]) -> str:
    """The ``subjectAltName`` value for `names`; raises DoubleCAError for an empty list or an invalid name."""
    if not names:
        raise DoubleCAError("at least one server name is required")
    parts = []
    for name in names:
        try:
            parts.append(f"IP:{ipaddress.ip_address(name)}")
        except ValueError:
            if not _DNS_NAME.fullmatch(name):
                raise DoubleCAError(f"invalid server name {name!r}") from None
            parts.append(f"DNS:{name}")
    return ",".join(parts)


def _der_element(data: bytes, offset: int) -> tuple[int, int, int]:
    """Tag, start of the content and end of the DER element at `offset`."""
    tag = data[offset]
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        size = length & 0x7F
        length = int.from_bytes(data[start : start + size], "big")
        start += size
    return tag, start, start + length


def spki_of_certificate(der: bytes) -> bytes:
    """The DER ``SubjectPublicKeyInfo`` of an X.509 certificate (`der`)."""
    _, tbs_outer, _ = _der_element(der, 0)
    _, offset, _ = _der_element(der, tbs_outer)
    # tbsCertificate: [0] version (optional), serial, signature, issuer, validity, subject, subjectPublicKeyInfo.
    if der[offset] == 0xA0:
        offset = _der_element(der, offset)[2]
    for _ in range(5):
        offset = _der_element(der, offset)[2]
    end = _der_element(der, offset)[2]
    return der[offset:end]


@dataclass(frozen=True)
class DoubleCA:
    """A created CA and its server certificate (paths into `directory`)."""

    directory: Path
    names: tuple[str, ...]

    @property
    def ca_file(self) -> Path:
        return self.directory / "ca.pem"

    @property
    def cert_file(self) -> Path:
        return self.directory / "server.pem"

    @property
    def key_file(self) -> Path:
        return self.directory / "server.key"

    @classmethod
    def create(
        cls,
        directory: Path,
        names: Sequence[str] = DEFAULT_NAMES,
        *,
        openssl: str = "openssl",
        run: Callable[..., Any] = subprocess.run,
    ) -> DoubleCA:
        """Create a new CA and server certificate for `names` in `directory` (replacing older files)."""
        san = subject_alt_names(names)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "ca.cnf").write_text(_CONFIG.format(cn="agent doubles test CA", san=san), encoding="utf-8")
        (directory / "server.cnf").write_text(_CONFIG.format(cn=names[0], san=san), encoding="utf-8")
        steps = (
            ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"],
            [
                "req",
                "-new",
                "-x509",
                "-key",
                "ca.key",
                "-config",
                "ca.cnf",
                "-extensions",
                "v3_ca",
                *SIGN,
                "1",
                "-out",
                "ca.pem",
            ],
            ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "server.key"],
            ["req", "-new", "-key", "server.key", "-config", "server.cnf", "-out", "server.csr"],
            [
                "x509",
                "-req",
                "-in",
                "server.csr",
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca.key",
                *SIGN,
                "2",
                "-out",
                "server.pem",
                "-extfile",
                "server.cnf",
                "-extensions",
                "v3_server",
            ],
        )
        previous = os.umask(0o077)
        try:
            for step in steps:
                cls._openssl(run, [openssl, *step], directory)
        finally:
            os.umask(previous)
        (directory / "server.csr").unlink()
        (directory / NAMES_FILE).write_text("\n".join(names) + "\n", encoding="utf-8")
        return cls(directory, tuple(names))

    @classmethod
    def open_or_create(
        cls,
        directory: Path,
        names: Sequence[str] = DEFAULT_NAMES,
        *,
        openssl: str = "openssl",
        run: Callable[..., Any] = subprocess.run,
    ) -> DoubleCA:
        """The CA already in `directory` (so several doubles share one CA), else a new one for `names`.

        Runs under an exclusive lock on ``directory/.lock``, so doubles started in parallel never interleave their
        files. A CA whose server certificate expires within `RENEW_SECONDS` is replaced. A valid CA that does not cover
        every name of `names` is refused (DoubleCAError) rather than replaced, because replacing it would break the
        doubles already using it: create the shared CA with every name first (``--ca-name``).
        """
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / LOCK_FILE).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            files = [directory / name for name in ("ca.pem", "server.pem", "server.key", NAMES_FILE)]
            if all(path.is_file() for path in files) and cls._valid(run, openssl, directory):
                stored = tuple(files[3].read_text(encoding="utf-8").split())
                missing = [name for name in names if name not in stored]
                if missing:
                    raise DoubleCAError(f"the CA in {directory} does not cover {', '.join(missing)}")
                return cls(directory, stored)
            return cls.create(directory, names, openssl=openssl, run=run)

    @staticmethod
    def _valid(run: Callable[..., Any], openssl: str, directory: Path) -> bool:
        """Whether the server certificate stays valid for `RENEW_SECONDS` (``openssl x509 -checkend``)."""
        argv = [openssl, "x509", "-checkend", str(RENEW_SECONDS), "-noout", "-in", "server.pem"]
        try:
            completed = run(argv, cwd=directory, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise DoubleCAError(f"cannot run {openssl}: {exc.strerror}") from None
        return bool(completed.returncode == 0)

    @staticmethod
    def _openssl(run: Callable[..., Any], argv: list[str], cwd: Path) -> None:
        try:
            completed = run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise DoubleCAError(f"cannot run {argv[0]}: {exc.strerror}") from None
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            raise DoubleCAError(f"openssl {argv[1]} failed: {detail[0] if detail else completed.returncode}")

    def server_context(self) -> ssl.SSLContext:
        """A server-side TLS context with the server certificate (TLS 1.2 or newer)."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_file, self.key_file)
        return context

    def client_context(self) -> ssl.SSLContext:
        """A verifying client context that trusts only this CA."""
        return ssl.create_default_context(cafile=str(self.ca_file))

    def ca_pem(self) -> str:
        """The CA certificate in PEM form (public)."""
        return self.ca_file.read_text(encoding="ascii")

    def spki_sha256(self) -> str:
        """Base64 SHA-256 of the server key's SubjectPublicKeyInfo (Chromium SPKI pin)."""
        der = ssl.PEM_cert_to_DER_cert(self.cert_file.read_text(encoding="ascii"))
        return base64.b64encode(hashlib.sha256(spki_of_certificate(der)).digest()).decode("ascii")
