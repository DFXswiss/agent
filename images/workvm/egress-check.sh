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
warp=${WARP_CLI:-/usr/local/bin/warp-cli}
anchor=com.apple/000.agent.workvm
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
report() { # name result detail (`info` is shown and does not fail the run)
  echo "check=$1 result=$2 detail=$3"
  [ "$2" = pass ] || [ "$2" = info ] || failed=1
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
connected() { "$warp" --accept-tos status 2>/dev/null | grep -q 'Status update: Connected'; }
if connected; then
  report client pass connected
else
  report client fail "warp-cli status is not connected"
fi
egress_ok device_egress "$(trace_ip)"

# 2. The anchor is effective: pf on, Apple's hook in the main ruleset before the client's anchor (our child anchor
#    is evaluated first), the client's anchor still hooked, and the watcher has loaded the rule on the tunnel.
if pfctl -s info 2>/dev/null | grep -q 'Status: Enabled'; then report pf pass enabled; else report pf fail disabled; fi
main=$(pfctl -sr 2>/dev/null)
first_anchor=$(printf '%s\n' "$main" | grep -n '^anchor ' | head -n 1)
first_pass=$(printf '%s\n' "$main" | grep -n '^pass.* quick' | head -n 1 | cut -d: -f1)
client=$(printf '%s\n' "$main" | grep -n '^anchor "com.cloudflare' | head -n 1 | cut -d: -f1)
case "$first_anchor" in
  *:'anchor "com.apple/*"'*)
    if [ -n "$first_pass" ] && [ "$first_pass" -lt "${first_anchor%%:*}" ]; then
      report hook fail "a pass quick rule comes before com.apple/*"
    else
      report hook pass "com.apple/* is the first anchor"
    fi
    ;;
  *) report hook fail "com.apple/* is not the first anchor of the main ruleset" ;;
esac
child=$(pfctl -a com.apple -sA 2>/dev/null | sed -n '/./{s/^ *//;p;q;}')
if [ "$child" = "$anchor" ]; then report first_child pass "$anchor"; else report first_child fail "first child is ${child:-none}"; fi
if [ -n "$client" ]; then report client_anchor pass "line $client"; else report client_anchor fail "client anchor not hooked"; fi
egress_if=$(pfctl -a "$anchor" -sr 2>/dev/null | sed -n 's/^pass out quick on \(utun[0-9]*\) .*/\1/p' | head -1)
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

# UDP to the site network (DNS of the gateway): pf's `user` matches UDP, the slot rule refuses it at once.
if [ -n "${lan:-}" ] && as_slot dig "@$lan" +time=2 +tries=1 +short example.com >/dev/null 2>&1; then
  report lan_udp fail "UDP to the gateway answered"
else
  report lan_udp pass "blocked"
fi
# ICMP: pf's `user` does not match ICMP and the Cloudflare client passes the private networks, so the boundary for
# ICMP is that no slot process sends it: Lima's user network (gvisor-tap-vsock) does not forward a VM's ICMP out of
# its virtual network. A ping of the slot account itself goes through an unprivileged ICMP socket that pf cannot
# attribute; it is reported for information only (no process of a slot account pings; the VM check below counts).
if [ -n "${lan:-}" ] && as_slot ping -c 1 -t 3 "$lan" >/dev/null 2>&1; then
  report lan_icmp_slot_socket info "a slot account's own ICMP socket reaches the gateway (not used by Lima)"
else
  report lan_icmp_slot_socket pass "no answer"
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
  # the tunnel's MTU (1300 on the device): a large transfer must complete
  size=$(in_vm curl -s -m 120 -o /dev/null -w '%{size_download}' 'https://speed.cloudflare.com/__down?bytes=50000000' 2>/dev/null)
  if [ "${size:-0}" = 50000000 ]; then report vm_large pass "50 MB"; else report vm_large fail "got ${size:-0} bytes"; fi
  # ICMP and UDP from inside the VM to the site network. A documentation address (never routed) that answers means
  # the user network fakes echo replies; then a gateway answer says nothing and the check passes as `fake`.
  if in_vm ping -c 1 -W 3 192.0.2.1 >/dev/null 2>&1; then
    report vm_lan_icmp pass "fake: the user network answers every echo request"
  elif [ -n "${lan:-}" ] && in_vm ping -c 1 -W 3 "$lan" >/dev/null 2>&1; then
    report vm_lan_icmp fail "the gateway answers ICMP from the VM"
  else
    report vm_lan_icmp pass "no answer"
  fi
  if [ -n "${lan:-}" ] && in_vm sh -c "nc -z -w 3 $lan 80 || nc -z -w 3 $lan 22" >/dev/null 2>&1; then
    report vm_lan_tcp fail "the gateway answers TCP from the VM"
  else
    report vm_lan_tcp pass "blocked"
  fi
  if [ -n "${lan:-}" ] && in_vm sh -c "dig @$lan +time=2 +tries=1 +short example.com" >/dev/null 2>&1; then
    report vm_lan_udp fail "the gateway answers UDP from the VM"
  else
    report vm_lan_udp pass "blocked"
  fi
fi

# 6. Client disconnected: the slot has no internet (fail-closed), never the site address.
if [ "$disconnect" = 1 ]; then
  "$warp" --accept-tos disconnect >/dev/null 2>&1
  sleep 10
  leaked=$(trace_ip as_slot)
  if [ -n "$leaked" ]; then
    report disconnected fail "internet while disconnected ip=$leaked"
  else
    report disconnected pass "no internet"
  fi
  "$warp" --accept-tos connect >/dev/null 2>&1
  waited=0
  until connected || [ "$waited" -ge 90 ]; do sleep 3; waited=$((waited + 3)); done
  sleep 12 # the watcher loads the pass rule within two of its rounds
  egress_ok reconnected "$(trace_ip as_slot)"
fi

exit "$failed"
