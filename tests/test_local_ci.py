from __future__ import annotations

import json
import unittest

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli.local_ci import (
    BEGIN_MARK,
    END_MARK,
    LocalCiError,
    evaluate,
    parse_comment,
    render_block,
    verify_comment,
)

HEAD = "ee9040d9013c38acee07fd15628a3a9c4404a147"


def _run(**overrides: object) -> dict:
    base: dict = {
        "id": "format",
        "name": "Format",
        "command": "npm run format:check",
        "result": "pass",
        "exit_code": 0,
        "duration_s": 12.4,
        "timeout_s": 900,
    }
    base.update(overrides)
    return base


def _payload(**overrides: object) -> dict:
    base: dict = {
        "schema": "dfx-local-ci/v1",
        "repo": "DFXswiss/backend",
        "head": HEAD,
        "private": True,
        "recorded_at": "2026-09-02T15:00:00Z",
        "required": ["format", "test"],
        "runs": [
            _run(),
            _run(id="test", name="Test", command="npm test", duration_s=172.3, timeout_s=1800),
        ],
    }
    base.update(overrides)
    return base


def _comment(payload: dict) -> str:
    return (
        "EN:\nReady after 1 review pass.\nFits Frick names to 35 characters.\n\n"
        "DE:\nBereit nach 1 Review-Durchlauf.\nKappt Frick-Namen auf 35 Zeichen.\n\n"
        "<details>\n<summary>Details</summary>\n\n"
        f"{BEGIN_MARK}\n```json\n{json.dumps(payload)}\n```\n{END_MARK}\n\n"
        "</details>\n"
    )


class LocalCiTests(unittest.TestCase):
    def test_round_trip_pass(self) -> None:
        comment = _comment(_payload())
        report = parse_comment(comment)
        self.assertEqual(report.head, HEAD)
        self.assertEqual(report.required, ("format", "test"))
        verdict = evaluate(report)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.status, "pass")
        again = parse_comment("preamble\n" + render_block(report) + "\ntail")
        self.assertEqual(again, report)

    def test_missing_marker_pair(self) -> None:
        with self.assertRaisesRegex(LocalCiError, "exactly one"):
            parse_comment("no markers here")

    def test_two_marker_pairs(self) -> None:
        body = _comment(_payload())
        with self.assertRaisesRegex(LocalCiError, "exactly one"):
            parse_comment(body + body)

    def test_duration_over_timeout_fails(self) -> None:
        payload = _payload(
            runs=[
                _run(),
                _run(id="test", name="Test", command="npm test", duration_s=1801, timeout_s=1800),
            ]
        )
        verdict = verify_comment(_comment(payload))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.status, "fail")
        self.assertTrue(any("duration_s" in r for r in verdict.reasons))

    def test_failed_run_fails_verdict(self) -> None:
        payload = _payload(
            runs=[
                _run(),
                _run(id="test", name="Test", command="npm test", result="fail", exit_code=1),
            ]
        )
        verdict = verify_comment(_comment(payload))
        self.assertFalse(verdict.ok)
        self.assertTrue(any("result is fail" in r for r in verdict.reasons))

    def test_missing_required_run(self) -> None:
        payload = _payload(runs=[_run()])
        verdict = verify_comment(_comment(payload))
        self.assertFalse(verdict.ok)
        self.assertTrue(any("test: missing run" in r for r in verdict.reasons))

    def test_require_ids_mismatch(self) -> None:
        verdict = verify_comment(_comment(_payload()), require_ids=frozenset({"format", "build"}))
        self.assertFalse(verdict.ok)
        self.assertTrue(any("missing ids" in r for r in verdict.reasons))

    def test_public_repo_not_applicable(self) -> None:
        payload = _payload(private=False)
        verdict = verify_comment(_comment(payload))
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.status, "not_applicable")

    def test_unknown_payload_key(self) -> None:
        payload = _payload()
        payload["verdict"] = "pass"
        with self.assertRaisesRegex(LocalCiError, "unknown keys"):
            parse_comment(_comment(payload))

    def test_empty_required_rejected(self) -> None:
        with self.assertRaisesRegex(LocalCiError, "non-empty"):
            parse_comment(_comment(_payload(required=[])))

    def test_pass_with_nonzero_exit_fails(self) -> None:
        payload = _payload(
            runs=[_run(exit_code=1), _run(id="test", name="Test", command="npm test")]
        )
        verdict = verify_comment(_comment(payload))
        self.assertFalse(verdict.ok)
        self.assertTrue(any("exit_code is 1" in r for r in verdict.reasons))

if __name__ == "__main__":
    unittest.main()
