#!/bin/sh
# Egress checks of the work VM boundary on an execution device (docs/platform/spec/13-work-vm.md section 6.3).
#
# Run as root by the operator on the device after `agent-runner workvm install` and after the Cloudflare client is
# installed and enrolled. Traffic of the work VMs leaves the host from the slot accounts' sockets (Lima's user
# network runs in the slot account), so every probe runs as a slot account: what the probe sees is what a work VM
# sees. With --vm NAME the internet probes run inside that running work VM of the slot as well.
#
#   egress-check.sh --account ACCOUNT --site-ip ADDRESS [--site-ip6 ADDRESS] [--vm NAME] [--disconnect-test]
#
#   --account          a slot account (e.g. <prefix>wvm00)
#   --site-ip          the site's public IPv4 address; the work VM must never leave with it
#   --site-ip6         the site's public IPv6 address or prefix start (optional), checked like --site-ip
#   --vm               a running work VM of that slot (optional; probes from inside the VM too)
#   --disconnect-test  disconnect the Cloudflare client for about 20 seconds (the whole device's tunnel!) and check
#                      that the slot has no internet meanwhile; reconnects afterwards
#
# Output: one line per check `check=<name> result=pass|fail detail=...`; exit 0 only when every check passed.
# Prints only addresses and codes (no tokens, no credentials).
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin
export PATH

account=
site_ip=
site_ip6=
vm=
disconnect=0
wrapper=/usr/local/libexec/agent-runner-workvm-limactl
warp=${WARP_CLI:-'/Applications/Cloudflare WARP.app/Contents/Resources/warp-cli'}
trace=https://cloudflare.com/cdn-cgi/trace

usage() {
  sed -n '9,17p' "$0" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --account) account=${2:-}; shift 2 ;;
    --site-ip) site_ip=${2:-}; shift 2 ;;
    --site-ip6) site_ip6=${2:-}; shift 2 ;;
    --vm) vm=${2:-}; shift 2 ;;
    --disconnect-test) disconnect=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$account" ] && [ -n "$site_ip" ] || usage
[ "$(id -u)" = 0 ] || { echo "egress-check: run as root" >&2; exit 2; }
id -u "$account" >/dev/null 2>&1 || { echo "egress-check: no such account" >&2; exit 2; }

failed=0
report() { # name result detail
  echo "check=$1 result=$2 detail=$3"
  [ "$2" = pass ] || failed=1
}

as_slot() { sudo -n -u "$account" "$@"; }
in_vm() { sudo -n -u "$account" "$wrapper" shell "$vm" -- "$@"; }

trace_ip() { # command prefix...: prints the ip= of Cloudflare's trace or nothing
  "$@" curl -s -m 15 "$trace" 2>/dev/null | sed -n 's/^ip=//p'
}

egress_ok() { # name ip
  if [ -z "$2" ]; then
    report "$1" fail "no answer"
  elif [ "$2" = "$site_ip" ]; then
    report "$1" fail "left with the site address"
  else
    report "$1" pass "ip=$2"
  fi
}

# 1. The device's own traffic goes through the client (precondition, not a work VM property).
if "$warp" status 2>/dev/null | grep -q 'Status update: Connected'; then
  report client pass connected
else
  report client fail "warp-cli status is not connected"
fi
egress_ok device_egress "$(trace_ip)"

# 2. The watcher has loaded the internet rule on the tunnel interface.
egress_if=$(pfctl -a agent.workvm -sr 2>/dev/null | sed -n 's/^pass out quick on \(utun[0-9]*\) .*/\1/p' | head -1)
if [ -n "$egress_if" ]; then report anchor pass "egress=$egress_if"; else report anchor fail "no egress rule"; fi

# 3. A slot account leaves with a Cloudflare address, IPv4, IPv6 (when offered) and HTTP/3.
egress_ok slot_egress "$(trace_ip as_slot)"
ipify=$(as_slot curl -s -m 15 https://api.ipify.org 2>/dev/null)
egress_ok slot_ipify "$ipify"
ipv6=$(as_slot curl -s -6 -m 15 https://api64.ipify.org 2>/dev/null)
if [ -z "$ipv6" ]; then
  report slot_ipv6 pass "no IPv6 egress"
elif [ -n "$site_ip6" ] && case "$ipv6" in "$site_ip6"*) true ;; *) false ;; esac; then
  report slot_ipv6 fail "left with the site IPv6 address"
else
  report slot_ipv6 pass "ip=$ipv6"
fi
h3=$(as_slot curl --http3-only -s -m 15 "$trace" 2>/dev/null | sed -n 's/^ip=//p')
if [ -n "$h3" ]; then egress_ok slot_http3 "$h3"; else report slot_http3 pass "no HTTP/3 answer (curl or path without QUIC)"; fi

# 4. Mail port 25 is closed, the local network, the host and the runner's loopback are unreachable.
smtp=$(dig +short MX gmail.com 2>/dev/null | sort -n | awk 'NR == 1 { print $2 }')
if as_slot nc -z -G 5 -w 5 "${smtp:-gmail-smtp-in.l.google.com}" 25 >/dev/null 2>&1; then
  report port25 fail "port 25 reachable"
else
  report port25 pass "blocked"
fi
lan=$(route -n get default 2>/dev/null | awk '$1 == "gateway:" { print $2 }')
own=$(ipconfig getifaddr en0 2>/dev/null)
for target in ${lan:-} ${own:-}; do
  if as_slot nc -z -G 3 -w 3 "$target" 80 >/dev/null 2>&1 || as_slot nc -z -G 3 -w 3 "$target" 22 >/dev/null 2>&1; then
    report "lan_$target" fail "reachable"
  else
    report "lan_$target" pass "blocked"
  fi
done
if as_slot nc -z -G 3 -w 3 127.0.0.1 22 >/dev/null 2>&1; then
  report loopback fail "host loopback reachable"
else
  report loopback pass "blocked"
fi

# 5. Optionally the same from inside a running work VM.
if [ -n "$vm" ]; then
  vm_ip=$(in_vm curl -s -m 15 "$trace" 2>/dev/null | sed -n 's/^ip=//p')
  egress_ok vm_egress "$vm_ip"
  if in_vm sh -c "nc -z -w 5 ${smtp:-gmail-smtp-in.l.google.com} 25" >/dev/null 2>&1; then
    report vm_port25 fail "port 25 reachable"
  else
    report vm_port25 pass "blocked"
  fi
fi

# 6. Client disconnected: the slot has no internet (fail-closed), never the site address.
if [ "$disconnect" = 1 ]; then
  "$warp" disconnect >/dev/null 2>&1
  sleep 10
  leaked=$(trace_ip as_slot)
  if [ -n "$leaked" ]; then
    report disconnected fail "internet while disconnected ip=$leaked"
  else
    report disconnected pass "no internet"
  fi
  "$warp" connect >/dev/null 2>&1
  sleep 10
  egress_ok reconnected "$(trace_ip as_slot)"
fi

exit "$failed"
