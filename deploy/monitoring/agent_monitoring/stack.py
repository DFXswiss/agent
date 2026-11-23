"""Checks of a running monitoring stack over the Prometheus and Grafana HTTP APIs (08-monitoring.md section 7).

    python -m agent_monitoring.stack --prometheus URL --grafana URL --grafana-password-file FILE \\
        --server NAME --textfile FILE [--alert-file FILE] [--expect-active UID…] [--timeout S]

1. Prometheus has a series with `server=NAME` for every metric name of the runner textfile (`# TYPE` lines).
2. Grafana: the data source `prometheus` is healthy; the three dashboards are provisioned in the folder `Agent
   platform`; every panel query and every variable query returns data through Grafana (`/api/ds/query`, variables
   replaced by `.+`, `$__rate_interval` by `1m`).
3. Grafana: the seven rules of the alert file are provisioned in the folder `Infrastructure`, each has been evaluated
   without error, and every rule of `--expect-active` is pending or firing.

The checks are repeated until all pass or `--timeout` expires (scrapes, the first rule evaluation and `rate()` need a
few intervals); the result is one JSON report on stdout, exit 0 only when every check passed. Standard library and
PyYAML only: the module runs with the system Python of the test-stack VM.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from agent_monitoring.assets import (
    ALERT_FILE,
    ALERT_FOLDER,
    DASHBOARD_FOLDER,
    DASHBOARD_UIDS,
    RULE_UIDS,
    dashboard_queries,
)

TYPE_RE = re.compile(r"^# TYPE (\S+) ", re.MULTILINE)
VARIABLE_RE = re.compile(r"\$(server|vm|session)\b")
NEVER_EVALUATED = "0001-01-01T00:00:00Z"


class Api:
    """JSON over HTTP with optional basic auth; `opener` is replaceable for tests."""

    def __init__(self, base: str, auth: tuple[str, str] | None = None, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Accept": "application/json"}
        if auth is not None:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        self.timeout = timeout

    def call(self, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or b"null")


def textfile_metrics(text: str) -> set[str]:
    return set(TYPE_RE.findall(text))


def check_prometheus(prometheus: Api, server: str, expected: set[str]) -> list[str]:
    selector = '{__name__=~"agent_runner_.+", server="' + server + '"}'
    query = urllib.parse.urlencode({"query": f"group by (__name__) ({selector})"})
    result = prometheus.call(f"/api/v1/query?{query}")["data"]["result"]
    present = {item["metric"]["__name__"] for item in result}
    return [f"prometheus: no series of {name} with server={server}" for name in sorted(expected - present)]


def grafana_query(expr: str) -> str:
    return VARIABLE_RE.sub(".+", expr).replace("$__rate_interval", "1m")


def _has_values(frames: list[dict[str, Any]]) -> bool:
    for frame in frames:
        values = frame.get("data", {}).get("values") or []
        if len(values) > 1 and values[-1]:
            return True
    return False


def check_dashboards(grafana: Api) -> list[str]:
    problems = []
    health = grafana.call("/api/datasources/uid/prometheus/health")
    if health.get("status") != "OK":
        problems.append(f"grafana: data source prometheus not healthy: {health.get('message')}")
    for uid in DASHBOARD_UIDS:
        found = grafana.call(f"/api/dashboards/uid/{uid}")
        meta = found["meta"]
        if meta.get("folderTitle") != DASHBOARD_FOLDER or not meta.get("provisioned"):
            problems.append(f"grafana: {uid} not provisioned in folder {DASHBOARD_FOLDER!r}")
        for query in dashboard_queries(found["dashboard"]):
            expr = query
            if query.startswith("label_values("):
                expr = f"count({query[len('label_values(') : query.rindex(',')]})"
            body = {
                "queries": [
                    {"refId": "A", "datasource": {"uid": "prometheus"}, "expr": grafana_query(expr), "instant": True}
                ],
                "from": "now-5m",
                "to": "now",
            }
            answer = grafana.call("/api/ds/query", body)["results"]["A"]
            if answer.get("error") or not _has_values(answer.get("frames", [])):
                problems.append(f"grafana: {uid}: no data for {expr}: {answer.get('error', 'empty')}")
    return problems


def check_rules(grafana: Api, titles: dict[str, str], expect_active: set[str]) -> list[str]:
    problems = []
    provisioned = {rule["uid"]: rule for rule in grafana.call("/api/v1/provisioning/alert-rules")}
    folders = {item["uid"]: item["title"] for item in grafana.call("/api/folders")}
    for uid in RULE_UIDS:
        rule = provisioned.get(uid)
        if rule is None:
            problems.append(f"grafana: rule {uid} not provisioned")
        elif folders.get(rule["folderUID"]) != ALERT_FOLDER:
            problems.append(f"grafana: rule {uid} not in folder {ALERT_FOLDER!r}")
    states = {}
    for group in grafana.call("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]:
        for rule in group["rules"]:
            states[rule["name"]] = rule
    for uid in RULE_UIDS:
        state = states.get(titles[uid])
        if state is None:
            problems.append(f"grafana: rule {uid} has no evaluation state")
        elif state.get("health") == "error" or state.get("lastError"):
            problems.append(f"grafana: rule {uid} evaluation error: {state.get('lastError')}")
        elif state.get("lastEvaluation", NEVER_EVALUATED) == NEVER_EVALUATED:
            problems.append(f"grafana: rule {uid} not evaluated yet")
        elif uid in expect_active and state.get("state") not in {"pending", "firing"}:
            problems.append(f"grafana: rule {uid} is {state.get('state')}, expected pending or firing")
    return problems


def rule_titles(alert_text: str) -> dict[str, str]:
    doc = yaml.safe_load(alert_text)
    return {rule["uid"]: rule["title"] for group in doc["groups"] for rule in group["rules"]}


def run_checks(
    checks: Callable[[], list[str]],
    timeout: float,
    *,
    pause: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """`checks()` until it returns no problem or `timeout` passed; the problems of the last round."""
    deadline = clock() + timeout
    while True:
        try:
            problems = checks()
        except (OSError, ValueError, KeyError) as exc:
            problems = [f"request failed: {exc}"]
        if not problems or clock() >= deadline:
            return problems
        sleep(pause)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_monitoring.stack", description=__doc__.splitlines()[0])
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--grafana", required=True)
    parser.add_argument("--grafana-user", default="admin")
    parser.add_argument("--grafana-password-file", type=Path, required=True)
    parser.add_argument("--server", required=True, help="server label of the runner device")
    parser.add_argument("--textfile", type=Path, required=True, help="the runner textfile node_exporter reads")
    parser.add_argument("--alert-file", type=Path, default=ALERT_FILE)
    parser.add_argument("--expect-active", action="append", default=[], choices=RULE_UIDS)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    password = args.grafana_password_file.read_text(encoding="utf-8").strip()
    prometheus, grafana = Api(args.prometheus), Api(args.grafana, (args.grafana_user, password))
    expected = textfile_metrics(args.textfile.read_text(encoding="utf-8"))
    titles = rule_titles(args.alert_file.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"server": args.server, "metrics": len(expected), "dashboards": list(DASHBOARD_UIDS)}

    def checks() -> list[str]:
        return [
            *check_prometheus(prometheus, args.server, expected),
            *check_dashboards(grafana),
            *check_rules(grafana, titles, set(args.expect_active)),
        ]

    problems = run_checks(checks, args.timeout)
    report.update(rules=list(RULE_UIDS), expect_active=args.expect_active, problems=problems, ok=not problems)
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
