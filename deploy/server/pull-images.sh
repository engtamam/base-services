#!/usr/bin/env bash
# ==================================================================================================
#  base-services — smart image fetch (installer side)
# --------------------------------------------------------------------------------------------------
#  Makes the platform's images pullable on the server WITHOUT hand-editing a manifest or re-typing a
#  token. The base-services images are PUBLIC, so a clean install needs NO credential — this script
#  DETECTS that and no-ops. It earns its keep for a PRODUCT that adds PRIVATE images:
#
#    1. It derives the image list from the values files (image.repository + digest) and groups it by
#       REGISTRY host — the single source of truth, not a hand-kept list.
#    2. For each host it tests an ANONYMOUS pull. Public host -> nothing to do. Private host -> it needs
#       a credential: reuse a SAVED one silently, else PROMPT once (hidden) / read REG_USER+REG_TOKEN,
#       verify it against the registry, and SAVE it (root-only, 600) so future runs are non-interactive.
#    3. It writes a Kubernetes imagePullSecret (dockerconfigjson) and binds it to the namespace's
#       default ServiceAccount, so k3s can pull every private image. (A product whose chart names a
#       pull secret sets image.pullSecrets to $PULL_SECRET in its values.)
#    4. Optionally PRE-WARMS the images into the node so `apply` doesn't stall on a first pull.
#
#  Usage:
#    sudo bash deploy/server/pull-images.sh                 # ensure creds + pull-secret (+ prewarm)
#    sudo bash deploy/server/pull-images.sh --relogin       # force re-prompt even if a cred is saved
#    sudo bash deploy/server/pull-images.sh --no-prewarm    # just the credential + pull-secret
#    REG_TOKEN=... REG_USER=... sudo -E bash ... --relogin  # non-interactive (CI / automation)
#
#  Env:  PRODUCT_NAME (slug for names) · NS/NAMESPACE (default base-services) · VALUES_DIR (default
#        deploy/pinned, else deploy/services) · CREDS_FILE (default /etc/<slug>/registry-creds.json) ·
#        PULL_SECRET (default <slug>-registry) · REG_USER/REG_TOKEN (bypass the prompt)
# ==================================================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
SLUG="$(printf '%s' "$PRODUCT_NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-')"; [[ -n "$SLUG" ]] || SLUG="platform"
NS="${NS:-${NAMESPACE:-base-services}}"
if [[ -z "${VALUES_DIR:-}" ]]; then
  if compgen -G "$ROOT/deploy/pinned/*.values.yaml" >/dev/null; then VALUES_DIR="$ROOT/deploy/pinned";
  else VALUES_DIR="$ROOT/deploy/services"; fi
fi
CREDS_FILE="${CREDS_FILE:-/etc/$SLUG/registry-creds.json}"
PULL_SECRET="${PULL_SECRET:-$SLUG-registry}"
RELOGIN=0; PREWARM=1
for a in "$@"; do case "$a" in --relogin) RELOGIN=1;; --no-prewarm) PREWARM=0;; esac; done

C_G=$'\e[32m'; C_C=$'\e[36m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_N=$'\e[0m'
info(){ echo -e "${C_C}▸ $*${C_N}"; }
ok(){   echo -e "${C_G}✔ $*${C_N}"; }
warn(){ echo -e "${C_Y}⚠ $*${C_N}"; }
die(){  echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ── the image list, DERIVED from the values files (repository + digest) ────────────────────────────
# repository: ghcr.io/<ns>/<svc>   digest: "sha256:..."   ->   ghcr.io/<ns>/<svc>@sha256:...
# A placeholder (all-zero) digest means "not published yet"; fall back to :latest so the host is still
# discovered for a credential, even though that exact ref may not resolve.
image_refs() {
  local f repo dig
  for f in "$VALUES_DIR"/*.values.yaml; do
    [[ -f "$f" ]] || continue
    repo="$(sed -nE 's/^[[:space:]]*repository:[[:space:]]*"?([^"[:space:]]+)"?.*/\1/p' "$f" | head -1)"
    dig="$(sed -nE 's/^[[:space:]]*digest:[[:space:]]*"?(sha256:[0-9a-f]+)"?.*/\1/p' "$f" | head -1)"
    [[ -n "$repo" ]] || continue
    if [[ -n "$dig" && "$dig" != "sha256:0000000000000000000000000000000000000000000000000000000000000000" ]]; then
      echo "${repo}@${dig}"
    else
      echo "${repo}:latest"
    fi
  done
}
registry_hosts() { image_refs | sed -E 's#^([^/]+)/.*#\1#' | sort -u; }
first_ref_for()  { image_refs | grep -E "^$1/" | head -1; }   # a representative ref on a host

# ── public detection: does an ANONYMOUS pull work? (scrub DOCKER_CONFIG so saved logins don't lie) ──
host_is_public() {  # host
  local ref; ref="$(first_ref_for "$1")"; [[ -n "$ref" ]] || return 1
  have docker || return 1                              # cannot test anonymously -> treat as needing a cred
  local tmpcfg; tmpcfg="$(mktemp -d)"
  DOCKER_CONFIG="$tmpcfg" docker manifest inspect "$ref" >/dev/null 2>&1
  local rc=$?; rm -rf "$tmpcfg"; return $rc
}

# ── credential store (JSON: { "<host>": {"username":"..","token":".."} }) ─────────────────────────
ensure_store() { install -d -m 700 "$(dirname "$CREDS_FILE")"; [[ -f "$CREDS_FILE" ]] || { echo '{}' > "$CREDS_FILE"; chmod 600 "$CREDS_FILE"; }; }
saved_user()   { jq -r --arg h "$1" '.[$h].username // empty' "$CREDS_FILE" 2>/dev/null; }
saved_token()  { jq -r --arg h "$1" '.[$h].token    // empty' "$CREDS_FILE" 2>/dev/null; }
save_cred() {   # host user token
  local tmp; tmp="$(mktemp)"
  jq --arg h "$1" --arg u "$2" --arg t "$3" '.[$h] = {username:$u, token:$t}' "$CREDS_FILE" > "$tmp"
  install -m 600 "$tmp" "$CREDS_FILE"; rm -f "$tmp"
}

# Verify a credential actually works. The registry /v2/ ping answers 401-with-a-Bearer-challenge even
# for a VALID token (the normal handshake), so a "200 from /v2/" check rejects good tokens. `docker
# login` does the real handshake and is present on a provisioned box. Only if docker is absent do we
# fall back to the registry /v2/ where 200 OR 401 both mean "the host answered".
verify_cred() {  # host user token
  local host="$1" user="$2" tok="$3"
  if have docker; then printf '%s' "$tok" | docker login "$host" -u "$user" --password-stdin >/dev/null 2>&1; return; fi
  curl -s -o /dev/null -w '%{http_code}' -u "$user:$tok" "https://$host/v2/" | grep -qE '^(200|401)$'
}

# Get a working (user, token) for a PRIVATE host: saved-and-valid -> reuse; else prompt (or env).
resolve_cred() {  # host  -> echoes "user\ttoken"
  local host="$1" user tok
  if [[ "$RELOGIN" == "0" ]]; then
    user="$(saved_user "$host")"; tok="$(saved_token "$host")"
    if [[ -n "$user" && -n "$tok" ]] && verify_cred "$host" "$user" "$tok"; then printf '%s\t%s' "$user" "$tok"; return 0; fi
  fi
  if [[ -n "${REG_TOKEN:-}" ]]; then user="${REG_USER:-$(saved_user "$host")}"; tok="$REG_TOKEN"
  else
    [[ -t 0 ]] || die "no saved credential for $host and no TTY to prompt (set REG_USER/REG_TOKEN)"
    echo -e "\n${C_Y}Registry $host needs a credential (a token with read:packages).${C_N}" >&2
    local def; def="$(saved_user "$host")"
    read -r -p "  username [${def:-<your-registry-user>}]: " user; user="${user:-$def}"
    read -r -s -p "  token (hidden): " tok; echo >&2
  fi
  [[ -n "$user" && -n "$tok" ]] || die "empty credential for $host"
  verify_cred "$host" "$user" "$tok" || die "credential rejected by $host (check the token + read:packages scope)"
  save_cred "$host" "$user" "$tok"; ok "credential for $host verified + saved" >&2
  printf '%s\t%s' "$user" "$tok"
}

# ── build the k8s imagePullSecret (dockerconfigjson) for the PRIVATE hosts ─────────────────────────
make_pull_secret() {  # <host...>
  local auths="{}" host cred user tok
  for host in "$@"; do
    cred="$(resolve_cred "$host")"; user="${cred%%$'\t'*}"; tok="${cred#*$'\t'}"
    local b64; b64="$(printf '%s:%s' "$user" "$tok" | base64 | tr -d '\n')"
    auths="$(jq -c --arg h "$host" --arg a "$b64" --arg u "$user" '.[$h] = {username:$u, auth:$a}' <<<"$auths")"
    have docker && printf '%s' "$tok" | docker login "$host" -u "$user" --password-stdin >/dev/null 2>&1 || true
  done
  local dockercfg; dockercfg="$(jq -c --argjson a "$auths" -n '{auths:$a}')"
  info "writing imagePullSecret $NS/$PULL_SECRET"
  kubectl create namespace "$NS" >/dev/null 2>&1 || true
  kubectl -n "$NS" create secret generic "$PULL_SECRET" \
    --type=kubernetes.io/dockerconfigjson \
    --from-literal=.dockerconfigjson="$dockercfg" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # Bind to the default ServiceAccount so every pod in the namespace (incl. freshly-spawned CronJob
  # pods) pulls without a per-manifest edit. A product that sets image.pullSecrets in its values gets
  # the same secret by name; this SA binding is the belt to that suspenders.
  kubectl -n "$NS" patch serviceaccount default \
    -p "{\"imagePullSecrets\":[{\"name\":\"$PULL_SECRET\"}]}" >/dev/null 2>&1 || true
  ok "imagePullSecret $PULL_SECRET ready + bound to default ServiceAccount"
}

# ── VERIFY the NODE can actually pull a private image (k3s ≠ docker login) ─────────────────────────
verify_node_pull() {  # <host>
  have k3s || return 0
  local host="$1" cred user tok probe; probe="$(first_ref_for "$host")"; [[ -n "$probe" ]] || return 0
  cred="$(resolve_cred "$host" 2>/dev/null)" || return 0
  user="${cred%%$'\t'*}"; tok="${cred#*$'\t'}"
  info "verifying the NODE can pull a private image (k3s pulls via the secret, not docker login)"
  if k3s ctr images pull --user "$user:$tok" "$probe" >/dev/null 2>&1; then
    ok "node pull verified"; k3s ctr images rm "$probe" >/dev/null 2>&1 || true
  else
    warn "the NODE could NOT pull $probe — pods will ImagePullBackOff. The token needs READ access to"
    warn "  that registry's packages (read:packages + membership). Fix it and retry: sudo bash $0 --relogin"
  fi
}

# ── optional: pre-warm images into the node so `apply` doesn't stall on a first pull ──────────────
prewarm() {
  [[ "$PREWARM" == "1" ]] || return 0
  info "pre-warming images into the node"
  local ref
  for ref in $(image_refs); do
    if   have k3s;    then k3s ctr images pull "$ref" >/dev/null 2>&1 && ok "pulled $ref" || warn "skip $ref (not published yet?)"
    elif have docker; then docker pull "$ref"        >/dev/null 2>&1 && ok "pulled $ref" || warn "skip $ref (not published yet?)"
    fi
  done
}

main() {
  [[ $EUID -eq 0 ]] || die "run as root (writes $CREDS_FILE + the k8s secret): sudo bash $0"
  have jq       || die "jq is required (provision-server.sh installs it)"
  have kubectl  || die "kubectl not found — run provision-server.sh first (installs k3s)"
  local hosts; hosts="$(registry_hosts)"
  [[ -n "$hosts" ]] || die "no image repositories found in $VALUES_DIR — is the release pinned?"
  info "registries: $(echo "$hosts" | paste -sd, -)"
  ensure_store

  # Which hosts actually need a credential? (Public ones do not — the whole point of the no-op.)
  local private=()
  local h
  for h in $hosts; do
    if [[ "$RELOGIN" == "0" ]] && host_is_public "$h"; then ok "$h — images are PUBLIC, no credential needed"
    else private+=("$h"); fi
  done

  if [[ ${#private[@]} -eq 0 ]]; then
    ok "all images are public — no pull-secret needed"
  else
    make_pull_secret "${private[@]}"
    for h in "${private[@]}"; do verify_node_pull "$h"; done
  fi
  prewarm
  echo; ok "images are pullable${private:+ — creds saved at $CREDS_FILE (root-only); updates reuse them silently}"
}
main "$@"
