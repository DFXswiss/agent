"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


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

# Bounded regex consumers still accept a CSI/ESC prefix whose final byte is the
# next literal character. Exact secret and path redaction does not use this
# expression. It searches a fixed set of views with KMP. Tabs and newlines stay
# in every view, so they separate a secret instead of being skipped.
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


# Second bytes of 7-bit CSI/OSC/DCS/PM/APC/SOS introducers, plus the ST
# backslash. View "markers" drops these. View "finals" keeps every one of them
# except the ST backslash, so a secret can start on a string-control final.
# View "every final" drops the introducer bytes and keeps the backslash.
_INTRODUCER_OR_ST_SECOND = frozenset("[]P^_X\\")


@dataclass(frozen=True)
class _TextView:
    """One projection of raw text and the raw span of each view character.

    ``raw_starts`` is ``None`` when the view is the raw text and character ``i``
    occupies raw span ``[i, i + 1)``.
    """

    text: str
    raw_starts: tuple[int, ...] | None
    raw_ends: tuple[int, ...] | None


def _is_removed_control(char: str) -> bool:
    """Return whether the sanitizer drops this C0/C1 byte, keeping tab and LF."""
    code = ord(char)
    return code <= 0x08 or 0x0B <= code <= 0x1F or 0x7F <= code <= 0x9F


def _complete_csi_end(text: str, index: int) -> int | None:
    """Return the end of a complete CSI, or ``None`` when the sequence is open."""
    limit = len(text)
    if text.startswith("\x1b[", index):
        cursor = index + 2
    elif index < limit and text[index] == "\x9b":
        cursor = index + 1
    else:
        return None
    while cursor < limit and "0" <= text[cursor] <= "?":
        cursor += 1
    while cursor < limit and " " <= text[cursor] <= "/":
        cursor += 1
    if cursor < limit and "@" <= text[cursor] <= "~":
        return cursor + 1
    return None


def _complete_esc_end(text: str, index: int) -> int | None:
    """Return the end of a complete ESC sequence that is not CSI or a string."""
    limit = len(text)
    if index >= limit or text[index] != "\x1b":
        return None
    if index + 1 < limit and text[index + 1] == "[":
        return None
    if _is_terminal_string_control(text, index):
        return None
    cursor = index + 1
    while cursor < limit and " " <= text[cursor] <= "/":
        cursor += 1
    if cursor < limit and "0" <= text[cursor] <= "~":
        return cursor + 1
    return None


def _scan_csi(text: str, index: int) -> tuple[int, int | None] | None:
    """Return ``(next_index, final_index)`` for a 7-bit or 8-bit CSI.

    ``final_index`` is ``None`` when the sequence ends before its final byte.
    ``next_index`` is past that final, or past the truncated tail.
    """
    limit = len(text)
    if text.startswith("\x1b[", index):
        cursor = index + 2
    elif index < limit and text[index] == "\x9b":
        cursor = index + 1
    else:
        return None
    while cursor < limit and "0" <= text[cursor] <= "?":
        cursor += 1
    while cursor < limit and " " <= text[cursor] <= "/":
        cursor += 1
    if cursor < limit and "@" <= text[cursor] <= "~":
        return cursor + 1, cursor
    return cursor, None


def _scan_plain_esc(text: str, index: int) -> tuple[int, int | None] | None:
    """Return ``(next_index, final_index)`` for an ESC sequence.

    CSI and string introducers are not recognized here; callers skip those
    first. A truncated sequence has ``final_index is None``. The final byte
    includes the backslash of ``ESC \\``.
    """
    limit = len(text)
    if index >= limit or text[index] != "\x1b":
        return None
    cursor = index + 1
    if cursor >= limit:
        return index + 1, None
    if " " <= text[cursor] <= "/":
        while cursor < limit and " " <= text[cursor] <= "/":
            cursor += 1
        if cursor < limit and "0" <= text[cursor] <= "~":
            return cursor + 1, cursor
        return cursor, None
    if "0" <= text[cursor] <= "~":
        return cursor + 1, cursor
    return index + 1, None


def _keep_view_char(
    text: str,
    index: int,
    chars: list[str],
    starts: list[int],
    ends: list[int],
) -> None:
    chars.append(text[index])
    starts.append(index)
    ends.append(index + 1)


def _finish_view(
    chars: list[str], starts: list[int], ends: list[int]
) -> _TextView:
    return _TextView("".join(chars), tuple(starts), tuple(ends))


def _view_without_terminal_controls(text: str) -> _TextView:
    """Drop complete string controls, complete ESC/CSI sequences, and C0/C1."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if _is_terminal_string_control(text, index):
            end, _terminated = _terminal_string_control_end(text, index)
            index = max(end, index + 1)
            continue
        csi_end = _complete_csi_end(text, index)
        if csi_end is not None:
            index = csi_end
            continue
        esc_end = _complete_esc_end(text, index)
        if esc_end is not None:
            index = esc_end
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_without_control_markers(text: str) -> _TextView:
    """Drop introducers, terminators, and C0/C1.

    Escape parameters, intermediates, and finals stay, except the second byte
    of a 7-bit CSI/OSC/DCS/PM/APC/SOS introducer and the ST backslash. String
    payloads stay, so a secret can run from one payload into the next.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if (
            text[index] == "\x1b"
            and index + 1 < limit
            and text[index + 1] in _INTRODUCER_OR_ST_SECOND
        ):
            index += 2
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_without_sequences_keep_payloads(text: str) -> _TextView:
    """Drop complete ESC/CSI sequences. Keep string payloads without wrappers."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if _is_terminal_string_control(text, index):
            index += 2 if text[index] == "\x1b" else 1
            continue
        csi_end = _complete_csi_end(text, index)
        if csi_end is not None:
            index = csi_end
            continue
        esc_end = _complete_esc_end(text, index)
        if esc_end is not None:
            index = esc_end
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_keeping_escape_finals(text: str) -> _TextView:
    r"""Drop C0/C1 and the ST backslash. Keep ESC finals, including introducers.

    ``ESC P`` then ``ESC \`` then ``rivate-key`` therefore reads ``Private-key``.
    The marker view removes that ``P`` and cannot see the same secret.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if (
            text[index] == "\x1b"
            and index + 1 < limit
            and text[index + 1] == "\\"
        ):
            index += 2
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _keep_escape_final(
    text: str,
    final_index: int | None,
    chars: list[str],
    starts: list[int],
    ends: list[int],
) -> None:
    if final_index is not None:
        _keep_view_char(text, final_index, chars, starts, ends)


def _view_keeping_every_escape_final(text: str) -> _TextView:
    r"""Drop introducers, CSI and ESC noise, and C0/C1. Keep every escape final.

    String payloads stay. The backslash of ``ESC \`` stays, so ``a ESC \ b``
    reads ``a\b``. CSI parameters and intermediates and ESC intermediates do
    not sit in front of that final. The finals view still drops ``ESC \`` so
    ``ESC P`` followed by ``ESC \`` can spell ``Private-key``.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if _is_terminal_string_control(text, index):
            index += 2 if text[index] == "\x1b" else 1
            continue
        csi = _scan_csi(text, index)
        if csi is not None:
            next_index, final_index = csi
            _keep_escape_final(text, final_index, chars, starts, ends)
            index = next_index
            continue
        if text[index] == "\x1b":
            scanned = _scan_plain_esc(text, index)
            if scanned is None:
                raise AssertionError("ESC scan missed an ESC byte")
            next_index, final_index = scanned
            _keep_escape_final(text, final_index, chars, starts, ends)
            index = next_index
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_keeping_control_introducers(text: str) -> _TextView:
    """Keep one introducer byte of each control and drop the rest of it.

    Seven-bit CSI and string controls keep their second byte. Eight-bit
    CSI/OSC/DCS/PM/APC/SOS keep that C1 byte. Parameters, intermediates,
    finals, payloads, and terminators are dropped, so the introducer can be
    the next secret character without the bytes that follow it.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if _is_terminal_string_control(text, index):
            introducer = index if text[index] != "\x1b" else index + 1
            _keep_view_char(text, introducer, chars, starts, ends)
            end, _terminated = _terminal_string_control_end(text, index)
            index = max(end, index + 1)
            continue
        csi = _scan_csi(text, index)
        if csi is not None:
            next_index, _final_index = csi
            introducer = index + 1 if text[index] == "\x1b" else index
            _keep_view_char(text, introducer, chars, starts, ends)
            index = next_index
            continue
        if text[index] == "\x1b":
            scanned = _scan_plain_esc(text, index)
            if scanned is None:
                raise AssertionError("ESC scan missed an ESC byte")
            index = scanned[0]
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_keeping_control_finals(text: str) -> _TextView:
    r"""Keep each control's final byte and drop string payloads.

    CSI and other ESC sequences contribute only their final, including the
    backslash of ``ESC \``. A terminated string contributes its terminator
    byte (that backslash, BEL, or the 8-bit ST byte) and nothing from its
    payload. An unterminated string contributes nothing.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    limit = len(text)
    index = 0
    while index < limit:
        if _is_terminal_string_control(text, index):
            end, terminated = _terminal_string_control_end(text, index)
            if terminated and end > index:
                _keep_view_char(text, end - 1, chars, starts, ends)
            index = max(end, index + 1)
            continue
        csi = _scan_csi(text, index)
        if csi is not None:
            next_index, final_index = csi
            _keep_escape_final(text, final_index, chars, starts, ends)
            index = next_index
            continue
        if text[index] == "\x1b":
            scanned = _scan_plain_esc(text, index)
            if scanned is None:
                raise AssertionError("ESC scan missed an ESC byte")
            next_index, final_index = scanned
            _keep_escape_final(text, final_index, chars, starts, ends)
            index = next_index
            continue
        if _is_removed_control(text[index]):
            index += 1
            continue
        _keep_view_char(text, index, chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _compose_base_view(base: _TextView, local: _TextView) -> _TextView:
    """Map a view of ``base.text`` through ``base`` onto the original raw text.

    ``local`` indexes ``base.text``. When ``base`` is the raw text, those
    indexes are already raw spans.
    """
    if base.raw_starts is None and base.raw_ends is None:
        return local
    if base.raw_starts is None or base.raw_ends is None:
        raise AssertionError("view raw map is incomplete")
    if local.raw_starts is None or local.raw_ends is None:
        raise AssertionError("view raw map is incomplete")
    if (
        len(base.raw_starts) != len(base.text)
        or len(base.raw_ends) != len(base.text)
        or len(local.raw_starts) != len(local.text)
        or len(local.raw_ends) != len(local.text)
    ):
        raise AssertionError("view raw map length does not match the view")
    starts: list[int] = []
    ends: list[int] = []
    base_limit = len(base.text)
    for index in range(len(local.text)):
        base_start = local.raw_starts[index]
        base_end = local.raw_ends[index]
        if base_start < 0 or base_end <= base_start or base_end > base_limit:
            raise AssertionError("view span is outside the base text")
        raw_start = base.raw_starts[base_start]
        raw_end = base.raw_ends[base_end - 1]
        if raw_end <= raw_start:
            raise AssertionError("composed span did not advance")
        starts.append(raw_start)
        ends.append(raw_end)
    return _TextView(local.text, tuple(starts), tuple(ends))


def _known_secret_views(base: _TextView) -> tuple[_TextView, ...]:
    """Return the fixed view set of ``base``.

    Plain text is searched once, as ``base`` itself. With a terminal control
    the set is ``base``, controls removed, introducers and terminators and
    C0/C1 removed, complete ESC/CSI removed with string payloads kept, ESC
    finals kept except ``ESC \\``, every escape final kept (including that
    backslash) with introducers and CSI/ESC noise removed, introducer bytes
    only, and control finals or string terminators only. The builders index
    ``base.text``; their maps are composed through ``base`` onto raw spans.
    """
    text = base.text
    if _TERMINAL_CONTROL_RE.search(text) is None:
        return (base,)
    return (
        base,
        _compose_base_view(base, _view_without_terminal_controls(text)),
        _compose_base_view(base, _view_without_control_markers(text)),
        _compose_base_view(base, _view_without_sequences_keep_payloads(text)),
        _compose_base_view(base, _view_keeping_escape_finals(text)),
        _compose_base_view(base, _view_keeping_every_escape_final(text)),
        _compose_base_view(base, _view_keeping_control_introducers(text)),
        _compose_base_view(base, _view_keeping_control_finals(text)),
    )


def _kmp_borders(pattern: str) -> list[int]:
    """Longest proper prefix of each pattern prefix that is also a suffix."""
    length = len(pattern)
    borders = [0] * length
    matched = 0
    index = 1
    while index < length:
        if pattern[index] == pattern[matched]:
            matched += 1
            borders[index] = matched
            index += 1
            continue
        if matched:
            matched = borders[matched - 1]
            continue
        index += 1
    return borders


def _literal_spans(text: str, pattern: str) -> list[tuple[int, int]]:
    """Every overlapping occurrence of ``pattern``, in O(text + pattern)."""
    if not pattern:
        return []
    borders = _kmp_borders(pattern)
    spans: list[tuple[int, int]] = []
    matched = 0
    index = 0
    limit = len(text)
    size = len(pattern)
    while index < limit:
        if text[index] == pattern[matched]:
            index += 1
            matched += 1
            if matched == size:
                spans.append((index - size, index))
                matched = borders[matched - 1]
            continue
        if matched:
            matched = borders[matched - 1]
            continue
        index += 1
    return spans


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _view_char_raw_span(view: _TextView, index: int) -> tuple[int, int]:
    """Raw half-open span of one character in ``view``."""
    if view.raw_starts is None and view.raw_ends is None:
        return index, index + 1
    if view.raw_starts is None or view.raw_ends is None:
        raise AssertionError("view raw map is incomplete")
    return view.raw_starts[index], view.raw_ends[index]


def _percent_run_end(text: str, index: int) -> int:
    """End of the consecutive ``%HH`` run starting at ``index``, or ``index``."""
    limit = len(text)
    cursor = index
    while (
        cursor + 2 < limit
        and text[cursor] == "%"
        and text[cursor + 1] in _HEX_DIGITS
        and text[cursor + 2] in _HEX_DIGITS
    ):
        cursor += 3
    return cursor


def _utf8_sequence_length(lead: int) -> int:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def _utf8_continuation(value: int, low: int, high: int) -> bool:
    return low <= value <= high


def _utf8_sequence_valid(data: bytes, index: int, size: int) -> bool:
    """Return whether ``data[index:index + size]`` is one strict UTF-8 scalar."""
    lead = data[index]
    if size == 2:
        return _utf8_continuation(data[index + 1], 0x80, 0xBF)
    if size == 3:
        second = data[index + 1]
        if lead == 0xE0:
            second_ok = _utf8_continuation(second, 0xA0, 0xBF)
        elif lead == 0xED:
            second_ok = _utf8_continuation(second, 0x80, 0x9F)
        else:
            second_ok = _utf8_continuation(second, 0x80, 0xBF)
        return second_ok and _utf8_continuation(data[index + 2], 0x80, 0xBF)
    if size == 4:
        second = data[index + 1]
        if lead == 0xF0:
            second_ok = _utf8_continuation(second, 0x90, 0xBF)
        elif lead == 0xF4:
            second_ok = _utf8_continuation(second, 0x80, 0x8F)
        else:
            second_ok = _utf8_continuation(second, 0x80, 0xBF)
        return (
            second_ok
            and _utf8_continuation(data[index + 2], 0x80, 0xBF)
            and _utf8_continuation(data[index + 3], 0x80, 0xBF)
        )
    return lead < 0x80


def _utf8_codepoint(data: bytes, index: int, size: int) -> str:
    if size == 2:
        value = ((data[index] & 0x1F) << 6) | (data[index + 1] & 0x3F)
    elif size == 3:
        value = (
            ((data[index] & 0x0F) << 12)
            | ((data[index + 1] & 0x3F) << 6)
            | (data[index + 2] & 0x3F)
        )
    elif size == 4:
        value = (
            ((data[index] & 0x07) << 18)
            | ((data[index + 1] & 0x3F) << 12)
            | ((data[index + 2] & 0x3F) << 6)
            | (data[index + 3] & 0x3F)
        )
    else:
        value = data[index]
    return chr(value)


def _utf8_units(data: bytes) -> list[tuple[str, int]]:
    """One strict UTF-8 scalar, or one rejected byte, in a single left-to-right pass.

    A truncated or ill-formed sequence is not merged into a character it does
    not encode. The rejected lead is emitted alone and the scan resumes at the
    next byte, so a later valid sequence stays intact.
    """
    units: list[tuple[str, int]] = []
    index = 0
    limit = len(data)
    while index < limit:
        size = _utf8_sequence_length(data[index])
        if (
            size > 1
            and index + size <= limit
            and _utf8_sequence_valid(data, index, size)
        ):
            units.append((_utf8_codepoint(data, index, size), size))
            index += size
            continue
        units.append((chr(data[index]), 1))
        index += 1
    return units


def _append_view_span(
    view: _TextView,
    view_index: int,
    view_length: int,
    char: str,
    chars: list[str],
    starts: list[int],
    ends: list[int],
) -> None:
    raw_start = _view_char_raw_span(view, view_index)[0]
    raw_end = _view_char_raw_span(view, view_index + view_length - 1)[1]
    if raw_end <= raw_start:
        raise AssertionError("percent-decoded span did not advance")
    chars.append(char)
    starts.append(raw_start)
    ends.append(raw_end)


def _append_percent_run(
    view: _TextView,
    run_start: int,
    run_end: int,
    chars: list[str],
    starts: list[int],
    ends: list[int],
) -> None:
    text = view.text
    encoded = bytearray()
    cursor = run_start
    while cursor < run_end:
        encoded.append(int(text[cursor + 1:cursor + 3], 16))
        cursor += 3
    byte_index = 0
    for char, size in _utf8_units(bytes(encoded)):
        _append_view_span(
            view,
            run_start + 3 * byte_index,
            3 * size,
            char,
            chars,
            starts,
            ends,
        )
        byte_index += size


def _percent_decoded_view(view: _TextView) -> _TextView:
    """Decode consecutive ``%HH`` runs as UTF-8 and copy every other character.

    A valid sequence, including a multi-byte scalar, becomes one character
    whose raw span covers every triplet of that sequence. An ASCII ``%HH``
    stays one character spanning its three source characters. An invalid or
    truncated sequence becomes one character per byte and is not merged into
    a character those bytes do not encode. A literal ``%`` that begins a
    valid triplet is consumed by the run.
    """
    text = view.text
    limit = len(text)
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < limit:
        run_end = _percent_run_end(text, index)
        if run_end != index:
            _append_percent_run(view, index, run_end, chars, starts, ends)
            index = run_end
            continue
        _append_view_span(view, index, 1, text[index], chars, starts, ends)
        index += 1
    return _finish_view(chars, starts, ends)


def _view_spans_to_raw(
    view: _TextView, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    if not spans:
        return []
    if view.raw_starts is None or view.raw_ends is None:
        return list(spans)
    raw: list[tuple[int, int]] = []
    for start, end in spans:
        raw.append((view.raw_starts[start], view.raw_ends[end - 1]))
    return raw


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union half-open spans, including spans that only touch."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            if end > current_end:
                current_end = end
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _replace_spans(
    text: str, spans: list[tuple[int, int]], replacement: str
) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor or end < start:
            raise AssertionError("redaction spans are not ordered")
        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _collect_literal_raw_spans(
    raw_spans: list[tuple[int, int]], view: _TextView, secret: str
) -> None:
    raw_spans.extend(_view_spans_to_raw(view, _literal_spans(view.text, secret)))


def _redact_known_secret(
    text: str,
    secret: str,
    *,
    percent: bool,
    replacement: str,
) -> str:
    """Redact the union of view matches, including controls inside each span.

    Percent mode decodes the raw text once. When that decoded text contains a
    terminal control, the same control views are searched and their maps
    compose back to raw spans. Each undecoded view still contributes one
    percent-decoded copy. Decoded control views are not decoded again.
    """
    if not text or not secret:
        return text
    raw_spans: list[tuple[int, int]] = []
    base = _TextView(text, None, None)
    decoded_base = (
        _percent_decoded_view(base) if percent and "%" in text else None
    )
    for view in _known_secret_views(base):
        _collect_literal_raw_spans(raw_spans, view, secret)
        if not percent or "%" not in view.text:
            continue
        if view is base:
            if decoded_base is None:
                raise AssertionError("percent-decoded raw text is missing")
            decoded = decoded_base
        else:
            decoded = _percent_decoded_view(view)
        _collect_literal_raw_spans(raw_spans, decoded, secret)
    if decoded_base is not None:
        # Control views of the once-decoded raw text. A second decode would
        # turn ``%2541`` into ``A``.
        for view in _known_secret_views(decoded_base)[1:]:
            _collect_literal_raw_spans(raw_spans, view, secret)
    merged = _merge_spans(raw_spans)
    if not merged:
        return text
    return _replace_spans(text, merged, replacement)


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
    """Redact one exact secret across the fixed terminal-control views."""
    if not secret:
        return text
    return _redact_known_secret(
        text, secret, percent=False, replacement=replacement
    )


def redact_terminal_interleaved_urlencoded_secret(
    text: str, secret: str, *, replacement: str = "[redacted]"
) -> str:
    """Redact a secret written raw or as percent-encoded UTF-8.

    Literal characters stay case-sensitive. Each fixed view is searched with
    KMP, and so is its percent-decoded copy. The raw text is percent-decoded
    once; when that decoded text contains a terminal control, the same control
    views are searched and their maps compose back to raw spans. Those decoded
    control views are not percent-decoded again. Consecutive ``%HH`` triplets
    (either hex case) are one UTF-8 byte stream: a valid sequence becomes one
    character mapped to the raw span of all of its triplets, and an invalid or
    truncated sequence falls back to one character per byte. An ASCII ``%HH``
    stays one character spanning its three-character raw span. The union of
    the mapped spans is redacted. A secret that contains ``%`` uses the same
    linear scans.
    """
    if not secret:
        return text
    return _redact_known_secret(
        text, secret, percent=True, replacement=replacement
    )


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal escapes and controls, preserving only tab and newline."""
    without_strings = redact_terminal_string_controls(text, replacement="")
    without_escapes = _TERMINAL_ESCAPE_RE.sub("", without_strings)
    return _TERMINAL_CONTROL_RE.sub("", without_escapes)
