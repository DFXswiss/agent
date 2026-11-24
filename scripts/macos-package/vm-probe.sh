#!/bin/bash
# Installation probe of the runner package in a fresh macOS guest of Lima (KOR-59, ADR-074).
#
#   scripts/macos-package/vm-probe.sh <lima instance> <package> <install script> [<request file content>]
#
# Runs on the test device that holds the instance. In the guest, `sudo` (password from ~/password, Lima's macOS
# template) stands in for the system dialog of the agent way: it runs the very root script `agent-platform device add`
# hands to osascript (agent_runner.install.admin.install_script). The probe records the system state before, installs,
# shows what was created, runs the doctor as the service account, installs again (idempotent), removes the
# installation with the root helper and compares the state with the one before: "NOTHING LEFT" or the difference.
# The runner runs as the guest's user (the Mac's owner); no account is created.
set -euo pipefail
vm=$1
package=$2
script=$3
request=${4:-}
limactl copy "$package" "$vm:/tmp/runner.pkg"
limactl copy "$script" "$vm:/tmp/install.sh"
limactl shell "$vm" -- /bin/bash -s -- "$request" <<'GUEST'
set -uo pipefail
request=$1
password=$(cat ~/password)
root() { printf '%s\n' "$password" | sudo -S -p '' "$@"; }
payload="/Library/Application Support/DFXAgentRunner"
snapshot() {
  dscl . -list /Users | sort
  dscl . -list /Groups | sort
  ls -1 /Library/LaunchDaemons
  pkgutil --pkgs | sort
  ls -1 "/Library/Application Support" /private/var
  ls -1A "$HOME" "$HOME/.local/share" 2>/dev/null
  ls -1A "$HOME/.lima" 2>/dev/null
  root launchctl list | awk 'NR > 1 {print $3}' | grep -v '^com.apple.' | sort
}
step() { printf '\n== %s\n' "$*"; }
snapshot >/tmp/before.txt
digest=$(shasum -a 256 /tmp/runner.pkg | cut -d ' ' -f 1)
req=""
if [ -n "$request" ]; then
  req=$HOME/enroll.request
  ( umask 077; printf '%s\n' "$request" >"$req" )
fi
step "install (the root script of the agent way, via sudo instead of the dialog)"
time root /bin/sh -c "$(cat /tmp/install.sh)" dfx-agent-runner /tmp/runner.pkg "$digest" "$req"
echo "rc=$?"
[ -z "$req" ] || { [ -e "$req" ] && echo "request file still with the user (expected: copied, not moved)"; rm -f "$req"; }
step "state.json"; cat "$payload/state.json"
step "spool (must be empty)"; root ls -la "$payload/spool"
step "payload"; ls -la "$payload" "$payload/versions"; readlink "$payload/current"
step "runner home of the owner (no new account)"; ls -la "$HOME/.local/share/dfx-agent-runner"
dscl . -list /Users | grep -i dfx || echo "no account with dfx in its name"
step "daemons"; ls -la /Library/LaunchDaemons | grep dfx || true
root launchctl print system/swiss.dfx.agent-runner.maintenance | grep -E '^\s*(state|program|last exit code)' || true
root launchctl print system/swiss.dfx.agent-runner 2>&1 | grep -E '^\s*(state|last exit code)|Could not find' || true
step "maintenance log"; root tail -3 "$payload/log/maintenance.log" || true
step "time machine exclusion"; tmutil isexcluded "$HOME/.local/share/dfx-agent-runner" || true
step "doctor as the owner"
( cd "$HOME" && /usr/bin/env -i HOME="$HOME" \
  PATH="$payload/current/lima/bin:$payload/current/postgresql/bin:$payload/host/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  AGENT_RUNNER_HOME="$HOME/.local/share/dfx-agent-runner" AGENT_RUNNER_TEST_MODE=1 AGENT_RUNNER_VM_PREFIX=dfx- \
  "$payload/current/libexec/dfx-agent-runner" agent_runner doctor | sed 's/^/  /' ) || true
step "host tools"; ls -la "$payload/host/bin" 2>/dev/null || echo "none"
step "install again (idempotent)"
root /bin/sh -c "$(cat /tmp/install.sh)" dfx-agent-runner /tmp/runner.pkg "$digest" "" && echo "rc=0"
step "uninstall (root helper)"
time root "$payload/current/libexec/root-helper" uninstall
step "leftovers (root helper is gone with the payload: checked by the snapshot)"
snapshot >/tmp/after.txt
if diff /tmp/before.txt /tmp/after.txt; then echo "NOTHING LEFT"; else echo "DIFFERENCE ABOVE"; fi
root ls -la /private/tmp | grep dfx-agent-runner || echo "no work directory left"
GUEST
