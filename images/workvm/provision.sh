#!/bin/bash
# Provisioning of the work image (docs/platform/spec/13-work-vm.md section 4, ADR-059).
#
# Runs once as root inside the image build VM, from a copy of this repository's `images/`:
#   sudo bash <bundle>/images/workvm/provision.sh
# Installs a developer's Ubuntu: build tools, Git, Python, Node, a container runtime, a browser (Playwright with
# Chromium), SSH over vsock for Lima, the job helper and the platform browser helper. Nothing is restricted for
# root; the session installs everything else itself. No credential is ever written into the image. Idempotent; the guard file ends it early.
set -euo pipefail
umask 022

GUARD=/var/lib/workvm-image.done
BUNDLE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGES=$BUNDLE/images
FILES=$IMAGES/workvm/files
CACHE=/var/cache/workvm-image
PLAYWRIGHT_VERSION=1.65.1

log() { echo "workvm-image: $*"; }
die() {
  echo "workvm-image: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
if [ -f "$GUARD" ]; then
  log "already provisioned ($GUARD)"
  exit 0
fi
find "$IMAGES" -name '._*' -type f -delete

version() { python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$IMAGES/versions.json" "$1"; }

install -d -m 0755 "$CACHE"
fetch() {
  local url=$1 sha=$2 dest=$CACHE/$3
  if [ ! -f "$dest" ] || ! echo "$sha  $dest" | sha256sum -c --status; then
    curl -fsSL --retry 3 -o "$dest.part" "$url"
    echo "$sha  $dest.part" | sha256sum -c --status || die "digest mismatch for $url"
    mv "$dest.part" "$dest"
  fi
}

log "SSH over vsock"
install -m 0644 "$FILES/etc/systemd/system/sshd-vsock.socket" /etc/systemd/system/sshd-vsock.socket
install -m 0644 "$FILES/etc/systemd/system/sshd-vsock@.service" /etc/systemd/system/sshd-vsock@.service
systemctl daemon-reload
systemctl enable --now sshd-vsock.socket

log "job helper"
install -m 0755 "$FILES/usr/local/sbin/workvm-job" /usr/local/sbin/workvm-job
install -m 0755 "$FILES/usr/local/bin/workvm-browse" /usr/local/bin/workvm-browse
install -m 0755 "$FILES/usr/local/bin/workvm-browser" /usr/local/bin/workvm-browser
install -m 0755 "$FILES/usr/local/bin/workvm-vnc" /usr/local/bin/workvm-vnc
install -d -m 0700 /var/lib/workvm/jobs
install -d -m 0755 /workspace

log "packages"
export DEBIAN_FRONTEND=noninteractive
# A build interrupted earlier may have left dpkg half-configured.
dpkg --configure -a
apt-get update -q
apt-get install -y -q --no-install-recommends \
  build-essential git curl wget ca-certificates gnupg unzip zip xz-utils jq less vim-tiny nano file \
  python3 python3-venv python3-pip python3-dev pipx \
  docker.io docker-compose-v2 \
  sqlite3 ripgrep fd-find tmux htop procps iproute2 iputils-ping dnsutils netcat-openbsd rsync openssh-client \
  pkg-config libssl-dev \
  xvfb x11vnc
systemctl enable docker.service containerd.service

log "Node"
node_version=$(version node)
fetch "$(version node_tarball_url)" "$(version node_tarball_sha256)" "node-$node_version.tar.xz"
rm -rf /usr/local/lib/nodejs
install -d -m 0755 /usr/local/lib/nodejs
tar -xJf "$CACHE/node-$node_version.tar.xz" -C /usr/local/lib/nodejs --strip-components=1 --no-same-owner
for tool in node npm npx corepack; do
  ln -sf "/usr/local/lib/nodejs/bin/$tool" "/usr/local/bin/$tool"
done

log "browser (Playwright $PLAYWRIGHT_VERSION with Chromium)"
export PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
npm install -g --no-fund --no-audit "playwright@$PLAYWRIGHT_VERSION"
ln -sf /usr/local/lib/nodejs/bin/playwright /usr/local/bin/playwright
playwright install --with-deps chromium
chmod -R a+rX /opt/playwright-browsers
printf 'PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers\nNODE_PATH=/usr/local/lib/nodejs/lib/node_modules\n' \
  >/etc/environment.d/90-workvm.conf 2>/dev/null || true
cat >/etc/profile.d/90-workvm.sh <<'PROFILE'
export PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
export NODE_PATH=/usr/local/lib/nodejs/lib/node_modules
PROFILE
chromium=$(find /opt/playwright-browsers -path '*chrome-linux*/chrome' -type f | head -n 1)
[ -n "$chromium" ] || die "chromium not found"
ln -sf "$chromium" /usr/local/bin/chromium

log "clean up"
apt-get clean
rm -rf "$CACHE"
/usr/local/sbin/workvm-job selftest
# The platform browser (13-work-vm.md section 13.3): start the daemon once, then leave no profile in the image.
/usr/local/bin/workvm-browser selftest
# The screen of a check for humans (ADR-101): the VNC server and the virtual display are installed.
/usr/local/bin/workvm-vnc selftest
systemctl stop workvm-browser.service 2>/dev/null || true
rm -rf /var/lib/workvm-browser /run/workvm-browser.sock
touch "$GUARD"
log "done"
