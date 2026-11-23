#!/usr/bin/env bash
# Deletes the monitoring test stack VM (README.md here):
#
#   down.sh [--prefix PREFIX]      the VM PREFIXmonitoring (default prefix $AGENT_RUNNER_VM_PREFIX)
#
# Idempotent: a missing VM is not an error. Nothing else is touched (the runner's textfile directory is only mounted).
set -euo pipefail

prefix=${AGENT_RUNNER_VM_PREFIX:-}
while [ $# -gt 0 ]; do
  case $1 in
    --prefix) prefix=${2-}; shift 2 ;;
    *) printf 'usage: down.sh [--prefix PREFIX]\n' >&2; exit 2 ;;
  esac
done
[[ $prefix =~ ^[a-z0-9-]*$ ]] || { echo "down: invalid --prefix" >&2; exit 2; }
command -v limactl >/dev/null || { echo "down: limactl not found" >&2; exit 2; }

vm=${prefix}monitoring
if limactl list --format '{{.Name}}' "$vm" >/dev/null 2>&1 && [ -n "$(limactl list --format '{{.Name}}' "$vm" 2>/dev/null)" ]; then
  limactl delete -f "$vm"
  echo "down: deleted $vm" >&2
else
  echo "down: $vm does not exist" >&2
fi
