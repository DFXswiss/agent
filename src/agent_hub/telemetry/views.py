"""The closed list of view types and the limits of the UI health report, a copy of `contracts/enums/ui-views.json`.

The contracts directory is not part of the installed package, so the values are kept here as code; the test
`test_views_equal_the_contract` fails when the two differ.
"""

from __future__ import annotations

VIEWS = (
    "home",
    "tasks",
    "tasks.overview",
    "task.chat",
    "task.sessions",
    "task.operations",
    "task.whiteboard",
    "task.audit",
    "session.terminal",
    "session.chat",
    "session.children",
    "whiteboard",
    "notifications",
    "members",
    "devices",
    "workgroups",
    "wg-settings",
    "monitoring",
    "audit",
    "settings",
    "platform-invites",
    "security",
    "invite",
    "login",
    "recover",
    "connect",
    "other",
)
FALLBACK = "other"
BUCKETS_MS = (200, 500, 1000, 2000, 5000, 10000, 30000)
LIMITS = {
    "count": 100000,
    "blocked_ms": 3600000,
    "max_bucket": len(BUCKETS_MS) - 1,
    "views_per_report": 64,
    "body_bytes": 8192,
    "versions_per_day": 20,
}
OTHER_VERSION = "other"
"""The version a report is counted under once a day holds `versions_per_day` other versions."""
VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$"
VIEW_PATTERN = r"^[a-z]+(-[a-z]+)*(\.[a-z]+(-[a-z]+)*)?$"
