#!/usr/bin/env bash
# Installs and (re)starts the monitoring test stack inside the VM (run as root by up.sh; idempotent):
#
#   provision.sh SERVER
#
# - Prometheus, node_exporter and Grafana from the release archives of versions.env (SHA-256 checked, cached under
#   /var/cache/agent-monitoring by archive.sh), systemd units agent-monitoring-{node-exporter,prometheus,grafana}.
#   Offline mode (up.sh --archives: the host's archive directory is mounted at /mnt/agent-monitoring-archives): the
#   archives are copied from there and nothing is downloaded.
# - node_exporter (root, like the host's LaunchDaemon) reads /var/lib/agent-monitoring/textfile, which holds only the
#   symlink agent_runner.prom -> /mnt/agent-runner-textfile/agent_runner.prom: the install step of textfile-dir.md.
# - Prometheus scrapes it every 15 s and adds server=SERVER, the label the host Alloy adds in the fleet.
# - Grafana provisions the data source `prometheus`, the three dashboards in the folder "Agent platform" (provider
#   with foldersFromFilesStructure, as in the infrastructure repository) and the alert file rendered for SERVER.
#   Admin password: random, /etc/agent-monitoring/grafana-admin (root:agent-grafana 0640), read by Grafana through
#   `$__file{…}` in /etc/agent-monitoring/grafana.ini (the `__FILE` variables are a feature of the Docker image's
#   entrypoint, not of the binary); no plugin download, no telemetry. Every service listens on the VM loopback.
set -euo pipefail

server=${1:?usage: provision.sh SERVER}
[[ $server =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "provision: invalid server name" >&2; exit 2; }
assets=/opt/agent-monitoring
runner_textfile=/mnt/agent-runner-textfile
etc=/etc/agent-monitoring
var=/var/lib/agent-monitoring
cache=/var/cache/agent-monitoring
# shellcheck source=versions.env
. "$assets/test-stack/versions.env"

log() { printf 'provision: %s\n' "$*" >&2; }

[ "$(uname -m)" = aarch64 ] || { log "only linux-arm64 archives are pinned"; exit 2; }

offline=
[ -d /mnt/agent-monitoring-archives ] && offline=/mnt/agent-monitoring-archives

fetch() { # name url sha256 -> /opt/<name> (archive content without its top directory)
  local name=$1 url=$2 sum=$3 archive
  archive="$cache/$(basename "$url")"
  bash "$assets/test-stack/archive.sh" "$url" "$sum" "$cache" $offline
  if [ ! -e "/opt/$name/.sha256-$sum" ]; then
    rm -rf "/opt/$name" && mkdir -p "/opt/$name"
    tar -xzf "$archive" -C "/opt/$name" --strip-components=1 --no-same-owner
    touch "/opt/$name/.sha256-$sum"
  fi
}

mkdir -p "$cache" "$etc" "$var/textfile" "$var/prometheus" "$var/grafana" "$etc/grafana/provisioning"
fetch "prometheus-$PROMETHEUS_VERSION" "$PROMETHEUS_URL" "$PROMETHEUS_SHA256"
fetch "node_exporter-$NODE_EXPORTER_VERSION" "$NODE_EXPORTER_URL" "$NODE_EXPORTER_SHA256"
fetch "grafana-$GRAFANA_VERSION" "$GRAFANA_URL" "$GRAFANA_SHA256"
ln -sfn "/opt/prometheus-$PROMETHEUS_VERSION/promtool" /usr/local/bin/promtool

id -u agent-prometheus >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin agent-prometheus
id -u agent-grafana >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin agent-grafana

# The install step of textfile-dir.md: a root-owned link in the collector directory to the runner's own file.
chmod 0755 "$var/textfile"
ln -sfn "$runner_textfile/agent_runner.prom" "$var/textfile/agent_runner.prom"

cat >"$etc/prometheus.yml" <<EOF
global:
  scrape_interval: 15s
  evaluation_interval: 15s
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["localhost:9100"]
        labels:
          server: "$server"
EOF
/usr/local/bin/promtool check config "$etc/prometheus.yml" >/dev/null

provisioning=$etc/grafana/provisioning
rm -rf "$provisioning" && mkdir -p "$provisioning/datasources" "$provisioning/alerting" "$provisioning/plugins" \
  "$provisioning/dashboards/json/Agent platform"
cat >"$provisioning/datasources/prometheus.yaml" <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    editable: false
EOF
cat >"$provisioning/dashboards/dashboards.yaml" <<EOF
apiVersion: 1
providers:
  - name: default
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    editable: true
    options:
      path: $provisioning/dashboards/json
      foldersFromFilesStructure: true
EOF
cp "$assets"/dashboards/agent-platform-*.json "$provisioning/dashboards/json/Agent platform/"
PYTHONPATH=$assets python3 - "$server" "$assets/alerting/agent-platform.yaml" \
  "$provisioning/alerting/agent-platform.yaml" <<'EOF'
import sys
from pathlib import Path

from agent_monitoring.assets import render_devices

server, source, target = sys.argv[1:]
Path(target).write_text(render_devices(Path(source).read_text(encoding="utf-8"), [server]), encoding="utf-8")
EOF
chown -R root:agent-grafana "$provisioning" && chmod -R g+rX "$provisioning"

if [ ! -s "$etc/grafana-admin" ]; then
  umask 077
  # no trailing newline in the password
  head -c 24 /dev/urandom | base64 | tr -d '/+=\n' >"$etc/grafana-admin"
  umask 022
fi
chown root:agent-grafana "$etc/grafana-admin" && chmod 0640 "$etc/grafana-admin"
printf '[security]\nadmin_password = $__file{%s}\n' "$etc/grafana-admin" >"$etc/grafana.ini"
chown agent-prometheus "$var/prometheus"
chown agent-grafana "$var/grafana"

unit() { # name user exec [env…]
  local name=$1 user=$2 exec=$3
  shift 3
  {
    echo "[Unit]"
    echo "Description=agent monitoring test stack: $name"
    echo "After=network-online.target"
    echo "[Service]"
    echo "User=$user"
    echo "ExecStart=$exec"
    for item in "$@"; do echo "Environment=$item"; done
    echo "Restart=on-failure"
    echo "[Install]"
    echo "WantedBy=multi-user.target"
  } >"/etc/systemd/system/agent-monitoring-$name.service"
}
unit node-exporter root "/opt/node_exporter-$NODE_EXPORTER_VERSION/node_exporter --web.listen-address=127.0.0.1:9100 --collector.textfile.directory=$var/textfile"
unit prometheus agent-prometheus "/opt/prometheus-$PROMETHEUS_VERSION/prometheus --config.file=$etc/prometheus.yml --storage.tsdb.path=$var/prometheus --web.listen-address=127.0.0.1:9090"
unit grafana agent-grafana \
  "/opt/grafana-$GRAFANA_VERSION/bin/grafana server --homepath /opt/grafana-$GRAFANA_VERSION --config $etc/grafana.ini" \
  "GF_PATHS_DATA=$var/grafana" "GF_PATHS_LOGS=$var/grafana/log" "GF_PATHS_PLUGINS=$var/grafana/plugins" \
  "GF_PATHS_PROVISIONING=$provisioning" "GF_SERVER_HTTP_ADDR=127.0.0.1" "GF_SERVER_HTTP_PORT=3000" \
  "GF_ANALYTICS_REPORTING_ENABLED=false" \
  "GF_ANALYTICS_CHECK_FOR_UPDATES=false" "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false" \
  "GF_PLUGINS_PREINSTALL_DISABLED=true" "GF_NEWS_NEWS_FEED_ENABLED=false"

systemctl daemon-reload
for name in node-exporter prometheus grafana; do
  systemctl enable -q "agent-monitoring-$name"
  systemctl restart "agent-monitoring-$name"
done

wait_for() { # url label
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0
    sleep 2
  done
  log "$2 did not become ready ($1)"
  systemctl --no-pager status "agent-monitoring-$2" >&2 || true
  exit 1
}
wait_for http://127.0.0.1:9100/metrics node-exporter
wait_for http://127.0.0.1:9090/-/ready prometheus
wait_for http://127.0.0.1:3000/api/health grafana
log "ready: server=$server prometheus :9090 grafana :3000 node_exporter :9100 (VM loopback)"
