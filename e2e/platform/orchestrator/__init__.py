"""E2E orchestrator of the platform suite (docs/platform/spec/07-testing.md section 5).

Starts, per run, a PostgreSQL 16 cluster, the hub, the doubles of the agent checkout, runner instances and the
relays that log and cut every connection; runs the Playwright specs against them; checks egress, doubles and
leftovers; collects and scans the artefacts. Python standard library plus httpx only.
"""
