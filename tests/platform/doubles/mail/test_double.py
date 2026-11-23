"""U-DBL-mail: the MailDouble lifecycle, endpoints and control routes."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA, DoubleCAError

from .conftest import USER, Mail


def test_endpoints_and_info_file(start_mail: Callable[..., Mail], tmp_path: Path, mail_ca: DoubleCA) -> None:
    info = tmp_path / "mail.json"
    mail = start_mail(info_path=info)
    written = json.loads(info.read_text())
    assert written == mail.double.endpoints()
    assert written["control"] == mail.double.url
    assert written["ca_file"] == str(mail_ca.ca_file)
    assert written["spki_sha256"] == mail_ca.spki_sha256()
    assert {written[k]["host"] for k in ("smtp", "imap", "imaps")} == {"127.0.0.1"}
    assert len({written[k]["port"] for k in ("smtp", "imap", "imaps")}) == 3
    assert mail.control("GET", "info").json() == written
    assert mail.control("GET", "ca").body.decode() == mail_ca.ca_pem()


def test_without_tls(start_mail: Callable[..., Mail]) -> None:
    mail = start_mail(tls=False)
    assert mail.double.imaps is None
    assert mail.double.ca is None
    endpoints = mail.double.endpoints()
    assert (endpoints["imaps"], endpoints["ca_file"], endpoints["spki_sha256"]) == (None, None, None)
    response = mail.control("GET", "ca")
    assert (response.status, response.json()) == (404, {"error": "the double runs without TLS"})


def test_own_ca_is_created_for_the_bound_address_and_removed(tmp_path: Path) -> None:
    with MailDouble(host="::1") as double:
        assert double.ca is not None
        directory = double.ca.directory
        assert double.ca.names == ("localhost", "127.0.0.1", "::1")
        assert double.url.startswith("http://[::1]:")
    assert not directory.exists()
    kept = tmp_path / "ca"
    with MailDouble(host="localhost", ca_dir=kept) as double:
        assert double.ca is not None
        assert double.ca.directory == kept
    assert (kept / "ca.pem").is_file()


def test_an_extra_bound_address_joins_the_certificate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def create(directory: Path, names: Any, **kwargs: Any) -> DoubleCA:
        seen["names"] = names
        raise DoubleCAError("stop here")

    monkeypatch.setattr(DoubleCA, "create", staticmethod(create))
    with pytest.raises(DoubleCAError):
        MailDouble(host="192.0.2.10", ca_dir=tmp_path / "ca")
    assert seen["names"] == ("localhost", "127.0.0.1", "::1", "192.0.2.10")


def test_construction_failures_release_everything(tmp_path: Path) -> None:
    with pytest.raises(DoubleCAError):
        MailDouble(openssl=str(tmp_path / "missing"))
    busy = socket.create_server(("127.0.0.1", 0))
    free = socket.create_server(("127.0.0.1", 0))
    smtp_port = free.getsockname()[1]
    free.close()
    try:
        with pytest.raises(OSError, match="in use"):
            MailDouble(tls=False, smtp_port=smtp_port, imap_port=busy.getsockname()[1])
    finally:
        busy.close()
    # The SMTP listener bound before the failure was closed again.
    socket.create_server(("127.0.0.1", smtp_port)).close()
    with pytest.raises(ValueError, match="invalid port"):
        MailDouble(tls=False, smtp_port=70000)


def test_control_accounts_deliver_messages(mail: Mail) -> None:
    response = mail.control("POST", "accounts", {"user": "v@example.test", "password": "pw", "junk_name": "Spam"})
    assert (response.status, response.json()) == (201, {"user": "v@example.test"})
    assert mail.control("GET", "accounts").json() == [
        {"user": USER, "folders": ["INBOX", "Junk"], "junk": "Junk"},
        {"user": "v@example.test", "folders": ["INBOX", "Spam"], "junk": "Spam"},
    ]
    delivered = mail.control(
        "POST",
        "deliver",
        {
            "to": [USER, "v@example.test"],
            "from": "Login <login@auth.double.test>",
            "subject": "x",
            "html": "<a>",
            "spam": True,
        },
    )
    assert delivered.json() == {
        "delivered": [
            {"user": USER, "folder": "Junk", "uid": 1},
            {"user": "v@example.test", "folder": "Spam", "uid": 1},
        ]
    }
    raw = mail.control(
        "POST", "deliver", {"to": USER, "from": "a@x.test", "raw": "Subject: r\n\nbody\n", "folder": "Junk"}
    )
    assert raw.json() == {"delivered": [{"user": USER, "folder": "Junk", "uid": 2}]}
    stored = mail.double.store.account(USER).folders["Junk"].messages[1]  # type: ignore[union-attr]
    assert (stored.raw, stored.sender) == (b"Subject: r\r\n\r\nbody\r\n", "a@x.test")
    plain = mail.control(
        "POST", "deliver", {"to": USER, "from": "a@x.test", "text": "t", "date": "Fri, 20 Nov 2026 08:00:00 +0000"}
    )
    assert plain.json()["delivered"][0]["folder"] == "INBOX"
    listed = mail.control("GET", "messages?user=" + USER + "&folder=Junk").json()
    assert [(m["uid"], m["subject"]) for m in listed] == [(1, "x"), (2, "r")]
    assert len(mail.control("GET", "messages").json()) == 4
    state = mail.control("GET", "state").json()
    assert state["messages"] == {USER: {"INBOX": 1, "Junk": 2}, "v@example.test": {"INBOX": 0, "Spam": 1}}
    assert state["ok"] is True
    assert state["done"] is True
    assert state["double"] == "mail"


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        ("accounts", [], "an account must be an object"),
        ("accounts", {"user": "x", "password": "pw", "junk_attribute": "no"}, "junk_attribute has the wrong type"),
        ("accounts", {"user": "x"}, "user and password must be strings"),
        ("accounts", {"user": 5, "password": "pw"}, "user and password must be strings"),
        ("accounts", {"user": "x", "password": "pw", "quota": 1}, "unknown account keys"),
        ("accounts", {"user": "x", "password": ""}, "invalid password"),
        ("deliver", {"from": "a@x.test"}, "to must be an address"),
        ("deliver", {"to": [], "from": "a@x.test"}, "to must be an address"),
        ("deliver", {"to": [1], "from": "a@x.test"}, "to must be an address"),
        ("deliver", {"to": USER}, "from must be a string"),
        ("deliver", {"to": USER, "from": ""}, "from must be a string"),
        ("deliver", {"to": USER, "from": "a@x.test", "subject": 1}, "subject must be a string"),
        ("deliver", {"to": USER, "from": "a@x.test", "spam": "yes"}, "spam must be a boolean"),
        ("deliver", {"to": "nobody@example.test", "from": "a@x.test"}, "unknown recipient"),
    ],
)
def test_control_errors(mail: Mail, path: str, body: Any, message: str) -> None:
    response = mail.control("POST", path, body)
    assert response.status == 400
    assert message in response.json()["error"]


def test_control_plumbing(mail: Mail) -> None:
    assert mail.control("GET", "nothing").status == 404
    response = mail.control("POST", "state")
    assert (response.status, response.headers["allow"]) == (405, "GET")
    assert mail.control("POST", "reset", b"{not json").status == 400
    bad_length = mail.client.request("POST", "/_double/reset", headers={"content-length": "x"})
    assert bad_length.status == 400
    outside = mail.client.request("GET", "/index.html")
    assert outside.status == 404
    assert mail.double.recorder.failures() == ["http GET /index.html: not a control route"]


def test_reset_keeps_accounts(mail: Mail) -> None:
    mail.control("POST", "deliver", {"to": USER, "from": "a@x.test"})
    mail.client.request("GET", "/x")
    state = mail.control("POST", "reset").json()
    assert state["messages"] == {USER: {"INBOX": 0, "Junk": 0}}
    assert state["failures"] == []
    assert state["requests"] == 0
    assert mail.control("GET", "journal").json() == []


def test_ca_names_join_the_certificate(tmp_path: Path) -> None:
    with MailDouble(ca_dir=tmp_path / "ca", ca_names=["mail.double.test", "127.0.0.1"]) as double:
        assert double.ca is not None
        assert double.ca.names == ("localhost", "127.0.0.1", "::1", "mail.double.test")
