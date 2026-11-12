from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from agent_cli.coordinator_common import (
    _SECRET_ASSIGNMENT_KEYWORDS,
    _redact_secret_assignments,
    redact as redact_shared,
)
from agent_cli.errors import (
    config_path,
    cursor_path,
    default_fetch,
    error_class,
    fingerprint,
    is_incident_line,
    line_fingerprint,
    load_config,
    redact,
    scan_errors,
    stack_sig,
)
from agent_cli.lane import redact_lane_text
from agent_cli.store import Store, StoreError
from agent_cli.text_sanitize import sanitize_terminal_text


def _runner_session(store: Store, sid: str = "runner-1") -> None:
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "skills": ["spine", "review-loop", "pr-review", "error-fix"],
        },
    )


def _write_config(home: Path, session_id: str = "runner-1") -> None:
    config_path(home).write_text(
        json.dumps({"session_id": session_id, "service": "api", "environment": "prod", "repo": "org/app"}),
        encoding="utf-8",
    )


def test_load_config_missing(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="error-fix.json not found"):
        load_config(config_path(tmp_path))


def test_load_config_missing_session_id(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(StoreError, match="session_id is missing"):
        load_config(path)


def test_load_config_rejects_credentials(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text(json.dumps({"session_id": "s", "password": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(json.dumps({"session_id": "s", "password ": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "headers": {"token": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "headers": {"Token": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?api_key=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?access_token=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "headers": {"api-key": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?api-key=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "client_secret": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "aws_secret_access_key": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?access_key=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q#password=x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(json.dumps({"session_id": "s", "api_key": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://u:p@logs.example/q"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)


def test_redact_and_fingerprint() -> None:
    raw = "TimeoutError bearer SECRETTOKENVALUE0123456789 user@example.com"
    cleaned = redact(raw)
    assert "SECRETTOKENVALUE0123456789" not in cleaned
    assert "user@example.com" not in cleaned
    assert "[redacted]" in cleaned
    quoted = redact('{"password":"hunter2"} Authorization: Basic abcdef Cookie: sid=1 https://u:p@host/x')
    assert "hunter2" not in quoted
    assert "abcdef" not in quoted
    assert "sid=1" not in quoted
    assert "u:p@" not in quoted
    digest = redact("Authorization: Digest username=alice, nonce=abc")
    assert "username=alice" not in digest
    assert "nonce=abc" not in digest
    dsn = redact("postgresql://u:p@logs.example/db TimeoutError")
    assert "u:p@" not in dsn
    assert "TimeoutError" in dsn
    cookie = redact("Cookie: sid=1; extra=remain")
    assert "sid=1" not in cookie
    assert "extra=remain" not in cookie
    jwt = redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc_def-ghi")
    assert "eyJhbGciOiJIUzI1NiJ9" not in jwt
    akia = redact("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in akia
    client = redact('{"client_secret":"s3cretvalue"} access-token=abcdef')
    assert "s3cretvalue" not in client
    assert "abcdef" not in client
    aws = redact('{"aws_secret_access_key":"wJalrXUtnFEMI"} aws_secret_access_key=wJalrXUtnFEMI')
    assert "wJalrXUtnFEMI" not in aws
    assert error_class(cleaned) == "TimeoutError"
    sig = stack_sig(cleaned)
    assert len(sig) == 16
    fp = fingerprint(service="api", error_class="TimeoutError", stack_sig=sig, environment="prod")
    assert fp.startswith("api|TimeoutError|")
    assert fp.endswith("|prod")


@pytest.mark.parametrize(
    ("raw", "contains_secret"),
    [
        ("m" * 1_000_000, False),
        ("api_key=model-secret-" + "m" * 1_000_000, True),
    ],
    ids=("separatorless-word", "keyword-prefix"),
)
def test_error_redaction_is_linear_on_large_words(
    raw: str, contains_secret: bool
) -> None:
    started = time.monotonic()
    cleaned = redact(raw)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"error redaction took {elapsed:.3f}s"
    if contains_secret:
        assert "model-secret" not in cleaned
        assert "[redacted]" in cleaned


def test_error_redaction_uses_unbounded_assignment_key_scanner() -> None:
    long_key = "A" * 10_000 + "authorization" + "Z" * 10_000
    cleaned = redact(
        f'{{"{long_key}":"first secret words","visible":"ok"}}'
    )

    assert "first secret words" not in cleaned
    assert '"visible":"ok"' in cleaned
    assert "[redacted]" in cleaned


def test_error_redaction_covers_legacy_passwd_and_access_key_assignments() -> None:
    cleaned = redact(
        "ERROR passwd=hunter2 "
        "db_passwd='correct horse' "
        'access-key="dash secret" '
        'accesskey=compact-secret '
        "aws_access_key_id=ASIAUNQUOTED "
        '{"access_key":"ASIAJSON","visible":"keep-me"}'
    )

    for secret in (
        "hunter2",
        "correct horse",
        "dash secret",
        "compact-secret",
        "ASIAUNQUOTED",
        "ASIAJSON",
    ):
        assert secret not in cleaned
    assert '"visible":"keep-me"' in cleaned
    assert cleaned.count("[redacted]") == 6


_TERMINATED_CONTROL_EXPECTED = "prefix [redacted]\nvisible line"


@pytest.mark.parametrize(
    ("controlled", "expected"),
    (
        pytest.param(
            "password\x1b]x=y\x07_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="osc-payload-operator",
        ),
        pytest.param(
            "password\x9dx=y\x9c_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="osc-8-bit",
        ),
        pytest.param(
            "password\x1bPx=y\x1b\\_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="dcs-7-bit",
        ),
        pytest.param(
            "password\x90x=y\x9c_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="dcs-8-bit",
        ),
        pytest.param(
            "password\x1b^x=y\x1b\\_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="pm-7-bit",
        ),
        pytest.param(
            "password\x9ex=y\x9c_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="pm-8-bit",
        ),
        pytest.param(
            "password\x1b_x=y\x1b\\_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="apc-7-bit",
        ),
        pytest.param(
            "password\x9fx=y\x9c_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="apc-8-bit",
        ),
        pytest.param(
            "password\x1bXx=y\x1b\\_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="sos-7-bit",
        ),
        pytest.param(
            "password\x98x=y\x9c_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="sos-8-bit",
        ),
        pytest.param(
            "passwor\x1b[31d\x1b]0;=\x07 hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="csi-final-completes-password-before-osc",
        ),
        pytest.param(
            "secre\x1b[31t\x1b]0;=\x07 hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="csi-final-completes-secret-before-osc",
        ),
        pytest.param(
            "password\x1b[31m_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="sgr-key-interleave",
        ),
        pytest.param(
            "password\x1b]8;;\x07_file = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="osc-empty-link",
        ),
        pytest.param(
            "password\x1b]title\x07suffix=hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="osc-key-suffix",
        ),
        pytest.param(
            '"password" \x1b]0;=\x07hunter2',
            _TERMINATED_CONTROL_EXPECTED,
            id="quoted-key-osc-operator",
        ),
        pytest.param(
            "password \x1b]8;;https://example.com\x07 = hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="osc-link-before-visible-operator",
        ),
        pytest.param(
            "password\x1bP0;=\x07\x1b\\hunter2",
            _TERMINATED_CONTROL_EXPECTED,
            id="dcs-bel-payload-before-st",
        ),
        pytest.param(
            "password\x1b]unterminated hunter2",
            "prefix [redacted]",
            id="unterminated-osc-7-bit",
        ),
        pytest.param(
            "password\x90unterminated hunter2",
            "prefix [redacted]",
            id="unterminated-dcs-8-bit",
        ),
    ),
)
def test_controlled_secret_keyword_lines_fail_closed_across_shared_sinks(
    controlled: str,
    expected: str,
) -> None:
    raw = f"prefix {controlled}\nvisible line"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact(raw),
        redact_lane_text(raw, limit=None),
    ):
        assert cleaned == expected
        assert "hunter2" not in cleaned


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        ("\x1b]", "\x07"),
        ("\x9d", "\x1b\\"),
        ("\x1bP", "\x9c"),
        ("\x90", "\x1b\\"),
        ("\x1b^", "\x9c"),
        ("\x9e", "\x1b\\"),
        ("\x1b_", "\x9c"),
        ("\x9f", "\x1b\\"),
        ("\x1bX", "\x9c"),
        ("\x98", "\x1b\\"),
    ),
    ids=(
        "osc-7-bit",
        "osc-8-bit",
        "dcs-7-bit",
        "dcs-8-bit",
        "pm-7-bit",
        "pm-8-bit",
        "apc-7-bit",
        "apc-8-bit",
        "sos-7-bit",
        "sos-8-bit",
    ),
)
def test_multiline_string_control_secret_lines_are_one_logical_line_across_shared_sinks(
    introducer: str, terminator: str
) -> None:
    raw = f"prefix {introducer}token\nhunter2{terminator}\nvisible line"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert cleaned == "prefix [redacted]\nvisible line"
        assert "hunter2" not in cleaned


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        ("\x1b]", "\x07"),
        ("\x9d", "\x07"),
        ("\x1bP", "\x1b\\"),
        ("\x90", "\x9c"),
        ("\x1b^", "\x1b\\"),
        ("\x9e", "\x9c"),
        ("\x1b_", "\x1b\\"),
        ("\x9f", "\x9c"),
        ("\x1bX", "\x1b\\"),
        ("\x98", "\x9c"),
    ),
    ids=(
        "osc-7-bit",
        "osc-8-bit",
        "dcs-7-bit",
        "dcs-8-bit",
        "pm-7-bit",
        "pm-8-bit",
        "apc-7-bit",
        "apc-8-bit",
        "sos-7-bit",
        "sos-8-bit",
    ),
)
@pytest.mark.parametrize("depth", (2, 3), ids=("depth-2", "depth-3"))
def test_nested_string_control_boundaries_match_across_sinks_and_sanitizer(
    introducer: str, terminator: str, depth: int
) -> None:
    marker = "Zx7Qp9Lm4V2"
    raw = (
        "before "
        + (introducer * depth)
        + "inner"
        + terminator
        + marker
        + (terminator * (depth - 1))
        + " after"
    )

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert cleaned == "before [redacted] after"
        assert marker not in cleaned
    assert sanitize_terminal_text(raw) == "before  after"


def test_nested_osc_payload_view_keeps_scanner_key_bytes_across_sinks() -> None:
    raw = (
        "x\x1b]\x1b]inner\x07 "
        "\x1b[api_key= hunter2\x07 visible"
    )

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert cleaned == "x[redacted]"
        assert "hunter2" not in cleaned
    assert sanitize_terminal_text(raw) == "x visible"


@pytest.mark.parametrize(
    ("payload", "suffix"),
    (
        ("a\x1b[31muthorization", "=hunter2"),
        ("au\x1b[31mthorization", " hunter2"),
        ("authoriz\x1b[31mation", ": hunter2"),
    ),
    ids=("equals", "space", "colon"),
)
def test_nested_osc_payload_clean_view_omits_complete_controls_across_sinks(
    payload: str,
    suffix: str,
) -> None:
    raw = f"prefix \x1b]0;{payload}\x07{suffix}"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert cleaned == "prefix [redacted]"
        assert "hunter2" not in cleaned


@pytest.mark.parametrize(
    "introducer",
    (
        "\x1b]",
        "\x9d",
        "\x1bP",
        "\x90",
        "\x1b^",
        "\x9e",
        "\x1b_",
        "\x9f",
        "\x1bX",
        "\x98",
    ),
    ids=(
        "osc-7-bit",
        "osc-8-bit",
        "dcs-7-bit",
        "dcs-8-bit",
        "pm-7-bit",
        "pm-8-bit",
        "apc-7-bit",
        "apc-8-bit",
        "sos-7-bit",
        "sos-8-bit",
    ),
)
def test_unterminated_nested_string_controls_consume_the_outer_span_everywhere(
    introducer: str,
) -> None:
    marker = "Zx7Qp9Lm4V2"
    raw = f"before {introducer}{introducer}inner {marker}"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert marker not in cleaned
        assert cleaned in ("[redacted]", "before [redacted]")
    assert sanitize_terminal_text(raw) == "before "


@pytest.mark.parametrize(
    "introducer",
    (
        "\x1b]",
        "\x9d",
        "\x1bP",
        "\x90",
        "\x1b^",
        "\x9e",
        "\x1b_",
        "\x9f",
        "\x1bX",
        "\x98",
    ),
    ids=(
        "osc-7-bit",
        "osc-8-bit",
        "dcs-7-bit",
        "dcs-8-bit",
        "pm-7-bit",
        "pm-8-bit",
        "apc-7-bit",
        "apc-8-bit",
        "sos-7-bit",
        "sos-8-bit",
    ),
)
def test_unterminated_multiline_string_control_secret_lines_fail_closed_across_shared_sinks(
    introducer: str,
) -> None:
    raw = f"prefix {introducer}token\nhunter2"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact(raw),
    ):
        assert cleaned == "prefix [redacted]"
        assert "hunter2" not in cleaned


def test_controlled_line_keyword_views_cover_scanner_control_joins() -> None:
    string_controls = [
        ("\x1b]", "\x07"),
        ("\x9d", "\x9c"),
        ("\x1bP", "\x1b\\"),
        ("\x90", "\x9c"),
        ("\x1b^", "\x1b\\"),
        ("\x9e", "\x9c"),
        ("\x1b_", "\x1b\\"),
        ("\x9f", "\x9c"),
        ("\x1bX", "\x1b\\"),
        ("\x98", "\x9c"),
    ]
    suffixes = (" hunter2", "=hunter2", " = hunter2", ": hunter2")
    payload_c0_suffixes = ("=hunter2", " hunter2", ": hunter2")
    lines: list[str] = []
    for keyword in _SECRET_ASSIGNMENT_KEYWORDS:
        for intermediate in ("", "!", " ", "$", "-"):
            if intermediate == "-" and "-" in keyword:
                split = keyword.index("-")
                controlled = (
                    keyword[:split]
                    + "\x1b[31-"
                    + keyword[split + 1]
                    + keyword[split + 2 :]
                )
            elif intermediate == "-":
                controlled = keyword + "\x1b[31-m"
            else:
                controlled = keyword[:-1] + f"\x1b[31{intermediate}{keyword[-1]}"
            lines.extend(controlled + suffix for suffix in suffixes)

        esc_final = keyword[:-1] + "\x1b" + keyword[-1]
        lines.extend(esc_final + suffix for suffix in suffixes)

        split = max(1, len(keyword) // 2)
        for introducer, terminator in string_controls:
            for payload in ("opaque", "0;=", "0;:"):
                controlled = (
                    keyword[:split]
                    + introducer
                    + payload
                    + terminator
                    + keyword[split:]
                )
                lines.extend(controlled + suffix for suffix in suffixes)
            for c0 in ("\x00", "\x08", "\x7f", "\r"):
                payload_keyword = (
                    "prefix "
                    + introducer
                    + "0;"
                    + keyword[:split]
                    + c0
                    + keyword[split:]
                    + terminator
                )
                lines.extend(
                    payload_keyword + suffix for suffix in payload_c0_suffixes
                )
            for nested_control in (
                "\x1b[31m",
                "\x9b31m",
                "\x1b7",
                "\x1b(0",
            ):
                for nested_split in range(1, len(keyword)):
                    payload_keyword = (
                        "prefix "
                        + introducer
                        + "0;"
                        + keyword[:nested_split]
                        + nested_control
                        + keyword[nested_split:]
                        + terminator
                    )
                    lines.extend(
                        payload_keyword + suffix
                        for suffix in payload_c0_suffixes
                    )

        for c0 in ("\x08", "\x00", "\x7f", "\r"):
            split = max(1, len(keyword) // 2)
            lines.append(
                keyword[:split]
                + c0
                + keyword[split:]
                + " is hunter2 \x1b[31mnote\x1b[0m"
            )

    lines.append("passwor\x1b[31!d\x1b]0;=\x07 hunter2")
    raw = "\n".join(lines)

    sinks = (
        ("shared", redact_shared(raw, limit=None)),
        ("lane", redact_lane_text(raw, limit=None)),
        ("errors", redact(raw)),
    )
    for sink, cleaned in sinks:
        cleaned_lines = cleaned.split("\n")
        assert len(cleaned_lines) == len(lines)
        for source, cleaned_line in zip(lines, cleaned_lines, strict=True):
            assert "hunter2" not in cleaned_line, (
                f"{sink} leaked generated line {source!r}"
            )


def test_assignment_scanner_rechecks_operator_after_control_completes_key(
) -> None:
    raw = "passwor\x1b[31!d\x1b]0;=\x07 hunter2"

    cleaned = _redact_secret_assignments(raw)

    assert "hunter2" not in cleaned
    assert "[redacted]" in cleaned


def test_controlled_line_without_secret_keyword_keeps_existing_handling() -> None:
    raw = "prefix \x1b[31mordinary\x1b[0m tail\nvisible line"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact(raw),
        redact_lane_text(raw, limit=None),
    ):
        assert cleaned == "prefix ordinary tail\nvisible line"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (
            "password \x1b[31m: correct-horse visible=keep",
            "correct-horse",
        ),
        (
            'password:\x1b[31m "correct horse" visible=keep',
            "correct horse",
        ),
        (
            "password=\x1b[31m correct-horse visible=keep",
            "correct-horse",
        ),
        ("password\x1b[=m hunter2 visible=keep", "hunter2"),
        ("password\x1b= hunter2 visible=keep", "hunter2"),
        ("password\x1b[:m hunter2 visible=keep", "hunter2"),
    ],
    ids=(
        "control-before-operator",
        "control-before-quoted",
        "control-before-unquoted",
        "csi-equals-parameter-is-operator",
        "esc-equals-final-is-operator",
        "csi-colon-parameter-is-operator",
    ),
)
def test_error_redaction_consumes_terminal_controls_around_assignment_operator(
    raw: str, secret: str
) -> None:
    cleaned = redact(raw)

    assert secret not in cleaned
    assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        pytest.param("\x1b]", "\x07", id="osc-7-bel"),
        pytest.param("\x1b]", "\x1b\\", id="osc-7-st-7"),
        pytest.param("\x1b]", "\x9c", id="osc-7-st-8"),
        pytest.param("\x9d", "\x07", id="osc-8-bel"),
        pytest.param("\x9d", "\x1b\\", id="osc-8-st-7"),
        pytest.param("\x9d", "\x9c", id="osc-8-st-8"),
        pytest.param("\x1bP", "\x1b\\", id="dcs-7-st-7"),
        pytest.param("\x1bP", "\x9c", id="dcs-7-st-8"),
        pytest.param("\x90", "\x1b\\", id="dcs-8-st-7"),
        pytest.param("\x90", "\x9c", id="dcs-8-st-8"),
        pytest.param("\x1b^", "\x1b\\", id="pm-7-st-7"),
        pytest.param("\x1b^", "\x9c", id="pm-7-st-8"),
        pytest.param("\x9e", "\x1b\\", id="pm-8-st-7"),
        pytest.param("\x9e", "\x9c", id="pm-8-st-8"),
        pytest.param("\x1b_", "\x1b\\", id="apc-7-st-7"),
        pytest.param("\x1b_", "\x9c", id="apc-7-st-8"),
        pytest.param("\x9f", "\x1b\\", id="apc-8-st-7"),
        pytest.param("\x9f", "\x9c", id="apc-8-st-8"),
        pytest.param("\x1bX", "\x1b\\", id="sos-7-st-7"),
        pytest.param("\x1bX", "\x9c", id="sos-7-st-8"),
        pytest.param("\x98", "\x1b\\", id="sos-8-st-7"),
        pytest.param("\x98", "\x9c", id="sos-8-st-8"),
    ),
)
@pytest.mark.parametrize("operator", ("=", ":"), ids=("equals", "colon"))
@pytest.mark.parametrize(
    ("value_prefix", "value"),
    (
        pytest.param("", "hunter2", id="compact-unquoted"),
        pytest.param(" ", "hunter2", id="spaced-unquoted"),
        pytest.param("", '"hunter two"', id="quoted"),
    ),
)
def test_error_redaction_uses_operator_inside_complete_string_control(
    introducer: str,
    terminator: str,
    operator: str,
    value_prefix: str,
    value: str,
) -> None:
    control = f"{introducer}0;{operator}{terminator}"

    cleaned = redact(f"password{control}{value_prefix}{value} visible")

    assert cleaned == "[redacted]"


def test_error_redaction_ignores_string_payload_operator_before_secret_key_complete(
) -> None:
    cleaned = redact("pass\x1b]0;=\x07word=hunter2 visible")

    assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    "control",
    (
        pytest.param("\x1b]title\x07", id="osc-7-bit"),
        pytest.param("\x9dtitle\x07", id="osc-8-bit"),
        pytest.param("\x1bPtitle\x1b\\", id="dcs-7-bit"),
        pytest.param("\x90title\x9c", id="dcs-8-bit"),
        pytest.param("\x1b^title\x1b\\", id="pm-7-bit"),
        pytest.param("\x9etitle\x9c", id="pm-8-bit"),
        pytest.param("\x1b_title\x1b\\", id="apc-7-bit"),
        pytest.param("\x9ftitle\x9c", id="apc-8-bit"),
        pytest.param("\x1bXtitle\x1b\\", id="sos-7-bit"),
        pytest.param("\x98title\x9c", id="sos-8-bit"),
        pytest.param("\x1b[31m", id="sgr"),
    ),
)
@pytest.mark.parametrize(
    "assignment",
    (
        pytest.param("_file = hunter2", id="underscore-visible-equals"),
        pytest.param("suffix=hunter2", id="suffix-visible-equals"),
        pytest.param("_file: hunter2", id="underscore-visible-colon"),
    ),
)
def test_redact_keeps_controls_inside_complete_secret_keys(
    control: str, assignment: str
) -> None:
    raw = f"neighbour password{control}{assignment} visible=keep"

    for cleaned in (redact_shared(raw), redact(raw)):
        assert cleaned == "neighbour [redacted]"
        assert all(
            char in "\t\n"
            or not (ord(char) < 32 or 0x7F <= ord(char) <= 0x9F)
            for char in cleaned
        )


def test_redact_fails_closed_on_secret_token_with_string_control() -> None:
    raw = "neighbour password\x1b]title opaque\x07suffix visible"

    assert redact_shared(raw) == "neighbour [redacted]"
    assert redact(raw) == "neighbour [redacted]"


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        pytest.param("\x1b]", "\x07", id="osc-7"),
        pytest.param("\x9d", "\x07", id="osc-8"),
        pytest.param("\x1bP", "\x1b\\", id="dcs-7"),
        pytest.param("\x90", "\x9c", id="dcs-8"),
        pytest.param("\x1b^", "\x1b\\", id="pm-7"),
        pytest.param("\x9e", "\x9c", id="pm-8"),
        pytest.param("\x1b_", "\x1b\\", id="apc-7"),
        pytest.param("\x9f", "\x9c", id="apc-8"),
        pytest.param("\x1bX", "\x1b\\", id="sos-7"),
        pytest.param("\x98", "\x9c", id="sos-8"),
    ),
)
@pytest.mark.parametrize("quoted", (False, True), ids=("unquoted", "quoted"))
@pytest.mark.parametrize(
    "separator", ("", " "), ids=("direct", "whitespace")
)
@pytest.mark.parametrize(
    "visible_operator", ("=", ":"), ids=("visible-equals", "visible-colon")
)
@pytest.mark.parametrize(
    "payload",
    ("0;=", "0;:", "8;;https://example.com"),
    ids=("payload-equals", "payload-colon", "payload-https"),
)
def test_error_redaction_prefers_visible_operator_after_string_controls(
    introducer: str,
    terminator: str,
    quoted: bool,
    separator: str,
    visible_operator: str,
    payload: str,
) -> None:
    key = '"password"' if quoted else "password"
    control = f"{introducer}{payload}{terminator}"
    raw = f"{key}{separator}{control} {visible_operator} hunter2 visible"

    for cleaned in (redact_shared(raw), redact(raw)):
        assert cleaned == "[redacted]"


@pytest.mark.parametrize("quoted", (False, True), ids=("unquoted", "quoted"))
@pytest.mark.parametrize(
    "separator", ("", " "), ids=("direct", "whitespace")
)
@pytest.mark.parametrize("payload_operator", ("=", ":"), ids=("equals", "colon"))
@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        pytest.param("\x1b]", "\x07", id="osc-7"),
        pytest.param("\x9d", "\x1b\\", id="osc-8"),
    ),
)
def test_error_redaction_uses_string_control_operator_after_all_separators(
    quoted: bool,
    separator: str,
    payload_operator: str,
    introducer: str,
    terminator: str,
) -> None:
    key = '"password"' if quoted else "password"
    control = f"{introducer}0;{payload_operator}{terminator}"
    raw = f"{key}{separator}{control} \x1b[31m hunter2 visible"

    for cleaned in (redact_shared(raw), redact(raw)):
        assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    "introducer",
    (
        pytest.param("\x1bP", id="dcs-7"),
        pytest.param("\x90", id="dcs-8"),
        pytest.param("\x1b^", id="pm-7"),
        pytest.param("\x9e", id="pm-8"),
        pytest.param("\x1b_", id="apc-7"),
        pytest.param("\x9f", id="apc-8"),
        pytest.param("\x1bX", id="sos-7"),
        pytest.param("\x98", id="sos-8"),
    ),
)
@pytest.mark.parametrize(
    "terminator", ("\x1b\\", "\x9c"), ids=("st-7", "st-8")
)
@pytest.mark.parametrize("payload_operator", ("=", ":"), ids=("equals", "colon"))
def test_error_redaction_does_not_end_non_osc_string_controls_at_bel(
    introducer: str, terminator: str, payload_operator: str
) -> None:
    control = (
        f"{introducer}0;{payload_operator}\x07still-opaque{terminator}"
    )
    raw = f"password{control}hunter2 visible"

    for cleaned in (redact_shared(raw), redact(raw)):
        assert cleaned == "[redacted]"


def test_redact_strips_otel_ids() -> None:
    trace_id = "a" * 32
    span_id = "b" * 16
    traceparent = f"00-{trace_id}-{span_id}-01"
    cases = [
        (
            f"trace_id={trace_id} span_id={span_id}",
            (trace_id, span_id),
            ("trace_id=[redacted]", "span_id=[redacted]"),
        ),
        (
            f"TRACE_ID={trace_id.upper()} SPAN_ID={span_id.upper()}",
            (trace_id.upper(), span_id.upper()),
            ("TRACE_ID=[redacted]", "SPAN_ID=[redacted]"),
        ),
        (
            f"trace_id: {trace_id} span_id: {span_id}",
            (trace_id, span_id),
            ("trace_id: [redacted]", "span_id: [redacted]"),
        ),
        (
            f'"trace_id":"{trace_id}","span_id":"{span_id}"',
            (trace_id, span_id),
            ('"trace_id":"[redacted]"', '"span_id":"[redacted]"'),
        ),
        (
            f'"trace_id": "{trace_id}", "span_id": "{span_id}"',
            (trace_id, span_id),
            ('"trace_id": "[redacted]"', '"span_id": "[redacted]"'),
        ),
        (
            f'trace_id="{trace_id}" span_id="{span_id}"',
            (trace_id, span_id),
            ('trace_id="[redacted]"', 'span_id="[redacted]"'),
        ),
        (
            '{"log":"...{\\"trace_id\\":\\"'
            + trace_id
            + '\\",\\"span_id\\":\\"'
            + span_id
            + '\\"}..."}',
            (trace_id, span_id),
            ('\\"trace_id\\":\\"[redacted]\\"', '\\"span_id\\":\\"[redacted]\\"'),
        ),
        (
            f"traceparent: {traceparent}",
            (traceparent, trace_id, span_id),
            ("traceparent: [redacted]",),
        ),
        (
            f"traceparent={traceparent}",
            (traceparent, trace_id, span_id),
            ("traceparent=[redacted]",),
        ),
        (
            f'"traceparent":"{traceparent}"',
            (traceparent, trace_id, span_id),
            ('"traceparent":"[redacted]"',),
        ),
        (
            f'"traceparent": "{traceparent}"',
            (traceparent, trace_id, span_id),
            ('"traceparent": "[redacted]"',),
        ),
        (
            f'traceparent="{traceparent}"',
            (traceparent, trace_id, span_id),
            ('traceparent="[redacted]"',),
        ),
        (
            '{"log":"...{\\"traceparent\\":\\"'
            + traceparent
            + '\\"}..."}',
            (traceparent, trace_id, span_id),
            ('\\"traceparent\\":\\"[redacted]\\"',),
        ),
    ]
    for shape, raw_ids, labelled in cases:
        cleaned = redact(shape)
        for raw_id in raw_ids:
            assert raw_id not in cleaned
        for fragment in labelled:
            assert fragment in cleaned


def test_redact_strips_otel_ids_of_non_standard_length() -> None:
    trace_id = "c" * 10
    span_id = "d" * 18
    cleaned = redact(f"trace_id={trace_id} span_id={span_id}")
    assert trace_id not in cleaned
    assert span_id not in cleaned
    assert "trace_id=[redacted]" in cleaned
    assert "span_id=[redacted]" in cleaned


def test_redact_strips_camel_case_otel_ids() -> None:
    trace_id = "a" * 32
    span_id = "b" * 16
    parent_span_id = "c" * 16
    cleaned = redact(f"traceId={trace_id} spanId={span_id} parentSpanId={parent_span_id}")
    for raw_id in (trace_id, span_id, parent_span_id):
        assert raw_id not in cleaned
    for labelled in (
        "traceId=[redacted]",
        "spanId=[redacted]",
        "parentSpanId=[redacted]",
    ):
        assert labelled in cleaned

    json_cleaned = redact(
        f'"traceId":"{trace_id}","spanId":"{span_id}","parentSpanId":"{parent_span_id}"'
    )
    for raw_id in (trace_id, span_id, parent_span_id):
        assert raw_id not in json_cleaned
    for labelled in (
        '"traceId":"[redacted]"',
        '"spanId":"[redacted]"',
        '"parentSpanId":"[redacted]"',
    ):
        assert labelled in json_cleaned

    snake_cleaned = redact(f"parent_span_id={parent_span_id}")
    assert parent_span_id not in snake_cleaned
    assert snake_cleaned == "parent_span_id=[redacted]"


def test_redact_strips_hyphenated_otel_trace_id() -> None:
    trace_id = "550e8400-e29b-41d4-a716-446655440000"
    cleaned = redact(f"trace_id={trace_id}")
    assert cleaned == "trace_id=[redacted]"
    for segment in ("550e8400", "e29b", "41d4", "a716", "446655440000"):
        assert segment not in cleaned


def test_redact_keeps_partial_non_otel_values() -> None:
    for raw in (
        "trace_id=deadbeef-cafe-x",
        "span_id=deadbeef-cafe-x",
        "traceparent=00-aaaa-bbbb-01x",
        "trace_id=deadbeefzz",
    ):
        assert redact(raw) == raw


def test_redact_strips_hyphenated_otel_span_id() -> None:
    span_id = "550e8400-e29b-41d4-a716-446655440000"
    assert redact(f"span_id={span_id}") == "span_id=[redacted]"


def test_redact_keeps_non_otel_span_keys() -> None:
    span_id = "b" * 16
    cleaned = redact(f"myspan_id={span_id}")
    assert cleaned == f"myspan_id={span_id}"
    assert "myspan_id=[redacted]" not in cleaned


def test_stack_sig_ignores_otel_ids() -> None:
    first = "TimeoutError trace_id=" + "a" * 32 + " span_id=" + "b" * 16 + " boom"
    second = "TimeoutError trace_id=" + "c" * 32 + " span_id=" + "d" * 16 + " boom"
    other = "ValueError trace_id=" + "c" * 32 + " span_id=" + "d" * 16 + " boom"
    assert stack_sig(first) == stack_sig(second)
    assert stack_sig(first) != stack_sig(other)


def test_stack_sig_ignores_camel_case_and_hyphenated_otel_ids() -> None:
    first = (
        "TimeoutError traceId="
        + "a" * 32
        + " spanId="
        + "b" * 16
        + " parentSpanId="
        + "c" * 16
        + " boom"
    )
    second = (
        "TimeoutError traceId="
        + "d" * 32
        + " spanId="
        + "e" * 16
        + " parentSpanId="
        + "f" * 16
        + " boom"
    )
    assert stack_sig(first) == stack_sig(second)

    first_hyphen = "TimeoutError trace_id=aabb-ccdd-eeff-abcd-bcde-cdef boom"
    second_hyphen = "TimeoutError trace_id=bbcc-ddee-ffaa-bcde-cdef-defa boom"
    assert re.sub(r"\d+", "", first_hyphen) != re.sub(r"\d+", "", second_hyphen)
    assert "trace_id=[redacted]" in redact(first_hyphen)
    assert "trace_id=[redacted]" in redact(second_hyphen)
    assert stack_sig(first_hyphen) == stack_sig(second_hyphen)


def test_redact_checks_sgr_parameter_bytes_with_shared_and_error_matchers() -> None:
    assert redact("\x1b[31mabcdef0123456789AB") == "[redacted]"
    assert redact("abcdef0123456789AB\x1b[31m") == "[redacted]"
    assert redact_shared("sk-abcdefghijklmn\x1b[31m") == "[redacted]"
    assert redact("\x1b[31mordinary\x1b[0m") == "ordinary"


@pytest.mark.parametrize(
    "raw",
    [
        "\x1bopaque-credential",
        "opaque\x1b-credential",
        "opaque-credential\x1b",
    ],
    ids=("start", "middle", "end"),
)
def test_lone_escape_redacts_the_complete_token_in_shared_sinks(raw: str) -> None:
    assert redact_shared(raw) == "[redacted]"
    assert redact(raw) == "[redacted]"


@pytest.mark.parametrize(
    "terminal_tail",
    (
        pytest.param("\x1b", id="lone-escape"),
        pytest.param("\x1b[31", id="partial-csi-7-bit"),
        pytest.param("\x9b31", id="partial-csi-8-bit"),
    ),
)
def test_secret_key_with_terminal_control_at_end_fails_closed(
    terminal_tail: str,
) -> None:
    raw = f"password{terminal_tail}"

    assert redact_shared(raw) == "[redacted]"
    assert redact(raw) == "[redacted]"


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        ("\x1b]", "\x07"),
        ("\x9d", "\x1b\\"),
        ("\x1bP", "\x9c"),
        ("\x90", "\x1b\\"),
        ("\x1b^", "\x9c"),
        ("\x9e", "\x1b\\"),
        ("\x1b_", "\x9c"),
        ("\x9f", "\x1b\\"),
        ("\x1bX", "\x9c"),
        ("\x98", "\x1b\\"),
    ),
    ids=(
        "osc-7-bit",
        "osc-8-bit",
        "dcs-7-bit",
        "dcs-8-bit",
        "pm-7-bit",
        "pm-8-bit",
        "apc-7-bit",
        "apc-8-bit",
        "sos-7-bit",
        "sos-8-bit",
    ),
)
def test_redact_replaces_complete_string_control_before_tokenization(
    introducer: str, terminator: str
) -> None:
    raw = f"before {introducer}title opaque-credential-123{terminator} after"
    assert redact_shared(raw) == "before [redacted] after"


@pytest.mark.parametrize(
    "introducer",
    (
        "\x1b]",
        "\x9d",
        "\x1bP",
        "\x90",
        "\x1b^",
        "\x9e",
        "\x1b_",
        "\x9f",
        "\x1bX",
        "\x98",
    ),
)
def test_redact_replaces_unterminated_string_control_before_tokenization(
    introducer: str,
) -> None:
    raw = f"before {introducer}title opaque-credential-123"
    assert redact_shared(raw) == "before [redacted]"


@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        ("\x1b]", "\x07"),
        ("\x9d", "\x1b\\"),
        ("\x1bP", "\x9c"),
        ("\x90", "\x1b\\"),
        ("\x1b^", "\x9c"),
        ("\x9e", "\x1b\\"),
        ("\x1b_", "\x9c"),
        ("\x9f", "\x1b\\"),
        ("\x1bX", "\x9c"),
        ("\x98", "\x1b\\"),
    ),
)
def test_redact_replaces_complete_string_control_inside_secret_assignment(
    introducer: str, terminator: str
) -> None:
    raw = f"token=x{introducer}0;opaque credential{terminator} visible=keep"
    cleaned = redact_shared(raw)

    assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    "introducer",
    (
        "\x1b]",
        "\x9d",
        "\x1bP",
        "\x90",
        "\x1b^",
        "\x9e",
        "\x1b_",
        "\x9f",
        "\x1bX",
        "\x98",
    ),
)
def test_redact_replaces_unterminated_string_control_inside_secret_assignment(
    introducer: str,
) -> None:
    cleaned = redact_shared(f"token=x{introducer}0;opaque credential")

    assert cleaned == "[redacted]"


def test_issue_coordinator_documents_terminal_redaction_order() -> None:
    documented = (
        Path(__file__).parents[1] / "docs" / "issue-coordinator.md"
    ).read_text(encoding="utf-8")
    ai_accounts = (
        Path(__file__).parents[1] / "docs" / "ai-accounts.md"
    ).read_text(encoding="utf-8")
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    skill = (
        Path(__file__).parents[1]
        / "src"
        / "agent_cli"
        / "skills"
        / "error-fix"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    for text in (documented, ai_accounts, readme, skill):
        normalized = " ".join(text.split())
        assert "control-stripped view" in normalized
        assert "payload-kept view" in normalized
        assert "key-join view" in normalized
        assert (
            "A whitespace token exactly `<config_dir>/` followed only by "
            "`[A-Za-z0-9._-]+` is the one structural exception"
            in normalized
        )
        assert "Any other byte in that token" in normalized
        assert "keyword elsewhere on the line, still triggers it" in normalized
        assert "logical line" in normalized
        assert "removable C0" in normalized
        assert (
            "Nested string controls keep their payload text at every nesting depth"
            in normalized
        )
        assert (
            "Retained string-control payloads are checked in both a "
            "payload-clean view and a payload-key-join view"
            in normalized
        )
        assert "payload-clean view omits complete nested CSI spans" in normalized
        assert (
            "payload-key-join view contributes exactly the key characters"
            in normalized
        )
        assert "assignment scanner may join into a key" in normalized
        assert (
            "second view is built lazily only for payloads that contain nested controls"
            in normalized
        )
        assert "keyword in either view activates the line rule" in normalized
        assert (
            "An unterminated nested string control fails closed and treats the "
            "whole logical line as matched"
            in normalized
        )
        assert "linear, nesting-aware" in normalized
        assert "every string-control span boundary" in normalized
        assert "introducer" in normalized
        assert "swallows every following physical line" in normalized

    assert (
        "then classifies secret assignments on the full\nraw text. During that scan,\n"
        "every complete or unterminated OSC, DCS, PM, APC, or SOS span is one opaque"
        in documented
    )
    assert (
        "Only after assignment classification does the pipeline replace those\n"
        "spans with `[redacted]` on the classified text, before whitespace tokenization"
        in documented
    )
    assert (
        "Before granting that exemption, the secret\n"
        "matcher checks the SGR parameter bytes joined to the visible text"
        in documented
    )
    assert "a match\nredacts the entire token" in documented
    assert (
        "The assignment scan treats complete and unterminated string-control\n"
        "spans as opaque; only after that classification are they replaced with\n"
        "`[redacted]` before tokenization"
        in documented
    )
    assert (
        "split as `pass` + span + `word` still joins to `password`" in documented
    )
    assert (
        "a terminal control ends an unquoted key\n"
        "only when an operator follows the skipped whitespace and controls before any\n"
        "further key character"
        in documented
    )
    assert (
        "only when no visible\n"
        "operator follows may a skipped, validly terminated string control supply its\n"
        "payload operator"
        in documented
    )
    assert (
        "BEL terminates\n"
        "only OSC, so BEL inside DCS, PM, APC, or SOS remains payload until ST."
        in documented
    )
    assert (
        "Before `task-done` is evaluated for a merged A38 head, `await_merge` evaluates\n"
        "the merged-head second-review outcome and starts or checks its timeout on every\n"
        "tick, including when the newest review is passing."
        in documented
    )
    assert "review record's timestamp" in documented
    assert "the tick\ntime is never substituted" in documented
    assert "stored report body" in documented
    assert "live repository value" in documented
    assert "same\ncontrol lexer" in documented
    for text in (documented, ai_accounts, readme, skill):
        normalized = " ".join(text.split())
        assert (
            "Nested string controls keep their payload text at every nesting depth"
            in normalized
        )
        assert (
            "Retained string-control payloads are checked in both a "
            "payload-clean view and a payload-key-join view"
            in normalized
        )
    assert (
        "even while cumulative CI is pending or temporarily unavailable"
        in " ".join(documented.split())
    )
    assert (
        "a raw whitespace token that contained a string-control\nspan"
        in documented
    )
    assert (
        "Credential assignments are\nthen classified on the full raw text"
        in ai_accounts
    )
    assert (
        "a terminal control ends an unquoted key only when, after\n"
        "skipping whitespace and controls"
        in ai_accounts
    )
    assert (
        "A raw whitespace token that\n"
        "contained a string-control span and contains a secret keyword is "
        "redacted whole"
        in ai_accounts
    )
    assert "BEL terminates OSC only" in ai_accounts
    assert (
        "the full capture first has its configured profile path replaced, then "
        "passes the controlled-logical-line rule, and then enters the precise "
        "raw secret scanner"
        in " ".join(readme.split())
    )
    assert (
        "That scanner classifies assignments while complete or unterminated OSC, "
        "DCS, PM, APC, and SOS spans remain opaque, replaces those spans with "
        "`[redacted]`, and applies raw-token redaction before terminal sanitization."
        in " ".join(readme.split())
    )
    assert (
        "A control ends an unquoted key only when an operator occurs\n"
        "before another key character"
        in readme
    )
    assert (
        "Otherwise the control stays inside the sticky key, so suffixes such as `_file`\n"
        "and `suffix` remain part of the secret key."
        in readme
    )
    assert (
        "payload operator remains opaque and later key fragments still join"
        in readme
    )
    assert "BEL ends\nonly OSC; the other string controls require ST." in readme


def test_error_fix_skill_documents_unbounded_secret_key_scanner() -> None:
    documented = (
        Path(__file__).parents[1]
        / "src"
        / "agent_cli"
        / "skills"
        / "error-fix"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert (
        "Secret-key assignments use a complete, unbounded scanner" in documented
    )
    assert "shared controlled-line structural rule" in documented
    assert (
        "each controlled raw logical line gets a compiled case-insensitive candidate check"
        in " ".join(documented.split())
    )
    assert "Only candidate lines build the three" in documented
    assert "a key-join view" in documented
    assert "mapped whitespace token through logical-line end" in documented
    assert "Standalone removable C0" in documented
    assert "payload newlines are\n   not boundaries" in documented
    assert "swallows every\n   following physical line" in documented
    assert "classifies secret assignments on the raw line" in documented
    assert (
        "replaces those spans only after assignment classification" in documented
    )
    assert (
        "a terminal control ends an\n"
        "   unquoted key only if a visible `=` or `:`, or otherwise an operator inside a\n"
        "   valid complete string control, occurs before another key character"
        in documented
    )
    assert (
        "While the key\n"
        "   is incomplete, the payload remains\n"
        "   opaque so later key fragments still join. BEL terminates OSC only"
        in documented
    )
    assert (
        "Fixed matching\n"
        "   windows apply only to URL schemes, terminal escape parameters"
        in documented
    )
    assert "Secret-key name affixes" not in documented


@pytest.mark.parametrize(
    "split_secret",
    [
        "ey\x1b[31JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc_def-ghi",
        "use\x1b[31Jr@example.com",
        "0123456789\x1b[31abcdefABCD",
        "AKI\x9b31AIOSFODNN7EXAMPLE",
    ],
    ids=("jwt", "email", "hex", "aws-key"),
)
def test_scan_redacts_error_tokens_split_by_csi_final_byte(
    tmp_path: Path, split_secret: str
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"line": f"ERROR RuntimeError key {split_secret}"}], None)

    created, enriched = scan_errors(store, fetch)

    assert enriched == []
    assert len(created) == 1
    excerpt = store.row("activity", created[0])["payload"]["excerpt"]
    assert "\x1b" not in excerpt
    assert "[redacted]" in excerpt


@pytest.mark.parametrize(
    "boundary_secret",
    (
        "\x1b[31A123456789012345678F",
        "A123456789012345678\x1b[31F",
    ),
    ids=("start-boundary", "end-boundary"),
)
def test_scan_redacts_20_hex_secret_with_boundary_csi(
    tmp_path: Path, boundary_secret: str
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"line": f"ERROR RuntimeError key {boundary_secret}"}], None)

    created, enriched = scan_errors(store, fetch)

    assert enriched == []
    assert len(created) == 1
    excerpt = store.row("activity", created[0])["payload"]["excerpt"]
    assert "123456789012345678" not in excerpt
    assert "[redacted]" in excerpt


def test_scan_inserts_once_then_enriches(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError bearer SECRETTOKENVALUE0123456789 boom"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], "2026-08-23T16:00:00Z")

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["type"] == "error.seen"
    assert row["session_id"] == "runner-1"
    payload = row["payload"]
    assert payload["service"] == "api"
    assert payload["environment"] == "prod"
    assert payload["class"] == "TimeoutError"
    assert payload["count"] == 1
    assert payload["repo"] == "org/app"
    assert payload["evidence"] is None
    assert "SECRETTOKENVALUE0123456789" not in payload["excerpt"]
    assert "fingerprint" in payload
    wakes = store.pending_wakes()
    assert any(w["activity_id"] == created[0] for w in wakes)

    created2, enriched2 = scan_errors(store, fetch)
    assert created2 == []
    assert enriched2 == created
    again = store.row("activity", created[0])
    assert again is not None
    assert again["payload"]["count"] == 2
    assert len([w for w in store.pending_wakes() if w["activity_id"] == created[0]]) == 1
    assert "line_fingerprint" not in payload


def test_scan_dedups_across_different_otel_ids(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    first = "TimeoutError trace_id=" + "a" * 32 + " span_id=" + "b" * 16 + " boom"
    second = "TimeoutError trace_id=" + "c" * 32 + " span_id=" + "d" * 16 + " boom"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {"ts": "2026-08-23T16:00:00Z", "line": first},
                {"ts": "2026-08-23T16:00:01Z", "line": second},
            ],
            None,
        )

    created, enriched = scan_errors(store, fetch)
    assert len(created) == 1
    assert enriched == created
    seen = [row for row in store.rows("activity") if row["type"] == "error.seen"]
    assert len(seen) == 1
    assert seen[0]["payload"]["count"] == 2
    assert len({row["payload"]["fingerprint"] for row in seen}) == 1


def test_line_fingerprint_is_sha256_of_server_container_line() -> None:
    import hashlib

    line = "boom once"
    expect = hashlib.sha256(b"prd\napi\nboom once").hexdigest()
    assert line_fingerprint(server="prd", container="api", line=line) == expect


def test_scan_stores_line_fingerprint_when_labels_present(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError bearer SECRETTOKENVALUE0123456789 boom"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": line,
                    "server": "prd",
                    "container": "api",
                }
            ],
            None,
        )

    created, _ = scan_errors(store, fetch)
    payload = store.row("activity", created[0])["payload"]
    raw_fp = line_fingerprint(server="prd", container="api", line=line)
    redacted_fp = line_fingerprint(server="prd", container="api", line=redact(line))
    assert payload["line_fingerprint"] == raw_fp
    assert raw_fp != redacted_fp
    assert "SECRETTOKENVALUE0123456789" not in payload["excerpt"]

    def fetch_no_labels(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [{"ts": "2026-08-23T16:00:01Z", "line": line}],
            None,
        )

    _, enriched = scan_errors(store, fetch_no_labels)
    assert enriched == created
    again = store.row("activity", created[0])["payload"]
    assert "line_fingerprint" not in again

    def fetch_labels(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:02Z",
                    "line": line,
                    "server": "prd",
                    "container": "api",
                }
            ],
            None,
        )

    _, enriched2 = scan_errors(store, fetch_labels)
    assert enriched2 == created
    labeled = store.row("activity", created[0])["payload"]
    assert labeled["line_fingerprint"] == raw_fp


def test_scan_omits_line_fingerprint_when_server_is_not_utf8(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": "TimeoutError boom",
                    "server": "\ud800",
                    "container": "api",
                }
            ],
            None,
        )

    created, _ = scan_errors(store, fetch)
    payload = store.row("activity", created[0])["payload"]
    assert "line_fingerprint" not in payload
    assert payload["class"] == "TimeoutError"


def test_scan_requires_error_fix_skill(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write("session", "insert", "runner-1", {"id": "runner-1", "kind": "runner", "status": "active", "skills": ["spine"]})
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": "boom"}], None)

    with pytest.raises(StoreError, match="does not have skill error-fix"):
        scan_errors(store, fetch)


def test_fingerprint_uses_full_redacted_line(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    prefix = "x" * 500

    def fetch_timeout(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": prefix + " TimeoutError boom"}], None)

    created, _ = scan_errors(store, fetch_timeout)
    assert len(created) == 1
    assert store.row("activity", created[0])["payload"]["class"] == "TimeoutError"

    def fetch_value(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:01Z", "line": prefix + " ValueError boom"}], None)

    created2, enriched2 = scan_errors(store, fetch_value)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != created[0]
    assert store.row("activity", created2[0])["payload"]["class"] == "ValueError"


def test_skip_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError once"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    fp = store.row("activity", eid)["payload"]["fingerprint"]
    store.write(
        "activity",
        "insert",
        "skip-1",
        {
            "id": "skip-1",
            "session_id": "runner-1",
            "type": "error.skip",
            "payload": {"error_id": eid, "fingerprint": fp, "reason": "noisy"},
            "execution_status": "done",
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_open_seen_is_preferred_over_newer_closed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = 0

    def now() -> str:
        nonlocal tick
        tick += 1
        return f"2026-08-23T16:00:{tick:02d}Z"

    monkeypatch.setattr("agent_cli.store.utcnow", now)
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError once"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    a_id = created[0]
    fp = store.row("activity", a_id)["payload"]["fingerprint"]
    store.write(
        "activity",
        "insert",
        "skip-1",
        {
            "id": "skip-1",
            "session_id": "runner-1",
            "type": "error.skip",
            "payload": {"error_id": a_id, "fingerprint": fp, "reason": "noisy"},
            "execution_status": "done",
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    b_id = created2[0]
    stored = store.row("activity", a_id)
    assert stored is not None
    payload = {key: value for key, value in stored.items() if not key.startswith("_")}
    store.write("activity", "update", a_id, payload)
    assert store.rows("activity")[0]["id"] == a_id

    created3, enriched3 = scan_errors(store, fetch)
    assert created3 == []
    assert enriched3 == [b_id]
    b_row = store.row("activity", b_id)
    assert b_row is not None
    assert b_row["payload"]["count"] == 2
    seen_ids = [
        row["id"] for row in store.rows("activity") if row["type"] == "error.seen"
    ]
    assert set(seen_ids) == {a_id, b_id}


def test_done_task_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError twice"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    store.write(
        "task",
        "insert",
        "task-1",
        {
            "id": "task-1",
            "session_id": "runner-1",
            "workflow": "implement",
            "state": "done",
            "payload": {"error_id": eid, "repo": "org/app"},
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_failed_task_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError failed"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    store.write(
        "task",
        "insert",
        "task-fail",
        {
            "id": "task-fail",
            "session_id": "runner-1",
            "workflow": "implement",
            "state": "failed",
            "payload": {"error_id": eid, "repo": "org/app"},
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_default_fetch_uses_forward_cursor_and_netrc(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeAuth:
        pass

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service": "api"},
                            "values": [
                                ["2000000000000000002", "TimeoutError late"],
                                ["2000000000000000001", "TimeoutError early"],
                            ],
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["trust_env"] = kwargs.get("trust_env")

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            captured["url"] = url
            captured["params"] = params
            captured["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", FakeAuth)
    monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    lines, cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        "2000000000000000000",
    )
    assert captured["trust_env"] is True
    assert isinstance(captured["auth"], FakeAuth)
    assert captured["params"]["direction"] == "forward"
    assert captured["params"]["start"] == "2000000000000000001"
    assert captured["url"] == "https://logs.example/query_range"
    assert [row["line"] for row in lines] == ["TimeoutError early", "TimeoutError late"]
    assert cursor == "2000000000000000002"
    assert "server" not in lines[0]
    assert "container" not in lines[0]


def test_default_fetch_maps_server_and_container_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAuth:
        pass

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {
                                "service": "api",
                                "server": "prd",
                                "container_name": "api",
                            },
                            "values": [["2000000000000000001", "TimeoutError one"]],
                        },
                        {
                            "stream": {"service": "api", "server": "prd"},
                            "values": [["2000000000000000002", "TimeoutError two"]],
                        },
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", FakeAuth)
    monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    lines, _cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        None,
    )
    both = next(row for row in lines if row["line"] == "TimeoutError one")
    partial = next(row for row in lines if row["line"] == "TimeoutError two")
    assert both["server"] == "prd"
    assert both["container"] == "api"
    assert partial["server"] == "prd"
    assert "container" not in partial


def test_default_fetch_skips_lines_without_nanosecond_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuth:
        pass

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service": "api"},
                            "values": [
                                ["not-a-ns", "TimeoutError skip"],
                                ["2000000000000000003", "TimeoutError keep"],
                            ],
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", FakeAuth)
    monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    lines, cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        "2000000000000000000",
    )
    assert [row["line"] for row in lines] == ["TimeoutError keep"]
    assert cursor == "2000000000000000003"


def test_default_fetch_prefers_env_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class BoomAuth:
        def __init__(self) -> None:
            raise AssertionError("NetRCAuth must not run when env auth is set")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"result": []}}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            captured["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", BoomAuth)
    monkeypatch.setenv("AGENT_ERROR_FIX_USER", "alice")
    monkeypatch.setenv("AGENT_ERROR_FIX_PASSWORD", "secret")
    default_fetch({"url": "https://logs.example/query_range", "query": "{job=\"api\"}"}, None)
    assert captured["auth"] == ("alice", "secret")


@pytest.mark.parametrize(
    ("user", "password"),
    [("alice", None), (None, "secret")],
)
def test_default_fetch_rejects_incomplete_env_auth(
    monkeypatch: pytest.MonkeyPatch, user: str | None, password: str | None
) -> None:
    class BoomAuth:
        def __init__(self) -> None:
            raise AssertionError("NetRCAuth must not run when env auth is incomplete")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"result": []}}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("Client must not run when env auth is incomplete")

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", BoomAuth)
    if user is None:
        monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    else:
        monkeypatch.setenv("AGENT_ERROR_FIX_USER", user)
    if password is None:
        monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AGENT_ERROR_FIX_PASSWORD", password)
    with pytest.raises(StoreError, match="incomplete"):
        default_fetch(
            {"url": "https://logs.example/query_range", "query": "{job=\"api\"}"},
            None,
        )


def test_scan_rejects_unreadable_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    cursor_path(tmp_path).mkdir()

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        raise AssertionError("fetch must not run when the cursor is unreadable")

    with pytest.raises(StoreError, match="error-fix.cursor"):
        scan_errors(store, fetch)


def test_is_incident_line_keeps_timeout_error_and_logger_level() -> None:
    assert is_incident_line("TimeoutError boom") is True
    assert is_incident_line("2026-08-23 12:00:00 ERROR [Service] failed") is True


def test_is_incident_line_drops_access_logs_and_plain_error_word() -> None:
    assert is_incident_line("POST /v1/log/clientError 204 - - 12ms") is False
    assert is_incident_line("TRACE /v1/log/clientError 204 -") is False
    assert is_incident_line("CONNECT /v1/log/clientError 204 -") is False
    assert is_incident_line("\x1b[32mGET /health 200\x1b[0m ok") is False
    assert is_incident_line("LOG request failed with an error during retry") is False


def test_scan_errors_drops_non_incident_lines(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": "POST /v1/log/clientError 204 - - 12ms",
                },
                {
                    "ts": "2026-08-23T16:00:01Z",
                    "line": "\x1b[32mGET /health 200\x1b[0m ok",
                },
                {
                    "ts": "2026-08-23T16:00:02Z",
                    "line": "LOG request failed with an error during retry",
                },
                {
                    "ts": "2026-08-23T16:00:05Z",
                    "line": "TRACE /v1/log/clientError 204 -",
                },
                {
                    "ts": "2026-08-23T16:00:06Z",
                    "line": "CONNECT /v1/log/clientError 204 -",
                },
                {"ts": "2026-08-23T16:00:03Z", "line": "TimeoutError boom"},
                {
                    "ts": "2026-08-23T16:00:04Z",
                    "line": "2026-08-23 12:00:00 ERROR [Service] failed",
                },
            ],
            "cursor-1",
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 2
    classes = {
        store.row("activity", aid)["payload"]["class"] for aid in created
    }
    assert classes == {"TimeoutError", "error"}
    assert cursor_path(tmp_path).read_text(encoding="utf-8").strip() == "cursor-1"


def test_line_must_not_match_drops_otherwise_valid_error(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    config_path(tmp_path).write_text(
        json.dumps(
            {
                "session_id": "runner-1",
                "service": "api",
                "environment": "prod",
                "repo": "org/app",
                "line_must_not_match": "noisy",
            }
        ),
        encoding="utf-8",
    )

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {"ts": "2026-08-23T16:00:00Z", "line": "ERROR noisy Service boom"},
                {"ts": "2026-08-23T16:00:01Z", "line": "ERROR keep Service boom"},
            ],
            None,
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    assert "keep" in store.row("activity", created[0])["payload"]["excerpt"]


def test_load_config_rejects_invalid_line_filters(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text(
        json.dumps({"session_id": "s", "line_must_match": ""}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="line_must_match is invalid"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "line_must_not_match": "("}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="line_must_not_match is invalid"):
        load_config(path)


def test_line_must_match_keeps_and_drops(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    config_path(tmp_path).write_text(
        json.dumps(
            {
                "session_id": "runner-1",
                "service": "api",
                "environment": "prod",
                "repo": "org/app",
                "line_must_match": "keep",
            }
        ),
        encoding="utf-8",
    )

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {"ts": "2026-08-23T16:00:00Z", "line": "ERROR drop Service boom"},
                {"ts": "2026-08-23T16:00:01Z", "line": "ERROR keep Service boom"},
            ],
            None,
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    assert "keep" in store.row("activity", created[0])["payload"]["excerpt"]
