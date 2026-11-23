#!/bin/bash
# agent-guest-harden.service (docs/platform/spec/05-runner.md sections 3.1 and 3.2). Runs as root on every boot after
# cloud-init. Lima re-runs cloud-init on every clone, which restores the default user's unrestricted sudo and the
# Lima parameters; this script undoes both before the guest services start.
#  1. The Lima default user is the only member of `agent-ctl` and may only restart the guest services with sudo.
#     A TEST_MODE golden (install.json "test_mode": true) keeps the user's unrestricted sudo for the test hooks of the
#     E2E suite (ADR-036); a production golden never has it. Session users are not affected either way.
#  2. The control token handed over by the Lima provisioning step (golden.yaml) is written to
#     /etc/agent-guest/control-token (root 0600); the staged copy is removed.
set -euo pipefail
umask 077

CIDATA=/mnt/lima-cidata
STAGED=/run/agent-guest-param/control-token
TOKEN_FILE=/etc/agent-guest/control-token
SUDOERS_TEMPLATE=/usr/local/lib/agent-guest/sudoers.in
SUDOERS_FILE=/etc/sudoers.d/90-agent-guest
TEST_SUDOERS_FILE=/etc/sudoers.d/91-agent-guest-test
INSTALL_RECORD=/var/lib/agent-golden/install.json
# Exit 0 only for an install record that says "test_mode": true (a missing or unreadable record fails).
TEST_MODE_CHECK='import json, sys; sys.exit(json.load(open(sys.argv[1])).get("test_mode") is not True)'

log() { echo "agent-guest-harden: $*"; }

# cloud-init's grant of unrestricted sudo goes first, so that every later failure leaves the user without it.
CLOUD_INIT_SUDOERS=/etc/sudoers.d/90-cloud-init-users
if [ -e "$CLOUD_INIT_SUDOERS" ]; then
  rm -f "$CLOUD_INIT_SUDOERS"
  log "removed $CLOUD_INIT_SUDOERS"
fi
# The TEST_MODE grant of the previous boot goes as well; it is written again below only for a TEST_MODE golden.
rm -f "$TEST_SUDOERS_FILE"

lima_user=$(sed -n 's/^LIMA_CIDATA_USER=//p' "$CIDATA/lima.env" 2>/dev/null | tr -d "\"'" || true)
if ! [[ "$lima_user" =~ ^[a-z_][a-z0-9_.-]{0,31}$ ]] || ! id -u "$lima_user" >/dev/null 2>&1; then
  log "Lima default user not found in $CIDATA/lima.env"
  exit 1
fi

# 1. Group membership and sudo of the Lima default user.
gpasswd -M "$lima_user" agent-ctl >/dev/null
for group in sudo admin wheel; do
  if id -nG "$lima_user" | tr ' ' '\n' | grep -qx "$group"; then
    gpasswd -d "$lima_user" "$group" >/dev/null
  fi
done
for file in /etc/sudoers.d/*; do
  [ -f "$file" ] && [ "$file" != "$SUDOERS_FILE" ] || continue
  if grep -Eq "^[[:space:]]*${lima_user}[[:space:]]" "$file"; then
    rm -f "$file"
    log "removed $file"
  fi
done
rendered=$(mktemp /etc/sudoers.d/.agent-guest.XXXXXX)
sed "s/@LIMA_USER@/$lima_user/g" "$SUDOERS_TEMPLATE" >"$rendered"
chmod 0440 "$rendered"
if ! visudo -cqf "$rendered"; then
  rm -f "$rendered"
  log "the rendered sudoers rule is invalid"
  exit 1
fi
mv -f "$rendered" "$SUDOERS_FILE"
if python3 -I -c "$TEST_MODE_CHECK" "$INSTALL_RECORD" 2>/dev/null; then
  rendered=$(mktemp /etc/sudoers.d/.agent-guest-test.XXXXXX)
  printf '%s ALL=(root) NOPASSWD: ALL\n' "$lima_user" >"$rendered"
  chmod 0440 "$rendered"
  if ! visudo -cqf "$rendered"; then
    rm -f "$rendered"
    log "the rendered TEST_MODE sudoers rule is invalid"
    exit 1
  fi
  mv -f "$rendered" "$TEST_SUDOERS_FILE"
  log "TEST_MODE: the Lima default user keeps sudo for the E2E test hooks"
fi

# 2. Control token: 32 random bytes, base64url without padding (05-runner.md section 3.2).
if [ -s "$STAGED" ]; then
  token=$(cat "$STAGED")
  rm -f "$STAGED"
  if ! [[ "$token" =~ ^[A-Za-z0-9_-]{43}$ ]]; then
    log "control token parameter has the wrong format"
    exit 1
  fi
  install -d -m 0700 -o root -g root /etc/agent-guest
  tmp=$(mktemp /etc/agent-guest/.control-token.XXXXXX)
  printf '%s' "$token" >"$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$TOKEN_FILE"
  log "control token installed"
elif [ -f "$TOKEN_FILE" ]; then
  log "no control token parameter; keeping the installed token"
else
  log "no control token parameter and no installed token; the control socket refuses every request"
fi
rm -rf "$(dirname "$STAGED")"
# Lima keeps its parameters also on the read-only configuration disk (/mnt/lima-cidata/param.env); Lima mounts it
# for root only (dmode=700, fmode=700), and the runner removes the parameter from the instance configuration on the
# host after the first start (05-runner.md section 3.2).
