#!/usr/bin/env bash
# Attack suite ATK-01..ATK-40 against real VMs (docs/platform/spec/07-testing.md section 2.3; ADR-008). Runs on the
# runner device from the repository root with the platform environment active (.venv/bin first in PATH).
#
#   scripts/run-atk.sh --prefix PREFIX [--port PORT] [--golden NAME] [--modes hooks,fault] [--only ATK-01,ATK-02] [--redteam] [--live] [--parallel N] [--out DIR] [--keep-golden] [--keep-vms] [-- PYTEST-ARGS]
#
# --redteam adds the scripted red team REDTEAM-DOUBLE to every mode; --live adds REDTEAM-LIVE (a real model through
# OpenRouter, mode hooks, marker live, never part of make platform-ci) and reads the OpenRouter key from stdin: it is
# handed to the test process on file descriptor 3 and lives only in memory (07-testing.md section 6). Combine with
# `--only NONE` to run the red team alone.
#
# With --golden NAME the suite uses that existing, stopped golden instead of building one (a shared TEST_MODE cache
# `cache-golden-<sha12>` of tools/wp/golden-cache.sh, or a production golden) and never deletes it; such a golden has
# no usable test model port, so every VM reaches the model double through an SSH reverse tunnel (ADR-034). A
# production golden runs with `--modes hooks` (the hook fault switch exists only in TEST_MODE, ADR-036).
#
# 1. Builds the TEST_MODE golden <prefix>golden-atk<port>-<hash> with `agent-runner image build --test-model-port
#    PORT` (sessions' guest API may reach the model double on that host port), unless it already exists. The hash
#    covers every input of the image, so a changed guest, image or build logic gets a fresh golden.
# 2. Runs the tests marked `atk` (tests/platform/atk): one VM per mode cloned from that golden, the 40 scripted
#    adversaries of the model double against real Claude Code, evidence and verdicts in --out. Arguments after `--`
#    go to pytest (e.g. `-- -k "atk_3[3-9] or atk_40"` with `--only ATK-33,...,ATK-40`).
# 3. Deletes every clone of the prefix (also on failure) and, without --keep-golden, the golden; fails if a clone
#    is left over.
#
# The runner store of the golden build lives in --runner-home (default build/atk/runner-home), never in the
# device's own runner home.
set -euo pipefail

prefix=""
port="${AGENT_ATK_PORT:-}"
only=""
golden=""
modes=""
redteam=0
live=0
parallel=3
out=""
keep_golden=0
keep_vms=0
runner_home="build/atk/runner-home"

log() { printf 'run-atk: %s\n' "$*" >&2; }
usage() { sed -n '4p' "$0" | sed 's/^# *//' >&2; exit 2; }

while [ $# -gt 0 ]; do
  case $1 in
  --prefix) prefix=${2:-}; shift 2 ;;
  --port) port=${2:-}; shift 2 ;;
  --only) only=${2:-}; shift 2 ;;
  --golden) golden=${2:-}; shift 2 ;;
  --modes) modes=${2:-}; shift 2 ;;
  --redteam) redteam=1; shift ;;
  --live) live=1; shift ;;
  --parallel) parallel=${2:-}; shift 2 ;;
  --out) out=${2:-}; shift 2 ;;
  --runner-home) runner_home=${2:-}; shift 2 ;;
  --keep-golden) keep_golden=1; shift ;;
  --keep-vms) keep_vms=1; shift ;;
  --) shift; break ;;
  *) usage ;;
  esac
done

[[ "$prefix" =~ ^[a-z0-9]+([.-][a-z0-9]+)*-$ ]] || { log "--prefix is required (lower case, ending with '-')"; exit 2; }
if [ -z "$port" ]; then
  if [ -n "${WP_PORT_BASE:-}" ]; then port=$((WP_PORT_BASE + 10)); else port=47610; fi
fi
[[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$port" -le 65535 ] || { log "--port must be a TCP port"; exit 2; }
[[ "$parallel" =~ ^[1-3]$ ]] || { log "--parallel must be 1..3 (plus the victim session: at most 4 per VM)"; exit 2; }
command -v limactl >/dev/null || { log "limactl not found (run on the runner device)"; exit 2; }
command -v agent-runner >/dev/null || { log "agent-runner not found (activate the platform environment)"; exit 2; }

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
out=${out:-build/atk/run-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$out" "$runner_home" && chmod 0700 "$runner_home"
runner_home=$(cd "$runner_home" && pwd)

[ -z "$modes" ] || [[ "$modes" =~ ^(hooks|fault)(,(hooks|fault))?$ ]] || { log "--modes must be hooks, fault or both"; exit 2; }
tunnel=0
if [ -n "$golden" ]; then
  [[ "$golden" =~ ^[a-z0-9]+([.-][a-z0-9]+)*$ ]] || { log "--golden must be a Lima instance name"; exit 2; }
  listed=0
  for _ in 1 2 3; do  # `limactl list` can come back empty while other instances are created or deleted
    # the whole list first: `grep -q` ends at the match, and with pipefail the cut-off `limactl` would fail the test
    names=$(limactl list --quiet 2>/dev/null || true)
    if grep -qx "$golden" <<<"$names"; then listed=1; break; fi
    sleep 3
  done
  [ "$listed" = 1 ] || { log "golden $golden does not exist"; exit 2; }
  tunnel=1
  keep_golden=1
else
  hash=$(find images src/agent_guest src/agent_runner/vm contracts -type f ! -name '*.pyc' ! -path '*/__pycache__/*' \
    | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-10)
  golden="${prefix}golden-atk${port}-${hash}"
fi

cleanup() {
  local rc=$?
  local left
  left=""
  [ "$keep_vms" = 1 ] || left=$(limactl list --quiet 2>/dev/null | grep "^${prefix}arp-" || true)
  for vm in $left; do
    log "deleting left-over clone $vm"
    limactl delete -f "$vm" >/dev/null 2>&1 || true
    rc=1
  done
  if [ "$keep_golden" = 0 ] && grep -qx "$golden" <<<"$(limactl list --quiet 2>/dev/null || true)"; then
    limactl delete -f "$golden" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

if [ "$tunnel" = 1 ]; then
  log "using the existing golden $golden (model double through SSH reverse tunnels)"
elif grep -qx "$golden" <<<"$(limactl list --quiet)"; then
  log "reusing golden $golden"
else
  log "building golden $golden (TEST_MODE, model port $port)"
  AGENT_RUNNER_HOME="$runner_home" AGENT_RUNNER_TEST_MODE=1 AGENT_RUNNER_VM_PREFIX="$prefix" \
    agent-runner image build --tag "atk${port}-${hash}" --test-model-port "$port" >&2
fi

if [ "$live" = 1 ]; then
  [ ! -t 0 ] || { log "--live reads the OpenRouter key from stdin (a pipe, never a file or an argument)"; exit 2; }
  exec 3<&0 0</dev/null
fi
log "attack suite: output in $out"
AGENT_ATK_RUN=1 AGENT_ATK_PREFIX="$prefix" AGENT_ATK_GOLDEN="$golden" AGENT_ATK_PORT="$port" \
  AGENT_ATK_TUNNEL="$tunnel" AGENT_ATK_MODES="$modes" AGENT_ATK_REDTEAM="$redteam" AGENT_ATK_LIVE="$live" \
  AGENT_ATK_OUT="$out" AGENT_ATK_ONLY="$only" AGENT_ATK_PARALLEL="$parallel" AGENT_ATK_KEEP_VMS="$keep_vms" \
  python -m pytest -c tests/platform/pytest.ini --confcutdir=tests/platform --import-mode=importlib \
  -p no:cacheprovider -m atk -v -rA tests/platform/atk "$@"
