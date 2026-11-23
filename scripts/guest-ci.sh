#!/usr/bin/env bash
# Linux-only guest tests in the Lima VM `<prefix>guest-ci` (make platform-ci-linux; docs/platform/spec/07-testing.md
# sections 1, 3 and 7; ADR-027 section 7). Runs on the runner device.
#
#   scripts/guest-ci.sh [run]   create or reuse the VM, copy the checkout in, run the tests marked `linux` as root
#                               under coverage, copy .coverage.linux.* back into the checkout, stop the VM
#   scripts/guest-ci.sh stop    stop the VM (it is kept for reuse)
#   scripts/guest-ci.sh delete  delete the VM
#
# Environment:
#   AGENT_RUNNER_VM_PREFIX  prefix of the instance name (work packages set wp-<tag>-; default empty)
#   GUEST_CI_VM             instance name (default ${AGENT_RUNNER_VM_PREFIX}guest-ci)
#   GUEST_CI_GOLDEN         clone the VM from this stopped golden instance (`agent-runner image build`, or the
#                           shared cache cache-golden-<sha12>) instead of creating it from template:ubuntu-24.04;
#                           the clone gets a boot script that writes the golden-build flag /run/agent-golden-build,
#                           so agent-guest-harden.service is skipped and the default user keeps the sudo the tests
#                           need (a test VM only; production clones always run the hardening)
#   GUEST_CI_CPUS / GUEST_CI_MEMORY / GUEST_CI_DISK   VM size for a new instance (default 4 / 4 GiB / 20 GiB)
#   GUEST_CI_KEEP_RUNNING=1 do not stop the VM after the run
#   GUEST_CI_PYTEST_ARGS    extra pytest arguments (for example -x or -k NAME)
set -euo pipefail

cmd=${1:-run}
vm=${GUEST_CI_VM:-${AGENT_RUNNER_VM_PREFIX:-}guest-ci}
repo=$(cd "$(dirname "$0")/.." && pwd)
# Inside the VM: outside /home and /root (ProtectHome= of the session units hides those) and matching the
# coverage path mapping `*/agent/src/` of .coveragerc-platform.
guest_repo=/var/lib/guest-ci/agent
guest_venv=/opt/guest-ci/venv
# Bump to re-provision existing VMs.
provision_version=3
# Same major versions as the platform-test extra (pytest-timeout: the hard limit per test of tests/platform/pytest.ini);
# coverage data files are compatible across 7.x.
pip_packages="pytest>=8.3.0 pytest-timeout>=2.4 coverage>=7.10.7"

log() { printf 'guest-ci: %s\n' "$*" >&2; }

command -v limactl >/dev/null || { log "limactl not found (run on the runner device)"; exit 2; }

status() { limactl list --format '{{.Status}}' "$vm" 2>/dev/null || true; }

in_vm() { limactl shell --workdir / "$vm" "$@"; }

create_vm() {
  if [ -n "${GUEST_CI_GOLDEN:-}" ]; then
    log "cloning $vm from golden $GUEST_CI_GOLDEN"
    limactl clone --tty=false --set '.provision += [{"mode": "system", "script": "#!/bin/sh\ntouch /run/agent-golden-build\n"}]' \
      "$GUEST_CI_GOLDEN" "$vm"
  else
    log "creating $vm from template:ubuntu-24.04"
    limactl create -y --name "$vm" --vm-type vz --cpus "${GUEST_CI_CPUS:-4}" --memory "${GUEST_CI_MEMORY:-4}" \
      --disk "${GUEST_CI_DISK:-20}" --mount-none --containerd none template:ubuntu-24.04
  fi
}

provision() {
  in_vm sudo env "PIP_PACKAGES=$pip_packages" "VENV=$guest_venv" "MARK=/var/lib/guest-ci/provisioned-$provision_version" \
    bash -euo pipefail -c '
      [ -e "$MARK" ] && exit 0
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq python3-venv git nftables acl tmux
      python3 -m venv "$VENV"
      "$VENV/bin/pip" install -q $PIP_PACKAGES
      mkdir -p "$(dirname "$MARK")"
      touch "$MARK"'
}

# The golden image's settings the sandbox self-test relies on (ADR-045): no executable memfd
# (images/files/etc/sysctl.d/60-agent-guest.conf) and no socket-activated root service on an abstract unix socket that
# a session unit could reach (iscsid, multipathd; images/provision/10-slim.sh masks or purges them). Applied on every
# run: a template VM has neither.
image_settings() {
  in_vm sudo bash -euo pipefail -c '
    sysctl -q -w vm.memfd_noexec=2
    systemctl disable --now iscsid.socket iscsid.service multipathd.socket multipathd.service \
      lxd-installer.socket >/dev/null 2>&1 || true
    systemctl mask iscsid.socket iscsid.service multipathd.socket multipathd.service lxd-installer.socket \
      >/dev/null 2>&1'
}

copy_repo() {
  local tarball
  tarball=$(mktemp -t guest-ci.XXXXXX)
  COPYFILE_DISABLE=1 tar -C "$repo" --exclude '__pycache__' --exclude '.coverage' --exclude '.coverage.*' \
    -czf "$tarball" \
    pyproject.toml .coveragerc-platform contracts src/agent_guest src/agent_doubles tests/platform
  limactl copy "$tarball" "$vm:/tmp/guest-ci-src.tgz"
  rm -f "$tarball"
  in_vm sudo bash -euo pipefail -c "rm -rf $guest_repo && mkdir -p $guest_repo && \
    tar --no-same-owner -xzf /tmp/guest-ci-src.tgz -C $guest_repo && rm -f /tmp/guest-ci-src.tgz"
}

run_tests() {
  local rc=0
  # shellcheck disable=SC2086
  in_vm sudo env "PYTHONPATH=$guest_repo/src" COVERAGE_FILE=.coverage.linux bash -c "cd $guest_repo && \
    rm -f .coverage.linux* && \
    $guest_venv/bin/coverage run --rcfile=.coveragerc-platform -m pytest -c tests/platform/pytest.ini \
      --confcutdir=tests/platform --import-mode=importlib -p no:cacheprovider -m linux \
      ${GUEST_CI_PYTEST_ARGS:-} tests/platform/guest_linux" || rc=$?
  in_vm sudo bash -euo pipefail -c "cd $guest_repo && rm -f /tmp/guest-ci-cov.tar && \
    if ls .coverage.linux.* >/dev/null 2>&1; then tar -cf /tmp/guest-ci-cov.tar .coverage.linux.*; \
    else tar -cf /tmp/guest-ci-cov.tar --files-from /dev/null; fi && chmod 0644 /tmp/guest-ci-cov.tar"
  find "$repo" -maxdepth 1 -name '.coverage.linux.*' -delete
  local fetched
  fetched=$(mktemp -t guest-ci-cov.XXXXXX)
  limactl copy "$vm:/tmp/guest-ci-cov.tar" "$fetched"
  tar -xf "$fetched" -C "$repo"
  rm -f "$fetched"
  log "coverage data: $(find "$repo" -maxdepth 1 -name '.coverage.linux.*' | wc -l | tr -d ' ') file(s)"
  return "$rc"
}

stop_vm() {
  if [ "$(status)" = "Running" ]; then
    log "stopping $vm (kept for reuse)"
    limactl stop "$vm" >/dev/null
  fi
}

case $cmd in
run)
  [ -n "$(status)" ] || create_vm
  [ "$(status)" = "Running" ] || limactl start -y "$vm" >/dev/null
  [ "${GUEST_CI_KEEP_RUNNING:-0}" = 1 ] || trap stop_vm EXIT
  provision
  image_settings
  in_vm uname -r | sed 's/^/guest-ci: kernel /' >&2
  copy_repo
  run_tests
  ;;
stop)
  stop_vm
  ;;
delete)
  if [ -n "$(status)" ]; then limactl delete -f "$vm"; fi
  ;;
*)
  log "unknown command $cmd (run|stop|delete)"
  exit 2
  ;;
esac
