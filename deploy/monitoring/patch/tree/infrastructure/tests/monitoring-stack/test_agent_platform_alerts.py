#!/usr/bin/env python3
"""Regression contract for the agent platform rules (agent-platform.yaml).

Source of the file: DFXswiss/agent deploy/monitoring/ (prepared patch). Pinned here: the seven rules and their
UIDs (at most 40 characters, the Grafana limit), the group conventions (folder Infrastructure, interval 1m,
execErrState and noDataState OK), the labels severity and component, the query/reduce/threshold wiring and the
per-device zero fallback of the absence rule: every device in its selector has its own
label_replace(vector(0), "server", "<device>", "", "") and no other zero exists. A bare `or vector(0)` would
fire forever, and a device in the selector without a fallback would vanish unnoticed.
"""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ALERTS = sorted(ROOT.glob("infrastructure/*/monitoring-stack/grafana/provisioning/alerting/agent-platform.yaml"))
DASHBOARDS = sorted(
    ROOT.glob("infrastructure/*/monitoring-stack/grafana/provisioning/dashboards/json/Agent platform/*.json")
)
UIDS = [
    "agent-runner-textfile-absent",
    "agent-runner-textfile-stale",
    "agent-runner-capacity-low",
    "agent-runner-vm-probe-failed",
    "agent-runner-session-oom",
    "agent-runner-sync-fatal",
    "agent-runner-hub-disconnected",
]


class AgentPlatformAlerts(unittest.TestCase):
    def setUp(self):
        self.assertEqual(len(ALERTS), 1, "agent-platform.yaml not found exactly once")
        self.doc = yaml.safe_load(ALERTS[0].read_text())
        self.rules = {rule["uid"]: rule for rule in self.doc["groups"][0]["rules"]}

    def test_group(self):
        self.assertEqual(self.doc["apiVersion"], 1)
        self.assertEqual(len(self.doc["groups"]), 1)
        group = self.doc["groups"][0]
        self.assertEqual((group["folder"], group["interval"]), ("Infrastructure", "1m"))

    def test_rules(self):
        self.assertEqual(list(self.rules), UIDS)
        for uid, rule in self.rules.items():
            self.assertLessEqual(len(uid), 40)
            self.assertEqual((rule["execErrState"], rule["noDataState"]), ("OK", "OK"), uid)
            self.assertEqual(rule["labels"]["component"], "agent-platform", uid)
            self.assertIn(rule["labels"]["severity"], {"warning", "critical"}, uid)
            data = {item["refId"]: item for item in rule["data"]}
            self.assertEqual(rule["condition"], "C", uid)
            self.assertEqual(data["A"]["datasourceUid"], "prometheus", uid)
            self.assertEqual((data["B"]["model"]["reducer"], data["B"]["model"]["expression"]), ("last", "A"), uid)
            self.assertEqual(data["C"]["model"]["expression"], "B", uid)

    def test_absence_has_one_zero_fallback_per_device(self):
        expr = {item["refId"]: item for item in self.rules[UIDS[0]]["data"]}["A"]["model"]["expr"]
        devices = re.search(r'server=~"([^"]+)"', expr).group(1).split("|")
        fallbacks = re.findall(r'label_replace\(vector\(0\), "server", "([^"]+)", "", ""\)', expr)
        self.assertEqual(fallbacks, devices)
        self.assertNotIn("<runner-device>", expr)
        self.assertEqual(expr.count("vector(0)"), len(devices))

    def test_dashboards(self):
        uids = sorted(yaml.safe_load(path.read_text())["uid"] for path in DASHBOARDS)
        self.assertEqual(uids, ["agent-platform-devices", "agent-platform-sessions", "agent-platform-vms"])


if __name__ == "__main__":
    unittest.main()
