"""Attack against the duplicate protection (KOR-28; KONZEPT section 11; 03-api.md sections 8.3, 9; ADR-012).

A session tries to publish the same pull-request comment several times by passing a new idempotency key with every
repetition (and once none at all), then tries to reuse one key for other content. The operations run through the
runner's **real** pipeline and the real `github.comment` executor against the GitHub double; the hub stand-in decides
duplicates on the registered `{epoch, tag}` slot as the hub does (H5/H6). Pass:

* GitHub receives exactly one comment POST for the repeated text; every repetition is `duplicate` of the first
  operation and is recorded (audited) as such;
* a key reused for another text is refused before registration as `params_invalid` (`/idempotency_key`), recorded as
  `rejected`, and nothing reaches GitHub for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.platform.runner.github.conftest import REPO
from tests.platform.runner.github.run10_kit import R10, run10, world
from tests.platform.runner.operations.slots import SlotHub

COMMENTS = f"POST /repos/{REPO}/issues/1/comments"
TEXT = "Approved, merge when green."


def comment_posts(r: R10) -> list[str]:
    return [path for path in r.journal() if path.startswith(COMMENTS)]


def test_a_session_rotating_keys_cannot_publish_the_same_comment_twice(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        hub = SlotHub()
        hub.install(r.ok)
        repo = github.double.world.repo(*REPO.split("/"))
        seeded = [item.body for item in repo.comments]
        sid = await r.session()
        params = {"repo": REPO, "number": 1, "body": TEXT}
        first = await r.run(sid, "github.comment", params, key="atk-key-1")
        assert first.state == "succeeded", first.answer
        repeats = [
            await r.run(sid, "github.comment", params, key=key) for key in ("atk-key-2", "atk-key-3", None, "atk-key-4")
        ]
        assert [op.state for op in repeats] == ["duplicate"] * 4
        assert {op.duplicate_of for op in repeats} == {first.id}
        assert all(op.answer is not None and op.answer["result"] == first.result for op in repeats)
        assert len(comment_posts(r)) == 1
        assert [entry[1:] for entry in hub.audit] == [("accepted", None)] + [("duplicate", first.id)] * 4
        # the same key for another text: refused locally, recorded as rejected, nothing sent to GitHub
        other = await r.run(sid, "github.comment", {**params, "body": "Rejected."}, key="atk-key-1")
        assert (other.state, other.error_code) == ("rejected", "params_invalid")
        assert other.answer is not None
        assert other.answer["error"]["pointers"] == ["/idempotency_key"]
        assert hub.audit[-1] == (other.id, "rejected", "params_invalid")
        assert len(comment_posts(r)) == 1
        assert [item.body for item in repo.comments] == [*seeded, TEXT]

    run10(store_dsn, github, tmp_path, body)
