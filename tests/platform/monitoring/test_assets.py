"""U-MON assets: schema of the three dashboards and the alert file, the conventions of the infrastructure repository
(provisioning format, folder, interval, states, labels, per-device zero fallback), the placeholder rendering, and the
consistency of every metric and label they use with the metric table of 08-monitoring.md section 3 (ADR-042 name of
the slot total) and with what the runner's textfile renderer (RUN-15) writes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from agent_monitoring import assets, sample

from agent_runner.metrics import textfile
from agent_runner.model.operator import STATES, Summary

from .conftest import MONITORING, REPO

SPEC = REPO / "docs" / "platform" / "spec" / "08-monitoring.md"
ALERT_TEXT = assets.ALERT_FILE.read_text(encoding="utf-8")
LABELS = {"server", "vm", "wg", "session", "state", "direction", "window", "__name__"}

DASHBOARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["uid", "title", "panels", "templating", "schemaVersion", "time", "refresh", "tags"],
    "properties": {
        "uid": {"type": "string", "pattern": "^agent-platform-[a-z]+$", "maxLength": 40},
        "id": {"type": "null"},
        "schemaVersion": {"type": "integer", "minimum": 39},
        "tags": {"type": "array", "contains": {"const": "agent-platform"}},
        "templating": {
            "type": "object",
            "required": ["list"],
            "properties": {
                "list": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["name", "type", "query", "datasource"],
                        "properties": {
                            "type": {"const": "query"},
                            "query": {
                                "type": "string",
                                "pattern": r"^label_values\(agent_runner_[a-z_]+(\{.*\})?, [a-z]+\)$",
                            },
                            "datasource": {"const": {"type": "prometheus", "uid": "prometheus"}},
                        },
                    },
                }
            },
        },
        "panels": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "type", "gridPos", "datasource", "targets"],
                "properties": {
                    "type": {"enum": ["stat", "timeseries"]},
                    "datasource": {"const": {"type": "prometheus", "uid": "prometheus"}},
                    "gridPos": {
                        "type": "object",
                        "required": ["h", "w", "x", "y"],
                        "properties": {"w": {"maximum": 24}, "x": {"maximum": 23}},
                    },
                    "targets": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["refId", "expr", "datasource"],
                            "properties": {"datasource": {"const": {"type": "prometheus", "uid": "prometheus"}}},
                        },
                    },
                },
            },
        },
    },
}

_QUERY = {
    "type": "object",
    "required": ["refId", "relativeTimeRange", "datasourceUid", "model"],
    "properties": {"datasourceUid": {"enum": ["prometheus", "__expr__"]}},
}
ALERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["apiVersion", "groups"],
    "additionalProperties": False,
    "properties": {
        "apiVersion": {"const": 1},
        "groups": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "required": ["orgId", "name", "folder", "interval", "rules"],
                "additionalProperties": False,
                "properties": {
                    "orgId": {"const": 1},
                    "name": {"const": "agent-platform"},
                    "folder": {"const": "Infrastructure"},
                    "interval": {"const": "1m"},
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["uid", "title", "condition", "execErrState", "noDataState", "for", "labels",
                                         "annotations", "data"],
                            "additionalProperties": False,
                            "properties": {
                                "uid": {"type": "string", "maxLength": 40},
                                "title": {"type": "string", "minLength": 1},
                                "condition": {"const": "C"},
                                "execErrState": {"const": "OK"},
                                "noDataState": {"const": "OK"},
                                "for": {"type": "string", "pattern": "^[0-9]+m$"},
                                "labels": {
                                    "type": "object",
                                    "required": ["severity", "component"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "severity": {"enum": ["warning", "critical"]},
                                        "component": {"const": "agent-platform"},
                                    },
                                },
                                "annotations": {
                                    "type": "object",
                                    "required": ["summary", "description"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "summary": {"type": "string", "minLength": 10},
                                        "description": {"type": "string", "minLength": 40},
                                    },
                                },
                                "data": {"type": "array", "minItems": 3, "maxItems": 3, "items": _QUERY},
                            },
                        },
                    },
                },
            },
        },
    },
}  # fmt: skip

# The rule table of 08-monitoring.md section 5: (uid, for).
SPEC_RULES = {
    "agent-runner-textfile-absent": "10m",
    "agent-runner-textfile-stale": "5m",
    "agent-runner-capacity-low": "10m",
    "agent-runner-vm-probe-failed": "5m",
    "agent-runner-session-oom": "0m",
    "agent-runner-sync-fatal": "0m",
    "agent-runner-hub-disconnected": "10m",
}


def alert_doc(devices: list[str] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(assets.render_devices(ALERT_TEXT, devices or ["runner-a"]))
    return doc


def rules(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["uid"]: rule for rule in doc["groups"][0]["rules"]}


def spec_metrics() -> set[str]:
    """The metric names of the table of 08 section 3 (`/ _suffix` shorthands expanded, ADR-042 applied)."""
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("## 3. Sink A") : text.index("## 4. Sink B")]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `agent_runner_"):
            continue
        previous = ""
        for token in re.findall(r"`(_?[a-z_]+)`", line.split("|")[1]):
            if not token.startswith(("agent_runner_", "_")):
                continue
            name = token
            if token.startswith("_"):
                parts = previous.split("_")
                name = "_".join(parts[: len(parts) - token.count("_")]) + token
            names.add(name)
            previous = name
    names.discard("agent_runner_device_vm_slots_total")
    names.add("agent_runner_device_vm_slots")
    return names


def runner_metrics() -> set[str]:
    """What the runner's renderer writes for a snapshot with every entity, the daily spend and the operator's
    subscriptions (KOR-23)."""
    operator = Summary(problem=None, states=dict.fromkeys(STATES, 1), sessions={"a": 1}, newest_age_s=1.0)
    return {metric.name for metric in textfile.metrics_of(sample.snapshot(1.0), spend=sample.SPEND, operator=operator)}


def asset_queries() -> list[str]:
    queries = assets.alert_queries(alert_doc())
    for uid in assets.DASHBOARD_UIDS:
        queries += assets.dashboard_queries(assets.dashboard(uid))
    return queries


# -- dashboards ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("uid", assets.DASHBOARD_UIDS)
def test_dashboard_schema_and_layout(uid: str) -> None:
    data = assets.dashboard(uid)
    jsonschema.validate(data, DASHBOARD_SCHEMA)
    assert data["uid"] == uid
    ids = [panel["id"] for panel in data["panels"]]
    assert len(ids) == len(set(ids))
    for panel in data["panels"]:
        assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24
        refs = [target["refId"] for target in panel["targets"]]
        assert len(refs) == len(set(refs))
    names = [item["name"] for item in data["templating"]["list"]]
    assert names[0] == "server"
    # every query is restricted by every variable of the dashboard (the selection applies to all panels)
    for panel in data["panels"]:
        for target in panel["targets"]:
            for name in names:
                assert f'{name}=~"${name}"' in target["expr"], (uid, panel["title"], name)


def test_dashboards_cover_the_levels_of_section_5() -> None:
    def metrics(uid: str) -> set[str]:
        return set().union(*(assets.metric_names(q) for q in assets.dashboard_queries(assets.dashboard(uid))))

    assert {
        "agent_runner_device_vm_slots", "agent_runner_device_vm_slots_free", "agent_runner_device_pool_ready",
        "agent_runner_device_vm_memory_budget_bytes", "agent_runner_device_vm_memory_used_bytes",
        "agent_runner_vm_state", "agent_runner_device_outbox_backlog", "agent_runner_device_hub_connected",
        "agent_runner_device_sync_fatal", "agent_runner_device_capacity_low",
    } <= metrics("agent-platform-devices")  # fmt: skip
    assert {
        "agent_runner_vm_cpu_ratio", "agent_runner_vm_memory_bytes", "agent_runner_vm_memory_limit_bytes",
        "agent_runner_vm_disk_used_bytes", "agent_runner_vm_network_receive_bytes_total",
        "agent_runner_vm_network_transmit_bytes_total", "agent_runner_vm_uptime_seconds", "agent_runner_vm_sessions",
        "agent_runner_vm_probe_ok",
    } <= metrics("agent-platform-vms")  # fmt: skip
    assert {
        "agent_runner_session_cpu_ratio", "agent_runner_session_memory_bytes",
        "agent_runner_session_memory_limit_bytes", "agent_runner_session_workspace_bytes",
        "agent_runner_session_io_read_bytes_total", "agent_runner_session_io_write_bytes_total",
        "agent_runner_session_oom_kills_total", "agent_runner_session_hook_denies_total",
        "agent_runner_session_sandbox_denials_total", "agent_runner_session_egress_denied_total",
        "agent_runner_session_model_tokens_total",
    } <= metrics("agent-platform-sessions")  # fmt: skip
    vm_var = next(v for v in assets.dashboard("agent-platform-vms")["templating"]["list"] if v["name"] == "vm")
    assert vm_var["query"] == 'label_values(agent_runner_vm_state{server=~"$server"}, vm)'


def test_dashboard_files_are_canonical_json() -> None:
    for path in sorted(assets.DASHBOARD_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n", path.name
    assert sorted(path.stem for path in assets.DASHBOARD_DIR.glob("*.json")) == sorted(assets.DASHBOARD_UIDS)


# -- alert rules -----------------------------------------------------------------------------------------------------


def test_alert_file_schema_and_rules_of_section_5() -> None:
    doc = alert_doc()
    jsonschema.validate(doc, ALERT_SCHEMA)
    found = rules(doc)
    assert list(found) == list(assets.RULE_UIDS)
    assert {uid: rule["for"] for uid, rule in found.items()} == SPEC_RULES
    for uid, rule in found.items():
        data = {item["refId"]: item for item in rule["data"]}
        assert rule["condition"] == "C"
        assert data["A"]["datasourceUid"] == "prometheus"
        assert data["A"]["model"]["datasource"] == {"type": "prometheus", "uid": "prometheus"}
        assert data["B"]["model"]["type"] == "reduce"
        assert data["C"]["model"]["type"] == "threshold"
        assert uid in ALERT_TEXT.split("\napiVersion: 1")[0], f"header comment does not explain {uid}"


def test_rule_expressions_follow_the_sketches_of_section_5() -> None:
    found = rules(alert_doc())

    def expr(uid: str) -> str:
        return " ".join(found[uid]["data"][0]["model"]["expr"].split())

    def evaluator(uid: str) -> dict[str, Any]:
        result: dict[str, Any] = found[uid]["data"][2]["model"]["conditions"][0]["evaluator"]
        return result

    assert "count_over_time(agent_runner_textfile_timestamp_seconds" in expr("agent-runner-textfile-absent")
    assert evaluator("agent-runner-textfile-absent") == {"type": "lt", "params": [20]}
    assert expr("agent-runner-textfile-stale").startswith("time() - ")
    assert evaluator("agent-runner-textfile-stale") == {"type": "gt", "params": [120]}
    assert "agent_runner_device_capacity_low" in expr("agent-runner-capacity-low")
    assert evaluator("agent-runner-capacity-low") == {"type": "gt", "params": [0]}
    assert expr("agent-runner-vm-probe-failed") == "max by (server, vm) (agent_runner_vm_probe_ok)"
    assert "increase(agent_runner_session_oom_kills_total[15m])" in expr("agent-runner-session-oom")
    assert evaluator("agent-runner-sync-fatal") == {"type": "gt", "params": [0]}
    assert evaluator("agent-runner-hub-disconnected") == {"type": "lt", "params": [1]}


def test_absence_rule_has_its_own_zero_fallback_per_device() -> None:
    assert ALERT_TEXT.count(assets.PLACEHOLDER) == 2
    expr = rules(alert_doc(["runner-a", "runner-b"]))["agent-runner-textfile-absent"]["data"][0]["model"]["expr"]
    assert 'server=~"runner-a|runner-b"' in expr
    assert re.findall(r'label_replace\(vector\(0\), "server", "([^"]+)", "", ""\)', expr) == ["runner-a", "runner-b"]
    assert expr.count("vector(0)") == 2
    assert assets.PLACEHOLDER not in assets.render_devices(ALERT_TEXT, ["runner-a"])


@pytest.mark.parametrize(
    ("devices", "message"),
    [([], "at least one"), (["Runner"], "invalid device"), (["a b"], "invalid device"), (["a", "a"], "duplicate")],
)
def test_device_names_are_checked(devices: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        assets.render_devices(ALERT_TEXT, devices)


def test_promtool_test_file_covers_every_rule() -> None:
    doc = yaml.safe_load((MONITORING / "alerting" / "agent-platform.test.yaml").read_text(encoding="utf-8"))
    assert doc["rule_files"] == ["agent-platform.rules.yaml"]
    cases = [case for test in doc["tests"] for case in test["alert_rule_test"]]
    for uid in assets.RULE_UIDS:
        firing = [case for case in cases if case["alertname"] == uid and case["exp_alerts"]]
        silent = [case for case in cases if case["alertname"] == uid and not case["exp_alerts"]]
        assert firing, f"{uid} never fires in the promtool tests"
        for case in firing:
            assert all(alert["exp_labels"]["component"] == "agent-platform" for alert in case["exp_alerts"])
        if uid != "agent-runner-sync-fatal":
            assert silent, f"{uid} has no healthy case"
    for test in doc["tests"]:
        assert test["interval"] == "15s"
        for series in test["input_series"]:
            assert assets.metric_names(series["series"]) <= runner_metrics()


def test_promtool_cases_check_the_end_of_every_for_window() -> None:
    """promtool lists firing alerts only: a rule with `for` needs a silent evaluation one minute before a firing one
    in the same case, so a shorter or longer `for` (or a wrong threshold) fails the test."""
    doc = yaml.safe_load((MONITORING / "alerting" / "agent-platform.test.yaml").read_text(encoding="utf-8"))
    pairs = set()
    for test in doc["tests"]:
        minutes: dict[str, set[tuple[int, bool]]] = {}
        for case in test["alert_rule_test"]:
            evaluation = (int(case["eval_time"].rstrip("m")), bool(case["exp_alerts"]))
            minutes.setdefault(case["alertname"], set()).add(evaluation)
        for uid, evaluations in minutes.items():
            if any((at + 1, True) in evaluations for at, firing in evaluations if not firing):
                pairs.add(uid)
    assert pairs == set(assets.RULE_UIDS) - {"agent-runner-session-oom", "agent-runner-sync-fatal"}


# -- consistency with RUN-15 -----------------------------------------------------------------------------------------


def test_spec_table_matches_the_runner_renderer() -> None:
    assert spec_metrics() == runner_metrics()


def test_every_metric_used_exists_in_the_metric_table() -> None:
    used = set().union(*(assets.metric_names(query) for query in asset_queries()))
    assert used, "no metric found"
    assert used <= spec_metrics()
    assert used <= runner_metrics()
    assert "agent_runner_device_vm_slots_total" not in "".join(asset_queries())  # ADR-042


def test_every_label_used_exists() -> None:
    for query in asset_queries():
        for selector in re.findall(r"\{([^}]*)\}", query):
            for name in re.findall(r"([a-z_]+)\s*=~?", selector):
                assert name in LABELS, (name, query)
        for grouping in re.findall(r"by \(([^)]*)\)", query):
            assert {item.strip() for item in grouping.split(",")} <= LABELS, query


def test_metric_name_extraction() -> None:
    query = 'sum by (vm) (rate(agent_runner_vm_network_receive_bytes_total{vm=~"$vm"}[1m])) / node_load1'
    assert assets.metric_names(query) == {"agent_runner_vm_network_receive_bytes_total"}


# -- documentation assets --------------------------------------------------------------------------------------------


def test_textfile_section_and_tile_use_only_known_placeholders() -> None:
    section = (MONITORING / "textfile-dir.md").read_text(encoding="utf-8")
    tile = (MONITORING / "inventory-tile.toml").read_text(encoding="utf-8")
    assert set(re.findall(r"<[a-z-]+>", section)) == {"<runner-device>", "<textfile-dir>", "<runner-textfile-dir>"}
    assert set(re.findall(r"<[a-z-]+>", tile)) == {"<runner-device>"}
    assert section.startswith("## Agent runner textfile\n")
    assert "ln -sfn <runner-textfile-dir>/agent_runner.prom <textfile-dir>/agent_runner.prom" in section
    assert 'prom_instance = "localhost:9100"' in tile
    assert textfile.FILE_NAME == "agent_runner.prom"


def test_versions_are_the_ones_of_the_existing_stack() -> None:
    pins = dict(
        line.split("=", 1)
        for line in (MONITORING / "test-stack" / "versions.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert (pins["PROMETHEUS_VERSION"], pins["NODE_EXPORTER_VERSION"], pins["GRAFANA_VERSION"]) == (
        "3.11.0",
        "1.8.2",
        "12.4.2",
    )
    for name in ("PROMETHEUS", "NODE_EXPORTER", "GRAFANA"):
        assert re.fullmatch(r"[0-9a-f]{64}", pins[f"{name}_SHA256"])
        assert pins[f"{name}_URL"].startswith("https://")
        assert pins[f"{name}_VERSION"] in pins[f"{name}_URL"]


def test_assets_dir_is_the_package_parent() -> None:
    assert Path(assets.ASSETS) == MONITORING
