#!/usr/bin/env bash
# Puts one pinned release archive into a directory (used by provision.sh in the VM and by fetch.sh on the host):
#
#   archive.sh URL SHA256 DIR [SOURCE-DIR]
#
# DIR/<file name of URL> is kept when its SHA-256 matches. Otherwise it is copied from SOURCE-DIR (offline mode of
# up.sh --archives: nothing is downloaded; a missing or different archive there is an error) or, without SOURCE-DIR,
# downloaded with curl. A download or copy is checked before it replaces the file. Exit 0 when DIR holds the archive.
set -euo pipefail

url=${1:?usage: archive.sh URL SHA256 DIR [SOURCE-DIR]}
sum=${2:?usage: archive.sh URL SHA256 DIR [SOURCE-DIR]}
dir=${3:?usage: archive.sh URL SHA256 DIR [SOURCE-DIR]}
source_dir=${4:-}
name=$(basename "$url")
[[ $sum =~ ^[0-9a-f]{64}$ ]] || { echo "archive: invalid SHA-256 for $name" >&2; exit 2; }

digest() { # file -> its SHA-256 (sha256sum in the VM, shasum on macOS)
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}
matches() { [ -f "$1" ] && [ "$(digest "$1")" = "$sum" ]; }

target="$dir/$name"
matches "$target" && exit 0
mkdir -p "$dir"
if [ -n "$source_dir" ]; then
  matches "$source_dir/$name" || {
    echo "archive: $name with the pinned SHA-256 is missing in $source_dir (run fetch.sh on the host)" >&2
    exit 1
  }
  echo "archive: copying $name" >&2
  cp "$source_dir/$name" "$target.part"
else
  echo "archive: downloading $name" >&2
  curl -fsSL --retry 3 -o "$target.part" "$url"
fi
matches "$target.part" || { echo "archive: SHA-256 mismatch for $name" >&2; rm -f "$target.part"; exit 1; }
mv "$target.part" "$target"
