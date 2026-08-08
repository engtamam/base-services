#!/usr/bin/env bash
# ==================================================================================================
#  base-services — boot-heal.sh
#  Self-heal the cluster after a reboot — especially an UNGRACEFUL power-off (a host crash, a yanked
#  cord, VirtualBox "Power Off"). k3s/kubelet then cannot drain pods, so on restart the old pods are
#  left in phase Unknown (a.k.a. NodeLost): the deployment shows 0/1 and the app never serves, until a
#  human force-deletes them. This runs ONCE at boot (systemd, After=k3s), waits for the API, and clears
#  those stale pods so their Deployments reschedule fresh copies — no human, no downtime past boot.
#
#  Idempotent + safe: it only deletes pods whose phase is NOT Running/Succeeded (Unknown/NodeLost/
#  Failed), never a healthy pod. Install with: sudo bash boot-heal.sh --install (writes the script +
#  systemd unit + a watchdog timer). PLUG-AND-PLAY: PRODUCT_NAME names the unit/bin (as a slug).
# ==================================================================================================
set -uo pipefail
PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
SLUG="$(printf '%s' "$PRODUCT_NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-')"; [ -n "$SLUG" ] || SLUG="platform"
NS="${NS:-${NAMESPACE:-base-services}}"        # kept for extensions; heal() is cluster-wide (-A)
BIN="/usr/local/bin/$SLUG-boot-heal.sh"
UNIT="/etc/systemd/system/$SLUG-boot-heal.service"
TIMER="/etc/systemd/system/$SLUG-boot-heal.timer"
KUBECTL="${KUBECTL:-kubectl}"
command -v k3s >/dev/null 2>&1 && KUBECTL="k3s kubectl"

heal() {
  # Wait for the API server to answer (k3s just started); give up after ~2 min rather than hang forever.
  for _ in $(seq 1 60); do $KUBECTL get --raw='/readyz' >/dev/null 2>&1 && break; sleep 2; done
  # Delete stale pods across ALL namespaces: phase Unknown (NodeLost) or Failed. Force + grace 0, since
  # the runtime state is already gone. Owned pods (a Deployment/StatefulSet) come back automatically.
  local stale
  stale="$($KUBECTL get pods -A --no-headers 2>/dev/null | awk '$4=="Unknown" || $4=="NodeLost" || $4=="Failed" {print $1" "$2}')"
  if [ -n "$stale" ]; then
    echo "$stale" | while read -r ns pod; do
      [ -n "$pod" ] && $KUBECTL -n "$ns" delete pod "$pod" --force --grace-period=0 >/dev/null 2>&1 \
        && echo "  healed: $ns/$pod"
    done
  else
    echo "  nothing to heal (no stale pods)"
  fi
}

install() {
  [ "$(id -u)" = "0" ] || { echo "run with sudo to install" >&2; exit 1; }
  install -m 0755 "$0" "$BIN"

  # k3s itself must come back on boot — everything the platform runs is a k8s workload it restarts.
  systemctl enable k3s.service >/dev/null 2>&1 && echo "✔ k3s.service enabled (auto-start on boot)" || true

  # 1) The BOOT unit — clears stranded pods once, right after k3s starts (ungraceful-reboot recovery).
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=$PRODUCT_NAME boot self-heal — clear stale (Unknown/NodeLost) pods after a reboot
After=k3s.service
Wants=k3s.service

[Service]
Type=oneshot
ExecStart=$BIN --run
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
UNITEOF

  # 2) A WATCHDOG timer — re-runs the heal every few minutes, so a pod stranded by a mid-run node blip
  #    (not just a reboot) also recovers on its own, without a human. Idempotent: a no-op when healthy.
  cat > "$TIMER" <<TIMEREOF
[Unit]
Description=$PRODUCT_NAME self-heal watchdog — periodically clear stale pods

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=$SLUG-boot-heal.service

[Install]
WantedBy=timers.target
TIMEREOF

  systemctl daemon-reload
  systemctl enable "$SLUG-boot-heal.service" >/dev/null 2>&1
  systemctl enable --now "$SLUG-boot-heal.timer" >/dev/null 2>&1
  echo "✔ installed $BIN + boot unit + 5-min watchdog timer (enabled)"
}

case "${1:-}" in
  --install) install;;
  --run|"") echo "▸ $PRODUCT_NAME boot-heal"; heal;;
  *) echo "usage: $0 [--install | --run]"; exit 2;;
esac
