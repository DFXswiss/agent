"""Grafana alert rules → Prometheus alerting rules, for `promtool check rules` and `promtool test rules`.

Every rule of the alert file has the shape of the infrastructure's rules: query `A` on the Prometheus data source,
`B` = reduce `last` of `A`, `C` = threshold on `B` (`gt` or `lt` with one parameter), condition `C`. Grafana
evaluates the instant vector of `A`, keeps each series' last value and fires the series whose value passes the
threshold; the Prometheus rule `(A) > x` (or `< x`) selects exactly those series, with the same `for`, labels and
annotations. The alert name is the rule UID. A rule of another shape is refused instead of converted loosely.
`promtool test rules` compares annotations too; `--without-annotations` leaves them out for that use.

    python -m agent_monitoring.rules [--without-annotations] --device NAME [--device NAME…] ALERT_FILE OUT_FILE
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from agent_monitoring.assets import render_devices

OPERATORS = {"gt": ">", "lt": "<"}


def _by_ref(rule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["refId"]: item for item in rule["data"]}


def convert_rule(rule: dict[str, Any], annotations: bool = True) -> dict[str, Any]:
    uid = rule["uid"]
    data = _by_ref(rule)
    if rule.get("condition") != "C" or set(data) != {"A", "B", "C"}:
        raise ValueError(f"{uid}: expected queries A, B, C with condition C")
    query, reduce, threshold = data["A"], data["B"]["model"], data["C"]["model"]
    if query["datasourceUid"] != "prometheus" or not query["model"].get("instant"):
        raise ValueError(f"{uid}: A must be an instant query on the prometheus data source")
    if reduce.get("type") != "reduce" or reduce.get("reducer") != "last" or reduce.get("expression") != "A":
        raise ValueError(f"{uid}: B must reduce A with `last`")
    conditions = threshold.get("conditions") or []
    if threshold.get("type") != "threshold" or threshold.get("expression") != "B" or len(conditions) != 1:
        raise ValueError(f"{uid}: C must be one threshold on B")
    evaluator = conditions[0]["evaluator"]
    operator = OPERATORS.get(evaluator["type"])
    if operator is None or len(evaluator["params"]) != 1:
        raise ValueError(f"{uid}: unsupported evaluator {evaluator['type']!r}")
    expr = " ".join(query["model"]["expr"].split())
    converted = {
        "alert": uid,
        "expr": f"({expr}) {operator} {evaluator['params'][0]}",
        "for": rule["for"],
        "labels": dict(rule["labels"]),
    }
    if annotations:
        converted["annotations"] = dict(rule["annotations"])
    return converted


def convert(doc: dict[str, Any], annotations: bool = True) -> dict[str, Any]:
    """A Prometheus rule file with one group per Grafana group."""
    return {
        "groups": [
            {
                "name": group["name"],
                "interval": group["interval"],
                "rules": [convert_rule(rule, annotations) for rule in group["rules"]],
            }
            for group in doc["groups"]
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_monitoring.rules", description=__doc__.splitlines()[0])
    parser.add_argument("--device", action="append", required=True, help="runner device (server label); repeatable")
    parser.add_argument("--without-annotations", action="store_true", help="for promtool test rules")
    parser.add_argument("alert_file", type=Path)
    parser.add_argument("out_file", type=Path)
    args = parser.parse_args(argv)
    try:
        doc = yaml.safe_load(render_devices(args.alert_file.read_text(encoding="utf-8"), args.device))
        rules = convert(doc, annotations=not args.without_annotations)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"rules: {exc}", file=sys.stderr)
        return 1
    args.out_file.write_text(yaml.safe_dump(rules, sort_keys=False, width=200), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
