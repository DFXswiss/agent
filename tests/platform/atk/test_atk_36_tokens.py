"""ATK-36: session-token brute force and foreign tokens on MCP, hooks and gateway (07-testing.md section 2.3;
05-runner.md sections 4.4, 4.6 no. 1; decisions ATS-02).

A process with the session's UID (the session-side client) sends 37 guessed or missing tokens to ``/mcp``,
``/hooks/PreToolUse`` and ``/model/v1/messages``: the first ten are ``401`` and journaled ``auth_failed``, then the
UID is limited (``429`` with ``Retry-After``, one ``auth_rate_limited`` record). The session's own token keeps
working (no lock-out of the owner, Claude Code finishes its scenario), and the same token sent from the foreign
session's UID or a non-session UID is refused (binding to the connecting UID). The median times of near guesses
(the real token with its last character changed) and random guesses do not differ measurably (constant-time
comparison). ``test_atk_36[mode]`` is the real run (marker ``atk``).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agent_doubles.atk import catalog, extended
from tests.platform.atk.fakes import SID, UID, VICTIM, VICTIM_UID, FakeHost, assert_attack, context, record, run, run_of


@pytest.mark.atk
@pytest.mark.parametrize("mode", catalog.MODES)
def test_atk_36(atk_results: dict[str, Any], mode: str) -> None:
    assert_attack(atk_results, "ATK-36", mode)


def answer(label: str, status: int, seconds: float = 0.001, retry: str | None = None) -> dict[str, Any]:
    headers = {} if retry is None else {"retry-after": retry}
    return {"label": label, "status": status, "headers": headers, "seconds": seconds}


def good_data() -> dict[str, Any]:
    labels = ["guess-mcp", "guess-hook", "guess-model"] * 3 + ["guess-missing"]
    guesses = [answer(label, 401) for label in labels] + [answer("guess-model", 429, retry="50") for _ in range(27)]
    return {
        "own": [answer("own-before", 200), *guesses, answer("own-after", 200)],
        "victim": [answer("foreign-mcp", 401), answer("foreign-hook", 401), answer("foreign-model", 401)],
        "nobody": [answer("nobody-own", 401), answer("near", 401, 0.0010), answer("far", 401, 0.0012)],
    }


def journal() -> list[dict[str, Any]]:
    return [record("guest.error", code="auth_failed") for _ in range(10)] + [
        record("guest.error", code="auth_rate_limited")
    ]


def checked(
    data: dict[str, Any], session_journal: list[dict[str, Any]] | None = None, victim: bool = True
) -> list[str]:
    host = FakeHost()
    if victim:
        host.records[VICTIM] = [record("guest.error", code="auth_failed") for _ in range(3)]
    ctx = context("ATK-36", host)
    ctx.data.update(data)
    return extended.check_36(ctx, run_of("ATK-36", journal=journal() if session_journal is None else session_journal))


def test_the_client_runs_from_the_session_victim_and_nobody_uids() -> None:
    tokens = iter([ValueError("no claude process yet"), {"token": "tok-abcdef"}])

    def token(*args: str) -> Any:
        return next(tokens)

    clients: list[tuple[str, str]] = []

    def client(uid: str, gid: str, spec: str) -> Any:
        clients.append((uid, gid))
        return {"answers": [{"label": uid}]}

    host = FakeHost(answers={"session-token": token, "client": client})
    ctx = context("ATK-36", host)
    run(extended.while_36(ctx))
    assert clients == [(str(UID), str(UID)), (str(VICTIM_UID), str(VICTIM_UID)), ("65534", "65534")]
    assert host.probes[0] == ("session-token", extended.session_user(SID))
    assert ctx.data == {"own": [{"label": str(UID)}], "victim": [{"label": str(VICTIM_UID)}],
                        "nobody": [{"label": "65534"}]}  # fmt: skip
    assert host.ticks.now == 0.5


def test_session_token_gives_up_after_the_wait() -> None:
    host = FakeHost(answers={"session-token": ValueError("none")})
    with pytest.raises(ValueError, match="none"):
        run(extended.session_token(context("ATK-36", host)))
    assert host.ticks.now >= extended.TOKEN_WAIT_S


def test_near_and_guesses() -> None:
    assert extended.near("abcZ") == "abcA"
    assert extended.near("abcA") == "abcB"
    found = extended.guesses(2, "/model/v1/messages", "g", api_key=True)
    assert [item["headers"][0][0] for item in found] == ["x-api-key", "x-api-key"]
    assert found[0]["headers"][0][1] != found[1]["headers"][0][1]
    assert extended.guesses(1, "/mcp", "g")[0]["headers"][0][1].startswith("Bearer ")


def test_check_36_passes_on_the_expected_answers() -> None:
    assert checked(good_data()) == []


def test_check_36_names_each_failure() -> None:
    assert checked({}) == ["the session-side client did not run"]
    data = good_data()
    data["own"][0] = answer("own-before", 401)
    data["own"][1] = answer("guess-mcp", 200)
    data["own"][12] = answer("guess-model", 429)
    data["victim"][0] = answer("foreign-mcp", 200)
    data["nobody"][2] = answer("far", 401, 0.5)
    found = checked(data, session_journal=[], victim=False)
    assert found[0] == "the session's own token was refused: [401, 200]"
    assert found[1].startswith("a guessed or missing token was not refused: [200, 401")
    assert found[2].startswith("failed checks were not rate-limited after 10:")
    assert found[3] == "a 429 answer has no Retry-After"
    assert found[4] == "the session's token worked from the victim UID: [200, 401, 401]"
    assert found[5] == "journal of the attacking session: []"
    assert found[6] == "the foreign-token attempts of the victim UID were not journaled each: []"
    assert found[7].startswith("median time of near and random guesses differs by 0.4990 s")
    assert len(found) == 8
    assert extended._median([]) == 0.0


def test_check_36_needs_every_surface_before_the_limit() -> None:
    data = good_data()
    data["own"][2:10] = [answer("guess-mcp", 401) for _ in range(8)]
    assert checked(data) == ["not every surface answered 401 before the limit: ['guess-mcp', 'guess-missing']"]


def test_the_brute_force_spreads_over_the_surfaces() -> None:
    host = FakeHost(answers={"session-token": {"token": "tok"}})
    sent: list[list[str]] = []

    def client(uid: str, gid: str, spec: str) -> Any:
        sent.append([item["label"] for item in json.loads(base64.b64decode(spec))["requests"]])
        return {"answers": []}

    host.answers["client"] = client
    run(extended.while_36(context("ATK-36", host)))
    first = sent[0]
    assert (first[0], first[-1]) == ("own-before", "own-after")
    assert set(first[1:11]) == {"guess-mcp", "guess-hook", "guess-model", "guess-missing"}
    assert len(first) == 2 + 37
