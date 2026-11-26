#!/bin/sh
# Checks of a work VM installation after a restart of the runner device (docs/platform/spec/13-work-vm.md section
# 6.1 and 6.3; runbook docs/platform/runbooks/work-vms.md). Run as root by the operator after the device restarted
# (and after `workvm install` on a test device, to compare). Read-only.
#
#   after-reboot-check.sh [--accounts N] [--prefix PREFIX]
#
# Checks: the APFS volume `AgentWorkVM` is mounted at /var/agent-workvm (the custom mount point of `diskutil apfs
# addVolume` does not persist across a restart; the /etc/fstab entry `workvm install` writes keeps it, HOT-25), owned
# by root, the slot homes
# and their Lima homes exist with their owners, `lima/_config` belongs to the slot account (0700), the image directory exists, the watcher's LaunchDaemon runs, its
# state is `ok …`, our anchor is loaded and the first child of `com.apple`, and `anchor "com.apple/*"` is the first
# anchor line of the main ruleset. One line per check `check=<name> result=pass|fail detail=...`; exit 0 only when
# every check passed. When only the mount fails: run `agent-runner workvm install` again as root; it mounts the volume
# back and writes the /etc/fstab entry (an installation before HOT-25 has none).
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

count=1
prefix=
while [ $# -gt 0 ]; do
  case "$1" in
    --accounts) [ $# -ge 2 ] || { echo "after-reboot-check: --accounts needs a number" >&2; exit 2; }; count=$2; shift 2 ;;
    --prefix) [ $# -ge 2 ] || { echo "after-reboot-check: --prefix needs a value" >&2; exit 2; }; prefix=$2; shift 2 ;;
    *) sed -n '6,8p' "$0" >&2; exit 2 ;;
  esac
done
case "$count" in '' | *[!0-9]*) echo "after-reboot-check: --accounts needs a number" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "after-reboot-check: run as root" >&2; exit 2; }

root=/var/agent-workvm
anchor=com.apple/000.agent.workvm
failed=0
report() {
  echo "check=$1 result=$2 detail=$3"
  [ "$2" = pass ] || failed=1
}

if mount | grep -Eq " on (/private)?$root \\("; then
  report volume_mounted pass "$root"
else
  report volume_mounted fail "nothing mounted at $root"
fi
volume=$(diskutil info "$root" 2>/dev/null | awk -F': *' '/Volume Name/ { print $2; exit }')
if [ "$volume" = AgentWorkVM ]; then report volume_name pass AgentWorkVM; else report volume_name fail "volume=${volume:-none}"; fi
owner=$(stat -f '%Su:%Sg' "$root" 2>/dev/null)
if [ "$owner" = root:wheel ]; then report volume_owner pass "$owner"; else report volume_owner fail "owner=${owner:-none}"; fi
if [ -d "$root/images" ]; then report images_dir pass present; else report images_dir fail missing; fi

i=0
while [ "$i" -lt "$count" ]; do
  account=$(printf '%swvm%02d' "$prefix" "$i")
  home="$root/$account"
  if [ "$(stat -f '%Su' "$home" 2>/dev/null)" = "$account" ] && [ ! -L "$home/lima" ] && [ -d "$home/lima/_config" ]; then
    report "home_$account" pass "$home"
  else
    report "home_$account" fail "home or Lima home missing (or lima/ is a link)"
  fi
  # Lima writes _config/networks.yaml at the first start: the slot account owns it (0700), as in any Lima home (HOT-18)
  config=$(stat -f '%Su %Lp' "$home/lima/_config" 2>/dev/null)
  if [ "$config" = "$account 700" ]; then
    report "lima_config_$account" pass "$account 0700"
  else
    report "lima_config_$account" fail "owner and mode ${config:-none} (fix: workvm install)"
  fi
  i=$((i + 1))
done

if launchctl print system/swiss.dfx.agent-runner.pf 2>/dev/null | grep -q 'state = running'; then
  report watcher pass running
else
  report watcher fail "LaunchDaemon not running"
fi
sleep 5 # two watcher rounds
state=$(cat /var/run/agent.workvm.state 2>/dev/null)
case "$state" in
  ok\ *) report watcher_state pass "$state" ;;
  *) report watcher_state fail "state=${state:-none}" ;;
esac
if [ -n "$(pfctl -a "$anchor" -sr 2>/dev/null)" ]; then report anchor_loaded pass "$anchor"; else report anchor_loaded fail empty; fi
child=$(pfctl -a com.apple -sA 2>/dev/null | sed -n '/./{s/^ *//;p;q;}')
if [ "$child" = "$anchor" ]; then report anchor_first pass "$anchor"; else report anchor_first fail "first child ${child:-none}"; fi
first=$(pfctl -sr 2>/dev/null | grep '^anchor ' | head -n 1)
case "$first" in
  'anchor "com.apple/*"'*) report hook pass "com.apple/* first" ;;
  *) report hook fail "first anchor line: ${first:-none}" ;;
esac
if pfctl -s info 2>/dev/null | grep -q 'Status: Enabled'; then report pf pass enabled; else report pf fail disabled; fi

exit "$failed"
