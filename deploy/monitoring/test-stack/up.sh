#!/usr/bin/env bash
# Starts the local monitoring test stack on the runner device (08-monitoring.md section 7; README.md here):
#
#   up.sh --textfile-dir DIR [--prefix PREFIX] [--server NAME] [--forward-port PORT] [--archives DIR]
#         [--check [CHECK-ARGS…]]
#
#   --textfile-dir DIR   the runner's TEXTFILE_DIR (absolute, must exist), mounted read-only into the VM; the VM's
#                        node_exporter reads DIR/agent_runner.prom through a symlink (textfile-dir.md)
#   --prefix PREFIX      instance name prefix (default $AGENT_RUNNER_VM_PREFIX); the VM is PREFIXmonitoring
#   --server NAME        the server label Prometheus adds (the host Alloy's label in the fleet; default runner-device)
#   --forward-port PORT  forward Prometheus to 127.0.0.1:PORT and Grafana to 127.0.0.1:PORT+1 on this host
#                        (default: no forward, every guest port stays in the VM)
#   --archives DIR       offline mode (hermetic E2E runs): the archives of versions.env come from DIR (absolute,
#                        filled by fetch.sh), mounted read-only; the VM's own outbound traffic is dropped from its
#                        first boot on except to the Lima host network (gateway, DNS) and NTP (lima.yaml)
#   --check [ARGS…]      run check.sh in the VM afterwards; ARGS go to it (--expect-active UID, --timeout S)
#
# Creates the VM from lima.yaml when it does not exist, starts it and runs provision.sh SERVER as root in it. An
# existing VM is reused only with the same mounts and forwards (recorded in the VM at creation; otherwise down.sh
# first). Exit code of the check when --check.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
assets=$(dirname "$here")
prefix=${AGENT_RUNNER_VM_PREFIX:-}
server=runner-device
textfile_dir=
archives=
forward=
check=0
check_args=()

usage() { printf 'usage:\n' >&2; sed -n '4,16p' "$0" >&2; exit 2; }
log() { printf 'up: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case $1 in
    --textfile-dir | --prefix | --server | --forward-port | --archives) [ $# -ge 2 ] || usage ;;
  esac
  case $1 in
    --textfile-dir) textfile_dir=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --server) server=$2; shift 2 ;;
    --forward-port) forward=$2; shift 2 ;;
    --archives) archives=$2; shift 2 ;;
    --check) check=1; shift; check_args=("$@"); break ;;
    *) usage ;;
  esac
done

[ -n "$textfile_dir" ] || usage
case $textfile_dir in /*) ;; *) log "--textfile-dir must be absolute"; exit 2 ;; esac
# The paths go into the JSON of `limactl create --set`: plain path characters only.
[[ $textfile_dir =~ ^/[A-Za-z0-9._/-]+$ ]] || { log "--textfile-dir: letters, digits and . _ / - only"; exit 2; }
[[ $assets =~ ^/[A-Za-z0-9._/-]+$ ]] || { log "the checkout path has other characters than letters, digits, . _ / -"; exit 2; }
[ -d "$textfile_dir" ] || { log "$textfile_dir does not exist"; exit 2; }
[[ $server =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { log "invalid --server"; exit 2; }
[[ $prefix =~ ^[a-z0-9-]*$ ]] || { log "invalid --prefix"; exit 2; }
if [ -n "$archives" ]; then
  [[ $archives =~ ^/[A-Za-z0-9._/-]+$ ]] || { log "--archives: absolute, letters, digits and . _ / - only"; exit 2; }
  [ -d "$archives" ] || { log "$archives does not exist (fill it with fetch.sh)"; exit 2; }
fi
if [ -n "$forward" ]; then
  [[ $forward =~ ^[0-9]+$ ]] && [ "$forward" -ge 1024 ] && [ "$forward" -le 65534 ] || { log "invalid --forward-port"; exit 2; }
fi
command -v limactl >/dev/null || { log "limactl not found (run on the runner device)"; exit 2; }

vm=${prefix}monitoring
conf="assets=$assets textfile=$textfile_dir forward=${forward:-none}"
[ -z "$archives" ] || conf="$conf archives=$archives"
record=/etc/agent-monitoring/up.conf
status=$(limactl list --format '{{.Status}}' "$vm" 2>/dev/null || true)
created=0

if [ -z "$status" ]; then
  mounts=$(printf '{"location": "%s", "mountPoint": "/opt/agent-monitoring", "writable": false}, {"location": "%s", "mountPoint": "/mnt/agent-runner-textfile", "writable": false}' \
    "$assets" "$textfile_dir")
  offline=
  if [ -n "$archives" ]; then
    mounts=$(printf '%s, {"location": "%s", "mountPoint": "/mnt/agent-monitoring-archives", "writable": false}' \
      "$mounts" "$archives")
    offline=' | .param.AGENT_MONITORING_OFFLINE = "1"'
  fi
  mounts="[$mounts]"
  forwards='[{"guestPortRange": [1, 65535], "ignore": true}]'
  if [ -n "$forward" ]; then
    forwards=$(printf '[{"guestPort": 9090, "hostIP": "127.0.0.1", "hostPort": %d}, {"guestPort": 3000, "hostIP": "127.0.0.1", "hostPort": %d}, {"guestPortRange": [1, 65535], "ignore": true}]' \
      "$forward" "$((forward + 1))")
  fi
  log "creating $vm"
  limactl create --tty=false --name "$vm" --set ".mounts = $mounts | .portForwards = $forwards$offline" "$here/lima.yaml"
  created=1
fi
if [ "$status" != Running ]; then
  log "starting $vm"
  limactl start --tty=false "$vm"
fi
if [ "$created" = 1 ]; then
  limactl shell --workdir / "$vm" sudo bash -c 'mkdir -p "${1%/*}" && printf "%s\n" "$2" >"$1"' _ "$record" "$conf"
else
  recorded=$(limactl shell --workdir / "$vm" sudo cat "$record" 2>/dev/null || true)
  if [ "$recorded" != "$conf" ]; then
    log "$vm exists with other mounts or forwards (${recorded:-unknown}); run down.sh first"
    exit 2
  fi
fi

limactl shell --workdir / "$vm" sudo bash /opt/agent-monitoring/test-stack/provision.sh "$server"
if [ -n "$forward" ]; then
  log "prometheus http://127.0.0.1:$forward grafana http://127.0.0.1:$((forward + 1))"
fi

if [ "$check" = 1 ]; then
  limactl shell --workdir / "$vm" sudo bash /opt/agent-monitoring/test-stack/check.sh "$server" ${check_args[@]+"${check_args[@]}"}
fi
