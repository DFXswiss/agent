#!/bin/sh
# Platform E2E suite (docs/platform/spec/07-testing.md sections 5 and 7):
#   e2e/platform/run.sh --topology t1|t2 [--runner <ssh target>] [--only E2E-06,...] [--repeat N] [--live]
#                       [--project desktop|mobile]
# Runs the orchestrator with the Python of the agent-core checkout (.venv, else python3.12 on PATH).
set -eu
suite=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$suite/../.." && pwd)
python="$repo/.venv/bin/python"
[ -x "$python" ] || python=$(command -v python3.12 || command -v python3)
cd "$suite"
exec "$python" -m orchestrator "$@"
