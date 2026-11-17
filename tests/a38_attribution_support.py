"""Placeholder dfx pr guard attribution patterns for tests.

The suite configures them for every test, as an adopter's Actions variable
does. Tests of a missing or invalid configuration remove or replace the
variable.
"""

from __future__ import annotations

import json

ATTRIBUTION_PATTERNS = {
    "tool_tokens": ["tool-a", "tool-b"],
    "session_headers": ["Tool-A-Session"],
    "noreply_emails": ["noreply@tool-a.example"],
}
ATTRIBUTION_PATTERNS_JSON = json.dumps(ATTRIBUTION_PATTERNS)
