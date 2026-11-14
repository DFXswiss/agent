"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re
from collections.abc import Callable


# Longer escape/control runs are not trusted presentation. The shared raw-token
# pass sees their ESC/C1 bytes and redacts the whole token instead of asking a
# backtracking expression to classify an unbounded sequence.
_MAX_ESCAPE_PARAMETER_BYTES = 64
_MAX_REGEX_TERMINAL_INTERLEAVES = 64


# String controls are scanned on the complete raw text before whitespace
# tokenization. Both 7-bit and 8-bit forms accept either ST spelling; an
# unterminated string consumes the remaining text rather than exposing its
# payload. The scanner below is the single source of truth for nesting-aware
# span boundaries in redaction, sanitization, and coordinator structural views.
_TERMINAL_STRING_CONTROL_INTRODUCER = re.compile(
    r"\x1b(?:\]|P|\^|_|X)|[\x90\x98\x9d\x9e\x9f]"
)
_TERMINAL_ESCAPE_RE = re.compile(
    r"(?:"
    rf"(?:\x1b\[|\x9b)[0-?]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}"
    rf"[ -/]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}[@-~]"  # CSI
    rf"|\x1b[ -/]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}"
    r"[0-~]"  # other complete ESC sequences
    r")"
)
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ESC_OR_C1_SEQUENCE_RE = re.compile(
    # Complete sequences must win before the fail-closed lone-ESC fallback so
    # the strict leading/trailing SGR exemption can still inspect one wrapper.
    rf"(?:{_TERMINAL_ESCAPE_RE.pattern}|\x1b|[\x80-\x9f])"
)
_SGR_WRAPPER_RE = re.compile(
    rf"\x1b\[([0-9;]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}})m"
)

# Bounded regex consumers need the same CSI-final rule as exact known-secret
# matching: a CSI final byte may itself be the next secret character. The first
# two alternatives therefore also accept a sequence prefix whose final byte is
# supplied by the following literal secret character. Exact secret/path
# redactors below use the nesting-aware linear walker instead of this regex.
# Only controls removed by the sanitizer may interleave a secret; preserved
# tabs and newlines remain frame content instead of being consumed by a match.
TERMINAL_INTERLEAVE_PATTERN = (
    r"(?:"
    rf"(?:\x1b\[|\x9b)[0-?]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}"
    rf"[ -/]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}"
    rf"|\x1b[ -/]{{0,{_MAX_ESCAPE_PARAMETER_BYTES}}}"
    rf"|{_TERMINAL_ESCAPE_RE.pattern}"
    rf"|{_TERMINAL_CONTROL_RE.pattern}"
    rf"){{0,{_MAX_REGEX_TERMINAL_INTERLEAVES}}}"
)


def _is_terminal_string_control(text: str, index: int) -> bool:
    """Return whether ``index`` starts a 7-bit or 8-bit string control."""
    if index < 0 or index >= len(text):
        return False
    if text[index] == "\x1b":
        return index + 1 < len(text) and text[index + 1] in "]P^_X"
    return ord(text[index]) in (0x90, 0x98, 0x9D, 0x9E, 0x9F)


def _scan_terminal_string_control(
    text: str,
    index: int,
    *,
    limit: int | None = None,
    wrappers: list[tuple[int, int]] | None = None,
) -> tuple[int, bool, int]:
    """Scan one nesting-aware string control in linear time.

    The final integer is the number of still-open controls at the scan limit.
    When requested, ``wrappers`` receives every introducer and accepted
    terminator byte span so payload views can omit exactly the wrappers whose
    outer boundary this same scan computes.
    """
    stop = len(text) if limit is None else min(len(text), max(0, limit))
    if index < 0 or index >= stop:
        return min(max(index, 0), stop), False, 0

    if text[index] == "\x1b":
        if index + 1 >= stop or text[index + 1] not in "]P^_X":
            return min(index + 1, stop), False, 0
        stack = [text[index + 1] == "]"]
        cursor = index + 2
    else:
        codepoint = ord(text[index])
        if codepoint not in (0x90, 0x98, 0x9D, 0x9E, 0x9F):
            return min(index + 1, stop), False, 0
        stack = [codepoint == 0x9D]
        cursor = index + 1
    if wrappers is not None:
        wrappers.append((index, cursor))

    while cursor < stop:
        char = text[cursor]
        if char == "\x1b" and cursor + 1 < stop:
            introducer = text[cursor + 1]
            if introducer == "\\":
                if wrappers is not None:
                    wrappers.append((cursor, cursor + 2))
                stack.pop()
                cursor += 2
                if not stack:
                    return cursor, True, 0
                continue
            if introducer in "]P^_X":
                if wrappers is not None:
                    wrappers.append((cursor, cursor + 2))
                stack.append(introducer == "]")
                cursor += 2
                continue
        codepoint = ord(char)
        if codepoint in (0x90, 0x98, 0x9D, 0x9E, 0x9F):
            if wrappers is not None:
                wrappers.append((cursor, cursor + 1))
            stack.append(codepoint == 0x9D)
            cursor += 1
            continue
        if char == "\x9c" or (char == "\x07" and stack[-1]):
            if wrappers is not None:
                wrappers.append((cursor, cursor + 1))
            stack.pop()
            cursor += 1
            if not stack:
                return cursor, True, 0
            continue
        cursor += 1
    return stop, False, len(stack)


def _terminal_string_control_end(
    text: str, index: int, *, limit: int | None = None
) -> tuple[int, bool]:
    """Return the shared nesting-aware span end and termination state."""
    end, terminated, _open_depth = _scan_terminal_string_control(
        text, index, limit=limit
    )
    return end, terminated


def _terminal_string_control_parts(
    text: str, index: int, *, limit: int | None = None
) -> tuple[int, bool, tuple[tuple[int, int], ...], bool]:
    """Return one span plus its wrappers and nested-unterminated state."""
    wrappers: list[tuple[int, int]] = []
    end, terminated, open_depth = _scan_terminal_string_control(
        text,
        index,
        limit=limit,
        wrappers=wrappers,
    )
    return end, terminated, tuple(wrappers), open_depth > 1


def _terminal_interleave_positions(
    text: str,
    index: int,
    expected: frozenset[str],
) -> tuple[int, ...]:
    """Return positions reachable after one removable terminal-control run.

    The linear walk has no per-atom limit: consecutive C0 controls, complete
    nesting-aware string controls, CSI, and other ESC sequences are one run.
    For CSI and other ESC sequences, the final byte may instead be the next
    literal secret byte; both that position and the position after the complete
    escape remain reachable. String controls must win before the generic ESC
    case.
    """
    positions = [index]
    cursor = index
    first_final_position: int | None = None
    while True:
        if cursor >= len(text):
            break
        if _is_terminal_string_control(text, cursor):
            end, _terminated = _terminal_string_control_end(text, cursor)
            cursor = max(cursor + 1, end)
            continue

        if text.startswith("\x1b[", cursor) or text[cursor] == "\x9b":
            cursor += 2 if text[cursor] == "\x1b" else 1
            parameter_count = 0
            while (
                cursor < len(text)
                and parameter_count < _MAX_ESCAPE_PARAMETER_BYTES
                and "0" <= text[cursor] <= "?"
            ):
                cursor += 1
                parameter_count += 1
            intermediate_count = 0
            while (
                cursor < len(text)
                and intermediate_count < _MAX_ESCAPE_PARAMETER_BYTES
                and " " <= text[cursor] <= "/"
            ):
                cursor += 1
                intermediate_count += 1
            if cursor < len(text) and "@" <= text[cursor] <= "~":
                if text[cursor] in expected and first_final_position is None:
                    first_final_position = cursor
                cursor += 1
            continue

        if text[cursor] == "\x1b":
            cursor += 1
            intermediate_count = 0
            while (
                cursor < len(text)
                and intermediate_count < _MAX_ESCAPE_PARAMETER_BYTES
                and " " <= text[cursor] <= "/"
            ):
                cursor += 1
                intermediate_count += 1
            if cursor < len(text) and "0" <= text[cursor] <= "~":
                if text[cursor] in expected and first_final_position is None:
                    first_final_position = cursor
                cursor += 1
            continue

        if _TERMINAL_CONTROL_RE.match(text, cursor) is not None:
            cursor += 1
            continue

        break
    if first_final_position is not None:
        positions.append(first_final_position)
    positions.append(cursor)
    return tuple(dict.fromkeys(positions))


def _match_interleaved_representation(
    text: str,
    starts: set[int],
    representation: tuple[frozenset[str], ...],
    *,
    allow_leading_interleave: bool,
) -> set[int]:
    """Match one literal or percent-encoded character representation."""
    positions = starts
    for offset, expected in enumerate(representation):
        candidates: set[int] = set()
        for position in positions:
            if allow_leading_interleave or offset:
                candidates.update(
                    _terminal_interleave_positions(text, position, expected)
                )
            else:
                candidates.add(position)
        positions = {
            position + 1
            for position in candidates
            if position < len(text) and text[position] in expected
        }
        if not positions:
            break
    return positions


def _redact_terminal_interleaved_representations(
    text: str,
    representations: tuple[tuple[tuple[frozenset[str], ...], ...], ...],
    *,
    replacement: str,
) -> str:
    """Redact non-overlapping known-secret matches with a linear text walk."""
    if not text or not representations:
        return text
    first = frozenset(
        character
        for representation in representations[0]
        for character in representation[0]
    )
    pieces: list[str] = []
    copied_to = 0
    index = 0
    while index < len(text):
        if text[index] not in first:
            index += 1
            continue
        positions = {index}
        for character_index, alternatives in enumerate(representations):
            matched: set[int] = set()
            for alternative in alternatives:
                matched.update(
                    _match_interleaved_representation(
                        text,
                        positions,
                        alternative,
                        allow_leading_interleave=character_index > 0,
                    )
                )
            positions = matched
            if not positions:
                break
        if not positions:
            index += 1
            continue
        end = max(positions)
        pieces.extend((text[copied_to:index], replacement))
        copied_to = end
        index = end
    if not pieces:
        return text
    pieces.append(text[copied_to:])
    return "".join(pieces)


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
    text = redact_terminal_string_controls(text, replacement=replacement)
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


def redact_terminal_string_controls(
    text: str, *, replacement: str = "[redacted]"
) -> str:
    """Replace complete or unterminated terminal string controls on raw text."""
    source = text or ""
    pieces: list[str] = []
    cursor = 0
    while True:
        introducer = _TERMINAL_STRING_CONTROL_INTRODUCER.search(source, cursor)
        if introducer is None:
            pieces.append(source[cursor:])
            break
        start = introducer.start()
        pieces.append(source[cursor:start])
        end, _terminated = _terminal_string_control_end(source, start)
        pieces.append(replacement)
        cursor = max(start + 1, end)
    return "".join(pieces)


def redact_terminal_interleaved_secret(
    text: str, secret: str, *, replacement: str = "[redacted]"
) -> str:
    """Redact one exact secret even when terminal atoms split its characters."""
    if not secret:
        return text
    representations = tuple(
        ((frozenset((character,)),),) for character in secret
    )
    return _redact_terminal_interleaved_representations(
        text, representations, replacement=replacement
    )


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

    character_representations: list[
        tuple[tuple[frozenset[str], ...], ...]
    ] = []
    for character in secret:
        encoded_symbols: list[frozenset[str]] = []
        for byte in character.encode("utf-8"):
            pair = f"{byte:02x}"
            encoded_symbols.extend(
                (
                    frozenset(("%",)),
                    frozenset((pair[0].lower(), pair[0].upper())),
                    frozenset((pair[1].lower(), pair[1].upper())),
                )
            )
        character_representations.append(
            (
                (frozenset((character,)),),
                tuple(encoded_symbols),
            )
        )
    return _redact_terminal_interleaved_representations(
        text,
        tuple(character_representations),
        replacement=replacement,
    )


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal escapes and controls, preserving only tab and newline."""
    without_strings = redact_terminal_string_controls(text, replacement="")
    without_escapes = _TERMINAL_ESCAPE_RE.sub("", without_strings)
    return _TERMINAL_CONTROL_RE.sub("", without_escapes)
