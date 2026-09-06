from __future__ import annotations

import json
import shlex
import unittest
from datetime import datetime, timezone

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli.a38 import A38Error, load_policy, verify_report
from agent_cli.local_ci import BEGIN_MARK, END_MARK

HEAD = "0123456789abcdef0123456789abcdef01234567"


def _minimal_configs() -> dict[str, dict]:
    return {
        "commands": {"steps": [["true"]]},
        "compose": {
            "companion": {
                "directory_env": "EXAMPLE_SERVICES_DIR",
                "ref_env": "EXAMPLE_SERVICES_REF",
                "ref": "main",
                "repository": "example/services",
            },
            "files": ["compose.yml"],
            "test_service": "app",
            "artifacts": [],
        },
        "immutable": {"path": "README.md"},
        "http-smoke": {
            "dockerfile": "Dockerfile",
            "platform": "linux/amd64",
            "container_port": 8080,
            "credentials": {
                "user_env": "EXAMPLE_USER",
                "password_env": "EXAMPLE_PASSWORD",
                "user": "reader",
                "password": "example-secret",
            },
            "health": {"path": "/health", "contains": "ok"},
            "root_path": "/",
            "manifest": {
                "path": "/app/manifest.json",
                "artifacts_key": "artifacts",
                "category_key": "category",
                "path_key": "path",
                "index": "index.html",
                "pdf_category": "pdf",
            },
        },
    }


def _job(*, command: str | None = "printf 'legacy value'", executor: object = None) -> dict:
    job = {
        "id": "unit",
        "name": "Unit",
        "timeout_s": 30,
        "workflow": ".github/workflows/ci.yml",
        "job": "unit",
    }
    if command is not None:
        job["command"] = command
    if executor is not None:
        job["executor"] = executor
    return job


def _executor_job(adapter: str, config: object) -> dict:
    return _job(command=None, executor={"adapter": adapter, "config": config})


def _policy_text(job: dict) -> str:
    return json.dumps(
        {
            "schema": "a38/v1",
            "standard": "A38",
            "documentation": "docs/a38.md",
            "mode": "enforce",
            "jobs": [job],
            "exclusions": [],
        }
    )


def _raw_policy(job_text: str) -> str:
    return (
        '{"schema":"a38/v1","standard":"A38",'
        '"documentation":"docs/a38.md","mode":"enforce",'
        f'"jobs":[{job_text}],"exclusions":[]}}'
    )


def _report(command: str) -> str:
    payload = {
        "schema": "dfx-local-ci/v1",
        "repo": "example/app",
        "head": HEAD,
        "private": True,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "required": ["unit"],
        "runs": [
            {
                "id": "unit",
                "name": "Unit",
                "command": command,
                "result": "pass",
                "exit_code": 0,
                "duration_s": 0.1,
                "timeout_s": 30,
            }
        ],
    }
    return f"{BEGIN_MARK}\n```json\n{json.dumps(payload)}\n```\n{END_MARK}\n"


class ExecutorPolicyTests(unittest.TestCase):
    def test_legacy_command_is_unchanged_byte_for_byte(self) -> None:
        command = "  printf '%s' \"$HOME; $(not-run)\"  "
        policy = load_policy(_policy_text(_job(command=command)))
        self.assertEqual(policy["jobs"][0]["command"], command)

    def test_all_adapter_ids_normalize_to_command_only_jobs(self) -> None:
        for adapter, config in _minimal_configs().items():
            with self.subTest(adapter=adapter):
                policy = load_policy(_policy_text(_executor_job(adapter, config)))
                normalized = policy["jobs"][0]
                self.assertNotIn("executor", normalized)
                self.assertEqual(
                    set(normalized),
                    {"id", "name", "command", "timeout_s", "workflow", "job"},
                )
                argv = shlex.split(normalized["command"])
                self.assertEqual(argv[:5], ["agent", "a38", "job", adapter, "--config"])
                self.assertEqual(len(argv), 6)
                self.assertEqual(json.loads(argv[5]), config)
                canonical = json.dumps(
                    config,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                self.assertEqual(argv[5], canonical)
                self.assertEqual(
                    normalized["command"],
                    f"agent a38 job {adapter} --config {shlex.quote(canonical)}",
                )

    def test_reordered_nested_keys_have_identical_normalization(self) -> None:
        first = {
            "steps": [{"argv": ["printf", "%s", "{repo}"], "stdout": "result.txt"}],
            "env": {"Z_LAST": "z", "A_FIRST": "a"},
        }
        second = {
            "env": {"A_FIRST": "a", "Z_LAST": "z"},
            "steps": [{"stdout": "result.txt", "argv": ["printf", "%s", "{repo}"]}],
        }
        one = load_policy(_policy_text(_executor_job("commands", first)))
        two = load_policy(_policy_text(_executor_job("commands", second)))
        self.assertEqual(one, two)

    def test_shell_metacharacters_quotes_and_unicode_round_trip_as_data(self) -> None:
        value = "'$HOME; $(touch never) && `false` café ☃"
        config = {"steps": [["printf", "%s", value]]}
        policy = load_policy(_policy_text(_executor_job("commands", config)))
        argv = shlex.split(policy["jobs"][0]["command"])
        self.assertEqual(argv[:5], ["agent", "a38", "job", "commands", "--config"])
        self.assertEqual(len(argv), 6)
        self.assertEqual(json.loads(argv[5]), config)
        self.assertIn("\\u00e9", argv[5])
        self.assertIn("\\u2603", argv[5])

    def test_normalized_policy_reload_is_idempotent(self) -> None:
        policy = load_policy(
            _policy_text(_executor_job("commands", {"steps": [["printf", "hello world"]]}))
        )
        self.assertEqual(load_policy(json.dumps(policy)), policy)

    def test_rejects_both_neither_and_job_extras(self) -> None:
        valid_executor = {"adapter": "commands", "config": {"steps": [["true"]]}}
        cases = {
            "both": _job(command="true", executor=valid_executor),
            "neither": _job(command=None),
            "extra": {**_job(), "surprise": True},
            "missing common key": {
                key: value for key, value in _job().items() if key != "workflow"
            },
        }
        for name, job in cases.items():
            with self.subTest(name=name), self.assertRaises(A38Error):
                load_policy(_policy_text(job))

    def test_rejects_executor_shape_types_and_unknown_adapter(self) -> None:
        cases = {
            "nonobject executor": _job(command=None, executor=[]),
            "missing adapter": _job(command=None, executor={"config": {}}),
            "missing config": _job(command=None, executor={"adapter": "commands"}),
            "executor extra": _job(
                command=None,
                executor={"adapter": "commands", "config": {"steps": [["true"]]}, "x": 1},
            ),
            "nonstring adapter": _job(command=None, executor={"adapter": 7, "config": {}}),
            "nonobject config": _executor_job("commands", []),
            "unknown adapter": _executor_job("example-unknown", {}),
        }
        for name, job in cases.items():
            with self.subTest(name=name), self.assertRaises(A38Error):
                load_policy(_policy_text(job))

    def test_rejects_nonfinite_values_in_nested_config_including_overflow(self) -> None:
        job_prefix = (
            '{"id":"unit","name":"Unit","timeout_s":30,'
            '"workflow":".github/workflows/ci.yml","job":"unit",'
            '"executor":{"adapter":"commands","config":'
        )
        for token in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(token=token), self.assertRaisesRegex(A38Error, "non-finite"):
                load_policy(
                    _raw_policy(
                        job_prefix
                        + '{"steps":[["true"]],"env":{"EXAMPLE_VALUE":'
                        + token
                        + "}}}}"
                    )
                )

    def test_rejects_duplicate_keys_inside_config(self) -> None:
        job = (
            '{"id":"unit","name":"Unit","timeout_s":30,'
            '"workflow":".github/workflows/ci.yml","job":"unit",'
            '"executor":{"adapter":"commands","config":'
            '{"steps":[["true"]],"steps":[["false"]]}}}'
        )
        with self.assertRaisesRegex(A38Error, "duplicate key"):
            load_policy(_raw_policy(job))

    def test_rejects_overlong_generated_command(self) -> None:
        config = {"steps": [["printf", "x" * 9000]]}
        with self.assertRaisesRegex(A38Error, "exceeds 8192"):
            load_policy(_policy_text(_executor_job("commands", config)))

    def test_rejects_strict_inner_adapter_config_errors(self) -> None:
        configs = {
            "commands": {"steps": [["true"]], "unknown": True},
            "compose": {
                "companion": {
                    "directory_env": "EXAMPLE_DIR",
                    "ref_env": "EXAMPLE_REF",
                    "ref": "main",
                },
                "files": ["compose.yml"],
                "test_service": "app",
                "artifacts": [],
            },
            "immutable": {"path": 42},
            "http-smoke": {"dockerfile": "Dockerfile"},
        }
        for adapter, config in configs.items():
            with self.subTest(adapter=adapter), self.assertRaisesRegex(
                A38Error, "config is invalid"
            ):
                load_policy(_policy_text(_executor_job(adapter, config)))

    def test_malformed_json_is_a38_error(self) -> None:
        with self.assertRaisesRegex(A38Error, "JSON is invalid"):
            load_policy('{"schema":')

    def test_verify_report_requires_the_normalized_command(self) -> None:
        policy = load_policy(
            _policy_text(_executor_job("commands", {"steps": [["printf", "example"]]}))
        )
        command = policy["jobs"][0]["command"]
        accepted = verify_report(
            _report(command), policy, repo="example/app", head=HEAD, private=True
        )
        self.assertTrue(accepted["ok"], msg=accepted)

        rejected = verify_report(
            _report(command + " "), policy, repo="example/app", head=HEAD, private=True
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("unit: command does not match policy", rejected["reasons"])


if __name__ == "__main__":
    unittest.main()
