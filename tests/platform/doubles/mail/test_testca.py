"""U-DBL-mail: the runtime test CA (keys made at run time, strict verification, SPKI pin)."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.mail.testca import DoubleCA, DoubleCAError, spki_of_certificate, subject_alt_names


def _handshake(ca: DoubleCA, client: ssl.SSLContext, server_hostname: str) -> str:
    """Serve one TLS connection with the CA's server certificate; the TLS version the client negotiated."""
    listener = socket.create_server(("127.0.0.1", 0))
    server = ca.server_context()

    def accept() -> None:
        conn, _ = listener.accept()
        try:
            with server.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(b"ok")
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        with (
            socket.create_connection(listener.getsockname()) as raw,
            client.wrap_socket(raw, server_hostname=server_hostname) as tls,
        ):
            assert tls.recv(2) == b"ok"
            return str(tls.version())
    finally:
        thread.join()
        listener.close()


def test_created_files_and_modes(mail_ca: DoubleCA) -> None:
    for path in (mail_ca.ca_file, mail_ca.cert_file, mail_ca.key_file):
        assert path.is_file()
    assert stat.S_IMODE(mail_ca.key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(mail_ca.directory.stat().st_mode) == 0o700
    assert not (mail_ca.directory / "server.csr").exists()
    assert mail_ca.ca_pem().startswith("-----BEGIN CERTIFICATE-----")
    assert mail_ca.names == ("localhost", "127.0.0.1", "::1")


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost"])
def test_strict_client_verifies_the_server(mail_ca: DoubleCA, name: str) -> None:
    client = mail_ca.client_context()
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    assert _handshake(mail_ca, client, name).startswith("TLS")


def test_default_context_trusts_the_ca_through_ssl_cert_file(
    mail_ca: DoubleCA, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(mail_ca.ca_file))
    assert _handshake(mail_ca, ssl.create_default_context(), "127.0.0.1").startswith("TLS")


def test_a_foreign_ca_is_refused(mail_ca: DoubleCA, tmp_path: Path) -> None:
    other = DoubleCA.create(tmp_path / "other", ["localhost"])
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(mail_ca, other.client_context(), "localhost")


def test_spki_pin_matches_openssl(mail_ca: DoubleCA) -> None:
    pubkey = subprocess.run(
        ["openssl", "x509", "-in", str(mail_ca.cert_file), "-pubkey", "-noout"], capture_output=True, check=True
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"], input=pubkey, capture_output=True, check=True
    ).stdout
    assert mail_ca.spki_sha256() == base64.b64encode(hashlib.sha256(der).digest()).decode()


def test_spki_of_a_certificate_without_version_and_with_long_lengths() -> None:
    # A synthetic v1-style tbsCertificate (no [0] version) whose outer SEQUENCE uses a two-byte length.
    def tlv(tag: int, content: bytes) -> bytes:
        size = len(content)
        head = bytes([size]) if size < 0x80 else bytes([0x82]) + size.to_bytes(2, "big")
        return bytes([tag]) + head + content

    spki = tlv(0x30, b"\x05\x00" * 3)
    fields = [tlv(0x02, b"\x01"), tlv(0x30, b""), tlv(0x30, b"i" * 150), tlv(0x30, b""), tlv(0x30, b"")]
    tbs = tlv(0x30, b"".join(fields) + spki)
    certificate = tlv(0x30, tbs + tlv(0x30, b"") + tlv(0x03, b"\x00"))
    assert spki_of_certificate(certificate) == spki


def test_subject_alt_names() -> None:
    assert subject_alt_names(["localhost", "192.0.2.1", "::1"]) == "DNS:localhost,IP:192.0.2.1,IP:::1"
    with pytest.raises(DoubleCAError, match="at least one"):
        subject_alt_names([])
    for bad in ("", "bad name", "-x.test", "a,DNS:evil"):
        with pytest.raises(DoubleCAError, match="invalid server name"):
            subject_alt_names([bad])


def test_create_reports_a_missing_openssl(tmp_path: Path) -> None:
    with pytest.raises(DoubleCAError, match="cannot run"):
        DoubleCA.create(tmp_path / "ca", openssl=str(tmp_path / "no-openssl"))


def test_create_reports_a_failing_step(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stderr = "unable to load key\nmore" if len(calls) == 1 else ""
        return subprocess.CompletedProcess(argv, 1, "", stderr)

    with pytest.raises(DoubleCAError, match="openssl ecparam failed: unable to load key"):
        DoubleCA.create(tmp_path / "a", run=run)
    with pytest.raises(DoubleCAError, match="openssl ecparam failed: 1"):
        DoubleCA.create(tmp_path / "b", run=run)


def test_create_restores_the_umask(tmp_path: Path) -> None:
    before = os.umask(0o022)
    try:
        DoubleCA.create(tmp_path / "ca", ["localhost"])
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


def test_open_or_create_reuses_a_directory(tmp_path: Path) -> None:
    first = DoubleCA.open_or_create(tmp_path / "ca", ["localhost", "192.0.2.5"])
    pem = first.ca_pem()
    again = DoubleCA.open_or_create(tmp_path / "ca", ["localhost"])
    assert again.ca_pem() == pem
    assert again.names == ("localhost", "192.0.2.5")
    (tmp_path / "ca" / "names.txt").unlink()
    renewed = DoubleCA.open_or_create(tmp_path / "ca")
    assert renewed.ca_pem() != pem
    assert renewed.names == ("localhost", "127.0.0.1", "::1")


def test_open_or_create_refuses_a_ca_without_the_names(tmp_path: Path) -> None:
    DoubleCA.open_or_create(tmp_path / "ca", ["localhost"])
    with pytest.raises(DoubleCAError, match=r"does not cover 192\.0\.2\.7"):
        DoubleCA.open_or_create(tmp_path / "ca", ["localhost", "192.0.2.7"])
    assert (tmp_path / "ca" / ".lock").is_file()


def test_open_or_create_renews_an_expiring_ca(tmp_path: Path) -> None:
    pem = DoubleCA.open_or_create(tmp_path / "ca").ca_pem()

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-checkend" in argv:
            assert argv[argv.index("-checkend") + 1] == "3600"
            return subprocess.CompletedProcess(argv, 1, "Certificate will expire", "")
        kwargs.pop("check", None)
        result: subprocess.CompletedProcess[str] = subprocess.run(argv, check=False, **kwargs)
        return result

    renewed = DoubleCA.open_or_create(tmp_path / "ca", run=run)
    assert renewed.ca_pem() != pem


def test_open_or_create_reports_a_missing_openssl_for_the_check(tmp_path: Path) -> None:
    DoubleCA.open_or_create(tmp_path / "ca")
    with pytest.raises(DoubleCAError, match="cannot run"):
        DoubleCA.open_or_create(tmp_path / "ca", openssl=str(tmp_path / "missing"))
