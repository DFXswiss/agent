"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
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


@dataclass(frozen=True)
class _TerminalControlAtom:
    """One pre-tokenized removable terminal-control atom."""

    start: int
    end: int
    final_position: int | None
    prefix_ends: tuple[int, ...]


@dataclass(frozen=True)
class _TerminalControlRun:
    """One maximal run of adjacent terminal-control atoms.

    ``boundaries_by_character`` holds every escape-prefix end and final byte
    that can land a secret character. The matcher consults the run once while
    scanning left to right; it does not restart at each boundary.
    """

    end: int
    boundaries_by_character: dict[str, tuple[int, ...]]


@dataclass(frozen=True)
class _TerminalInterleaveIndex:
    """Control-run lookup keyed by every atom boundary in the source text.

    ``run_starts`` and ``run_ends`` cover each run, including string-control
    payloads that are not themselves boundary keys. A secret may start only on
    ordinary text or on a recorded boundary, never inside a payload.
    """

    positions: dict[int, _TerminalControlRun]
    run_starts: tuple[int, ...]
    run_ends: tuple[int, ...]
    runs: tuple[_TerminalControlRun, ...]


def _terminal_control_atom(
    text: str, index: int
) -> _TerminalControlAtom | None:
    """Tokenize the terminal-control atom at ``index`` without rescanning it."""
    if index >= len(text):
        return None
    codepoint = ord(text[index])
    if not (
        codepoint <= 0x08
        or 0x0B <= codepoint <= 0x1F
        or 0x7F <= codepoint <= 0x9F
    ):
        return None
    if _is_terminal_string_control(text, index):
        end, _terminated = _terminal_string_control_end(text, index)
        end = max(index + 1, end)
        return _TerminalControlAtom(index, end, None, ())

    cursor = index
    final_position: int | None = None
    if text.startswith("\x1b[", cursor) or text[cursor] == "\x9b":
        cursor += 2 if text[cursor] == "\x1b" else 1
        prefix_ends: list[int] = [cursor]
        parameter_count = 0
        while (
            cursor < len(text)
            and parameter_count < _MAX_ESCAPE_PARAMETER_BYTES
            and "0" <= text[cursor] <= "?"
        ):
            cursor += 1
            parameter_count += 1
            prefix_ends.append(cursor)
        intermediate_count = 0
        while (
            cursor < len(text)
            and intermediate_count < _MAX_ESCAPE_PARAMETER_BYTES
            and " " <= text[cursor] <= "/"
        ):
            cursor += 1
            intermediate_count += 1
            prefix_ends.append(cursor)
        if cursor < len(text) and "@" <= text[cursor] <= "~":
            final_position = cursor
            cursor += 1
        return _TerminalControlAtom(
            index, cursor, final_position, tuple(dict.fromkeys(prefix_ends))
        )

    if text[cursor] == "\x1b":
        cursor += 1
        prefix_ends = [cursor]
        intermediate_count = 0
        while (
            cursor < len(text)
            and intermediate_count < _MAX_ESCAPE_PARAMETER_BYTES
            and " " <= text[cursor] <= "/"
        ):
            cursor += 1
            intermediate_count += 1
            prefix_ends.append(cursor)
        if cursor < len(text) and "0" <= text[cursor] <= "~":
            final_position = cursor
            cursor += 1
        return _TerminalControlAtom(
            index, cursor, final_position, tuple(dict.fromkeys(prefix_ends))
        )

    return _TerminalControlAtom(index, index + 1, None, ())


def _terminal_interleave_index(
    text: str,
    relevant_finals: frozenset[str],
) -> _TerminalInterleaveIndex:
    """Tokenize every terminal-control run once for exact-secret matching."""
    positions: dict[int, _TerminalControlRun] = {}
    run_starts: list[int] = []
    run_ends: list[int] = []
    runs: list[_TerminalControlRun] = []
    cursor = 0
    while cursor < len(text):
        control = _TERMINAL_CONTROL_RE.search(text, cursor)
        if control is None:
            break
        cursor = control.start()
        first = _terminal_control_atom(text, cursor)
        if first is None:  # pragma: no cover - guarded by the shared regex
            raise AssertionError("terminal-control index disagrees with lexer")
        atoms = [first]
        run_start = cursor
        cursor = first.end
        while cursor < len(text):
            following = _terminal_control_atom(text, cursor)
            if following is None:
                break
            atoms.append(following)
            cursor = following.end

        # A lone or partial ESC/CSI prefix may consume the first characters of
        # a secret. Every prefix end recorded by the atom lexer is therefore a
        # possible secret start and a possible landing position. Complete
        # escape finals, lone-ESC intermediates, and run ends keep their
        # existing fail-closed behaviour.
        boundaries = sorted(
            {
                boundary
                for atom in atoms
                for boundary in atom.prefix_ends
                if boundary < cursor
            }
            | {
                atom.final_position
                for atom in atoms
                if atom.final_position is not None
                and atom.final_position < cursor
            }
        )
        boundaries_by_character: dict[str, list[int]] = {}
        for boundary in boundaries:
            character = text[boundary]
            if character not in relevant_finals:
                continue
            boundaries_by_character.setdefault(character, []).append(boundary)
        run = _TerminalControlRun(
            end=cursor,
            boundaries_by_character={
                character: tuple(values)
                for character, values in boundaries_by_character.items()
            },
        )
        run_starts.append(run_start)
        run_ends.append(cursor)
        runs.append(run)
        for boundary in {*(atom.start for atom in atoms), *boundaries}:
            positions[boundary] = run
    return _TerminalInterleaveIndex(
        positions=positions,
        run_starts=tuple(run_starts),
        run_ends=tuple(run_ends),
        runs=tuple(runs),
    )


def _terminal_interleave_positions(
    text: str,
    index: int,
    expected: frozenset[str],
    terminal_index: _TerminalInterleaveIndex,
) -> tuple[int, ...]:
    """Return positions reachable after one removable terminal-control run.

    The precomputed index has no per-atom limit: consecutive C0 controls,
    complete nesting-aware string controls, CSI, and other ESC sequences are
    one run. For CSI and other ESC sequences, the final byte may instead be the
    next literal secret byte, and every reachable end of a lone or truncated
    prefix may start the secret. Those positions and the position after the
    complete control run remain reachable. String controls win before the
    generic ESC case, and no match attempt rescans their payload.
    """
    if index >= len(text):
        return (index,)
    indexed = terminal_index.positions.get(index)
    if indexed is None:
        return (index,)
    run = indexed
    first_boundary: int | None = None
    for character in expected:
        candidates = run.boundaries_by_character.get(character)
        if not candidates:
            continue
        offset = bisect_left(candidates, index)
        if offset < len(candidates):
            candidate = candidates[offset]
            if first_boundary is None or candidate < first_boundary:
                first_boundary = candidate
    landings = [index, run.end]
    if first_boundary is not None:
        landings.append(first_boundary)
    return tuple(dict.fromkeys(landings))


def _run_covering(
    index: int, terminal_index: _TerminalInterleaveIndex
) -> _TerminalControlRun | None:
    """Return the control run that contains ``index``, including its payload."""
    starts = terminal_index.run_starts
    if not starts:
        return None
    found = bisect_right(starts, index) - 1
    if found < 0 or index >= terminal_index.run_ends[found]:
        return None
    return terminal_index.runs[found]


def _next_boundary_start(
    run: _TerminalControlRun, index: int, first_symbols: frozenset[str]
) -> int:
    """Earliest secret-start boundary at or after ``index``, else the run end."""
    best = run.end
    for character in first_symbols:
        positions = run.boundaries_by_character.get(character)
        if not positions:
            continue
        offset = bisect_left(positions, index)
        if offset < len(positions) and positions[offset] < best:
            best = positions[offset]
    return best


def _secret_character_borders(
    pattern: tuple[tuple[tuple[frozenset[str], ...], ...], ...],
) -> list[int]:
    """Longest proper prefix of each secret-character prefix that is a suffix."""
    borders = [0] * len(pattern)
    length = 0
    index = 1
    while index < len(pattern):
        if pattern[index] == pattern[length]:
            length += 1
            borders[index] = length
            index += 1
            continue
        if length:
            length = borders[length - 1]
            continue
        index += 1
    return borders


def _read_secret_character(
    text: str,
    index: int,
    alternatives: tuple[tuple[frozenset[str], ...], ...],
    *,
    allow_leading_interleave: bool,
    terminal_index: _TerminalInterleaveIndex,
) -> tuple[tuple[int, int], ...]:
    """Return ``(start, end)`` spans for one secret character at ``index``.

    Each alternative is literal text or one percent-encoded byte sequence.
    A control run between symbols is one landing, not a new candidate start.
    The first symbol of the whole secret does not skip controls; the caller
    has already placed ``index`` on that byte.
    """
    found: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for alternative in alternatives:
        cursors: list[tuple[int, int | None]] = [(index, None)]
        for offset, expected in enumerate(alternative):
            following: list[tuple[int, int | None]] = []
            step_seen: set[tuple[int, int | None]] = set()
            for cursor, start in cursors:
                landings = (
                    _terminal_interleave_positions(
                        text, cursor, expected, terminal_index
                    )
                    if allow_leading_interleave or offset
                    else (cursor,)
                )
                for landing in landings:
                    if landing < len(text) and text[landing] in expected:
                        char_start = landing if start is None else start
                        step = (landing + 1, char_start)
                        if step not in step_seen:
                            step_seen.add(step)
                            following.append(step)
            cursors = following
            if not cursors:
                break
        for end, start in cursors:
            if start is None:
                continue
            span = (start, end)
            if span not in seen:
                seen.add(span)
                found.append(span)
    return tuple(found)


def _redact_terminal_interleaved_representations(
    text: str,
    representations: tuple[tuple[tuple[frozenset[str], ...], ...], ...],
    *,
    replacement: str,
) -> str:
    """Redact non-overlapping known-secret matches in one left-to-right scan.

    Secret characters form a KMP automaton. A control run, including a
    truncated ESC or CSI prefix, is a landing that does not advance the
    secret index by itself. Literal and percent-encoded alternatives share
    that scan, so repeated prefixes and runs of equal first bytes are not
    restarted per candidate. Several landings for one character stay on the
    same secret index; the active cursor follows the furthest landing and the
    redacted span runs through the furthest completing end. A string-control
    payload is not a secret start. After each match the scan resumes at its end.
    """
    if not text or not representations:
        return text
    first_symbols = frozenset(
        symbol
        for alternative in representations[0]
        for symbol in alternative[0]
    )
    relevant_finals = frozenset(
        character
        for representation in representations
        for alternative in representation
        for expected in alternative
        for character in expected
    )
    terminal_index = _terminal_interleave_index(text, relevant_finals)
    borders = _secret_character_borders(representations)
    pattern = representations
    count = len(pattern)
    pieces: list[str] = []
    copied_to = 0
    index = 0
    matched = 0
    anchor_offset = 0
    anchors: list[int] = []
    extra_ends: list[int] = []
    match_start = 0
    # False once an adopted landing no longer shares the recorded character
    # starts. Failure links would then name the wrong text offset, so a later
    # total failure resumes after this attempt's start instead of shifting.
    anchors_follow_cursor = True

    def emit(end: int) -> None:
        nonlocal copied_to, index, matched, anchor_offset, anchors_follow_cursor
        pieces.append(text[copied_to:match_start])
        pieces.append(replacement)
        copied_to = end
        index = end
        matched = 0
        anchor_offset = 0
        anchors.clear()
        extra_ends.clear()
        anchors_follow_cursor = True

    def remember(start: int) -> None:
        nonlocal anchor_offset
        if anchor_offset + matched != len(anchors):
            active = anchors[anchor_offset : anchor_offset + matched]
            anchors[:] = active
            anchor_offset = 0
        anchors.append(start)

    def fail_primary() -> None:
        nonlocal matched, anchor_offset, match_start, anchors_follow_cursor
        extra_ends.clear()
        border = borders[matched - 1]
        anchor_offset = anchor_offset + matched - border
        matched = border
        if matched == 0:
            anchor_offset = 0
            anchors.clear()
            anchors_follow_cursor = True
            return
        match_start = anchors[anchor_offset]

    def resume_fresh(at: int) -> None:
        nonlocal index, matched, anchor_offset, anchors_follow_cursor
        extra_ends.clear()
        anchors.clear()
        anchor_offset = 0
        matched = 0
        anchors_follow_cursor = True
        index = at

    while index < len(text) or matched:
        if matched == 0:
            if index >= len(text):
                break
            run = _run_covering(index, terminal_index)
            if run is not None:
                nxt = _next_boundary_start(run, index, first_symbols)
                if nxt != index:
                    index = nxt
                    continue
            if index >= len(text) or text[index] not in first_symbols:
                index += 1
                continue
            readings = _read_secret_character(
                text,
                index,
                pattern[0],
                allow_leading_interleave=False,
                terminal_index=terminal_index,
            )
            if not readings:
                index += 1
                continue
            start, end = max(readings, key=lambda span: (span[1], -span[0]))
            match_start = start
            anchors = [start]
            anchor_offset = 0
            matched = 1
            index = end
            extra_ends = [
                span_end for _start, span_end in readings if span_end != end
            ]
            anchors_follow_cursor = True
            if matched == count:
                emit(max(span_end for _start, span_end in readings))
            continue

        primary: list[tuple[int, int]] = []
        spans: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        cursors = (index, *extra_ends)
        for cursor_index, cursor in enumerate(cursors):
            for span in _read_secret_character(
                text,
                cursor,
                pattern[matched],
                allow_leading_interleave=True,
                terminal_index=terminal_index,
            ):
                if span in seen:
                    continue
                seen.add(span)
                spans.append(span)
                if cursor_index == 0:
                    primary.append(span)
        if not spans:
            if anchors_follow_cursor:
                fail_primary()
            else:
                resume_fresh(match_start + 1)
            continue
        if primary and anchors_follow_cursor:
            start, end = max(primary, key=lambda span: (span[1], -span[0]))
            remember(start)
        elif anchors_follow_cursor and matched == 1:
            start, end = max(spans, key=lambda span: (span[1], -span[0]))
            remember(start)
        else:
            end = max(spans, key=lambda span: (span[1], -span[0]))[1]
            anchors_follow_cursor = False
        index = end
        extra_ends = [
            span_end for _start, span_end in spans if span_end != end
        ]
        matched += 1
        if matched == count:
            emit(max(span_end for _start, span_end in spans))
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
