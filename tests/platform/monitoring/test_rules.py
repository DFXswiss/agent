"""U-MON rules: Grafana → Prometheus conversion for promtool (expression, `for`, labels, annotations, refusal of
other rule shapes) and its command line."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_monitoring import assets, rules

DOC: dict[str, Any] = yaml.safe_load(
    assets.render_devices(assets.ALERT_FILE.read_text(encoding="utf-8"), ["runner-a", "runner-b"])
)


def rule(uid: str) -> dict[str, Any]:
    found: dict[str, Any] = copy.deepcopy(next(r for r in DOC["groups"][0]["rules"] if r["uid"] == uid))
    return found


def test_converts_every_rule() -> None:
    converted = rules.convert(DOC)
    group = converted["groups"][0]
    assert (group["name"], group["interval"]) == ("agent-platform", "1m")
    by_name = {item["alert"]: item for item in group["rules"]}
    assert list(by_name) == list(assets.RULE_UIDS)
    assert by_name["agent-runner-capacity-low"] == {
        "alert": "agent-runner-capacity-low",
        "expr": "(max by (server) (agent_runner_device_capacity_low)) > 0",
        "for": "10m",
        "labels": {"severity": "warning", "component": "agent-platform"},
        "annotations": rule("agent-runner-capacity-low")["annotations"],
    }
    absent = by_name["agent-runner-textfile-absent"]["expr"]
    assert absent.endswith(" < 20")
    assert 'or label_replace(vector(0), "server", "runner-b", "", "")) < 20' in absent
    assert "\n" not in absent


def test_annotations_can_be_left_out() -> None:
    converted = rules.convert(DOC, annotations=False)
    assert all("annotations" not in item for item in converted["groups"][0]["rules"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.update(condition="B"), "condition C"),
        (lambda r: r["data"].pop(), "condition C"),
        (lambda r: r["data"][0].update(datasourceUid="loki"), "instant query"),
        (lambda r: r["data"][0]["model"].update(instant=False), "instant query"),
        (lambda r: r["data"][1]["model"].update(reducer="max"), "reduce A"),
        (lambda r: r["data"][1]["model"].update(type="math"), "reduce A"),
        (lambda r: r["data"][1]["model"].update(expression="C"), "reduce A"),
        (lambda r: r["data"][2]["model"].update(type="math"), "one threshold"),
        (lambda r: r["data"][2]["model"].update(expression="A"), "one threshold"),
        (lambda r: r["data"][2]["model"].update(conditions=[]), "one threshold"),
        (lambda r: r["data"][2]["model"]["conditions"][0]["evaluator"].update(type="within_range"), "unsupported"),
        (lambda r: r["data"][2]["model"]["conditions"][0]["evaluator"].update(params=[1, 2]), "unsupported"),
    ],
)
def test_refuses_other_rule_shapes(change: Any, message: str) -> None:
    broken = rule("agent-runner-sync-fatal")
    change(broken)
    with pytest.raises(ValueError, match=message):
        rules.convert_rule(broken)


def test_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "rules.yaml"
    assert rules.main(["--device", "runner-a", str(assets.ALERT_FILE), str(out)]) == 0
    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert written["groups"][0]["rules"][0]["annotations"]["summary"]
    assert rules.main(["--without-annotations", "--device", "runner-a", str(assets.ALERT_FILE), str(out)]) == 0
    assert "annotations" not in out.read_text(encoding="utf-8")

    assert rules.main(["--device", "Bad", str(assets.ALERT_FILE), str(out)]) == 1
    assert "invalid device" in capsys.readouterr().err
    assert rules.main(["--device", "runner-a", str(tmp_path / "missing.yaml"), str(out)]) == 1
    assert "missing.yaml" in capsys.readouterr().err
    broken = tmp_path / "broken.yaml"
    broken.write_text("groups: [", encoding="utf-8")
    assert rules.main(["--device", "runner-a", str(broken), str(out)]) == 1
    other = tmp_path / "other.yaml"
    other.write_text("groups: [{name: g, interval: 1m, rules: [{uid: x, data: []}]}]\n", encoding="utf-8")
    assert rules.main(["--device", "runner-a", str(other), str(out)]) == 1
    assert "x: expected queries" in capsys.readouterr().err
