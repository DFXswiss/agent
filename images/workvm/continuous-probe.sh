#!/bin/sh
# Continuous probe of the work VM boundary (docs/platform/spec/13-work-vm.md section 6.4, KOR-29 decisions #34).
#
# A sample (one `nc`, one `egress-check.sh`) can miss a boundary that fails only for some connections; this probe
# tries again and again. Every interval it tries, from where it runs (a slot account, or inside a VM with --vm):
#   loopback       a TCP port the device listens on, over the device's loopback (from a VM: the user network's
#                  gateway, which Lima maps to the host's loopback)
#   site_address   the same port over the device's own address in the site network (when given)
#   internet       Cloudflare's trace; a hit only when it answers `warp=off`, i.e. the request left past the
#                  device's Cloudflare client (`warp=on` is the intended way out)
# Every success of `loopback`/`site_address` and every `warp=off` answer is a hit: one line
# `hit <target> <detail> t=<unix time with ms>` on stdout, at once. Ends after --duration seconds (default: never)
# with a line `done tries=<n>`. Needs only sh, nc, curl and perl (for the time); no root.
#
#   continuous-probe.sh --port P [--loopback ADDRESS] [--site-address ADDRESS] [--trace-url URL]
#                       [--interval SECONDS] [--duration SECONDS]
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin
export PATH

port=
loopback=127.0.0.1
site=
trace=https://cloudflare.com/cdn-cgi/trace
interval=0.1
duration=0
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || { echo "continuous-probe: $1 needs a value" >&2; exit 2; }
  case "$1" in
    --port) port=$2 ;;
    --loopback) loopback=$2 ;;
    --site-address) site=$2 ;;
    --trace-url) trace=$2 ;;
    --interval) interval=$2 ;;
    --duration) duration=$2 ;;
    *) echo "continuous-probe: unknown option $1" >&2; exit 2 ;;
  esac
  shift 2
done
case "$port" in '' | *[!0-9]*) echo "continuous-probe: --port needs a number" >&2; exit 2 ;; esac
case "$duration" in *[!0-9]*) echo "continuous-probe: --duration needs whole seconds" >&2; exit 2 ;; esac

now() { perl -MTime::HiRes=time -e 'printf q(%.3f), time'; }
# macOS nc needs -G for the connect timeout; the OpenBSD nc of Ubuntu (inside a VM) has only -w
case "$(uname -s)" in Darwin) wait_opts="-G 1 -w 1" ;; *) wait_opts="-w 1" ;; esac
start=$(date +%s)
tries=0
while :; do
  tries=$((tries + 1))
  # shellcheck disable=SC2086 # the options are words on purpose
  nc -z $wait_opts "$loopback" "$port" >/dev/null 2>&1 && echo "hit loopback $loopback:$port t=$(now)" &
  if [ -n "$site" ]; then
    # shellcheck disable=SC2086 # the options are words on purpose
    nc -z $wait_opts "$site" "$port" >/dev/null 2>&1 && echo "hit site_address port=$port t=$(now)" &
  fi
  (
    answer=$(curl -s -m 2 "$trace" 2>/dev/null)
    case "$answer" in
      *warp=off*) echo "hit internet warp=off ip=$(printf '%s\n' "$answer" | sed -n 's/^ip=//p') t=$(now)" ;;
    esac
  ) &
  sleep "$interval"
  if [ "$duration" -gt 0 ] && [ $(($(date +%s) - start)) -ge "$duration" ]; then
    wait
    echo "done tries=$tries"
    exit 0
  fi
done
