"""Author review-completion declaration checked by dfx pr guard.

The guard does not run the review lanes and does not execute pull-request
code. A passing declaration is a consistent author statement plus the
guard's own thread check, not cryptographic proof that a review ran.
The statement must include the review record. The guard rejects a missing,
placeholder, or contradictory record. It does not prove which model ran.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

REVIEW_BEGIN = "<!-- A38-REVIEW:v1 -->"
REVIEW_END = "<!-- /A38-REVIEW:v1 -->"
LIFECYCLE_REASON = "Review completion missing"
# One full run is conformity plus logic of the same suffix. The other run is optional.
RUNS = (
    ("conformity-a", "logic-a"),
    ("conformity-b", "logic-b"),
)
LANE_IDS = tuple(lane_id for run in RUNS for lane_id in run)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NA_EVIDENCE = re.compile(
    r"^markdown-only rebase, previously approved at ([0-9a-f]{40})$"
)
_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_PASS_KEYS = frozenset({"id", "result", "status"})
_PASS_KEYS_V2 = _PASS_KEYS | {"provider", "model", "model_number", "prompt"}
_NA_KEYS = frozenset({"id", "result", "evidence"})
_OBJECT_KEYS = frozenset({"schema", "head", "passes", "defects", "lanes"})
_OBJECT_KEYS_V2 = _OBJECT_KEYS | {"set_aside"}
_SCHEMA_V2 = "a38-review/v2"


class ReviewError(ValueError):
    """The comment is not a readable A38 review block."""


def looks_like_review(body: str | None) -> bool:
    return isinstance(body, str) and "A38-REVIEW:v1" in body


def _reject_nonfinite(name: str) -> None:
    raise ReviewError(f"JSON contains non-finite number: {name}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ReviewError(f"JSON contains duplicate key: {key}")
        out[key] = value
    return out


def loads_strict(text: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_pairs_no_duplicates,
        )
    except ReviewError:
        raise
    except json.JSONDecodeError as exc:
        raise ReviewError(f"JSON is invalid: {exc.msg}") from exc


def parse_review_block(body: str) -> dict[str, Any]:
    if not isinstance(body, str) or not body:
        raise ReviewError("review comment body missing")
    if body.count(REVIEW_BEGIN) != 1 or body.count(REVIEW_END) != 1:
        raise ReviewError("review markers missing or duplicated")
    begin = body.find(REVIEW_BEGIN)
    end = body.find(REVIEW_END)
    if begin > end:
        raise ReviewError("review markers out of order")
    inside = body[begin + len(REVIEW_BEGIN) : end]
    fences = _FENCE.findall(inside)
    if len(fences) != 1:
        raise ReviewError("review block must contain one json fence")
    payload = loads_strict(fences[0].strip())
    if not isinstance(payload, dict):
        raise ReviewError("review block must be a JSON object")
    return payload


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _add(reasons: list[str], text: str) -> None:
    if text not in reasons:
        reasons.append(text)


def validate_visible(body: str, passes: int) -> list[str]:
    if not isinstance(body, str) or not _is_int(passes) or passes < 1:
        return ["review visible text missing"]
    begin = body.find(REVIEW_BEGIN)
    end = body.find(REVIEW_END)
    visible = body
    if 0 <= begin < end:
        visible = body[:begin] + body[end + len(REVIEW_END) :]
    lines = visible.splitlines()
    en_sentence = f"Ready after {passes} review passes."
    de_sentence = f"Bereit nach {passes} Review-Durchläufen."

    def next_content(start: int) -> int:
        """Next non-blank line. Blank lines may separate the required lines; other text may not."""
        for index in range(start, len(lines)):
            if lines[index].strip():
                return index
        return -1

    en_at = next_content(0)
    ready_at = next_content(en_at + 1) if en_at >= 0 and lines[en_at] == "EN:" else -1
    summary_at = next_content(ready_at + 1) if ready_at >= 0 and lines[ready_at] == en_sentence else -1
    if summary_at >= 0 and lines[summary_at] == "DE:":
        summary_at = -1
    de_at = next_content(summary_at + 1) if summary_at >= 0 else -1
    if de_at >= 0 and lines[de_at] != "DE:":
        de_at = -1
    de_sentence_at = next_content(de_at + 1) if de_at >= 0 else -1
    if de_sentence_at >= 0 and lines[de_sentence_at] != de_sentence:
        de_sentence_at = -1
    de_summary = next_content(de_sentence_at + 1) if de_sentence_at >= 0 else -1
    if min(en_at, ready_at, summary_at, de_at, de_sentence_at, de_summary) < 0:
        return ["review visible text missing"]
    return []


def _pass_lane_complete(lane: Mapping[str, Any]) -> bool:
    provider = lane.get("provider")
    model = lane.get("model")
    number = lane.get("model_number")
    prompt = lane.get("prompt")
    if not all(isinstance(value, str) for value in (provider, model, number, prompt)):
        return False
    if not _named_value(provider) or not _named_value(model) or not _named_value(number):
        return False
    return _prompt_ok(prompt)


def _validate_lanes(
    lanes: list[Any], *, head: str, markdown_only: bool, complete: bool
) -> list[str]:
    reasons: list[str] = []
    seen: list[str] = []
    kinds: set[str] = set()
    na_shas: list[str] = []
    present: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, Mapping):
            _add(reasons, "review lane is not an object")
            continue
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or lane_id not in LANE_IDS or lane_id in seen:
            _add(reasons, "review lane id is invalid")
            continue
        seen.append(lane_id)
        present.add(lane_id)
        result = lane.get("result")
        if result == "pass":
            kinds.add("pass")
            pass_keys = _PASS_KEYS_V2 if complete else _PASS_KEYS
            if (
                set(lane) != pass_keys
                or lane.get("status") != "complete"
                or (complete and not _pass_lane_complete(lane))
            ):
                _add(reasons, "review pass lane is malformed")
        elif result == "n_a":
            kinds.add("n_a")
            if set(lane) != _NA_KEYS:
                _add(reasons, "review n_a lane is malformed")
            else:
                evidence = lane.get("evidence")
                match = _NA_EVIDENCE.fullmatch(evidence) if isinstance(evidence, str) else None
                if match is None:
                    _add(reasons, "review n_a evidence is malformed")
                else:
                    sha = match.group(1)
                    if sha == head:
                        _add(reasons, "review n_a SHA equals the current head")
                    na_shas.append(sha)
        else:
            _add(reasons, "review lane result is invalid")
    complete = 0
    for pair in RUNS:
        count = sum(1 for lane_id in pair if lane_id in present)
        if count == 2:
            complete += 1
        elif count == 1:
            _add(reasons, "review run is incomplete")
    if complete < 1:
        _add(reasons, "review lanes must include one full run")
    if "pass" in kinds and "n_a" in kinds:
        _add(reasons, "review lanes mix pass and n_a")
    if "n_a" in kinds:
        if not markdown_only:
            _add(reasons, "review n_a requires a markdown-only change set")
        if len(set(na_shas)) > 1:
            _add(reasons, "review n_a SHAs differ")
    return reasons


def validate_declaration(
    payload: Mapping[str, Any], *, head: str, markdown_only: bool
) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["review declaration is not an object"]
    reasons: list[str] = []
    complete = payload.get("schema") == _SCHEMA_V2
    allowed = _OBJECT_KEYS_V2 if complete else _OBJECT_KEYS
    if set(payload) - allowed:
        _add(reasons, "review declaration has unknown keys")
    if payload.get("schema") not in {"a38-review/v1", _SCHEMA_V2}:
        _add(reasons, "review schema is not a38-review/v1 or a38-review/v2")
    if complete:
        aside = payload.get("set_aside")
        if (
            not isinstance(aside, str)
            or not aside.strip()
            or _is_placeholder(aside, allow_none=True)
        ):
            _add(reasons, "review set aside missing")
    declared = payload.get("head")
    if not isinstance(declared, str) or _SHA.fullmatch(declared) is None:
        _add(reasons, "review head is not a 40-hex SHA")
    elif declared != head:
        _add(reasons, "review head does not match the pull request")
    passes = payload.get("passes")
    if not _is_int(passes) or passes < 1:
        _add(reasons, "review passes must be an integer >= 1")
    if payload.get("defects") != 0 or not _is_int(payload.get("defects")):
        _add(reasons, "review defects must be 0")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        _add(reasons, "review lanes missing")
    else:
        reasons.extend(
            _validate_lanes(lanes, head=head, markdown_only=markdown_only, complete=complete)
        )
    return reasons


def _utc(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


_PLACEHOLDERS = frozenset({
    "not recorded",
    "unknown",
    "n/a",
    "na",
    "none",
    "-",
    "n.a.",
    "tbd",
    "todo",
    "example",
    "example model",
    "example-1",
})
_LANE_HEADER = re.compile(r"^[*_\s]*Lane `([a-z0-9-]+)`:")


def _is_placeholder(value: str, *, allow_none: bool = False) -> bool:
    banned = _PLACEHOLDERS - {"none"} if allow_none else _PLACEHOLDERS
    text = value.strip().casefold()
    return text in banned or text.rstrip(".") in banned


def _named_value(value: str) -> bool:
    text = value.strip()
    return 1 <= len(text) <= 200 and not _is_placeholder(text)


def _runs_line(lane_ids: Sequence[str]) -> str | None:
    present = set(lane_ids)
    complete = [pair for pair in RUNS if all(lane_id in present for lane_id in pair)]
    if len(complete) == 1:
        left, right = complete[0]
        return f"Runs: `{left}` with `{right}`. The second run may be omitted."
    if len(complete) == 2:
        return "Runs: `conformity-a` with `logic-a`, and `conformity-b` with `logic-b`."
    return None


def render_review_record(payload: Mapping[str, Any]) -> str:
    """Record lines the guard requires before the review machine block.

    A pass lane must already name its provider, model, model number, and
    prompt. A missing fact is an error. This does not fill in a sample.
    """
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        raise ReviewError("review lanes missing")
    lane_ids = [
        lane.get("id")
        for lane in lanes
        if isinstance(lane, Mapping) and isinstance(lane.get("id"), str)
    ]
    lines = [
        f"Head: `{payload.get('head')}`",
        _runs_line([lane_id for lane_id in lane_ids if isinstance(lane_id, str)]) or "",
        "",
    ]
    for lane in lanes:
        if not isinstance(lane, Mapping):
            raise ReviewError("review lane is not an object")
        lines.append(f"Lane `{lane.get('id')}`:")
        if lane.get("result") == "n_a":
            lines.append("Result: n_a")
        elif lane.get("result") == "pass":
            provider = lane.get("provider")
            model = lane.get("model")
            number = lane.get("model_number")
            prompt = lane.get("prompt")
            if (
                not all(isinstance(value, str) for value in (provider, model, number, prompt))
                or not _named_value(provider)
                or not _named_value(model)
                or not _named_value(number)
                or not _prompt_ok(prompt)
            ):
                raise ReviewError("review pass lane is malformed")
            assert isinstance(prompt, str)
            lines.extend([
                f"Provider: {provider}",
                f"Model: {model}",
                f"Model number: {number}",
                "Result: pass",
                "Prompt:",
                "```text",
                prompt.strip(),
                "```",
            ])
        else:
            raise ReviewError("review pass lane is malformed")
        lines.append("")
    lines.append(f"Final result: `passes` {payload.get('passes')}, `defects` 0.")
    aside = payload.get("set_aside")
    lines.append(
        f"Set aside: {aside}" if isinstance(aside, str) and aside.strip() else "Set aside: none."
    )
    lines.append("")
    return "\n".join(lines)


def _plain(value: str) -> str:
    """Drop emphasis marks. Keep the underscore inside tokens such as n_a."""
    text = re.sub(r"[*`]+", "", value)
    text = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", "", text)
    return text.strip()


def _starts_with_label(line: str, label: str) -> bool:
    prefix = re.sub(r"^[*_\s]+", "", line)
    return prefix.casefold().startswith(label.casefold() + ":")


def _record_items(text: str) -> tuple[list[tuple[str, str]], bool]:
    """Prose lines and fenced bodies. The fence tag is not significant.

    An unclosed fence, or a review marker inside a fence, is an error.
    """
    items: list[tuple[str, str]] = []
    error = False
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("```"):
            index += 1
            body: list[str] = []
            closed = False
            while index < len(lines):
                if lines[index].strip() == "```":
                    closed = True
                    index += 1
                    break
                if REVIEW_BEGIN in lines[index] or REVIEW_END in lines[index]:
                    error = True
                body.append(lines[index])
                index += 1
            if not closed:
                error = True
            items.append(("fence", "\n".join(body)))
            continue
        if stripped:
            items.append(("prose", stripped))
        index += 1
    return items, error


def _fences_between(
    items: Sequence[tuple[str, str]],
    start: int,
    end: int,
) -> list[str]:
    seen = -1
    fences: list[str] = []
    for kind, text in items:
        if kind == "prose":
            seen += 1
            if seen >= end:
                break
            continue
        if seen >= start:
            fences.append(text)
    return fences


def _prompt_ok(text: str) -> bool:
    if REVIEW_BEGIN in text or REVIEW_END in text:
        return False
    stripped = text.strip()
    if not stripped or _is_placeholder(stripped):
        return False
    words = [word for word in stripped.split() if word]
    return len(words) >= 3 and len(stripped) >= 12


def _lane_label(line: str) -> tuple[str, str] | None:
    prefix = re.sub(r"^[*_\s]+", "", line)
    folded = prefix.casefold()
    for label in ("Provider", "Model number", "Model", "Result", "Prompt"):
        token = label.casefold() + ":"
        if folded.startswith(token):
            return label, prefix.split(":", 1)[1].strip()
    return None


def _same_named(values: Sequence[str]) -> bool:
    cleaned = [_plain(value) for value in values if _plain(value)]
    if not cleaned or any(not _named_value(value) for value in cleaned):
        return False
    return len({value.casefold() for value in cleaned}) == 1


def _expand_negations(folded: str) -> str:
    """Punctuation stays so a later negated clause does not cross the sentence."""
    folded = re.sub(r"\bcannot\b", "can not", folded)
    folded = re.sub(r"\bcan['\u2019]t\b", "can not", folded)
    folded = re.sub(r"\bwon['\u2019]t\b", "will not", folded)
    folded = re.sub(
        r"\b(is|are|was|were|does|did|do)n['\u2019]?t\b",
        r"\1 not",
        folded,
    )
    return re.sub(r"\b([a-z]+)n['\u2019]t\b", r"\1 not", folded)


def _result_is_pass(value: str) -> bool:
    folded = _plain(value).casefold().strip()
    folded = _expand_negations(folded)
    if re.match(r"(?:not|no|never|nicht|kein|keine|fail|n_a)\b", folded):
        return False
    passes = list(re.finditer(r"\bpass(?:ed)?\b", folded))
    if not passes:
        return False
    if any(_span_negated(folded, match.start(), match.end()) for match in passes):
        return False
    if any(
        not _span_negated(folded, match.start(), match.end())
        for match in re.finditer(r"\bfail\b", folded)
    ):
        return False
    if any(
        not _span_negated(folded, match.start(), match.end())
        for match in re.finditer(r"\bn_a\b", folded)
    ):
        return False
    return True


def _is_na_result(value: str) -> bool:
    folded = _plain(value).casefold().strip()
    folded = _expand_negations(folded)
    if re.match(r"(?:not|no|never|nicht|kein|keine|pass|fail)\b", folded):
        return False
    nas = list(re.finditer(r"\bn_a\b", folded))
    if not nas:
        return False
    if any(_span_negated(folded, match.start(), match.end()) for match in nas):
        return False
    if any(
        not _span_negated(folded, match.start(), match.end())
        for match in re.finditer(r"\bpass(?:ed)?\b", folded)
    ):
        return False
    if any(
        not _span_negated(folded, match.start(), match.end())
        for match in re.finditer(r"\bfail\b", folded)
    ):
        return False
    return True


def _pass_lane_reasons(section: list[str], fences: Sequence[str]) -> list[str]:
    reasons: list[str] = []
    found: dict[str, list[str]] = {
        "Provider": [],
        "Model": [],
        "Model number": [],
        "Result": [],
        "Prompt": [],
    }
    for line in section:
        labeled = _lane_label(line)
        if labeled is not None:
            found[labeled[0]].append(labeled[1])
    for label, reason in (
        ("Provider", "review provider missing"),
        ("Model", "review model missing"),
        ("Model number", "review model number missing"),
    ):
        if not _same_named(found[label]):
            _add(reasons, reason)
    if not found["Result"] or any(not _result_is_pass(value) for value in found["Result"]):
        _add(reasons, "review result missing")
    if not found["Prompt"] or not any(
        _prompt_ok(text) for text in fences if text.strip()
    ):
        _add(reasons, "review prompt missing")
    return reasons


def _na_lane_reasons(section: list[str], fences: Sequence[str]) -> list[str]:
    for line in section:
        labeled = _lane_label(line)
        if labeled is not None and labeled[0] != "Result":
            return ["review n_a lane must not name a model"]
    if any(text.strip() for text in fences):
        return ["review n_a lane must not name a model"]
    results = [
        labeled[1]
        for line in section
        if (labeled := _lane_label(line)) is not None and labeled[0] == "Result"
    ]
    if not results or any(not _is_na_result(value) for value in results):
        return ["review result missing"]
    return []


def _set_aside_reasons(lines: list[str]) -> list[str]:
    indexes = [
        index for index, line in enumerate(lines) if _starts_with_label(line, "Set aside")
    ]
    if not indexes:
        return ["review set aside missing"]
    for index in indexes:
        value = _plain(lines[index].split(":", 1)[1])
        if value == "":
            bullets: list[str] = []
            for line in lines[index + 1 :]:
                if _starts_with_label(line, "Set aside"):
                    break
                if line.startswith("- "):
                    bullets.append(_plain(line[2:]))
            if not bullets or any(not item or _is_placeholder(item) for item in bullets):
                return ["review set aside missing"]
            continue
        if _is_placeholder(value, allow_none=True):
            return ["review set aside missing"]
    return []


_NEGATION_WORD = re.compile(r"\b(?:not|no|never|nicht|kein|keine)\b")


def _span_negated(folded: str, start: int, end: int) -> bool:
    """True when this claim's own clause contains a negation word.

    The clause runs from the previous comma or sentence break to the next one.
    """
    prefix = re.split(r"[,.!;]", folded[:start])[-1]
    suffix = re.split(r"[,.!;]", folded[end:], maxsplit=1)[0]
    return any(
        _NEGATION_WORD.search(part) is not None
        for part in (prefix, folded[start:end], suffix)
    )


def _one_sha(line: str, expected: object) -> bool:
    if not isinstance(expected, str):
        return False
    folded = _expand_negations(line.casefold())
    expected_sha = expected.casefold()
    found = re.findall(r"[0-9a-f]{40}", folded)
    if found != [expected_sha]:
        return False
    start = folded.find(expected_sha)
    return not _span_negated(folded, start, start + len(expected_sha))


def _expected_pairs(lane_ids: Sequence[str]) -> set[tuple[str, str]] | None:
    present = set(lane_ids)
    complete = [pair for pair in RUNS if all(lane_id in present for lane_id in pair)]
    if not complete:
        return None
    covered = {lane_id for pair in complete for lane_id in pair}
    if any(lane_id not in covered for lane_id in lane_ids):
        return None
    return set(complete)


def _claimed_pairs(line: str) -> set[tuple[str, str]] | None:
    folded = _expand_negations(line.casefold())
    body = folded.split(":", 1)[1]
    pieces = re.split(r"(\band\b|\bund\b|;|,)", body, flags=re.IGNORECASE)
    claimed: set[tuple[str, str]] = set()
    negated: set[tuple[str, str]] = set()
    allowed = set(RUNS)
    offset = len(folded) - len(body)
    for index, part in enumerate(pieces):
        if index % 2 == 1:
            offset += len(part)
            continue
        ids = list(
            re.finditer(
                r"\b(?:conformity-a|logic-a|conformity-b|logic-b)\b",
                part,
            )
        )
        if not ids:
            offset += len(part)
            continue
        if len(ids) != 2 or len({match.group() for match in ids}) != 2:
            return None
        pair = (ids[0].group(), ids[1].group())
        if pair not in allowed:
            pair = (ids[1].group(), ids[0].group())
        if pair not in allowed:
            return None
        start = offset + ids[0].start()
        end = offset + ids[1].end()
        if _span_negated(folded, start, end):
            negated.add(pair)
        else:
            claimed.add(pair)
        offset += len(part)
    if not claimed or claimed & negated:
        return None
    return claimed


# A count stays inside its own clause. A comma is the field boundary.
_COUNT_GAP = r"[^\w,.;!]{0,8}"


def _same_count(left: re.Match[str], right: re.Match[str]) -> bool:
    return left.start(1) == right.start(1) and left.end(1) == right.end(1)


def _final_result_ok(line: str, passes: object) -> bool:
    if not _is_int(passes):
        return False
    if not _starts_with_label(line, "Final result"):
        return False
    folded = _expand_negations(line.casefold())
    word_passes = list(re.finditer(rf"\bpasses{_COUNT_GAP}(\d+)\b", folded))
    word_defects = list(re.finditer(rf"\bdefects?{_COUNT_GAP}(\d+)\b", folded))
    # "defects 0, passes 1" must not read the 0 as a pass count.
    number_passes = [
        match
        for match in re.finditer(rf"\b(\d+){_COUNT_GAP}passes\b", folded)
        if not any(_same_count(match, owner) for owner in word_defects)
    ]
    number_defects = [
        match
        for match in re.finditer(rf"\b(\d+){_COUNT_GAP}defects?\b", folded)
        if not any(_same_count(match, owner) for owner in word_passes)
    ]
    pass_matches = [*word_passes, *number_passes]
    defect_matches = [
        match
        for match in (*word_defects, *number_defects)
        if not any(
            match.start() < other.end() and other.start() < match.end()
            for other in pass_matches
        )
    ]
    good_passes: list[int] = []
    negated_passes: list[int] = []
    for match in pass_matches:
        number = int(match.group(1))
        if _span_negated(folded, match.start(), match.end()):
            negated_passes.append(number)
        else:
            good_passes.append(number)
    good_defects: list[int] = []
    negated_defects: list[int] = []
    for match in defect_matches:
        number = int(match.group(1))
        if _span_negated(folded, match.start(), match.end()):
            negated_defects.append(number)
        else:
            good_defects.append(number)
    if not good_passes or any(number != passes for number in good_passes):
        return False
    if not good_defects or any(number != 0 for number in good_defects):
        return False
    if any(number == passes for number in negated_passes):
        return False
    if any(number == 0 for number in negated_defects):
        return False
    return True


def _looks_like_record(lines: Sequence[str]) -> bool:
    for line in lines:
        if _starts_with_label(line, "Head") or _starts_with_label(line, "Runs"):
            return True
        if _starts_with_label(line, "Final result") or _starts_with_label(line, "Set aside"):
            return True
        if _LANE_HEADER.match(line):
            return True
    return False


def validate_review_record(body: str, payload: Mapping[str, Any]) -> list[str]:
    """Require the human record to agree with the declaration.

    Field order, the fence tag, and extra explanation do not matter.
    A missing fact, a placeholder, or a contradiction still invalidates it.
    Lines inside a fence are the prompt, not record fields.
    """
    if not isinstance(body, str) or not isinstance(payload, Mapping):
        return ["review record missing"]
    begin = body.find(REVIEW_BEGIN)
    record = body[:begin] if begin >= 0 else body
    items, fence_error = _record_items(record)
    lines = [text for kind, text in items if kind == "prose"]
    if not _looks_like_record(lines):
        return ["review record missing"]
    reasons: list[str] = []
    if fence_error:
        _add(reasons, "review prompt missing")
    head = payload.get("head")
    heads = [line for line in lines if _starts_with_label(line, "Head")]
    if not heads or any(not _one_sha(line, head) for line in heads):
        _add(reasons, "review head line does not match")
    lanes = payload.get("lanes")
    lane_ids: list[str] = []
    lane_results: list[str] = []
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, Mapping) and isinstance(lane.get("id"), str):
                lane_ids.append(lane["id"])
                result = lane.get("result")
                lane_results.append(result if isinstance(result, str) else "")
            else:
                lane_ids.append("")
                lane_results.append("")
    run_lines = [line for line in lines if _starts_with_label(line, "Runs")]
    expected_pairs = _expected_pairs(lane_ids)
    if (
        expected_pairs is None
        or not run_lines
        or any(
            (claimed := _claimed_pairs(line)) is None or claimed != expected_pairs
            for line in run_lines
        )
    ):
        _add(reasons, "review runs line does not match")
    headers = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := _LANE_HEADER.match(line)) is not None
    ]
    if [lane_id for _, lane_id in headers] != lane_ids:
        _add(reasons, "review lane record missing")
    passes = payload.get("passes")
    finals = [line for line in lines if _starts_with_label(line, "Final result")]
    if not finals or any(not _final_result_ok(line, passes) for line in finals):
        _add(reasons, "review final result does not match")
    final_at = next(
        (index for index, line in enumerate(lines) if _starts_with_label(line, "Final result")),
        None,
    )
    if len(headers) == len(lane_ids):
        for position, (start, _lane_id) in enumerate(headers):
            end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
            if final_at is not None:
                end = min(end, final_at)
            section = lines[start + 1 : end]
            fences = _fences_between(items, start, end)
            if lane_results[position] == "n_a":
                for reason in _na_lane_reasons(section, fences):
                    _add(reasons, reason)
            else:
                for reason in _pass_lane_reasons(section, fences):
                    _add(reasons, reason)
    for reason in _set_aside_reasons(lines):
        _add(reasons, reason)
    if payload.get("schema") == _SCHEMA_V2:
        for reason in _original_match_reasons(lines, items, headers, payload):
            _add(reasons, reason)
    return reasons


def _norm_fact(value: str) -> str:
    return _plain(value).casefold().rstrip(".")


def _set_aside_text(lines: Sequence[str]) -> str | None:
    indexes = [
        index for index, line in enumerate(lines) if _starts_with_label(line, "Set aside")
    ]
    if not indexes:
        return None
    parts: list[str] = []
    for index in indexes:
        value = _plain(lines[index].split(":", 1)[1]).rstrip(".")
        if value == "":
            bullets: list[str] = []
            for line in lines[index + 1 :]:
                if _starts_with_label(line, "Set aside"):
                    break
                if line.startswith("- "):
                    bullets.append(_plain(line[2:]).rstrip("."))
            value = "\n".join(bullets)
        parts.append(value.casefold())
    return "\n".join(parts)


def _original_match_reasons(
    lines: list[str],
    items: Sequence[tuple[str, str]],
    headers: Sequence[tuple[int, str]],
    payload: Mapping[str, Any],
) -> list[str]:
    """Human lines must carry the same record the original already holds."""
    reasons: list[str] = []
    aside = payload.get("set_aside")
    stated = _set_aside_text(lines)
    if not isinstance(aside, str) or stated is None or stated != _norm_fact(aside):
        _add(reasons, "review record does not match the original")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        return ["review record does not match the original"]
    final_at = next(
        (index for index, line in enumerate(lines) if _starts_with_label(line, "Final result")),
        None,
    )
    for position, lane in enumerate(lanes):
        if not isinstance(lane, Mapping) or lane.get("result") != "pass":
            continue
        if position >= len(headers):
            _add(reasons, "review record does not match the original")
            continue
        start = headers[position][0]
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        if final_at is not None:
            end = min(end, final_at)
        section = lines[start + 1 : end]
        found: dict[str, str] = {}
        for line in section:
            labeled = _lane_label(line)
            if labeled is not None and labeled[0] in {"Provider", "Model", "Model number"}:
                found[labeled[0]] = labeled[1]
        expected = {
            "Provider": lane.get("provider"),
            "Model": lane.get("model"),
            "Model number": lane.get("model_number"),
        }
        if any(
            not isinstance(value, str) or _norm_fact(found.get(label, "")) != _norm_fact(value)
            for label, value in expected.items()
        ):
            _add(reasons, "review record does not match the original")
        prompt = lane.get("prompt")
        has_prompt_label = any(
            (labeled := _lane_label(line)) is not None and labeled[0] == "Prompt"
            for line in section
        )
        fences = [text.strip() for text in _fences_between(items, start, end) if text.strip()]
        if (
            not isinstance(prompt, str)
            or not has_prompt_label
            or not fences
            or any(text != prompt.strip() for text in fences)
        ):
            _add(reasons, "review record does not match the original")
    return reasons


def _comment_stamp(comment: Mapping[str, Any]) -> datetime | None:
    """When the comment was created. An edit does not make it newer."""
    created_raw = comment.get("created_at")
    if not isinstance(created_raw, str):
        return None
    return _utc(created_raw)


def _declaration_reasons(body: str, *, head: str, markdown_only: bool) -> list[str]:
    reasons: list[str] = []
    try:
        payload = parse_review_block(body)
    except ReviewError as exc:
        _add(reasons, str(exc))
        return reasons
    declaration = validate_declaration(payload, head=head, markdown_only=markdown_only)
    reasons.extend(declaration)
    passes = payload.get("passes")
    if _is_int(passes) and passes >= 1:
        reasons.extend(validate_visible(body, passes))
    else:
        _add(reasons, "review visible text missing")
    # A half run fails on the lane rules. Do not also demand a finished record.
    if not declaration:
        reasons.extend(validate_review_record(body, payload))
    return reasons


def select_review_comment(
    comments: Sequence[Mapping[str, Any]],
    *,
    author_id: int,
    guard_user_id: int,
    committed_at: str,
    head: str,
    markdown_only: bool,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Newest valid author declaration created after the head commit.

    Age is ``created_at`` only. An edit does not make a comment newer; post a
    new comment instead. Other comments do not remove an earlier valid
    declaration. A newer malformed declaration falls back to an older valid one.
    """
    committed = _utc(committed_at)
    if committed is None:
        return None, ["review commit time unavailable"]
    candidates: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for comment in comments:
        user = comment.get("user") if isinstance(comment, Mapping) else None
        if not isinstance(user, Mapping):
            continue
        if user.get("id") == guard_user_id:
            continue
        if not isinstance(comment.get("id"), int) or not isinstance(comment.get("created_at"), str):
            continue
        if user.get("id") != author_id:
            continue
        body = comment.get("body")
        if not looks_like_review(body if isinstance(body, str) else None):
            continue
        stamp = _comment_stamp(comment)
        if stamp is None or stamp <= committed:
            continue
        candidates.append((stamp, int(comment["id"]), comment))
    if not candidates:
        return None, ["review completion comment missing"]
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    newest_reasons: list[str] = []
    for _stamp, _cid, comment in candidates:
        body = comment.get("body")
        text = body if isinstance(body, str) else ""
        reasons = _declaration_reasons(text, head=head, markdown_only=markdown_only)
        if not reasons:
            return comment, []
        if not newest_reasons:
            newest_reasons = reasons
    return None, newest_reasons


def _is_bot(author: Any) -> bool:
    if not isinstance(author, Mapping):
        return False
    if author.get("__typename") == "Bot":
        return True
    login = author.get("login")
    return isinstance(login, str) and login.endswith("[bot]")


def _comment_nodes(thread: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    comments = thread.get("comments")
    if not isinstance(comments, Mapping):
        return None
    nodes = comments.get("nodes")
    if not isinstance(nodes, list):
        return None
    out: list[Mapping[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            return None
        out.append(node)
    return out


def _decision_marker(body: str) -> str | None:
    latest: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("User-Entscheid aufgehoben:"):
            latest = "off"
        elif stripped.startswith("User-Entscheid:"):
            latest = "on"
    return latest


def _open_thread_allowed(nodes: Sequence[Mapping[str, Any]]) -> bool:
    if not nodes:
        return False
    if _is_bot(nodes[0].get("author")):
        return False
    latest: str | None = None
    for node in nodes:
        body = node.get("body")
        if not isinstance(body, str):
            continue
        marker = _decision_marker(body)
        if marker is not None:
            latest = marker
    return latest == "on"


def unresolved_thread_reasons(threads: Sequence[Mapping[str, Any]]) -> list[str]:
    if isinstance(threads, (str, bytes)) or not isinstance(threads, Sequence):
        return ["review thread payload invalid"]
    blocked = False
    for thread in threads:
        if not isinstance(thread, Mapping) or not isinstance(thread.get("isResolved"), bool):
            return ["review thread payload invalid"]
        if thread["isResolved"]:
            continue
        nodes = _comment_nodes(thread)
        if nodes is None:
            return ["review thread payload invalid"]
        if not _open_thread_allowed(nodes):
            blocked = True
    if blocked:
        return ["unresolved review threads"]
    return []


_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { body author { login __typename } }
          }
        }
      }
    }
  }
}
""".strip()


def _load_threads(api: Any, repo: str, number: int) -> list[str]:
    # A thread whose first comment page is truncated fails closed. Paging
    # every thread's comments would be a second query shape; 100 comments
    # is the cap this gate will read.
    if repo.count("/") != 1:
        return ["review thread query failed"]
    owner, name = repo.split("/", 1)
    nodes: list[Any] = []
    cursor: str | None = None
    for _page in range(20):
        try:
            status, data, _headers = api.request(
                "POST",
                "/graphql",
                body={
                    "query": _THREADS_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": number,
                        "cursor": cursor,
                    },
                },
                retry=False,
            )
        except Exception:
            return ["review thread query failed"]
        if status != 200 or not isinstance(data, Mapping) or data.get("errors"):
            return ["review thread query failed"]
        repository = (data.get("data") or {}) if isinstance(data.get("data"), Mapping) else None
        if not isinstance(repository, Mapping):
            return ["review thread query failed"]
        pull = (repository.get("repository") or {}) if isinstance(repository.get("repository"), Mapping) else None
        if not isinstance(pull, Mapping):
            return ["review thread query failed"]
        pull_request = pull.get("pullRequest")
        connection = pull_request.get("reviewThreads") if isinstance(pull_request, Mapping) else None
        if not isinstance(connection, Mapping):
            return ["review thread query failed"]
        page = connection.get("nodes")
        info = connection.get("pageInfo")
        if not isinstance(page, list) or not isinstance(info, Mapping):
            return ["review thread query failed"]
        for node in page:
            comments = node.get("comments") if isinstance(node, Mapping) else None
            page_info = comments.get("pageInfo") if isinstance(comments, Mapping) else None
            # A missing page, a non-boolean flag, or another page all fail closed.
            if not isinstance(page_info, Mapping) or page_info.get("hasNextPage") is not False:
                return ["review thread query failed"]
        nodes.extend(page)
        if info.get("hasNextPage") is True:
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                return ["review thread query failed"]
            continue
        if info.get("hasNextPage") is not False:
            return ["review thread query failed"]
        return unresolved_thread_reasons(nodes)
    return ["review thread query failed"]


def _head_committed_at(api: Any, repo: str, head: str) -> str | None:
    """Committer time of the current head. Missing or unreadable time fails closed."""
    if _SHA.fullmatch(head) is None:
        return None
    try:
        status, data, _headers = api.request("GET", f"/repos/{repo}/commits/{head}")
    except Exception:
        return None
    if status != 200 or not isinstance(data, Mapping):
        return None
    commit = data.get("commit")
    committer = commit.get("committer") if isinstance(commit, Mapping) else None
    date = committer.get("date") if isinstance(committer, Mapping) else None
    if not isinstance(date, str) or _utc(date) is None:
        return None
    return date


def evaluate_review_gate(
    api: Any,
    *,
    repo: str,
    number: int,
    head: str,
    author_id: int,
    comments: Sequence[Mapping[str, Any]],
    markdown_only: bool,
) -> tuple[bool, list[str]]:
    try:
        guard_id, _login = api.resolve_own_user()
    except Exception as exc:
        from .a38_guard import GuardError

        if not isinstance(exc, GuardError):
            raise
        return False, ["guard identity unavailable"]
    if not isinstance(guard_id, int):
        return False, ["guard identity unavailable"]
    reasons: list[str] = []
    committed_at = _head_committed_at(api, repo, head)
    if committed_at is None:
        _add(reasons, "review commit time unavailable")
    else:
        _comment, selected = select_review_comment(
            comments,
            author_id=author_id,
            guard_user_id=guard_id,
            committed_at=committed_at,
            head=head,
            markdown_only=markdown_only,
        )
        reasons.extend(selected)
    reasons.extend(_load_threads(api, repo, number))
    if reasons:
        return False, reasons
    return True, []
