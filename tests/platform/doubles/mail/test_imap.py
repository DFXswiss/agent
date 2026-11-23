"""U-DBL-mail: the IMAP4rev1 subset (imaplib and raw protocol), plain and IMAPS."""

from __future__ import annotations

import imaplib
from collections.abc import Callable
from datetime import date

import pytest

from agent_doubles.mail import imap as imap_module
from agent_doubles.mail.imap import Bad, Literal, parse_date, parse_items, parse_set, quote, tokenize
from agent_doubles.mail.message import compose

from .conftest import PASSWORD, USER, Mail, Wire

DAY = 86_400


def _deliver(mail: Mail, sender: str = "Login <login@auth.double.test>", **kwargs: object) -> None:
    raw = compose(sender, [USER], str(kwargs.pop("subject", "Sign in")), when=mail.clock(), text="text", **kwargs)  # type: ignore[arg-type]
    mail.double.store.deliver(raw, sender=sender, recipients=[USER])


def test_imaplib_flow_over_imaps(mail: Mail) -> None:
    _deliver(mail)
    _deliver(mail, spam=True, subject="Junk one")
    assert mail.double.imaps is not None
    context = mail.double.ca.client_context()  # type: ignore[union-attr]
    conn = imaplib.IMAP4_SSL("127.0.0.1", mail.double.imaps.port, ssl_context=context, timeout=10)
    try:
        assert "IMAP4REV1" in conn.capabilities
        assert conn.login(USER, PASSWORD)[0] == "OK"
        status, lines = conn.list()
        assert status == "OK"
        assert lines == [b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren \\Junk) "/" Junk']
        assert conn.select("Junk", readonly=True) == ("OK", [b"1"])
        assert conn.search(None, "FROM", '"auth.double.test"') == ("OK", [b"1"])
        status, data = conn.fetch("1", "(BODY.PEEK[]<0.20>)")
        assert status == "OK"
        assert data[0][0] == b"1 (BODY[]<0> {20}"  # type: ignore[index]
        assert len(data[0][1]) == 20  # type: ignore[index,arg-type]
        assert conn.close()[0] == "OK"
    finally:
        conn.logout()
    records = mail.double.recorder.journal.records()
    assert {r["method"] for r in records} == {"IMAPS"}
    login = next(r for r in records if r["path"] == "LOGIN")
    assert login["body"] == {"user": USER, "password": f"<redacted {len(PASSWORD)} chars>"}
    assert PASSWORD not in str(records)
    assert mail.double.state()["ok"] is True


def test_login_failures_and_states(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.imap.host, mail.double.imap.port)
    client.line()
    assert client.imap("a SELECT INBOX")[-1] == "a BAD not authenticated\r\n"
    assert client.imap(f"b LOGIN {USER} wrong")[-1] == "b NO [AUTHENTICATIONFAILED] Invalid credentials\r\n"
    assert client.imap("c LOGIN only-one")[-1] == "c BAD invalid arguments\r\n"
    assert client.imap(f'd LOGIN "{USER}" "{PASSWORD}"')[-1].startswith("d OK [CAPABILITY IMAP4rev1]")
    assert client.imap(f"e LOGIN {USER} {PASSWORD}")[-1] == "e BAD already authenticated\r\n"
    assert client.imap("f SEARCH ALL")[-1] == "f BAD no mailbox selected\r\n"
    assert client.imap("g CAPABILITY") == ["* CAPABILITY IMAP4rev1\r\n", "g OK CAPABILITY completed\r\n"]
    assert client.imap("h NOOP") == ["h OK NOOP completed\r\n"]
    assert client.imap("i SELECT Archive")[-1] == "i NO [NONEXISTENT] Mailbox does not exist\r\n"
    assert client.imap("j LOGOUT") == ["* BYE mail double logging out\r\n", "j OK LOGOUT completed\r\n"]
    assert client.closed()
    failures = mail.double.recorder.failures()
    assert failures == [
        "imap SELECT: not authenticated",
        "imap LOGIN: invalid arguments",
        "imap LOGIN: already authenticated",
        "imap SEARCH: no mailbox selected",
    ]
    records = mail.double.recorder.journal.records()
    assert all("wrong" not in str(r["body"]) for r in records)
    assert [r["result"] for r in records if r["path"] == "LOGIN"] == ["NO", "BAD", "OK", "BAD"]


def test_syntax_errors(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.imap.host, mail.double.imap.port)
    client.line()
    assert client.imap("a", "*")[-1] == "* BAD missing tag or command\r\n"
    assert client.imap("(a) LOGIN", "*")[-1] == "* BAD missing tag or command\r\n"
    assert client.imap("b FROB")[-1] == "b BAD unknown command\r\n"
    assert client.imap("c X-1")[-1] == "c BAD unknown command\r\n"
    assert client.imap('d LOGIN "unterminated')[-1] == "d BAD unterminated quoted string\r\n"
    assert client.imap("e LIST (")[-1] == "e BAD unbalanced parenthesis\r\n"
    assert client.imap("f LIST )")[-1] == "f BAD unbalanced parenthesis\r\n"
    assert client.imap("g UID STORE 1 +FLAGS (\\Seen)")[-1] == "g BAD unsupported UID command\r\n"
    assert client.imap("h UID")[-1] == "h BAD unsupported UID command\r\n"
    assert client.imap("i UID (x)")[-1] == "i BAD unsupported UID command\r\n"
    unparsed = [r for r in mail.double.recorder.journal.records() if r["path"] == "(invalid)"]
    assert all(r["body"] is None for r in unparsed)
    assert len(mail.double.recorder.failures()) == 10


def test_literals(mail: Mail, wire: Callable[..., Wire], monkeypatch: pytest.MonkeyPatch) -> None:
    client = wire(mail.double.imap.host, mail.double.imap.port)
    client.line()
    client.send(f"a LOGIN {{{len(USER)}}}\r\n")
    assert client.line() == "+ Ready for literal data\r\n"
    client.send(f"{USER} {{{len(PASSWORD)}}}\r\n")
    assert client.line() == "+ Ready for literal data\r\n"
    client.send(f"{PASSWORD}\r\n")
    assert client.until("a")[-1].startswith("a OK")
    monkeypatch.setattr(imap_module, "MAX_LITERAL", 4)
    assert client.imap("b SELECT {5}") == ["b BAD literal too large\r\n"]
    # A literal cut short by the end of the stream ends the session.
    client.send("c SELECT {3}\r\n")
    assert client.line() == "+ Ready for literal data\r\n"
    client.send("IN")
    client.sock.shutdown(1)
    assert client.closed()


def test_list_patterns_and_custom_spam_folder(start_mail: Callable[..., Mail], wire: Callable[..., Wire]) -> None:
    mail = start_mail(account=False)
    mail.double.add_account(USER, PASSWORD, junk_name="INBOX.Spam Box", junk_attribute=False, delimiter=".")
    client = wire(mail.double.imap.host, mail.double.imap.port)
    client.line()
    client.imap(f"a LOGIN {USER} {PASSWORD}")
    assert client.imap('b LIST "" *') == [
        '* LIST (\\HasNoChildren) "." INBOX\r\n',
        '* LIST (\\HasNoChildren) "." "INBOX.Spam Box"\r\n',
        "b OK LIST completed\r\n",
    ]
    assert client.imap('c LIST "" %') == ['* LIST (\\HasNoChildren) "." INBOX\r\n', "c OK LIST completed\r\n"]
    assert client.imap('d LIST "" inbox') == ['* LIST (\\HasNoChildren) "." INBOX\r\n', "d OK LIST completed\r\n"]
    assert client.imap('e LIST "" Inbox.*')[-1] == "e OK LIST completed\r\n"
    assert len(client.imap('e2 LIST "" Inbox.*')) == 1
    assert client.imap('f LIST "INBOX" *') == ["f OK LIST completed\r\n"]
    assert client.imap('g EXAMINE "INBOX.Spam Box"')[-1] == "g OK [READ-ONLY] EXAMINE completed\r\n"


def test_select_reports_and_new_mail(mail: Mail, imap_session: Callable[..., Wire]) -> None:
    _deliver(mail)
    client = imap_session()
    lines = client.imap("s SELECT inbox")
    assert lines[0] == "* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)\r\n"
    assert "* 1 EXISTS\r\n" in lines
    assert "* 0 RECENT\r\n" in lines
    assert any(line.startswith("* OK [UIDNEXT 2]") for line in lines)
    assert lines[-1] == "s OK [READ-WRITE] SELECT completed\r\n"
    _deliver(mail)
    assert client.imap("n NOOP") == ["* 2 EXISTS\r\n", "n OK NOOP completed\r\n"]
    _deliver(mail)
    assert client.imap("q SEARCH ALL") == ["* 3 EXISTS\r\n", "* SEARCH 1 2 3\r\n", "q OK SEARCH completed\r\n"]
    assert client.imap("c CLOSE") == ["c OK CLOSE completed\r\n"]
    assert client.imap("d CLOSE")[-1] == "d BAD no mailbox selected\r\n"


def test_search_keys(mail: Mail, imap_session: Callable[..., Wire]) -> None:
    _deliver(mail, date="Fri, 20 Nov 2026 08:00:00 +0000", subject="First")
    mail.clock.now += DAY
    _deliver(mail, sender="Other <news@other.test>", subject="News")
    mail.clock.now += DAY
    _deliver(mail, date="not a date")
    raw = b"Subject: no date\r\n\r\nbody\r\n"
    mail.double.store.deliver(raw, sender="x@other.test", recipients=[USER])
    client = imap_session("INBOX")

    def search(criteria: str) -> str:
        lines = client.imap(f"t SEARCH {criteria}")
        assert lines[-1] == "t OK SEARCH completed\r\n", lines
        return lines[-2].rstrip("\r\n")

    assert search("ALL") == "* SEARCH 1 2 3 4"
    assert search('FROM "AUTH.double.test"') == "* SEARCH 1 3"
    assert search("TO example.test") == "* SEARCH 1 2 3"
    assert search("SUBJECT news") == "* SEARCH 2"
    assert search("SINCE 22-Nov-2026") == "* SEARCH 2 3 4"
    assert search("BEFORE 22-nov-2026") == "* SEARCH 1"
    assert search("ON 23-Nov-2026") == "* SEARCH 3 4"
    assert search("SENTON 20-Nov-2026") == "* SEARCH 1"
    assert search("SENTSINCE 21-Nov-2026") == "* SEARCH 2"
    assert search("SENTBEFORE 21-Nov-2026") == "* SEARCH 1"
    assert search("NOT FROM other.test") == "* SEARCH 1 3 4"  # message 4 has no From header
    assert search("OR SUBJECT First SUBJECT News") == "* SEARCH 1 2"
    assert search("(FROM other.test SUBJECT news)") == "* SEARCH 2"
    assert search("2:*") == "* SEARCH 2 3 4"
    assert search("UID 1,3") == "* SEARCH 1 3"
    assert search("UNSEEN") == "* SEARCH 1 2 3 4"
    assert search("SEEN") == "* SEARCH"
    assert client.imap("u UID SEARCH FROM other.test")[-2] == "* SEARCH 2\r\n"
    for bad in ("", "FROM", "FROM (x)", "NOT", "OR ALL", "SINCE 31-Feb-2026", "SINCE yesterday", "KEYWORD x"):
        assert client.imap(f"b SEARCH {bad}".rstrip())[-1].startswith("b BAD"), bad


def test_fetch_items_and_seen(mail: Mail, imap_session: Callable[..., Wire]) -> None:
    _deliver(mail)
    _deliver(mail)
    raw = mail.double.store.account(USER).folders["INBOX"].messages[0].raw  # type: ignore[union-attr]
    header, _, text = raw.partition(b"\r\n\r\n")
    examine = imap_session("INBOX", examine=True)
    lines = examine.imap("f FETCH 1 (BODY[] UID)")
    assert lines[0] == f"* 1 FETCH (BODY[] {{{len(raw)}}}\r\n"
    assert mail.double.store.account(USER).folders["INBOX"].messages[0].flags == set()  # type: ignore[union-attr]
    client = imap_session("INBOX")
    lines = client.imap("f FETCH 1 FAST")
    assert lines[0] == f'* 1 FETCH (FLAGS () INTERNALDATE "21-Nov-2026 12:00:00 +0000" RFC822.SIZE {len(raw)})\r\n'
    lines = client.imap("f FETCH 1 BODY.PEEK[HEADER]")
    assert lines[0] == f"* 1 FETCH (BODY[HEADER] {{{len(header) + 4}}}\r\n"
    lines = client.imap("f FETCH 1 BODY.PEEK[TEXT]<2.3>")
    assert lines[:2] == ["* 1 FETCH (BODY[TEXT]<2> {3}\r\n", f"{text[2:5].decode()})\r\n"]
    lines = client.imap("f FETCH 1 RFC822.HEADER")
    assert lines[0].startswith("* 1 FETCH (RFC822.HEADER {")
    lines = client.imap("f FETCH 1 BODY[]")
    assert lines[-2] == " FLAGS (\\Seen))\r\n"
    lines = client.imap("f FETCH 1 (RFC822.TEXT FLAGS)")
    assert lines[-2] == " FLAGS (\\Seen))\r\n"
    lines = client.imap("f FETCH 2 RFC822")
    assert lines[0].startswith("* 2 FETCH (RFC822 {")
    assert lines[-2] == " FLAGS (\\Seen))\r\n"
    assert client.imap("f FETCH 1:* UID") == [
        "* 1 FETCH (UID 1)\r\n",
        "* 2 FETCH (UID 2)\r\n",
        "f OK FETCH completed\r\n",
    ]
    assert client.imap("u UID FETCH 2:* FLAGS") == [
        "* 2 FETCH (UID 2 FLAGS (\\Seen))\r\n",
        "u OK UID FETCH completed\r\n",
    ]
    assert client.imap("u UID FETCH 2 (UID)") == ["* 2 FETCH (UID 2)\r\n", "u OK UID FETCH completed\r\n"]
    assert client.imap("u UID FETCH 9 UID") == ["u OK UID FETCH completed\r\n"]
    assert client.imap("s SEARCH SEEN")[-2] == "* SEARCH 1 2\r\n"
    for bad in ("3 UID", "0 UID", "1", "x UID", "1 ENVELOPE", "1 ()", "1 ((UID))", "1 BODY[MIME]"):
        assert client.imap(f"b FETCH {bad}")[-1].startswith("b BAD"), bad


def test_message_without_header_body_separator(mail: Mail, imap_session: Callable[..., Wire]) -> None:
    mail.double.store.deliver(b"Subject: only header\r\n", sender="x@y.test", recipients=[USER])
    client = imap_session("INBOX")
    lines = client.imap("f FETCH 1 (BODY.PEEK[TEXT])")
    assert lines[0] == "* 1 FETCH (BODY[TEXT] {0}\r\n"


def test_empty_folder(imap_session: Callable[..., Wire]) -> None:
    client = imap_session("Junk")
    assert client.imap("f FETCH 1:* UID")[-1].startswith("f BAD")
    assert client.imap("u UID FETCH 1:* UID") == ["u OK UID FETCH completed\r\n"]
    assert client.imap("s SEARCH ALL") == ["* SEARCH\r\n", "s OK SEARCH completed\r\n"]


def test_helpers() -> None:
    assert tokenize(['a "q\\"x\\\\" (b (c)) d', Literal("lit"), " e"]) == ["a", 'q"x\\', ["b", ["c"]], "d", "lit", "e"]
    assert quote("INBOX") == "INBOX"
    assert quote("") == '""'
    assert quote('a "b"\\') == '"a \\"b\\"\\\\"'
    assert parse_set("3:1,5,*", 7) == {1, 2, 3, 5, 7}
    assert parse_set("1:4000000000", 3) == {1, 2, 3}
    assert parse_set("9:4000000000,8", 3) == {8, 9}
    with pytest.raises(Bad):
        parse_set(["1"], 3)
    assert parse_date("1-Jan-2026") == date(2026, 1, 1)
    with pytest.raises(Bad):
        parse_date(["1-Jan-2026"])
    with pytest.raises(Bad):
        parse_items([])
    with pytest.raises(Bad):
        parse_items([["UID"]])


def test_imap_idle_connection_closes_on_stop(start_mail: Callable[..., Mail], wire: Callable[..., Wire]) -> None:
    mail = start_mail()
    client = wire(mail.double.imap.host, mail.double.imap.port)
    client.line()
    mail.double.stop()
    assert client.closed()
