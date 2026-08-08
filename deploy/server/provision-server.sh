#!/usr/bin/env bash
# ==================================================================================================
#  base-services — Production server provisioning
# --------------------------------------------------------------------------------------------------
#  Turns a BLANK Ubuntu box into a host ready to run the platform on single-node k3s, then hands off
#  to deploy/k3s/bootstrap.sh (Calico + metrics-server + datastores). setup.sh runs this as phase 1.
#
#  Target:  Ubuntu 24.04 LTS (also 22.04). Run as root:  sudo bash provision-server.sh
#
#  IDEMPOTENT + RESUMABLE. Every section records a stamp under $STATE_DIR when it finishes; a re-run
#  skips finished sections, so a half-provisioned box (network drop, a reboot) is fixed by re-running
#  the same command. `--dry-run` prints the plan and changes nothing.
#
#  PLUG-AND-PLAY. Product-agnostic: PRODUCT_NAME names the banners and (as a slug) the state dir, log,
#  deploy user, and /etc drop-in files — so a product reuses this unchanged by setting one variable.
#
#  Sections:
#    0  preflight        root, Ubuntu, arch, internet
#    1  base packages    ca-certificates, curl, ufw, fail2ban, chrony, unattended-upgrades, jq
#    2  deploy user      non-root sudo user + SSH authorized_keys (from $DEPLOY_PUBKEY / URL)
#    3  SSH hardening     key-only, no root login  (ONLY once a key is installed — never lock out)
#    4  firewall (UFW)    deny-in; allow SSH + 80 + 443
#    5  fail2ban          sshd jail
#    6  auto-updates      unattended security upgrades
#    7  time + locale     timezone + en_US.UTF-8 (Arabic content is UTF-8 end-to-end)
#    8  swap              swapfile when RAM is small
#    9  kernel + ulimits  inotify/conntrack/file-max for Postgres + k8s
#   10  Docker CE         official repo + compose plugin
#   10.5 Helm             deploy goes through the base-services Helm chart
#   11  k3s               single-node Kubernetes (flannel policy OFF for Calico; Traefik OFF — own edge)
#
#  Environment (all optional; never hardcode a secret here):
#    PRODUCT_NAME    banner + slug for names        (default: base-services)
#    DEPLOY_USER     deploy account name            (default: the slug)
#    DEPLOY_PUBKEY   an SSH public key STRING, or a URL/path to one (e.g. https://github.com/you.keys)
#    SSH_PORT        sshd port                      (default: 22)
#    TIMEZONE        IANA tz                        (default: Asia/Riyadh)
#    K3S_VERSION     pin the k3s channel/version    (default: stable)
#    SKIP_SSH_HARDEN =1 to keep password SSH (useful on a console-only VM)
# ==================================================================================================
set -Eeuo pipefail

# ── identity (a filesystem-safe slug drives state dir, log, drop-in file names) ───────────────────
PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
SLUG="$(printf '%s' "$PRODUCT_NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-')"
[[ -n "$SLUG" ]] || SLUG="platform"

# ── knobs (env-overridable; the script never edits itself to retarget) ────────────────────────────
DEPLOY_USER="${DEPLOY_USER:-$SLUG}"
DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}"
SSH_PORT="${SSH_PORT:-22}"
TIMEZONE="${TIMEZONE:-Asia/Riyadh}"
K3S_VERSION="${K3S_VERSION:-stable}"
SKIP_SSH_HARDEN="${SKIP_SSH_HARDEN:-0}"
SWAP_MIN_RAM_GB="${SWAP_MIN_RAM_GB:-4}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

STATE_DIR="/var/lib/$SLUG/setup"        # completed-section stamps (resume across re-runs)
LOG_FILE="/var/log/$SLUG-provision.log"
CHANGES=()

# ── tiny ui + helpers ─────────────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then BOLD=$'\e[1m'; CY=$'\e[36m'; GR=$'\e[32m'; YE=$'\e[33m'; RE=$'\e[31m'; DIM=$'\e[2m'; NC=$'\e[0m'
else BOLD=; CY=; GR=; YE=; RE=; DIM=; NC=; fi
ts()     { date '+%Y-%m-%d %H:%M:%S'; }
header() { echo -e "\n${BOLD}${CY}━━━  $*  ━━━${NC}"; }
info()   { echo -e "${CY}  $(ts) ▸ $*${NC}"; }
ok()     { echo -e "${GR}  $(ts) ✔ $*${NC}"; }
warn()   { echo -e "${YE}  $(ts) ⚠ $*${NC}"; }
die()    { echo -e "${RE}  $(ts) ✖ FATAL: $*${NC}" >&2; [[ -f "$LOG_FILE" ]] && echo "See $LOG_FILE" >&2; exit 1; }
record() { CHANGES+=("$1"); }
have()   { command -v "$1" >/dev/null 2>&1; }
# The deploy user's home — TOLERANT: `getent` exits 2 for an unknown user, which under `set -e` would
# abort the whole run (e.g. in --dry-run, where the user was never actually created). Never fail; fall
# back to the conventional path so a dry-run can plan the SSH/kubeconfig steps without the user existing.
user_home() { local h; h="$(getent passwd "$1" 2>/dev/null | cut -d: -f6 || true)"; echo "${h:-/home/$1}"; }
run() {
  if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] $*${NC}"; return 0; fi
  "$@"
}
write_file() {  # write_file <path> <mode> <<<content  — idempotent, dry-run aware
  local path="$1" mode="$2" content; content="$(cat)"
  if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] write $path (${mode})${NC}"; return 0; fi
  [[ -f "$path" && "$(cat "$path")" == "$content" ]] && return 0
  install -D -m "$mode" /dev/null "$path"; printf '%s\n' "$content" > "$path"
}
section_done() { [[ -f "$STATE_DIR/$1" ]]; }
mark_done()    { (( DRY_RUN )) || { mkdir -p "$STATE_DIR"; touch "$STATE_DIR/$1"; }; }
# Run a section unless its stamp exists. `force:` prefix always runs (idempotent asserts like the firewall).
step() {
  local name="$1" fn="$2" force="${3:-}"
  if [[ "$force" != "force" ]] && section_done "$name"; then ok "[$name] already done — skipping"; return; fi
  "$fn"; mark_done "$name"
}
_on_error() {
  local code=$? line=${BASH_LINENO[0]}
  echo -e "\n${RE}  ✖ failed at line $line (exit $code)${NC}" >&2
  echo -e "${YE}  Re-run to resume — finished sections are skipped: sudo bash $0${NC}" >&2
}
trap _on_error ERR

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 0 — preflight
# ══════════════════════════════════════════════════════════════════════════════════════════════════
preflight() {
  header "Preflight"
  [[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
  [[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || warn "tested on Ubuntu; ID=${ID:-?} may work but is unverified"
  case "$(uname -m)" in x86_64|aarch64) ok "arch $(uname -m)";; *) die "unsupported arch $(uname -m)";; esac
  if curl -sf --connect-timeout 5 https://get.k3s.io >/dev/null 2>&1; then ok "internet reachable"
  else warn "cannot reach get.k3s.io — online installs will fail if this box is air-gapped"; fi
  mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"
  ok "Ubuntu ${VERSION_ID:-?}, provisioning as $([[ $DRY_RUN == 1 ]] && echo 'DRY-RUN' || echo 'LIVE')"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 1 — base packages
# ══════════════════════════════════════════════════════════════════════════════════════════════════
install_base() {
  header "Base packages"
  run apt-get -o DPkg::Lock::Timeout=600 update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq \
    ca-certificates curl gnupg jq git python3 python3-jwt openssl \
    ufw fail2ban unattended-upgrades chrony \
    apt-transport-https
  ok "base packages present (curl, jq, ufw, fail2ban, chrony, ...)"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 2 — deploy user + SSH key
# ══════════════════════════════════════════════════════════════════════════════════════════════════
resolve_pubkey() {  # echoes the key material, or empty
  local src="$DEPLOY_PUBKEY"
  [[ -z "$src" ]] && return 0
  if [[ "$src" == ssh-* ]]; then printf '%s\n' "$src"; return 0; fi
  if [[ "$src" =~ ^https?:// ]]; then curl -fsSL "$src" 2>/dev/null; return 0; fi
  [[ -f "$src" ]] && cat "$src"
}
setup_deploy_user() {
  header "Deploy user ($DEPLOY_USER)"
  if id "$DEPLOY_USER" >/dev/null 2>&1; then ok "user $DEPLOY_USER exists"
  else run adduser --disabled-password --gecos "" "$DEPLOY_USER"; record "created user $DEPLOY_USER"; fi
  run usermod -aG sudo "$DEPLOY_USER"
  # Passwordless sudo for the deploy account (key-only login means no password to type).
  write_file "/etc/sudoers.d/90-$DEPLOY_USER" 0440 <<< "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL"

  local key; key="$(resolve_pubkey)"
  if [[ -n "$key" ]]; then
    local home; home="$(user_home "$DEPLOY_USER")"
    run install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
    if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] install SSH key for $DEPLOY_USER${NC}"
    else
      grep -qxF "$key" "$home/.ssh/authorized_keys" 2>/dev/null || printf '%s\n' "$key" >> "$home/.ssh/authorized_keys"
      chmod 600 "$home/.ssh/authorized_keys"; chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
    fi
    record "installed SSH key for $DEPLOY_USER"; ok "SSH key installed for $DEPLOY_USER"
  else
    warn "no DEPLOY_PUBKEY given — SSH key NOT installed (SSH hardening will be skipped to avoid lockout)"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 3 — SSH hardening (ONLY when a key is present — never lock yourself out)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
harden_ssh() {
  header "SSH hardening"
  local home; home="$(user_home "$DEPLOY_USER")"
  if [[ "$SKIP_SSH_HARDEN" == "1" ]]; then warn "SKIP_SSH_HARDEN=1 — leaving password auth on"; return; fi
  if [[ ! -s "$home/.ssh/authorized_keys" ]]; then
    warn "no authorized_keys for $DEPLOY_USER — REFUSING to disable password auth (would lock you out)"; return
  fi
  write_file "/etc/ssh/sshd_config.d/50-$SLUG.conf" 0644 <<EOF
Port ${SSH_PORT}
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
  record "hardened sshd (key-only, no root, port ${SSH_PORT})"
  run systemctl restart ssh 2>/dev/null || run systemctl restart sshd 2>/dev/null || true
  ok "sshd hardened — keep THIS session open and verify a new key login before closing it"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 4 — firewall  (force: always re-assert; UFW adds are idempotent)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_firewall() {
  header "Firewall (UFW)"
  have ufw || { warn "ufw missing — skipping"; return; }
  run ufw --force default deny incoming
  run ufw --force default allow outgoing
  run ufw allow "${SSH_PORT}/tcp" comment 'SSH'
  run ufw allow 80/tcp  comment 'HTTP'
  run ufw allow 443/tcp comment 'HTTPS'
  if ufw status 2>/dev/null | grep -q "Status: active"; then ok "UFW active — rules ensured"
  else run ufw --force enable; ok "UFW enabled (deny-in; allow ${SSH_PORT}/80/443)"; fi
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 5 — fail2ban
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_fail2ban() {
  header "fail2ban"
  have fail2ban-server || { warn "fail2ban missing — skipping"; return; }
  write_file "/etc/fail2ban/jail.d/$SLUG-sshd.local" 0644 <<EOF
[sshd]
enabled  = true
port     = ${SSH_PORT}
backend  = systemd
maxretry = 4
findtime = 600
bantime  = 3600
EOF
  run systemctl enable fail2ban; run systemctl restart fail2ban
  ok "fail2ban active (sshd jail; 1h ban after 4 fails)"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 6 — unattended security upgrades
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_auto_updates() {
  header "Unattended security upgrades"
  write_file /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  run systemctl enable unattended-upgrades 2>/dev/null || true
  ok "unattended security upgrades enabled"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 7 — time + UTF-8 locale (Arabic content is UTF-8 end to end)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_time_locale() {
  header "Time + locale"
  run timedatectl set-timezone "$TIMEZONE" 2>/dev/null || warn "could not set tz $TIMEZONE"
  run systemctl enable chrony 2>/dev/null || true
  run systemctl restart chrony 2>/dev/null || true
  ok "timezone $TIMEZONE, NTP active"
  if locale -a 2>/dev/null | grep -qiE 'en_US\.utf-?8'; then ok "UTF-8 locale present"
  else
    run sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen 2>/dev/null || true
    run locale-gen en_US.UTF-8 2>/dev/null || true
    run update-locale LANG=en_US.UTF-8 2>/dev/null || true
    ok "en_US.UTF-8 generated (covers Arabic script)"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 8 — swap
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_swap() {
  header "Swap"
  local ram_gb; ram_gb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
  if swapon --show 2>/dev/null | grep -q .; then ok "swap already active"; return; fi
  if (( ram_gb >= SWAP_MIN_RAM_GB )); then ok "RAM ${ram_gb}GB ≥ ${SWAP_MIN_RAM_GB}GB — no swap needed"; return; fi
  info "RAM ${ram_gb}GB — creating ${SWAP_SIZE_GB}GB swapfile"
  if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] swapfile ${SWAP_SIZE_GB}G${NC}"
  else
    fallocate -l "${SWAP_SIZE_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$(( SWAP_SIZE_GB*1024 )) status=none
    chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  ok "swap active (${SWAP_SIZE_GB}GB)"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 9 — kernel + ulimit tuning (Postgres + k8s: inotify watches, conntrack, file handles)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
setup_kernel() {
  header "Kernel + ulimits"
  write_file "/etc/sysctl.d/90-$SLUG.conf" 0644 <<'EOF'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches   = 524288
fs.file-max                   = 2097152
net.core.somaxconn            = 4096
vm.max_map_count              = 262144
net.ipv4.ip_forward           = 1
EOF
  run sysctl --system >/dev/null 2>&1 || true
  write_file "/etc/security/limits.d/90-$SLUG.conf" 0644 <<'EOF'
*  soft  nofile  1048576
*  hard  nofile  1048576
EOF
  ok "sysctl + nofile limits applied (k8s/Postgres friendly)"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 10 — Docker CE (official repo + compose plugin)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
install_docker() {
  header "Docker CE"
  if have docker && docker info >/dev/null 2>&1; then ok "Docker already installed";
  else
    run install -m 0755 -d /etc/apt/keyrings
    if (( ! DRY_RUN )); then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
      . /etc/os-release
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    fi
    run apt-get -o DPkg::Lock::Timeout=600 update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    record "installed Docker CE"
  fi
  run systemctl enable --now docker 2>/dev/null || true
  run usermod -aG docker "$DEPLOY_USER" 2>/dev/null || true
  ok "Docker running; $DEPLOY_USER in docker group"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 10.5 — Helm  (deploy goes through the base-services Helm CHART; setup.sh phase 6 dies without it)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
install_helm() {
  header "Helm"
  if have helm; then ok "helm present ($(helm version --short 2>/dev/null || echo '?'))"; return; fi
  if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] install helm 3 via get-helm-3${NC}"; return; fi
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash >/dev/null
  record "installed helm $(helm version --short 2>/dev/null || echo '')"
  ok "helm installed"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# 11 — k3s (single-node Kubernetes; Traefik OFF — the platform exposes via its own edge)
#
#  base-services' entire security model is a default-deny NetworkPolicy on every service, and stock
#  flannel does NOT enforce NetworkPolicy: the objects apply cleanly, `kubectl get netpol` looks
#  right, and traffic fails OPEN — deny-by-default becomes theatre with a green rollout. So we
#  disable flannel's policy path here and let deploy/k3s/bootstrap.sh install Calico on top.
#
#  These flags are install-time only: they cannot be added to a running k3s afterwards without
#  reinstalling it. Getting them wrong here is not a config fix later.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
install_k3s() {
  header "k3s (single-node Kubernetes)"
  local k3s_args="--flannel-backend=none --disable-network-policy --disable traefik --write-kubeconfig-mode 644"
  if have kubectl && systemctl is-active --quiet k3s 2>/dev/null; then
    ok "k3s already running"
    # Assert the CNI assumption rather than trust it: a k3s installed by hand still runs, still
    # answers, and still fails open. Warn loudly — silence here is how a cluster ends up in
    # production without policy enforcement.
    if [[ -f /etc/systemd/system/k3s.service ]] && ! grep -q 'flannel-backend=none' /etc/systemd/system/k3s.service 2>/dev/null; then
      warn "this k3s was installed WITH flannel — NetworkPolicy will NOT be enforced."
      warn "preflight.sh will fail (correctly). Fix: k3s-uninstall.sh, then re-run this script."
    fi
  else
    if (( DRY_RUN )); then echo -e "${DIM}  [dry-run] curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=$K3S_VERSION sh -s - $k3s_args${NC}"
    else
      # shellcheck disable=SC2086
      curl -sfL https://get.k3s.io | \
        INSTALL_K3S_CHANNEL="$K3S_VERSION" \
        sh -s - $k3s_args
    fi
    record "installed k3s (flannel policy off for Calico, traefik disabled)"
  fi
  # Make kubectl usable for root AND the deploy user (k3s writes /etc/rancher/k3s/k3s.yaml).
  if (( ! DRY_RUN )); then
    local home; home="$(user_home "$DEPLOY_USER")"
    install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.kube"
    install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /etc/rancher/k3s/k3s.yaml "$home/.kube/config" 2>/dev/null || true
    grep -q 'KUBECONFIG=/etc/rancher' /root/.bashrc 2>/dev/null || echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> /root/.bashrc
    # WAIT FOR THE NODE TO BE READY before handing off. A fresh k3s answers its API within a second but
    # the node needs 10–30s to go Ready; chaining bootstrap.sh immediately would otherwise apply
    # workloads onto a node that cannot yet schedule them (pods stuck Pending). Bounded, non-fatal.
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    if kubectl wait --for=condition=Ready node --all --timeout=90s >/dev/null 2>&1; then
      ok "k3s node Ready"
    else
      warn "k3s node not Ready within 90s — bootstrap.sh may need a moment (or re-run)"
    fi
  fi
  run kubectl get nodes 2>/dev/null || true
  ok "k3s ready — kubectl configured for root and $DEPLOY_USER"
}

# ══════════════════════════════════════════════════════════════════════════════════════════════════
main() {
  header "$PRODUCT_NAME — server provisioning"
  preflight
  step base            install_base
  # deploy_user + ssh_harden are `force` (re-assert every run, like the firewall). They are idempotent,
  # and — crucially — a first run WITHOUT a key legitimately no-ops the key install and the hardening;
  # stamping those "done" would mean a later run WITH a key (DEPLOY_PUBKEY) silently never installs the
  # key nor hardens SSH. Re-asserting fixes the two-phase "box first, key later" flow.
  step deploy_user     setup_deploy_user  force
  step ssh_harden      harden_ssh         force
  step firewall        setup_firewall     force
  step fail2ban        setup_fail2ban
  step auto_updates    setup_auto_updates
  step time_locale     setup_time_locale
  step swap            setup_swap
  step kernel          setup_kernel
  step docker          install_docker
  step helm            install_helm
  step k3s             install_k3s

  header "Done"
  if (( ${#CHANGES[@]} )); then echo -e "${GR}Changes:${NC}"; printf '  • %s\n' "${CHANGES[@]}"; else echo "No changes (already provisioned)."; fi
  cat <<EOF

${BOLD}Next — the cluster (setup.sh does this for you; or run it directly):${NC}

  sudo deploy/k3s/bootstrap.sh   # Calico + metrics-server + postgres/redis
  sudo deploy/k3s/preflight.sh   # PROVES default-deny is enforced, not just installed

bootstrap only INSTALLS. preflight PROVES. Do not skip preflight — it is the only thing that
tells you whether this host's NetworkPolicy actually drops a packet.
EOF
}
main "$@"
