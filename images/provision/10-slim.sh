#!/bin/bash
# Slim steps of the golden image (docs/platform/spec/05-runner.md section 3.1, Spike A slim.yaml).
# Lima system provisioning: runs as root on every boot; the guard file makes every boot after the first a no-op.
set -euo pipefail

GUARD=/var/lib/agent-golden/slim.done
[ -f "$GUARD" ] && exit 0

export DEBIAN_FRONTEND=noninteractive

# Services an agent VM does not need and that cost boot time or I/O. Only installed packages are purged, so that
# one unknown name does not abort the purge of the others.
systemctl disable --now snapd.service snapd.socket snapd.seeded.service 2>/dev/null || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer \
  motd-news.timer fstrim.timer e2scrub_all.timer dpkg-db-backup.timer \
  update-notifier-download.timer update-notifier-motd.timer 2>/dev/null || true
purge=()
for package in snapd modemmanager multipath-tools udisks2 unattended-upgrades apport motd-news-config \
  ubuntu-advantage-tools landscape-common needrestart; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
    purge+=("$package")
  fi
done
if [ ${#purge[@]} -gt 0 ]; then
  apt-get -o DPkg::Lock::Timeout=300 purge -y "${purge[@]}"
fi
systemctl mask systemd-networkd-wait-online.service
# Firmware metadata refresh (fwupd is part of the Ubuntu base image): its timer runs after the boot of every clone and
# fetches from a CDN over HTTPS - an agent VM has no firmware to update and must not reach the internet on its own.
systemctl disable --now fwupd-refresh.timer 2>/dev/null || true
systemctl mask fwupd-refresh.timer fwupd-refresh.service
# Entropy seeding from an Ubuntu server at boot (pollinate) and the Ubuntu Pro client's status, news and ESM jobs: each
# would reach the internet from every clone; the kernel's own RNG and virtio-rng seed the VM (ATS-03, A-20).
systemctl disable --now pollinate.service ubuntu-advantage.service ua-timer.timer esm-cache.service \
  apt-news.service 2>/dev/null || true
systemctl mask pollinate.service ubuntu-advantage.service ua-reboot-cmds.service ua-timer.timer \
  ua-timer.service esm-cache.service apt-news.service
# Socket activation of root services an agent VM never uses: iscsid listens on an abstract unix socket, which no mount
# namespace hides from a session, and the LXD installer would install a snap as root (ATS-03, ADR-045).
systemctl disable --now iscsid.socket lxd-installer.socket 2>/dev/null || true
systemctl mask iscsid.socket iscsid.service lxd-installer.socket lxd-installer.service

# GRUB timeout 0 (idempotent: replace or append once).
if grep -q '^GRUB_TIMEOUT=' /etc/default/grub; then
  sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub
else
  echo 'GRUB_TIMEOUT=0' >>/etc/default/grub
fi
grep -q '^GRUB_RECORDFAIL_TIMEOUT=' /etc/default/grub || echo 'GRUB_RECORDFAIL_TIMEOUT=0' >>/etc/default/grub
update-grub 2>/dev/null || true

install -d -m 0755 /var/lib/agent-golden
touch "$GUARD"
