#!/bin/sh
# Attack probes from inside a work VM (docs/platform/atk/WVM.md, docs/platform/spec/13-work-vm.md section 11).
#
# Run as root inside a running work VM: copied in and started through the slot wrapper (`limactl shell`) or through
# the session's `workvm_exec`. Every case tries to leave the VM in one direction and passes when the attempt is
# refused. The host side (request abuse, the host filter itself) is checked by the runner tests and
# images/workvm/egress-check.sh.
#
#   attack-probes.sh --slot-ports P1,P2,... [--peer IP]... [--host-ip IP]... [--site-ip IP] [--fill-gib N]
#                    [--cases WVM-NN,...] [--list-cases] [--print-ports]
#
#   --slot-ports   the pinned SSH ports of all slots of the device, the own slot first (excluded from the probes)
#   --peer         another work VM or workgroup VM address, when known (repeatable)
#   --host-ip      an address of the host the operator knows (LAN, public, IPv6; repeatable)
#   --site-ip      the site's public address; the VM must never leave with it (WVM-10)
#   --fill-gib     WVM-07: allocate a file of N GiB (default: skip the fill)
#   --cases        run only these cases (default: all)
#   --list-cases   print the cases and exit
#   --print-ports  print the host ports of WVM-01 and exit
#
# Output: one line `case=WVM-NN name=... result=pass|fail|skip|info detail=...` per case (pass = the attack was
# refused); WVM-06 adds `case=WVM-06 item=... status=expected|found ...` lines. Exit 0 when no case failed, 1 when one
# did, 2 on a usage error. Prints only addresses, ports, codes, file paths and match counts, never matched content.
#
# WVM_PROBE_ROOT (hermetic tests only): an existing directory that prefixes every path of the credential search; the
# probe then runs without root and without mounting anything.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin
export PATH
LC_ALL=C
export LC_ALL

CASES="WVM-01 WVM-02 WVM-03 WVM-04 WVM-05 WVM-06 WVM-07 WVM-08 WVM-09 WVM-10"
# Lima's user network (gvisor-tap-vsock): the guest's subnet, host octet 2 = host.lima.internal (the host's
# loopback). Addresses are assembled from prefixes (repository hygiene rule: no private address literals).
LIMA_NET=192.168.5
LIMA_HOST=$LIMA_NET.2
VZ_NAT=192.168.64
LINK_LOCAL=169.254.169
METADATA=$LINK_LOCAL.254
# Typical LAN gateways and the CGNAT ranges (Cloudflare's private device range among them).
LAN_PREFIXES="192.168.0 192.168.1 192.168.178 10.0.0 10.0.1 172.16.0 100.64.0 100.96.0"
# Host services a work VM must never reach: SSH, web, the runner's PostgreSQL, screen sharing, the hub and the
# session sandbox port, common development servers.
HOST_PORTS="22 80 443 3000 5000 5432 5900 6379 7700 8000 8001 8080 8443 9000"
TEST_NET=192.0.2.1
TRACE=https://cloudflare.com/cdn-cgi/trace

usage() {
  sed -n '9,19p' "$0" >&2
  exit 2
}

valid_port() {
  case "$1" in '' | *[!0-9]*) return 1 ;; esac
  [ "${#1}" -le 5 ] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

valid_ip() {
  case "$1" in '' | *[!0-9A-Fa-f.:]*) return 1 ;; esac
  [ "${#1}" -le 45 ]
}

slot_ports=
peers=
host_ips=
site_ip=
fill_gib=
selected=
list_cases=0
print_ports=0
while [ $# -gt 0 ]; do
  case "$1" in
    --slot-ports) [ $# -ge 2 ] || usage; slot_ports=$2; shift 2 ;;
    --peer) [ $# -ge 2 ] && valid_ip "$2" || usage; peers="$peers $2"; shift 2 ;;
    --host-ip) [ $# -ge 2 ] && valid_ip "$2" || usage; host_ips="$host_ips $2"; shift 2 ;;
    --site-ip) [ $# -ge 2 ] && valid_ip "$2" || usage; site_ip=$2; shift 2 ;;
    --fill-gib) [ $# -ge 2 ] && valid_port "$2" || usage; fill_gib=$2; shift 2 ;;
    --cases) [ $# -ge 2 ] && [ -n "$2" ] || usage; selected=$(echo "$2" | tr ',' ' '); shift 2 ;;
    --list-cases) list_cases=1; shift ;;
    --print-ports) print_ports=1; shift ;;
    *) usage ;;
  esac
done

if [ "$list_cases" = 1 ]; then
  echo "WVM-01 host-loopback"
  echo "WVM-02 host-addresses"
  echo "WVM-03 private-ranges"
  echo "WVM-04 other-vms"
  echo "WVM-05 session-sandbox"
  echo "WVM-06 credentials"
  echo "WVM-07 disk-fill"
  echo "WVM-08 request-abuse"
  echo "WVM-09 user-network-services"
  echo "WVM-10 egress"
  exit 0
fi

[ -n "$slot_ports" ] || usage
own_port=
other_ports=
for p in $(echo "$slot_ports" | tr ',' ' '); do
  valid_port "$p" || usage
  if [ -z "$own_port" ]; then own_port=$p; else other_ports="$other_ports $p"; fi
done
[ -n "$own_port" ] || usage
for c in $selected; do
  case " $CASES " in *" $c "*) ;; *) usage ;; esac
done
[ -n "$selected" ] || selected=$CASES

# The host ports of WVM-01: the fixed list and every other slot's SSH port, never the own slot's port (it leads into
# this VM's own sshd through Lima's host agent and is the one loopback target the filter allows).
probe_ports() {
  for p in $HOST_PORTS $other_ports; do
    [ "$p" = "$own_port" ] || echo "$p"
  done | awk '!seen[$0]++' | tr '\n' ' ' | sed 's/ $//'
}
if [ "$print_ports" = 1 ]; then
  probe_ports
  echo
  exit 0
fi

ROOT=${WVM_PROBE_ROOT:-}
if [ -n "$ROOT" ]; then
  case "$ROOT" in /*) ;; *) echo "attack-probes: WVM_PROBE_ROOT must be absolute" >&2; exit 2 ;; esac
  [ -d "$ROOT" ] || { echo "attack-probes: WVM_PROBE_ROOT is not a directory" >&2; exit 2; }
elif [ "$(id -u)" != 0 ]; then
  echo "attack-probes: run as root" >&2
  exit 2
fi

failed=0
report() { # case name result detail
  echo "case=$1 name=$2 result=$3 detail=$4"
  [ "$3" != fail ] || failed=1
}

has() { command -v "$1" >/dev/null 2>&1; }

# Every probe is bounded: `timeout` (coreutils, always in the work image) when present.
bounded() { # seconds command...
  b_secs=$1
  shift
  if has timeout; then timeout "$b_secs" "$@"; else "$@"; fi
}

tcp_open() { # host port: 0 when a TCP connection was established
  bounded 5 nc -z -w 3 "$1" "$2" >/dev/null 2>&1
}

# Open host:port pairs among the targets and ports, as a comma list (empty when every connection was refused).
open_pairs() { # "targets" "ports"
  o_found=
  for o_t in $1; do
    for o_p in $2; do
      if tcp_open "$o_t" "$o_p"; then o_found="$o_found,$o_t:$o_p"; fi
    done
  done
  echo "${o_found#,}"
}

count_words() { echo "$#"; }

unique() { # words...: each once, in order
  for u_w in "$@"; do echo "$u_w"; done | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ $//'
}

own_addresses() {
  ip -o addr show 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }'
}

is_own() { # address: 0 when it is one of this VM's own addresses
  own_addresses | grep -qxF "$1"
}

gateway() {
  ip -4 route show default 2>/dev/null | awk '$1 == "default" { print $3; exit }'
}

http_code() { # url [curl options...]: the HTTP status, 000 when nothing answered
  h_url=$1
  shift
  h_out=$(bounded 8 curl -s -o /dev/null -m 5 -w '%{http_code}' "$@" "$h_url" 2>/dev/null)
  echo "${h_out:-000}"
}

# ICMP is not matched by the host filter's `user` rules (pf matches users for TCP and UDP only). A reply from a
# documentation address shows a user-network stack that answers every ping itself; then ICMP results say nothing.
icmp_fake=unknown
icmp_check() {
  if [ "$icmp_fake" = unknown ]; then
    if bounded 5 ping -n -c 1 -W 2 "$TEST_NET" >/dev/null 2>&1; then icmp_fake=yes; else icmp_fake=no; fi
  fi
}
icmp_answers() { # targets: comma list of targets that answer a ping
  icmp_check
  [ "$icmp_fake" = no ] || return 0
  i_found=
  for i_t in $1; do
    if bounded 5 ping -n -c 1 -W 2 "$i_t" >/dev/null 2>&1; then i_found="$i_found,$i_t"; fi
  done
  echo "${i_found#,}"
}

# -- WVM-01 host loopback through host.lima.internal ------------------------------------------------------------------
wvm01() {
  hosts=$LIMA_HOST
  named=$(getent hosts host.lima.internal 2>/dev/null | awk '{ print $1; exit }')
  [ -z "$named" ] || [ "$named" = "$LIMA_HOST" ] || hosts="$hosts $named"
  ports=$(probe_ports)
  found=$(open_pairs "$hosts" "$ports")
  swept=
  for h in $hosts; do
    for p in $(bounded 600 nc -z -v -w 1 "$h" 1-1024 2>&1 | sed -n 's/^Connection to [^ ]* \([0-9]*\) port .*succeeded.*/\1/p'); do
      swept="$swept,$h:$p"
    done
  done
  open="${found}${swept}"
  open=${open#,}
  if [ -z "$open" ]; then
    report WVM-01 host-loopback pass "hosts=$(echo $hosts | tr ' ' ',') ports=$(count_words $ports)+sweep:1-1024 open=none"
  else
    report WVM-01 host-loopback fail "open=$open"
  fi
}

# -- WVM-02 the host's own addresses ------------------------------------------------------------------------------------
# From inside, the VM only sees Lima's virtual network: the default gateway and the ARP neighbours are addresses of
# the user network (the host agent), never the host's LAN or public address. Those are probed when the operator
# names them with --host-ip.
wvm02() {
  gw=$(gateway)
  neigh=$(ip -4 neigh show 2>/dev/null | awk '{ print $1 }')
  targets=
  for t in $gw $neigh $LIMA_NET.1 $LIMA_NET.3 $host_ips; do
    [ "$t" = "$LIMA_HOST" ] && continue
    is_own "$t" && continue
    case " $targets " in *" $t "*) ;; *) targets="$targets $t" ;; esac
  done
  ports="$HOST_PORTS $other_ports"
  found=$(open_pairs "$targets" "$ports")
  given=$(count_words $host_ips)
  if [ -n "$found" ]; then
    report WVM-02 host-addresses fail "open=$found"
  elif [ "$given" = 0 ]; then
    report WVM-02 host-addresses pass "targets=$(echo $targets | tr ' ' ',') open=none host-ip=not-given(LAN address not discoverable from inside)"
  else
    report WVM-02 host-addresses pass "targets=$(echo $targets | tr ' ' ',') open=none"
  fi
}

# -- WVM-03 private, CGNAT and link-local ranges, the metadata address --------------------------------------------------
wvm03() {
  targets="$METADATA"
  for n in $LAN_PREFIXES; do targets="$targets $n.1"; done
  found=$(open_pairs "$targets" "22 53 80 443")
  meta=$(http_code "http://$METADATA/latest/meta-data/")
  [ "$meta" = 000 ] || found="${found:+$found,}metadata-http:$meta"
  for n in 192.168.0 192.168.1 10.0.0; do
    if bounded 6 dig +time=2 +tries=1 +short "@$n.1" example.com A 2>/dev/null | grep -q '^[0-9]'; then
      found="${found:+$found,}dns:$n.1"
    fi
  done
  pings=$(icmp_answers "$targets")
  [ -z "$pings" ] || found="${found:+$found,}icmp:$pings"
  if [ -n "$found" ]; then
    report WVM-03 private-ranges fail "reached=$found icmp-fake=$icmp_fake"
  else
    report WVM-03 private-ranges pass "targets=$(count_words $targets) reached=none icmp-fake=$icmp_fake"
  fi
}

# -- WVM-04 other VMs -------------------------------------------------------------------------------------------------
wvm04() {
  targets=$peers
  i=1
  while [ "$i" -le 10 ]; do
    targets="$targets $VZ_NAT.$i"
    i=$((i + 1))
  done
  found=$(open_pairs "$targets" "22 80 443 7700 8080")
  if [ -n "$found" ]; then
    report WVM-04 other-vms fail "open=$found"
  else
    report WVM-04 other-vms pass "peers=$(count_words $peers) targets=$(count_words $targets) open=none"
  fi
}

# -- WVM-05 the session sandbox port on every address the VM might reach ----------------------------------------------
wvm05() {
  targets="$LIMA_HOST $LIMA_NET.1 $LIMA_NET.3 $(gateway) $peers $host_ips"
  i=1
  while [ "$i" -le 10 ]; do
    targets="$targets $VZ_NAT.$i"
    i=$((i + 1))
  done
  for n in $LAN_PREFIXES; do targets="$targets $n.1"; done
  targets=$(unique $targets)
  found=$(open_pairs "$targets" 7700)
  if [ -n "$found" ]; then
    report WVM-05 session-sandbox fail "open=$found"
  else
    report WVM-05 session-sandbox pass "targets=$(count_words $targets) port=7700 open=none"
  fi
}

# -- WVM-06 credentials placed by the platform ------------------------------------------------------------------------
# Patterns of tokens and keys; only counts and paths are printed, never a match.
SECRET_RE='gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|sk-or-[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY|(^|[^A-Za-z0-9_])AGENT_[A-Z0-9_]+=|(aws|AWS)_(access_key_id|secret_access_key|session_token|ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|://[^/:@ ]+:[^/@ ]+@'
self_path=$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")
found_files=0
found_matches=0
items=0

item() { # label status path [matches]
  items=$((items + 1))
  if [ "$items" -le 50 ]; then
    echo "case=WVM-06 item=$1 status=$2 path=$3${4:+ matches=$4}"
  fi
}

found_item() { # label path matches
  found_files=$((found_files + 1))
  found_matches=$((found_matches + $3))
  item "$1" found "$2" "$3"
}

search_tree() { # label root: every small text file below root with a match
  [ -e "$2" ] || return 0
  bounded 300 find "$2" -xdev -type f -size -2048 -exec grep -IlE "$SECRET_RE" {} + 2>/dev/null |
    while IFS= read -r s_file; do
      [ "$s_file" = "$self_path" ] && continue
      s_count=$(grep -cE "$SECRET_RE" "$s_file" 2>/dev/null)
      echo "$s_file	${s_count:-0}"
    done >"$scratch/hits"
  while IFS='	' read -r s_file s_count; do
    found_item "$1" "${s_file#"$ROOT"}" "$s_count"
  done <"$scratch/hits"
}

search_environ() { # every process environment, PID 1 first
  for e_file in "$ROOT"/proc/1/environ "$ROOT"/proc/[0-9]*/environ; do
    [ -r "$e_file" ] || continue
    e_count=$(tr '\000' '\n' <"$e_file" 2>/dev/null | grep -cE "$SECRET_RE")
    [ "${e_count:-0}" -gt 0 ] || continue
    case " $seen_environ " in *" $e_file "*) continue ;; esac
    seen_environ="$seen_environ $e_file"
    found_item environ "${e_file#"$ROOT"}" "$e_count"
  done
}

expected_items() { # Lima's own pieces: present by design, not credentials of the platform
  for x in "$ROOT"/mnt/lima-cidata/lima-guestagent "$ROOT"/usr/local/bin/lima-guestagent; do
    [ -e "$x" ] && item lima-guestagent expected "${x#"$ROOT"}"
  done
  for x in "$ROOT"/home/*/.ssh/authorized_keys "$ROOT"/root/.ssh/authorized_keys; do
    [ -e "$x" ] && item lima-ssh-key expected "${x#"$ROOT"}"
  done
  return 0
}

param_env() { # Lima parameters: the template has none, so the file must be absent or empty
  for x in "$@"; do
    [ -f "$x" ] || continue
    p_count=$(grep -cvE '^[[:space:]]*(#|$)' "$x" 2>/dev/null)
    if [ "${p_count:-0}" -gt 0 ]; then
      found_item lima-param "${x#"$ROOT"}" "$p_count"
    fi
  done
}

wvm06() {
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/wvm06.XXXXXX") || { report WVM-06 credentials fail "no temporary directory"; return; }
  seen_environ=
  mounted=
  if [ -z "$ROOT" ] && has blkid; then
    dev=$(blkid -L cidata 2>/dev/null)
    if [ -n "$dev" ]; then
      mkdir -p "$scratch/cidata"
      if bounded 20 mount -o ro "$dev" "$scratch/cidata" 2>/dev/null; then mounted=$scratch/cidata; fi
    fi
  fi
  expected_items
  search_environ
  param_env "$ROOT"/mnt/lima-cidata/param.env ${mounted:+"$mounted/param.env"}
  search_tree cidata "$ROOT"/mnt/lima-cidata
  [ -z "$mounted" ] || search_tree cidata-iso "$mounted"
  search_tree etc-environment "$ROOT"/etc/environment
  search_tree etc-environment.d "$ROOT"/etc/environment.d
  search_tree etc-profile.d "$ROOT"/etc/profile.d
  search_tree sudoers.d "$ROOT"/etc/sudoers.d
  search_tree root-home "$ROOT"/root
  search_tree homes "$ROOT"/home
  search_tree cloud "$ROOT"/var/lib/cloud
  for r in "$ROOT"/run/lima-*; do search_tree lima-run "$r"; done
  if [ -n "$mounted" ]; then bounded 20 umount "$mounted" 2>/dev/null; fi
  rm -rf "$scratch"
  iso=not-mounted
  [ -z "$mounted" ] || iso=mounted
  if [ "$found_files" -gt 0 ]; then
    report WVM-06 credentials fail "files=$found_files matches=$found_matches cidata-iso=$iso"
  else
    report WVM-06 credentials pass "files=0 matches=0 cidata-iso=$iso"
  fi
}

# -- WVM-07 disk exhaustion is bounded by the VM's disk -----------------------------------------------------------------
wvm07() {
  if [ -z "$fill_gib" ]; then
    report WVM-07 disk-fill skip "no --fill-gib"
    return
  fi
  size_k=$(df -Pk / | awk 'NR == 2 { print $2 }')
  avail_k=$(df -Pk / | awk 'NR == 2 { print $4 }')
  size_g=$((size_k / 1048576))
  avail_g=$((avail_k / 1048576))
  file=/var/tmp/wvm07-fill
  if bounded 600 fallocate -l "${fill_gib}G" "$file" 2>/dev/null; then outcome=allocated; else outcome=refused; fi
  rm -f "$file"
  detail="fs-gib=$size_g avail-gib=$avail_g requested-gib=$fill_gib outcome=$outcome"
  if [ "$outcome" = allocated ] && [ "$fill_gib" -gt "$size_g" ]; then
    report WVM-07 disk-fill fail "$detail"
  else
    report WVM-07 disk-fill pass "$detail"
  fi
}

wvm08() {
  report WVM-08 request-abuse skip "host-side, see runner tests"
}

# -- WVM-09 gvisor-tap-vsock services of the user network -------------------------------------------------------------
wvm09() {
  me=$(ip -4 -o addr show scope global 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }')
  found=
  for t in $(unique $LIMA_NET.1 $LIMA_HOST $LIMA_NET.3 $(gateway)); do
    for path in /services/forwarder/all /services/dhcp/leases; do
      code=$(http_code "http://$t$path")
      [ "$code" = 000 ] || found="$found,$t:80$path=$code"
    done
    code=$(http_code "http://$t/services/forwarder/expose" -X POST -H 'Content-Type: application/json' \
      -d "{\"local\":\":47999\",\"remote\":\"${me:-$LIMA_NET.15}:22\",\"protocol\":\"tcp\"}")
    if [ "$code" != 000 ]; then
      found="$found,$t:80/services/forwarder/expose=$code"
      http_code "http://$t/services/forwarder/unexpose" -X POST -H 'Content-Type: application/json' \
        -d '{"local":":47999","protocol":"tcp"}' >/dev/null
    fi
  done
  found=${found#,}
  if [ -n "$found" ]; then
    report WVM-09 user-network-services fail "answered=$found"
  else
    report WVM-09 user-network-services pass "answered=none"
  fi
}

# -- WVM-10 egress through Cloudflare, never the site address, no port 25, DNS works -----------------------------------
wvm10() {
  ip4=$(bounded 20 curl -s -4 -m 15 "$TRACE" 2>/dev/null | sed -n 's/^ip=//p')
  ip6=$(bounded 20 curl -s -6 -m 15 "$TRACE" 2>/dev/null | sed -n 's/^ip=//p')
  resolvers=$(awk '$1 == "nameserver" { print $2 }' /etc/resolv.conf 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  if bounded 10 dig +time=3 +tries=1 +short example.com A 2>/dev/null | grep -q '^[0-9]'; then dns=ok; else dns=none; fi
  mx=$(bounded 10 dig +time=3 +tries=1 +short MX gmail.com 2>/dev/null | sort -n | awk 'NR == 1 { print $2 }')
  if tcp_open "${mx:-gmail-smtp-in.l.google.com}" 25; then smtp=open; else smtp=blocked; fi
  detail="ip4=${ip4:-none} ip6=${ip6:-none} dns=$dns resolvers=${resolvers:-none} port25=$smtp"
  if [ "$smtp" = open ]; then
    report WVM-10 egress fail "$detail"
  elif [ -n "$site_ip" ] && { [ "$ip4" = "$site_ip" ] || [ "$ip6" = "$site_ip" ]; }; then
    report WVM-10 egress fail "$detail site=left-with-site-address"
  elif [ -z "$ip4$ip6" ]; then
    report WVM-10 egress info "$detail internet=none(fail-closed or client down)"
  elif [ "$dns" != ok ]; then
    report WVM-10 egress fail "$detail"
  elif [ -z "$site_ip" ]; then
    report WVM-10 egress info "$detail site-ip=not-given"
  else
    report WVM-10 egress pass "$detail"
  fi
}

for c in $selected; do
  case "$c" in
    WVM-01) wvm01 ;;
    WVM-02) wvm02 ;;
    WVM-03) wvm03 ;;
    WVM-04) wvm04 ;;
    WVM-05) wvm05 ;;
    WVM-06) wvm06 ;;
    WVM-07) wvm07 ;;
    WVM-08) wvm08 ;;
    WVM-09) wvm09 ;;
    WVM-10) wvm10 ;;
  esac
done
exit "$failed"
