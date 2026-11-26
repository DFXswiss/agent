#!/bin/sh
# Rehearsal of the work VM host boundary on a test device (docs/platform/spec/13-work-vm.md section 6.4, KOR-29
# operations finding 1). Run as root by the operator on a test device with the Cloudflare client installed and
# connected; no hub is needed. It never loads the main pf ruleset itself.
#
#   host-rehearsal.sh --runner-account NAME --site-ip ADDRESS [--prefix PREFIX] [--deadline-minutes N]
#                     [--log DIR] [--no-service-restart] [--vm NAME]
#
#   --runner-account      an existing unprivileged account (the sudoers rule names it; nothing runs as it)
#   --site-ip             the site's public IPv4 address (a slot must never leave with it)
#   --prefix              VM prefix of the rehearsal slot accounts (default `reh-`, accounts `reh-wvm00`)
#   --deadline-minutes    after N minutes (default 20) an armed timer uninstalls everything by itself, also when the
#                         operator's connection breaks; disarmed at the normal end
#   --log DIR             where the log and the saved state go (default /var/tmp/agent-workvm-rehearsal-<time>)
#   --no-service-restart  skip the restart of the client's system service (step 6)
#   --vm NAME             also run the continuous prober inside this running work VM of the rehearsal slot
#
# Steps: 1 save the state; 2 arm the timer; 3 `workvm install --plan`, then `install` with one slot; 4 check (main
# ruleset unchanged, client anchor still hooked, our anchor hooked before it, watcher state, `firewall check`,
# `egress-check.sh` for the slot); 5 disconnect and reconnect the client, checking fail-closed and recovery; 6 restart
# the client's service and check again; 7 `workvm uninstall` and compare with the saved state. One line per check
# `check=<name> result=pass|fail detail=...`; exit 0 only when every check passed. Environment: AGENT_RUNNER (default
# `agent-runner`), the command of the runner; EGRESS_CHECK (default: egress-check.sh next to this script).
set -u
trap '' HUP # a broken operator session must not stop the rehearsal (the timer below also survives it)
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin
export PATH

runner_account=
site_ip=
prefix=reh-
deadline=20
log=
restart=1
vm=
# Lima's user network maps this gateway address to the host's loopback (written from its octets: no address
# literals in the repository)
lima_gateway=$(printf '%d.%d.%d.%d' 192 168 5 2)
here=$(cd "$(dirname "$0")" && pwd)
runner=${AGENT_RUNNER:-agent-runner}
egress_check=${EGRESS_CHECK:-$here/egress-check.sh}
warp=${WARP_CLI:-/usr/local/bin/warp-cli}
service=com.cloudflare.1dot1dot1dot1.macos.warp.daemon

usage() {
  sed -n '6,15p' "$0" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --runner-account) runner_account=${2:-}; shift 2 ;;
    --site-ip) site_ip=${2:-}; shift 2 ;;
    --prefix) prefix=${2:-}; shift 2 ;;
    --deadline-minutes) deadline=${2:-}; shift 2 ;;
    --log) log=${2:-}; shift 2 ;;
    --no-service-restart) restart=0; shift ;;
    --vm) vm=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$runner_account" ] && [ -n "$site_ip" ] || usage
case "$deadline" in '' | *[!0-9]*) usage ;; esac
[ "$(id -u)" = 0 ] || { echo "host-rehearsal: run as root" >&2; exit 2; }
log=${log:-/var/tmp/agent-workvm-rehearsal-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$log" && chmod 0700 "$log"
home="$log/runner-home"
# the runner refuses a home that others can read (`config.check_home`); set it here, whatever mode mask the caller has
mkdir -p "$home" && chmod 0700 "$home"
account="${prefix}wvm00"
# Never on a device that already has an installation: the rehearsal's uninstall would remove it.
installed=
for path in /etc/sudoers.d/agent-runner-workvm /Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist \
  /etc/pf.anchors/agent.workvm /usr/local/libexec/agent-runner-workvm-limactl /var/agent-workvm; do
  [ -e "$path" ] && installed="$installed $path"
done
mount | grep -Eq ' on (/private)?/var/agent-workvm ' && installed="$installed volume"
[ -n "$(pfctl -a com.apple/000.agent.workvm -sr 2>/dev/null)" ] && installed="$installed anchor"
dscl . -list /Users | grep -q 'wvm[0-9][0-9]$' && installed="$installed slot-accounts"
if [ -n "$installed" ]; then
  echo "host-rehearsal: this device already has a work VM installation ($installed); use a test device without one" >&2
  exit 2
fi

# One slot account: no fixed number exists (ADR-075 addendum 2), the RAM above the reserve decides how many accounts
# `workvm install` creates, so the reserve leaves room for exactly one standard work VM (8192 MiB).
physical_mib=$(($(sysctl -n hw.memsize) / 1048576))
AGENT_RUNNER_HOME=$home AGENT_RUNNER_VM_PREFIX=$prefix AGENT_RUNNER_MEMORY_RESERVE_MIB=$((physical_mib - 8192))
export AGENT_RUNNER_HOME AGENT_RUNNER_VM_PREFIX AGENT_RUNNER_MEMORY_RESERVE_MIB

exec 3>&1 >>"$log/rehearsal.log" 2>&1
failed=0
report() { # name result detail
  line="check=$1 result=$2 detail=$3"
  echo "$line"
  echo "$line" >&3
  echo "$line" >>"$log/results.txt"
  [ "$2" = pass ] || failed=1
}
say() { echo "== $*"; }

hook_ok() { # 0 when com.apple/* is the first anchor line, no pass quick before it, and ours is its first child
  main=$(pfctl -sr 2>/dev/null)
  a=$(printf '%s\n' "$main" | grep -n '^anchor ' | head -n 1)
  p=$(printf '%s\n' "$main" | grep -n '^pass.* quick' | head -n 1 | cut -d: -f1)
  case "$a" in *:'anchor "com.apple/*"'*) ;; *) return 1 ;; esac
  if [ -n "$p" ] && [ "$p" -lt "${a%%:*}" ]; then return 1; fi
  child=$(pfctl -a com.apple -sA 2>/dev/null | sed -n '/./{s/^ *//;p;q;}')
  [ -z "$(pfctl -a com.apple/000.agent.workvm -sr 2>/dev/null)" ] || [ "$child" = com.apple/000.agent.workvm ]
}
client_hooked() { pfctl -sr 2>/dev/null | grep -q '^anchor "com.cloudflare'; }
connected() { "$warp" --accept-tos status 2>/dev/null | grep -q 'Status update: Connected'; }
wait_connected() { # seconds
  waited=0
  until connected || [ "$waited" -ge "$1" ]; do sleep 3; waited=$((waited + 3)); done
  connected
}
slot_internet() { sudo -n -u "$account" curl -s -m 10 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p'; }
main_rules() { pfctl -sr 2>/dev/null | grep -v '^anchor "com.cloudflare'; }

# 1. Save the state before anything changes.
say "1 state before"
# How pf takes the IPv6 entries of the table (the unspecified address `::` is not kept): load a probe table into a
# scratch child that sorts last and holds no rules, read it back, flush it.
printf 'table <wvm_probe> persist { ::, ::1 }\n' | pfctl -q -a com.apple/zzz.agent-workvm-probe -f - 2>/dev/null
pfctl -a com.apple/zzz.agent-workvm-probe -t wvm_probe -T show >"$log/pf-table-probe.txt" 2>/dev/null
pfctl -q -a com.apple/zzz.agent-workvm-probe -F all 2>/dev/null
# pfctl prints ALTQ notices on stderr: every saved pf state takes stdout only, like the comparisons later
pfctl -s info >"$log/before.info" 2>/dev/null
pfctl -sr >"$log/before.rules" 2>/dev/null
main_rules >"$log/before.main"
pfctl -sA >"$log/before.anchors" 2>/dev/null
pfctl -a com.cloudflare.warp_svc -sr 2>/dev/null | grep -c . >"$log/before.client-rules"
shasum -a 256 /etc/pf.conf >"$log/before.pf-conf"
dscl . -list /Users >"$log/before.users"
dscl . -list /Groups >"$log/before.groups"
if connected; then report client_connected pass "before"; else report client_connected fail "client not connected"; fi
if hook_ok; then report hook_before pass "com.apple/* before the client"; else report hook_before fail "no usable hook"; fi

# 2. Arm the timer: uninstall by itself after the deadline (a lost operator session never leaves the filter half set).
say "2 timer ${deadline} min"
# shellcheck disable=SC2016 # expanded by the inner shell from its arguments
nohup sh -c '
  trap "" HUP
  sleep "$1"
  echo "== timer expired: uninstall, reconnect the client"
  "$2" workvm uninstall
  "$3" --accept-tos connect
' timer "$((deadline * 60))" "$runner" "$warp" </dev/null >>"$log/timer.log" 2>&1 &
timer=$!
echo "$timer" >"$log/timer.pid"
disarm() { kill "$timer" 2>/dev/null; }

# 3. Plan, then install one slot.
say "3 install"
if "$runner" workvm install --runner-account "$runner_account" --quota-gib 20 --plan >"$log/plan.txt" 2>&1; then
  report plan pass "$(grep -c . "$log/plan.txt") lines"
else
  report plan fail "see plan.txt"
fi
if grep -q 'main_ruleset=untouched' "$log/plan.txt"; then report plan_main pass untouched; else report plan_main fail "plan touches the main ruleset"; fi
if "$runner" workvm install --runner-account "$runner_account" --quota-gib 20 >"$log/install.txt" 2>&1; then
  report install pass ok
else
  report install fail "see install.txt"
fi

# 4. Check the installed state.
check_all() { # phase
  sleep 12 # two watcher rounds
  if [ "$(main_rules)" = "$(cat "$log/before.main")" ]; then
    report "main_unchanged_$1" pass same
  else
    report "main_unchanged_$1" fail "main ruleset differs"
  fi
  if client_hooked; then report "client_hooked_$1" pass hooked; else report "client_hooked_$1" fail "client anchor gone"; fi
  if hook_ok; then report "hook_$1" pass "com.apple/* first"; else report "hook_$1" fail "our anchor not before the client"; fi
  state=$(cat /var/run/agent.workvm.state 2>/dev/null)
  case "$state" in
    ok\ utun*) report "watcher_$1" pass "$state" ;;
    *) report "watcher_$1" fail "state=${state:-none}" ;;
  esac
  if "$runner" workvm firewall check >"$log/check-$1.txt" 2>&1; then
    report "firewall_check_$1" pass "$(grep -o 'egress=[a-z0-9-]*' "$log/check-$1.txt" | head -1)"
  else
    report "firewall_check_$1" fail "see check-$1.txt"
    # what pf holds, for the diagnosis (rules with their counters and the table as pf prints them)
    { pfctl -a com.apple/000.agent.workvm -vsr; echo "== table"; pfctl -a com.apple/000.agent.workvm -t workvm_blocked -T show; } \
      >"$log/pf-$1.txt" 2>/dev/null
  fi
  "$egress_check" --account "$account" --site-ip "$site_ip" >"$log/egress-$1.txt" 2>&1
  bad=$(grep -c 'result=fail' "$log/egress-$1.txt")
  if [ "$bad" = 0 ]; then report "egress_$1" pass "$(grep -c 'result=pass' "$log/egress-$1.txt") checks"; else report "egress_$1" fail "$bad failed, see egress-$1.txt"; fi
  if curl -s -m 10 -o /dev/null https://cloudflare.com/cdn-cgi/trace; then report "device_$1" pass "device reaches the internet"; else report "device_$1" fail "device lost the internet"; fi
}
say "4 checks"
check_all installed
# the same checks the operator runs after a restart of the device (volume mount, homes, watcher, anchor, hook)
if "$here/after-reboot-check.sh" --accounts 1 --prefix "$prefix" >"$log/after-reboot-baseline.txt" 2>&1; then
  report reboot_check_baseline pass "$(grep -c 'result=pass' "$log/after-reboot-baseline.txt") checks"
else
  report reboot_check_baseline fail "see after-reboot-baseline.txt"
fi

# 4b. The continuous prober (images/workvm/continuous-probe.sh): from now until the uninstall, about every 100 ms,
# as the slot account (and with --vm inside that running work VM of the slot): a TCP port the device really listens
# on, over loopback and over the device's own site address, and the internet through Cloudflare's trace - a hit
# there only when it answers `warp=off` (past the client), in every phase, so no phase marker can lag. Every hit is
# a failure of the boundary; the root side saves at that moment the anchor's rules with counters, its table, the
# children of com.apple, the main ruleset and the client's anchor with counters, the matching pf states,
# `pfctl -s info`, the client's state and the watcher state.
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null >"$log/listeners.txt"
port=$(awk 'NR > 1 && ($9 ~ /^\*:[0-9]+$/ || $9 ~ /^127\.0\.0\.1:[0-9]+$/) { sub(/.*:/, "", $9); print $9; exit }' "$log/listeners.txt")
own=$(ipconfig getifaddr en0 2>/dev/null)
probe_any=$(awk -v p=":$port" 'NR > 1 && $9 == "*" p { print "yes"; exit }' "$log/listeners.txt")
site_target=$([ "$probe_any" = yes ] && echo "$own")
probe_script=/var/tmp/agent-workvm-continuous-probe.sh
install -m 0755 "$here/continuous-probe.sh" "$probe_script"
hits=0
snapshot() { # line
  hits=$((hits + 1))
  dir="$log/hit-$hits"
  mkdir -p "$dir"
  echo "$1" >"$dir/hit.txt"
  pfctl -a com.apple/000.agent.workvm -vsr >"$dir/anchor.txt" 2>/dev/null
  pfctl -a com.apple/000.agent.workvm -t workvm_blocked -T show >"$dir/table.txt" 2>/dev/null
  pfctl -a com.apple -sA >"$dir/children.txt" 2>/dev/null
  pfctl -vsr >"$dir/main.txt" 2>/dev/null
  pfctl -a com.cloudflare.warp_svc -vsr >"$dir/client.txt" 2>/dev/null
  pfctl -vss 2>/dev/null | grep -A2 -E "127\.0\.0\.1|${own:-none}" >"$dir/states.txt"
  pfctl -s info >"$dir/info.txt" 2>/dev/null
  "$warp" --accept-tos status >"$dir/client-status.txt" 2>/dev/null
  cat /var/run/agent.workvm.state >"$dir/watcher.txt" 2>/dev/null
}
watch_hits() { # source: read hit lines from stdin, snapshot each
  while read -r line; do
    case "$line" in
      hit\ *) echo "$1 $line" >>"$log/prober.txt"; snapshot "$1 $line" ;;
      done\ *) echo "$1 $line" >>"$log/prober.txt" ;;
    esac
  done
}
probers=
if [ -n "$port" ]; then
  sudo -n -u "$account" "$probe_script" --port "$port" --site-address "$site_target" 2>/dev/null | watch_hits slot &
  probers=$!
  if [ -n "$vm" ]; then
    # inside the VM, Lima's user network maps the gateway to the host's loopback
    # shellcheck disable=SC2024 # root reads the script and hands it to the VM's shell on stdin
    sudo -n -u "$account" /usr/local/libexec/agent-runner-workvm-limactl shell "$vm" -- sh -s -- \
      --port "$port" --loopback "$lima_gateway" --site-address "$site_target" <"$probe_script" 2>/dev/null | watch_hits vm &
    probers="$probers $!"
  fi
  report prober_started pass "port=$port site_address=$([ -n "$site_target" ] && echo yes || echo no-listener-on-it) vm=${vm:-none}"
else
  report prober_started fail "no TCP port listening on loopback (see listeners.txt)"
fi
stop_prober() {
  [ -n "$probers" ] || return 0
  pkill -KILL -u "$account" -f 'continuous-probe' 2>/dev/null
  # shellcheck disable=SC2086 # a list of process IDs
  kill $probers 2>/dev/null
  sleep 1
  for target in loopback site_address internet; do
    count=$(grep -c " hit $target " "$log/prober.txt" 2>/dev/null)
    first=$(grep -m 1 " hit $target " "$log/prober.txt" 2>/dev/null | sed -n 's/.* t=//p')
    if [ "${count:-0}" = 0 ]; then
      report "continuous_$target" pass "never reached"
    else
      report "continuous_$target" fail "$count hits, first at t=$first (snapshots hit-*)"
    fi
  done
  rm -f "$probe_script"
}

# 5. Disconnect: the slot has no internet; reconnect: it comes back by itself.
say "5 disconnect"
"$warp" --accept-tos disconnect >/dev/null 2>&1
sleep 10
leak=$(slot_internet)
if [ -z "$leak" ]; then report disconnected pass "no internet"; else report disconnected fail "internet ip=$leak"; fi
gw=$(route -n get default 2>/dev/null | awk '$1 == "gateway:" { print $2 }')
if [ -n "$gw" ] && sudo -n -u "$account" nc -z -G 3 -w 3 "$gw" 80 >/dev/null 2>&1; then report disconnected_lan fail "gateway reachable"; else report disconnected_lan pass blocked; fi
if sudo -n -u "$account" nc -z -G 3 -w 3 127.0.0.1 22 >/dev/null 2>&1; then report disconnected_loopback fail reachable; else report disconnected_loopback pass blocked; fi
if hook_ok || ! client_hooked; then report hook_disconnected pass "hook intact or client anchor removed"; else report hook_disconnected fail "client before our anchor"; fi
"$warp" --accept-tos connect >/dev/null 2>&1
if wait_connected 120; then report reconnect pass connected; else report reconnect fail "not connected after 120 s"; fi
check_all reconnected

# 6. Restart the client's service (as a self-update does) and check that the hook survives and the slot recovers.
if [ "$restart" = 1 ]; then
  say "6 service restart"
  launchctl kickstart -k "system/$service" >/dev/null 2>&1
  sleep 5
  leak=$(slot_internet)
  if [ -z "$leak" ]; then
    report restart_window pass "no internet while the client restarts"
  elif [ "$leak" = "$site_ip" ]; then
    report restart_window fail "left with the site address"
  elif hook_ok; then
    report restart_window pass "through the client ip=$leak"
  else
    report restart_window fail "internet without hook ip=$leak"
  fi
  if wait_connected 180; then report restart_reconnect pass connected; else report restart_reconnect fail "not connected after 180 s"; fi
    check_all restarted
fi

# 7. Uninstall and compare with the saved state.
say "7 uninstall"
stop_prober
if "$runner" workvm uninstall >"$log/uninstall.txt" 2>&1; then report uninstall pass ok; else report uninstall fail "see uninstall.txt"; fi
disarm
if [ "$(main_rules)" = "$(cat "$log/before.main")" ]; then report after_main pass same; else report after_main fail "main ruleset differs"; fi
if [ -z "$(pfctl -a com.apple/000.agent.workvm -sr 2>/dev/null)" ]; then report after_anchor pass empty; else report after_anchor fail "rules left"; fi
if pfctl -sA 2>/dev/null | cmp -s - "$log/before.anchors"; then report after_anchors pass same; else report after_anchors fail "anchor list differs (see before.anchors)"; fi
if dscl . -list /Groups | cmp -s - "$log/before.groups"; then report after_groups pass same; else report after_groups fail "groups differ"; fi
if shasum -a 256 /etc/pf.conf | cmp -s - "$log/before.pf-conf"; then report after_pf_conf pass same; else report after_pf_conf fail "pf.conf changed"; fi
if dscl . -list /Users | cmp -s - "$log/before.users"; then report after_users pass same; else report after_users fail "accounts differ"; fi
if launchctl print system/swiss.dfx.agent-runner.pf >/dev/null 2>&1; then report after_daemon fail "watcher still loaded"; else report after_daemon pass gone; fi
if client_hooked; then report after_client pass hooked; else report after_client fail "client anchor gone"; fi
client_rules=$(pfctl -a com.cloudflare.warp_svc -sr 2>/dev/null | grep -c .)
if [ "$client_rules" -gt 0 ]; then
  report after_client_rules pass "$client_rules rules (before: $(cat "$log/before.client-rules"))"
else
  report after_client_rules fail "client anchor empty"
fi

echo "== result failed=$failed log=$log"
echo "log: $log/rehearsal.log failed=$failed" >&3
exit "$failed"
