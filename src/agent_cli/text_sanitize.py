"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re
from collections.abc import Callable


# String controls must be matched on the complete raw text before whitespace
# tokenization. Both 7-bit and 8-bit forms accept either ST spelling; an
# unterminated string consumes the remaining text rather than exposing its
# payload.
_TERMINAL_STRING_RE = re.compile(
    r"(?:"
    r"\x1b\][\s\S]*?(?:\x07|\x9c|\x1b\\|\Z)"  # OSC, 7-bit
    r"|\x9d[\s\S]*?(?:\x07|\x9c|\x1b\\|\Z)"  # OSC, 8-bit
    r"|\x1b[P^_X][\s\S]*?(?:\x9c|\x1b\\|\Z)"  # DCS/PM/APC/SOS, 7-bit
    r"|[\x90\x98\x9e\x9f][\s\S]*?(?:\x9c|\x1b\\|\Z)"  # 8-bit strings
    r")"
)
_TERMINAL_ESCAPE_RE = re.compile(
    r"(?:"
    rf"{_TERMINAL_STRING_RE.pattern}"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other complete ESC sequences
    r")"
)
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ESC_OR_C1_SEQUENCE_RE = re.compile(
    rf"(?:{_TERMINAL_ESCAPE_RE.pattern}|[\x80-\x9f])"
)
_SGR_WRAPPER_RE = re.compile(r"\x1b\[([0-9;]*)m")

# Before ordinary terminal sanitization, a known secret needs a stricter path:
# a CSI final byte may itself be the next secret character. The first two
# alternatives therefore also accept a sequence prefix whose final byte is
# supplied by the following literal secret character. Only controls removed by
# the sanitizer may interleave a secret; preserved tabs and newlines remain
# frame content instead of being consumed by a redaction match.
TERMINAL_INTERLEAVE_PATTERN = (
    r"(?:"
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*"
    r"|\x1b[ -/]*"
    rf"|{_TERMINAL_ESCAPE_RE.pattern}"
    rf"|{_TERMINAL_CONTROL_RE.pattern}"
    r")*"
)


def _is_exempt_sgr_wrapped_token(
    token: str,
    matches: list[re.Match[str]],
    secret_matcher: Callable[[str], bool] | None,
) -> bool:
    """Keep only strict leading/trailing SGR wrappers around visible text."""
    if not matches or len(matches) > 2:
        return False

    index = 0
    content_start = 0
    content_end = len(token)
    first = matches[0]
    if (
        first.start() == 0
        and _SGR_WRAPPER_RE.fullmatch(first.group()) is not None
    ):
        content_start = first.end()
        index = 1

    if index < len(matches):
        last = matches[index]
        if (
            last.end() == len(token)
            and _SGR_WRAPPER_RE.fullmatch(last.group()) is not None
        ):
            content_end = last.start()
            index += 1

    if index != len(matches) or content_start >= content_end:
        return False

    if secret_matcher is not None:
        reconstructed = token[content_start:content_end]
        if content_start:
            leading = _SGR_WRAPPER_RE.fullmatch(first.group())
            if leading is None:
                return False
            reconstructed = leading.group(1) + reconstructed
        if content_end < len(token):
            trailing = _SGR_WRAPPER_RE.fullmatch(matches[-1].group())
            if trailing is None:
                return False
            reconstructed += trailing.group(1)
        if secret_matcher(reconstructed):
            return False

    return True


def _token_has_redactable_terminal_sequence(
    token: str, secret_matcher: Callable[[str], bool] | None
) -> bool:
    matches = list(_ESC_OR_C1_SEQUENCE_RE.finditer(token))
    return bool(matches) and not _is_exempt_sgr_wrapped_token(
        token, matches, secret_matcher
    )


def redact_terminal_interleaved_tokens(
    text: str,
    *,
    replacement: str = "[redacted]",
    secret_matcher: Callable[[str], bool] | None = None,
) -> str:
    """Fail closed on a raw token containing an ESC/C1 sequence.

    Replacement happens before terminal sanitization and preserves every token
    separator verbatim. String controls are replaced on the complete raw text
    first, so whitespace in their payload cannot split them into visible
    tokens. The only presentation exemption is one strict SGR sequence at the
    leading and/or trailing token boundary around visible text. Before that
    exemption is granted, SGR parameter bytes are joined to the visible text
    and checked by ``secret_matcher`` when supplied.
    """
    if not text:
        return text
    text = _TERMINAL_STRING_RE.sub(replacement, text)
    pieces: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        boundary = text[index].isspace() and not 0x80 <= codepoint <= 0x9F
        if boundary:
            if start < index:
                token = text[start:index]
                pieces.append(
                    replacement
                    if _token_has_redactable_terminal_sequence(
                        token, secret_matcher
                    )
                    else token
                )
            pieces.append(text[index])
            index += 1
            start = index
            continue
        index += 1
    if start < len(text):
        token = text[start:]
        pieces.append(
            replacement
            if _token_has_redactable_terminal_sequence(token, secret_matcher)
            else token
        )
    return "".join(pieces)


def redact_terminal_interleaved_secret(
    text: str, secret: str, *, replacement: str = "[redacted]"
) -> str:
    """Redact one exact secret even when terminal controls split its characters."""
    if not secret:
        return text
    pattern = re.compile(
        TERMINAL_INTERLEAVE_PATTERN.join(re.escape(char) for char in secret)
    )
    return pattern.sub(replacement, text)


def redact_terminal_interleaved_urlencoded_secret(
    text: str, secret: str, *, replacement: str = "[redacted]"
) -> str:
    """Redact a secret whose characters may independently be URL-encoded.

    Literal characters remain case-sensitive. Percent-encoded UTF-8 bytes accept
    either hex case, and removable terminal controls may occur anywhere inside
    or between the literal/encoded character representations.
    """
    if not secret:
        return text

    character_patterns: list[str] = []
    for character in secret:
        encoded_bytes: list[str] = []
        for byte in character.encode("utf-8"):
            pair = f"{byte:02x}"
            encoded_bytes.extend(
                (re.escape("%"), rf"(?i:{pair[0]})", rf"(?i:{pair[1]})")
            )
        encoded = TERMINAL_INTERLEAVE_PATTERN.join(encoded_bytes)
        character_patterns.append(rf"(?:{re.escape(character)}|{encoded})")
    pattern = re.compile(TERMINAL_INTERLEAVE_PATTERN.join(character_patterns))
    return pattern.sub(replacement, text)


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal escapes and controls, preserving only tab and newline."""
    without_escapes = _TERMINAL_ESCAPE_RE.sub("", text or "")
    return _TERMINAL_CONTROL_RE.sub("", without_escapes)
