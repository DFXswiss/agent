#!/usr/bin/env bash
# Checks of the provisioned test stack inside the VM (run as root by up.sh --check):
#
#   check.sh SERVER [--expect-active RULE-UID]… [--timeout SECONDS]
#
# 1. promtool check metrics on the runner textfile node_exporter reads (through the symlink of textfile-dir.md).
# 2. The alert file converted into Prometheus rules (agent_monitoring.rules, devices `test-device`, `healthy-device`):
#    promtool check rules, then promtool test rules with alerting/agent-platform.test.yaml (every rule fires
#    in its test case and stays silent in the healthy one).
# 3. No error of Grafana's provisioning in the journal of its current start.
# 4. agent_monitoring.stack: series in Prometheus, dashboards and their queries in Grafana, rules provisioned and
#    evaluated without error, the --expect-active rules pending or firing.
# 5. Offline mode (up.sh --archives): the VM's outbound drop of lima.yaml is in place.
# Prints one line per step and the JSON report of step 4; exit 0 only when every step passed.
set -euo pipefail

server=${1:?usage: check.sh SERVER [--expect-active UID]… [--timeout S]}
shift
assets=/opt/agent-monitoring
textfile=/var/lib/agent-monitoring/textfile/agent_runner.prom
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export PYTHONPATH=$assets

step() { printf 'check: %s\n' "$*"; }

if [ -d /mnt/agent-monitoring-archives ]; then
  # offline mode fails closed: without the drop the VM would reach the internet
  /usr/sbin/nft list chain inet agent_monitoring_offline output | grep -q 'policy drop;' || {
    step "offline: outbound drop missing"
    exit 1
  }
  step "offline: outbound traffic dropped"
fi

/usr/local/bin/promtool check metrics <"$textfile"
step "promtool check metrics: ok ($(grep -c '^# TYPE ' "$textfile") metrics)"

python3 -m agent_monitoring.rules --device test-device "$assets/alerting/agent-platform.yaml" \
  "$work/checked.rules.yaml"
/usr/local/bin/promtool check rules "$work/checked.rules.yaml"
step "promtool check rules: ok"
python3 -m agent_monitoring.rules --without-annotations --device test-device --device healthy-device \
  "$assets/alerting/agent-platform.yaml" "$work/agent-platform.rules.yaml"
cp "$assets/alerting/agent-platform.test.yaml" "$work/"
(cd "$work" && /usr/local/bin/promtool test rules agent-platform.test.yaml)
step "promtool test rules: ok"

# Only the current start of Grafana: an earlier start may have logged errors that provision.sh has fixed since.
invocation=$(systemctl show -p InvocationID --value agent-monitoring-grafana)
[ -n "$invocation" ] || { step "grafana: not running"; exit 1; }
errors=$(journalctl "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat | grep -E 'logger=provisioning' |
  grep -E 'level=(error|crit)' || true)
if [ -n "$errors" ]; then
  printf '%s\n' "$errors" >&2
  step "grafana provisioning: errors"
  exit 1
fi
step "grafana provisioning: no errors"

python3 -m agent_monitoring.stack --prometheus http://127.0.0.1:9090 --grafana http://127.0.0.1:3000 \
  --grafana-password-file /etc/agent-monitoring/grafana-admin --server "$server" --textfile "$textfile" "$@"
step "prometheus and grafana APIs: ok"
