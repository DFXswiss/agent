#!/bin/bash
# Stage-2 provisioning of the golden image (docs/platform/spec/05-runner.md sections 3.1, 4.1-4.3, 5.1, 8, 15).
#
# Runs as root inside the build VM after `limactl start` finished the slim step (golden.yaml), from a copy of this
# repository's `images/` and `src/agent_guest/` (bundle layout of images/README.md):
#   sudo bash <bundle>/images/provision/install.sh [--test-model-port PORT]
# --test-model-port builds a TEST_MODE golden: the platform user may also reach the model double on that port of
# the Lima host gateway. Idempotent: every step can run again; /var/lib/agent-golden.done ends it early.
set -euo pipefail
umask 022

GUARD=/var/lib/agent-golden.done
BUNDLE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGES=$BUNDLE/images
FILES=$IMAGES/files
CACHE=/var/cache/agent-golden
CIDATA=${AGENT_GOLDEN_CIDATA:-/mnt/lima-cidata}
TEST_MODEL_PORT=

log() { echo "agent-golden: $*"; }
die() {
  echo "agent-golden: $*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case $1 in
  --test-model-port)
    [ $# -ge 2 ] || die "--test-model-port needs a port"
    TEST_MODEL_PORT=$2
    shift 2
    ;;
  *) die "unknown argument $1" ;;
  esac
done
if [ -n "$TEST_MODEL_PORT" ] && ! [[ "$TEST_MODEL_PORT" =~ ^[1-9][0-9]{0,4}$ && "$TEST_MODEL_PORT" -le 65535 ]]; then
  die "--test-model-port must be a TCP port"
fi
[ "$(id -u)" -eq 0 ] || die "must run as root"
if [ -f "$GUARD" ]; then
  recorded=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["test_model_port"] or "")' \
    /var/lib/agent-golden/install.json)
  [ "$recorded" = "$TEST_MODEL_PORT" ] || die "already provisioned with test model port '$recorded'"
  log "already provisioned ($GUARD)"
  exit 0
fi
[ -f /var/lib/agent-golden/slim.done ] || die "the slim step of golden.yaml did not finish"
# For the rest of this boot the guest services may start without the boot hardening (agent-guest-harden.service).
touch /run/agent-golden-build
[ -d "$BUNDLE/src/agent_guest" ] || die "bundle without src/agent_guest: $BUNDLE"
# A bundle packed on macOS may carry AppleDouble files (._name); they must never reach the image.
find "$BUNDLE/images" "$BUNDLE/src" -name '._*' -type f -delete

version() { python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$IMAGES/versions.json" "$1"; }

# fetch URL SHA256 FILE: download once into the cache and verify the pinned digest.
fetch() {
  local url=$1 sha=$2 dest=$CACHE/$3
  if [ ! -f "$dest" ] || ! echo "$sha  $dest" | sha256sum -c --status; then
    curl -fsSL --retry 3 -o "$dest.part" "$url"
    echo "$sha  $dest.part" | sha256sum -c --status || die "sha256 mismatch for $url"
    mv -f "$dest.part" "$dest"
  fi
  echo "$dest"
}

lima_user=$(sed -n 's/^LIMA_CIDATA_USER=//p' "$CIDATA/lima.env" 2>/dev/null | tr -d "\"'" || true)
lima_user=${lima_user:-${SUDO_USER:-}}
id -u "$lima_user" >/dev/null 2>&1 || die "Lima default user not found"

install -d -m 0755 "$CACHE" /var/lib/agent-golden

log "packages"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
apt-get -o DPkg::Lock::Timeout=300 update -q
apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends tmux nftables acl ca-certificates curl xz-utils libatomic1

log "node $(version node)"
node_version=$(version node)
node_dir=/opt/node-v$node_version-linux-arm64
if [ ! -x "$node_dir/bin/node" ]; then
  tarball=$(fetch "$(version node_tarball_url)" "$(version node_tarball_sha256)" "node.tar.xz")
  rm -rf "$node_dir.part"
  install -d -m 0755 "$node_dir.part"
  tar -xJf "$tarball" -C "$node_dir.part" --strip-components=1 --no-same-owner
  mv "$node_dir.part" "$node_dir"
fi
ln -sfn "$node_dir" /opt/node
for tool in node npm npx; do ln -sfn "/opt/node/bin/$tool" "/usr/local/bin/$tool"; done
[ "$(/opt/node/bin/node --version)" = "v$node_version" ] || die "node version mismatch"

claude_version=$(version claude_code)
log "claude code $claude_version"
wrapper=$(fetch "$(version claude_code_tarball_url)" "$(version claude_code_tarball_sha256)" "claude-code.tgz")
native=$(fetch "$(version claude_code_native_tarball_url)" "$(version claude_code_native_tarball_sha256)" \
  "claude-code-native.tgz")
prefix=$CACHE/npm
rm -rf "$prefix"
# Both pinned tarballs in one offline install: the wrapper's postinstall finds the native package next to it and
# replaces its placeholder with the native binary; nothing is fetched from the registry.
PATH=/opt/node/bin:$PATH npm install --global --prefix "$prefix" --cache "$CACHE/npm-cache" --offline --no-audit \
  --no-fund --no-update-notifier "$native" "$wrapper"
binary=$(realpath "$prefix/bin/claude")
head -c 4 "$binary" | grep -q 'ELF' || die "npm did not install the native Claude Code binary ($binary)"
install -d -m 0755 /opt/claude
install -m 0755 -o root -g root "$binary" /opt/claude/claude
ln -sfn /opt/claude/claude /usr/local/bin/claude
/opt/claude/claude --version | grep -q "^$claude_version " || die "claude version mismatch"
rm -rf /root/.npm /root/.claude /root/.claude.json
python3 -I - "$claude_version" >/opt/claude/install.json <<'PY'
import hashlib, json, sys
with open("/opt/claude/claude", "rb") as handle:
    digest = hashlib.sha256(handle.read()).hexdigest()
package = f"@anthropic-ai/claude-code-linux-arm64@{sys.argv[1]}"
print(json.dumps({"version": sys.argv[1], "path": "/opt/claude/claude", "package": package, "sha256": digest},
                 sort_keys=True))
PY
chmod 0644 /opt/claude/install.json
rm -rf "$prefix"

log "users and groups"
getent group agent-ctl >/dev/null || groupadd --system agent-ctl
if ! id -u platform >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin platform
fi
gpasswd -M "$lima_user" agent-ctl >/dev/null
# The account of the main orchestrator's agent tool (05-runner.md section 17.4): no login, no home, no network.
getent group agent-orch >/dev/null || groupadd --system agent-orch
if ! id -u agent-orch >/dev/null 2>&1; then
  useradd --system --gid agent-orch --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin agent-orch
fi

log "guest agent"
install -d -m 0755 -o root -g root /opt/agent-guest /opt/agent-guest/harness
rm -rf /opt/agent-guest/agent_guest
cp -R "$BUNDLE/src/agent_guest" /opt/agent-guest/agent_guest
find /opt/agent-guest/agent_guest -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -I -m compileall -q /opt/agent-guest/agent_guest
chown -R root:root /opt/agent-guest
find /opt/agent-guest/agent_guest -type d -exec chmod 0755 {} +
find /opt/agent-guest/agent_guest -type f -exec chmod 0644 {} +
install -m 0644 -o root -g root "$IMAGES/mcp.json" /opt/agent-guest/mcp.json
install -m 0644 -o root -g root "$IMAGES/harness/RULES.md" /opt/agent-guest/harness/RULES.md
install -m 0644 -o root -g root "$IMAGES/mcp-orchestrator.json" /opt/agent-guest/mcp-orchestrator.json

# The agent tool of the main orchestrator (05-runner.md section 17.4, key-model.md KM-37): its sources from the bundle
# and its pinned wheels, unpacked (no pip in the image); readable and executable only by root and agent-orch.
log "agent tool"
TOOL_WHEELS="anyio certifi cffi cryptography h11 httpcore httpx idna pycparser typing_extensions"
tool_dir=/opt/agent-orch
rm -rf "$tool_dir.part"
install -d -m 0755 "$tool_dir.part/lib/agent_cli" "$tool_dir.part/bin"
for wheel in $TOOL_WHEELS; do
  file=$(fetch "$(version "tool_wheel_${wheel}_url")" "$(version "tool_wheel_${wheel}_sha256")" "$wheel.whl")
  python3 -I -m zipfile -e "$file" "$tool_dir.part/lib"
done
for part in agent_platform agent_runner; do
  [ -d "$BUNDLE/src/$part" ] || die "bundle without src/$part"
  cp -R "$BUNDLE/src/$part" "$tool_dir.part/lib/$part"
done
install -m 0644 "$BUNDLE/src/agent_cli/__init__.py" "$BUNDLE/src/agent_cli/keys.py" "$tool_dir.part/lib/agent_cli/"
install -m 0755 "$IMAGES/orchestrator/agent-platform" "$tool_dir.part/bin/agent-platform"
find "$tool_dir.part" -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -I -m compileall -q "$tool_dir.part/lib"
chown -R root:agent-orch "$tool_dir.part"
find "$tool_dir.part" -type d -exec chmod 0750 {} +
find "$tool_dir.part/lib" -type f -exec chmod 0640 {} +
chmod 0750 "$tool_dir.part/bin/agent-platform"
rm -rf "$tool_dir"
mv "$tool_dir.part" "$tool_dir"
runuser -u agent-orch -- /usr/bin/python3 -I -c "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/agent-orch/lib")
import agent_platform.cli, agent_platform.relay, httpx
from cryptography.hazmat.primitives.asymmetric import x25519
x25519.X25519PrivateKey.generate()
PY
)" || die "the agent tool does not load as agent-orch"

log "managed settings"
install -d -m 0755 -o root -g root /etc/claude-code
(cd /opt/agent-guest && python3 -E -s -m agent_guest.sandbox.settings --output /etc/claude-code/managed-settings.json)

log "system files"
(cd "$FILES" && find etc usr -type f) | while read -r path; do
  mode=0644
  case $path in *.sh) mode=0755 ;; esac
  install -D -m "$mode" -o root -g root "$FILES/$path" "/$path"
done
rm -f /etc/nftables.d/agent-sessions.test.nft
if [ -n "$TEST_MODEL_PORT" ]; then
  gateway=$(getent ahostsv4 host.lima.internal | awk 'NR == 1 { print $1 }' || true)
  [ -n "$gateway" ] || die "host.lima.internal does not resolve"
  sed -e "s/@GATEWAY@/$gateway/" -e "s/@PORT@/$TEST_MODEL_PORT/" "$FILES/fragments/agent-sessions.test.nft.in" \
    >/etc/nftables.d/agent-sessions.test.nft
  chmod 0644 /etc/nftables.d/agent-sessions.test.nft
  log "TEST_MODE: model double port $TEST_MODEL_PORT on the host gateway"
fi
nft -c -f /etc/nftables.conf

log "session mount"
install -d -m 0711 -o root -g root /srv/sessions
fstab_line=$(grep -v '^#' "$FILES/fragments/fstab")
grep -qxF "$fstab_line" /etc/fstab || echo "$fstab_line" >>/etc/fstab
systemctl daemon-reload
mountpoint -q /srv/sessions || mount /srv/sessions
for option in noexec nosuid nodev; do
  findmnt -n -o OPTIONS /srv/sessions | tr ',' '\n' | grep -qx "$option" || die "/srv/sessions lacks $option"
done

log "services"
# The image's kernel settings (no executable memfd, ADR-045) apply now, for the self-test of this build, and at boot.
sysctl -q -p /etc/sysctl.d/60-agent-guest.conf
[ "$(cat /proc/sys/vm/memfd_noexec)" = 2 ] || die "vm.memfd_noexec is not 2"
systemd-tmpfiles --create /etc/tmpfiles.d/agent-guest.conf
systemctl enable --now nftables.service
systemctl restart nftables.service
systemctl enable agent-guest-harden.service agent-guest-control.service agent-guest-api.service

python3 -I - "$claude_version" "$node_version" "$TEST_MODEL_PORT" >/var/lib/agent-golden/install.json <<'PY'
import json, sys
port = sys.argv[3]
print(json.dumps({"claude_code": sys.argv[1], "node": sys.argv[2], "test_mode": bool(port),
                  "test_model_port": int(port) if port else None}, sort_keys=True))
PY
rm -rf "$CACHE"
apt-get clean
touch "$GUARD"
log "done"
