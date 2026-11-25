#!/usr/bin/env bash
# Linux-only guest tests in the Lima VM `<prefix>guest-ci` (make platform-ci-linux; docs/platform/spec/07-testing.md
# sections 1, 3 and 7; ADR-027 section 7). Runs on the runner device.
#
#   scripts/guest-ci.sh [run]   create or reuse the VM, copy the checkout in, run the tests marked `linux` as root
#                               under coverage, copy .coverage.linux.* back into the checkout, compare the web build
#                               of this device with the one of the VM (web-build), stop the VM
#   scripts/guest-ci.sh web-build   only the build comparison: build web/ here and in the VM below another directory
#                               (one with a `src` component) with the same commit, time and version; the build hashes
#                               must be equal (06-web.md section 10.3, decision HOT-18)
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
provision_version=4
# Same major versions as the platform-test extra (pytest-timeout: the hard limit per test of tests/platform/pytest.ini);
# coverage data files are compatible across 7.x.
pip_packages="pytest>=8.3.0 pytest-timeout>=2.4 coverage>=7.10.7"
# Node of the work image (images/versions.json, checksum-pinned) for the web build of the VM.
guest_node=/opt/guest-ci/node
# The web build of the VM: below a directory named `src`, unlike the checkout on this device.
guest_web=/var/lib/guest-ci/src/agent/web
guest_web_out=/var/lib/guest-ci/web-out

log() { printf 'guest-ci: %s\n' "$*" >&2; }

# A value of images/versions.json (the pins of the work image).
version_of() {
  sed -n "s/^ *\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$repo/images/versions.json"
}

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
  local node_url node_sha
  node_url=$(version_of node_tarball_url)
  node_sha=$(version_of node_tarball_sha256)
  in_vm sudo env "PIP_PACKAGES=$pip_packages" "VENV=$guest_venv" "MARK=/var/lib/guest-ci/provisioned-$provision_version" \
    "NODE_URL=$node_url" "NODE_SHA=$node_sha" "NODE_DIR=$guest_node" \
    bash -euo pipefail -c '
      [ -e "$MARK" ] && exit 0
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq python3-venv git nftables acl tmux curl xz-utils libatomic1
      python3 -m venv "$VENV"
      "$VENV/bin/pip" install -q $PIP_PACKAGES
      curl -fsSL "$NODE_URL" -o /tmp/guest-ci-node.tar.xz
      echo "$NODE_SHA  /tmp/guest-ci-node.tar.xz" | sha256sum -c --quiet
      rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
      tar -xJf /tmp/guest-ci-node.tar.xz -C "$NODE_DIR" --strip-components 1 && rm -f /tmp/guest-ci-node.tar.xz
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
    pyproject.toml README.md LICENSE .coveragerc-platform contracts src docs/platform/agents.md tests/platform
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
      ${GUEST_CI_PYTEST_ARGS:-} tests/platform/guest_linux tests/platform/test_install_block.py" || rc=$?
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

# The web build of both sides: a fresh copy of web/ with its own `npm ci` (same lock file, no install scripts), built
# with an empty environment but for PATH, HOME and the commit time, commit and version (outside a git checkout - the
# gates copy the tree without .git - fixed values, the same for both sides). This device builds below the scratch
# directory, the VM below $guest_web (a `src` component), in another time zone.
web_build() {
  local scratch="${TMPDIR:-/tmp}/guest-ci-web-$vm" epoch commit version host_node vm_node rc=0
  epoch=$(git -C "$repo" log -1 --format=%ct 2>/dev/null) || epoch=0
  commit=$(git -C "$repo" rev-parse HEAD 2>/dev/null) || commit=0000000000000000000000000000000000000000
  version=$(node -p 'require(process.argv[1]).version' "$repo/web/package.json") || { log "web build: node missing"; return 1; }
  local build=("SOURCE_DATE_EPOCH=$epoch" "BUILD_COMMIT=$commit" "BUILD_VERSION=$version")
  rm -rf "$scratch" && mkdir -p "$scratch/host"
  COPYFILE_DISABLE=1 tar -C "$repo" --exclude 'web/node_modules' -czf "$scratch/web.tgz" web
  host_node=$(node --version)
  vm_node=$(in_vm "$guest_node/bin/node" --version) || { log "web build: no Node in the VM"; return 1; }
  log "web build: ${build[*]}, Node $host_node here, $vm_node in the VM"
  [ "$host_node" = "$vm_node" ] || log "web build: WARNING the Node versions differ (VM: images/versions.json)"
  limactl copy "$scratch/web.tgz" "$vm:/tmp/guest-ci-web.tgz"
  in_vm sudo env -i "${build[@]}" "PATH=$guest_node/bin:/usr/bin:/bin" HOME=/root TZ=Pacific/Kiritimati \
    "npm_config_cache=/var/lib/guest-ci/npm-cache" "WEB=$guest_web" "OUT=$guest_web_out" \
    bash -euo pipefail -c 'rm -rf "$(dirname "$WEB")" "$OUT" && mkdir -p "$(dirname "$WEB")" &&
      tar --no-same-owner -xzf /tmp/guest-ci-web.tgz -C "$(dirname "$WEB")" && rm -f /tmp/guest-ci-web.tgz &&
      cd "$WEB" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error >/dev/null &&
      node scripts/build-compare.mjs build "$OUT" >&2 && chmod 0644 "$OUT/build-manifest.json"' ||
    { log "web build: the build in the VM failed"; rc=1; }
  [ "$rc" = 0 ] && { limactl copy "$vm:$guest_web_out/build-manifest.json" "$scratch/linux.json" || rc=1; }
  [ "$rc" = 0 ] && {
    (tar -xzf "$scratch/web.tgz" -C "$scratch/host" && cd "$scratch/host/web" &&
      npm ci --ignore-scripts --no-audit --no-fund --loglevel=error >/dev/null &&
      env -i "${build[@]}" "PATH=$PATH" "HOME=$HOME" node scripts/build-compare.mjs build "$scratch/out" >&2) ||
      { log "web build: the build on this device failed"; rc=1; }
  }
  [ "$rc" = 0 ] && {
    (cd "$scratch/host/web" && node scripts/build-compare.mjs compare "$scratch/out/build-manifest.json" \
      "$scratch/linux.json" 2>&1) | sed 's/^/guest-ci: web build (this device \/ Linux VM): /' >&2 || rc=1
  }
  rm -rf "$scratch"
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
  rc=0
  run_tests || rc=$?
  web_build || rc=1
  exit "$rc"
  ;;
web-build)
  [ -n "$(status)" ] || create_vm
  [ "$(status)" = "Running" ] || limactl start -y "$vm" >/dev/null
  [ "${GUEST_CI_KEEP_RUNNING:-0}" = 1 ] || trap stop_vm EXIT
  provision
  web_build
  ;;
stop)
  stop_vm
  ;;
delete)
  if [ -n "$(status)" ]; then limactl delete -f "$vm"; fi
  ;;
*)
  log "unknown command $cmd (run|web-build|stop|delete)"
  exit 2
  ;;
esac
