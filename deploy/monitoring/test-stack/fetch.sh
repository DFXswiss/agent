#!/usr/bin/env bash
# Downloads the pinned archives of versions.env into a directory on the host, once, before hermetic runs:
#
#   fetch.sh DIR
#
# `up.sh --archives DIR` then provisions the stack from DIR without any download from the VM (E2E-17). Archives
# already present with the pinned SHA-256 are kept; every download is checked (archive.sh).
set -euo pipefail

dir=${1:?usage: fetch.sh DIR}
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=versions.env
. "$here/versions.env"
for item in PROMETHEUS NODE_EXPORTER GRAFANA; do
  url_var=${item}_URL sum_var=${item}_SHA256
  bash "$here/archive.sh" "${!url_var}" "${!sum_var}" "$dir"
done
echo "fetch: $dir holds the archives of versions.env" >&2
